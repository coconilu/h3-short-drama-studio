from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


BATCH_ACTIVE_STATES = ("running", "paused", "cancelling")
BATCH_TERMINAL_STATES = ("completed", "completed_with_errors", "cancelled")
ITEM_ACTIVE_STATES = ("submitting", "running")
ITEM_TERMINAL_STATES = ("completed", "failed", "cancelled")

PlanProvider = Callable[[str], dict[str, Any]]
Submitter = Callable[[str], dict[str, Any]]
Syncer = Callable[[str], dict[str, Any]]

_DB_PATH: Path | None = None
_PLAN_PROVIDER: PlanProvider | None = None
_SUBMITTER: Submitter | None = None
_SYNCER: Syncer | None = None
_STOP_EVENT = threading.Event()
_WAKE_EVENT = threading.Event()
_WORKER_THREAD: threading.Thread | None = None
_POLL_SECONDS = 2.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_production_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS production_batches (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN (
            'running', 'paused', 'cancelling', 'completed', 'completed_with_errors', 'cancelled'
          )),
          item_count INTEGER NOT NULL,
          submitted_count INTEGER NOT NULL DEFAULT 0,
          completed_count INTEGER NOT NULL DEFAULT 0,
          failed_count INTEGER NOT NULL DEFAULT 0,
          cancelled_count INTEGER NOT NULL DEFAULT 0,
          config TEXT NOT NULL DEFAULT '{}',
          message TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_production_batches_project
          ON production_batches(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS production_batch_items (
          id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL REFERENCES production_batches(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          title TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN (
            'queued', 'submitting', 'running', 'completed', 'failed', 'cancelled'
          )),
          plan_hash TEXT NOT NULL,
          plan_snapshot TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 3,
          h3_project TEXT,
          prompt_ids TEXT NOT NULL DEFAULT '[]',
          message TEXT NOT NULL,
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT,
          UNIQUE(batch_id, shot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_production_items_claim
          ON production_batch_items(state, batch_id, ordinal);
        CREATE TABLE IF NOT EXISTS production_batch_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id TEXT NOT NULL REFERENCES production_batches(id) ON DELETE CASCADE,
          item_id TEXT REFERENCES production_batch_items(id) ON DELETE CASCADE,
          event TEXT NOT NULL,
          level TEXT NOT NULL DEFAULT 'info',
          message TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_production_events_batch
          ON production_batch_events(batch_id, id DESC);
        """
    )


def configure_production_scheduler(
    db_path: Path,
    plan_provider: PlanProvider,
    submitter: Submitter,
    syncer: Syncer,
    *,
    poll_seconds: float = 2.0,
) -> None:
    global _DB_PATH, _PLAN_PROVIDER, _SUBMITTER, _SYNCER, _POLL_SECONDS
    _DB_PATH = db_path
    _PLAN_PROVIDER = plan_provider
    _SUBMITTER = submitter
    _SYNCER = syncer
    _POLL_SECONDS = max(0.02, poll_seconds)


def _configured() -> tuple[Path, PlanProvider, Submitter, Syncer]:
    if _DB_PATH is None or _PLAN_PROVIDER is None or _SUBMITTER is None or _SYNCER is None:
        raise RuntimeError("生产调度器尚未配置")
    return _DB_PATH, _PLAN_PROVIDER, _SUBMITTER, _SYNCER


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    setting = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
    if not setting:
        raise HTTPException(404, "当前没有已激活的项目")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (setting["value"],)).fetchone()
    if not project:
        raise HTTPException(404, "当前项目不存在")
    return dict(project)


def _event(
    db: sqlite3.Connection,
    batch_id: str,
    event: str,
    message: str,
    *,
    item_id: str | None = None,
    level: str = "info",
) -> None:
    db.execute(
        """INSERT INTO production_batch_events
        (batch_id, item_id, event, level, message, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (batch_id, item_id, event, level, message, utc_now()),
    )


def _decode_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _refresh_batch(db: sqlite3.Connection, batch_id: str) -> None:
    batch = db.execute("SELECT * FROM production_batches WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        return
    counts = {
        row["state"]: row["count"]
        for row in db.execute(
            "SELECT state, COUNT(*) AS count FROM production_batch_items WHERE batch_id = ? GROUP BY state",
            (batch_id,),
        ).fetchall()
    }
    submitted = db.execute(
        "SELECT COUNT(*) AS count FROM production_batch_items WHERE batch_id = ? AND attempts > 0",
        (batch_id,),
    ).fetchone()["count"]
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    terminal = completed + failed + cancelled
    next_state = batch["state"]
    message = batch["message"]
    completed_at = batch["completed_at"]
    if terminal == batch["item_count"]:
        completed_at = utc_now()
        if failed:
            next_state = "completed_with_errors"
            message = f"{completed} 个完成，{failed} 个失败，{cancelled} 个取消"
        elif cancelled:
            next_state = "cancelled"
            message = f"批次已停止；{completed} 个完成，{cancelled} 个未提交或已取消"
        else:
            next_state = "completed"
            message = f"全部 {completed} 个镜头草稿已生成，等待审片"
    elif batch["state"] == "cancelling" and not counts.get("running", 0) and not counts.get("submitting", 0):
        next_state = "cancelled"
        message = "批次已停止，未提交项目已取消"
        completed_at = utc_now()
    elif batch["state"] == "running":
        message = f"{completed}/{batch['item_count']} 完成；单 GPU 串行调度"
    db.execute(
        """UPDATE production_batches SET state = ?, submitted_count = ?, completed_count = ?,
        failed_count = ?, cancelled_count = ?, message = ?, updated_at = ?, completed_at = ? WHERE id = ?""",
        (next_state, submitted, completed, failed, cancelled, message, utc_now(), completed_at, batch_id),
    )


def _batch_public(db: sqlite3.Connection, row: sqlite3.Row, include_events: bool = True) -> dict[str, Any]:
    batch = dict(row)
    batch["config"] = _decode_json(batch.get("config"), {})
    batch["items"] = []
    for item_row in db.execute(
        "SELECT * FROM production_batch_items WHERE batch_id = ? ORDER BY ordinal, created_at",
        (batch["id"],),
    ).fetchall():
        item = dict(item_row)
        item["prompt_ids"] = _decode_json(item.get("prompt_ids"), [])
        item["plan_snapshot"] = _decode_json(item.get("plan_snapshot"), {})
        batch["items"].append(item)
    batch["events"] = []
    if include_events:
        batch["events"] = [
            dict(event)
            for event in db.execute(
                "SELECT * FROM production_batch_events WHERE batch_id = ? ORDER BY id DESC LIMIT 80",
                (batch["id"],),
            ).fetchall()
        ]
    return batch


def list_batches(db_path: Path) -> list[dict[str, Any]]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        return [
            _batch_public(db, item, include_events=False)
            for item in db.execute(
                "SELECT * FROM production_batches WHERE project_id = ? ORDER BY created_at DESC",
                (project["id"],),
            ).fetchall()
        ]


def get_batch(db_path: Path, batch_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        batch = db.execute(
            "SELECT * FROM production_batches WHERE id = ? AND project_id = ?",
            (batch_id, project["id"]),
        ).fetchone()
        if not batch:
            raise HTTPException(404, "生产批次不存在")
        return _batch_public(db, batch)


def create_batch(
    db_path: Path,
    plan_provider: PlanProvider,
    shot_ids: list[str],
    *,
    name: str,
    max_attempts: int,
) -> dict[str, Any]:
    if len(shot_ids) != len(set(shot_ids)):
        raise HTTPException(400, "生产批次不能包含重复镜头")
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        placeholders = ",".join("?" for _ in shot_ids)
        shots = db.execute(
            f"SELECT * FROM shots WHERE project_id = ? AND id IN ({placeholders}) ORDER BY ordinal, id",
            (project["id"], *shot_ids),
        ).fetchall()
        if len(shots) != len(shot_ids):
            raise HTTPException(404, "部分镜头不属于当前项目")
        items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        issues: list[dict[str, str]] = []
        for shot_row in shots:
            shot = dict(shot_row)
            plan = plan_provider(shot["id"])
            if plan.get("status") != "approved":
                issues.append({"shot_id": shot["id"], "message": "当前生成计划尚未批准"})
            elif not plan.get("ready"):
                issues.append({"shot_id": shot["id"], "message": "当前生成计划存在阻断项"})
            elif shot.get("status") in ("生成中", "已定稿"):
                issues.append({"shot_id": shot["id"], "message": f"镜头当前状态为{shot['status']}"})
            items.append((shot, plan))
        if issues:
            raise HTTPException(409, {"message": "生产批次门禁未通过", "issues": issues})

        now = utc_now()
        batch_id = f"production-{uuid.uuid4().hex[:12]}"
        candidate_total = sum(int(plan["spec"]["candidate_count"]) for _, plan in items)
        db.execute(
            """INSERT INTO production_batches
            (id, project_id, name, state, item_count, config, message, created_at, updated_at, started_at)
            VALUES (?, ?, ?, 'running', ?, ?, '等待单 GPU 调度器提交首个镜头', ?, ?, ?)""",
            (
                batch_id,
                project["id"],
                name.strip() or f"{project['episode']} 生产批次",
                len(items),
                json.dumps({"concurrency": 1, "candidate_total": candidate_total, "snapshot_policy": "immutable"}, ensure_ascii=False),
                now,
                now,
                now,
            ),
        )
        for shot, plan in items:
            item_id = f"production-item-{uuid.uuid4().hex[:12]}"
            public_snapshot = {key: value for key, value in plan.items() if not key.startswith("_")}
            db.execute(
                """INSERT INTO production_batch_items
                (id, batch_id, shot_id, ordinal, title, state, plan_hash, plan_snapshot,
                 max_attempts, message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, '等待提交', ?, ?)""",
                (
                    item_id, batch_id, shot["id"], shot["ordinal"], shot["title"], plan["plan_hash"],
                    json.dumps(public_snapshot, ensure_ascii=False), max_attempts, now, now,
                ),
            )
        _event(db, batch_id, "created", f"已冻结 {len(items)} 个镜头、{candidate_total} 条候选的批准计划")
        db.commit()
        batch = db.execute("SELECT * FROM production_batches WHERE id = ?", (batch_id,)).fetchone()
        result = _batch_public(db, batch)
    _WAKE_EVENT.set()
    return result


def recover_batches(db_path: Path) -> int:
    with closing(connect(db_path)) as db:
        uncertain = db.execute(
            "SELECT * FROM production_batch_items WHERE state = 'submitting'"
        ).fetchall()
        now = utc_now()
        for item in uncertain:
            message = "API 在确认 ComfyUI 接收结果前中断；为避免重复消耗，已停止并等待人工重试"
            db.execute(
                """UPDATE production_batch_items SET state = 'failed', message = ?, error = ?,
                updated_at = ?, completed_at = ? WHERE id = ?""",
                (message, "submission_outcome_unknown", now, now, item["id"]),
            )
            db.execute(
                """UPDATE production_batches SET state = 'paused', message = ?, updated_at = ?
                WHERE id = ? AND state = 'running'""",
                (message, now, item["batch_id"]),
            )
            _event(db, item["batch_id"], "submission_unknown", message, item_id=item["id"], level="error")
        for batch_id in {item["batch_id"] for item in uncertain}:
            _refresh_batch(db, batch_id)
        db.commit()
        return len(uncertain)


def claim_next_item(db_path: Path) -> dict[str, Any] | None:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT 1 FROM production_batch_items WHERE state IN ('submitting', 'running') LIMIT 1"
        ).fetchone()
        if active:
            db.commit()
            return None
        item = db.execute(
            """SELECT items.* FROM production_batch_items items
            JOIN production_batches batches ON batches.id = items.batch_id
            WHERE items.state = 'queued' AND batches.state = 'running'
            ORDER BY batches.created_at, items.ordinal, items.created_at LIMIT 1"""
        ).fetchone()
        if not item:
            db.commit()
            return None
        now = utc_now()
        db.execute(
            """UPDATE production_batch_items SET state = 'submitting', attempts = attempts + 1,
            message = '正在提交到 ComfyUI', error = NULL, started_at = COALESCE(started_at, ?),
            updated_at = ? WHERE id = ? AND state = 'queued'""",
            (now, now, item["id"]),
        )
        _event(db, item["batch_id"], "submitting", f"开始提交镜头 {item['title']}", item_id=item["id"])
        db.commit()
        claimed = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (item["id"],)).fetchone()
        return dict(claimed)


def _fail_item(db_path: Path, item: dict[str, Any], message: str, error: str) -> None:
    with closing(connect(db_path)) as db:
        now = utc_now()
        db.execute(
            """UPDATE production_batch_items SET state = 'failed', message = ?, error = ?,
            updated_at = ?, completed_at = ? WHERE id = ?""",
            (message, error[-5000:], now, now, item["id"]),
        )
        _event(db, item["batch_id"], "failed", message, item_id=item["id"], level="error")
        _refresh_batch(db, item["batch_id"])
        db.commit()


def submit_claimed_item(db_path: Path, item: dict[str, Any], plan_provider: PlanProvider, submitter: Submitter) -> None:
    try:
        current = plan_provider(item["shot_id"])
        if current.get("status") != "approved" or current.get("plan_hash") != item["plan_hash"]:
            _fail_item(db_path, item, "批准计划已变化，未提交 GPU", "approved_plan_changed")
            return
        result = submitter(item["shot_id"])
        result_state = str(result.get("state") or "已提交")
        completed = result_state == "完成"
        now = utc_now()
        with closing(connect(db_path)) as db:
            db.execute(
                """UPDATE production_batch_items SET state = ?, h3_project = ?, prompt_ids = ?,
                message = ?, updated_at = ?, completed_at = ? WHERE id = ?""",
                (
                    "completed" if completed else "running",
                    result.get("h3_project"),
                    json.dumps(result.get("prompt_ids") or []),
                    result.get("message") or result_state,
                    now,
                    now if completed else None,
                    item["id"],
                ),
            )
            _event(
                db, item["batch_id"], "completed" if completed else "submitted",
                result.get("message") or result_state, item_id=item["id"],
            )
            _refresh_batch(db, item["batch_id"])
            db.commit()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        _fail_item(db_path, item, "镜头提交失败，等待人工重试", detail)
    except Exception as exc:
        _fail_item(db_path, item, "镜头提交失败，等待人工重试", str(exc))


def poll_running_items(db_path: Path, syncer: Syncer) -> int:
    with closing(connect(db_path)) as db:
        items = [dict(item) for item in db.execute("SELECT * FROM production_batch_items WHERE state = 'running'").fetchall()]
    changed = 0
    for item in items:
        try:
            result = syncer(item["shot_id"])
        except Exception:
            continue
        state = str(result.get("state") or "")
        next_state = "completed" if state == "完成" else "failed" if state == "失败" else "running"
        now = utc_now()
        with closing(connect(db_path)) as db:
            db.execute(
                """UPDATE production_batch_items SET state = ?, prompt_ids = ?, message = ?,
                error = CASE WHEN ? = 'failed' THEN ? ELSE error END, updated_at = ?,
                completed_at = CASE WHEN ? IN ('completed', 'failed') THEN ? ELSE completed_at END
                WHERE id = ? AND state = 'running'""",
                (
                    next_state,
                    json.dumps(result.get("prompt_ids") or _decode_json(item.get("prompt_ids"), [])),
                    result.get("message") or item["message"],
                    next_state,
                    result.get("message") or "H3 生成失败",
                    now,
                    next_state,
                    now,
                    item["id"],
                ),
            )
            if next_state != "running":
                changed += 1
                _event(
                    db, item["batch_id"], next_state, result.get("message") or state,
                    item_id=item["id"], level="error" if next_state == "failed" else "info",
                )
            _refresh_batch(db, item["batch_id"])
            db.commit()
    return changed


def mutate_batch(db_path: Path, batch_id: str, action: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        batch = db.execute(
            "SELECT * FROM production_batches WHERE id = ? AND project_id = ?",
            (batch_id, project["id"]),
        ).fetchone()
        if not batch:
            raise HTTPException(404, "生产批次不存在")
        now = utc_now()
        if action == "pause":
            if batch["state"] != "running":
                raise HTTPException(409, "只有运行中的批次可以暂停")
            db.execute("UPDATE production_batches SET state = 'paused', message = '已暂停后续提交；当前 GPU 任务继续跟踪', updated_at = ? WHERE id = ?", (now, batch_id))
            _event(db, batch_id, "paused", "已暂停后续镜头提交")
        elif action == "resume":
            if batch["state"] != "paused":
                raise HTTPException(409, "只有已暂停的批次可以继续")
            db.execute("UPDATE production_batches SET state = 'running', message = '已恢复，等待单 GPU 调度', completed_at = NULL, updated_at = ? WHERE id = ?", (now, batch_id))
            _event(db, batch_id, "resumed", "已恢复生产调度")
        elif action == "cancel":
            if batch["state"] in BATCH_TERMINAL_STATES:
                raise HTTPException(409, "批次已经结束")
            active = db.execute("SELECT 1 FROM production_batch_items WHERE batch_id = ? AND state IN ('submitting', 'running')", (batch_id,)).fetchone()
            db.execute(
                "UPDATE production_batches SET state = ?, message = ?, updated_at = ? WHERE id = ?",
                (
                    "cancelling" if active else "cancelled",
                    "已停止后续提交；当前 ComfyUI 任务完成后结束" if active else "批次已取消，未向 GPU 提交剩余镜头",
                    now,
                    batch_id,
                ),
            )
            db.execute(
                """UPDATE production_batch_items SET state = 'cancelled', message = '批次取消，未提交 GPU',
                updated_at = ?, completed_at = ? WHERE batch_id = ? AND state = 'queued'""",
                (now, now, batch_id),
            )
            _event(db, batch_id, "cancel_requested", "停止后续提交；不把无法确认的 ComfyUI 中止冒充为成功取消", level="warning")
            _refresh_batch(db, batch_id)
        else:
            raise HTTPException(400, "不支持的批次操作")
        db.commit()
    _WAKE_EVENT.set()
    return get_batch(db_path, batch_id)


def retry_item(db_path: Path, item_id: str, plan_provider: PlanProvider) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        item = db.execute(
            """SELECT items.*, batches.project_id, batches.state AS batch_state
            FROM production_batch_items items JOIN production_batches batches ON batches.id = items.batch_id
            WHERE items.id = ? AND batches.project_id = ?""",
            (item_id, project["id"]),
        ).fetchone()
        if not item:
            raise HTTPException(404, "批次镜头不存在")
        if item["state"] != "failed":
            raise HTTPException(409, "只有失败镜头可以重试")
        if item["attempts"] >= item["max_attempts"]:
            raise HTTPException(409, f"已达到本批次最多 {item['max_attempts']} 次提交尝试")
        current = plan_provider(item["shot_id"])
        if current.get("status") != "approved" or current.get("plan_hash") != item["plan_hash"]:
            raise HTTPException(409, "当前批准计划与批次快照不同，请新建生产批次")
        now = utc_now()
        db.execute(
            """UPDATE production_batch_items SET state = 'queued', message = '人工重试，等待提交',
            error = NULL, completed_at = NULL, updated_at = ? WHERE id = ?""",
            (now, item_id),
        )
        db.execute(
            """UPDATE production_batches SET state = 'running', message = '失败镜头已重新排队',
            completed_at = NULL, updated_at = ? WHERE id = ?""",
            (now, item["batch_id"]),
        )
        _event(db, item["batch_id"], "retry", f"人工重试镜头 {item['title']}", item_id=item_id, level="warning")
        _refresh_batch(db, item["batch_id"])
        db.commit()
        batch_id = item["batch_id"]
    _WAKE_EVENT.set()
    return get_batch(db_path, batch_id)


def production_worker_loop() -> None:
    db_path, plan_provider, submitter, syncer = _configured()
    while not _STOP_EVENT.is_set():
        poll_running_items(db_path, syncer)
        claimed = claim_next_item(db_path)
        if claimed:
            submit_claimed_item(db_path, claimed, plan_provider, submitter)
            continue
        _WAKE_EVENT.wait(_POLL_SECONDS)
        _WAKE_EVENT.clear()


def start_production_worker() -> int:
    global _WORKER_THREAD
    db_path, _, _, _ = _configured()
    _STOP_EVENT.clear()
    _WAKE_EVENT.clear()
    recovered = recover_batches(db_path)
    _WORKER_THREAD = threading.Thread(target=production_worker_loop, name="jingchang-production-worker", daemon=True)
    _WORKER_THREAD.start()
    _WAKE_EVENT.set()
    return recovered


def stop_production_worker() -> None:
    _STOP_EVENT.set()
    _WAKE_EVENT.set()
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        _WORKER_THREAD.join(timeout=10)


class ProductionBatchCreate(BaseModel):
    shot_ids: list[str] = Field(min_length=1, max_length=200)
    name: str = Field("", max_length=120)
    max_attempts: int = Field(3, ge=1, le=5)
    confirm: bool = False


def create_production_router(db_path: Path, plan_provider: PlanProvider) -> APIRouter:
    router = APIRouter()

    @router.get("/api/production-batches")
    def api_list_batches() -> list[dict[str, Any]]:
        return list_batches(db_path)

    @router.get("/api/production-batches/{batch_id}")
    def api_get_batch(batch_id: str) -> dict[str, Any]:
        return get_batch(db_path, batch_id)

    @router.post("/api/production-batches")
    def api_create_batch(payload: ProductionBatchCreate) -> dict[str, Any]:
        if not payload.confirm:
            raise HTTPException(400, "实际提交 GPU 的生产批次必须显式确认")
        return create_batch(
            db_path, plan_provider, payload.shot_ids,
            name=payload.name, max_attempts=payload.max_attempts,
        )

    @router.post("/api/production-batches/{batch_id}/{action}")
    def api_mutate_batch(batch_id: str, action: str) -> dict[str, Any]:
        return mutate_batch(db_path, batch_id, action)

    @router.post("/api/production-items/{item_id}/retry")
    def api_retry_item(item_id: str) -> dict[str, Any]:
        return retry_item(db_path, item_id, plan_provider)

    return router
