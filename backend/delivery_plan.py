from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_delivery_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS delivery_plans (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('draft', 'locked')),
          revision INTEGER NOT NULL,
          plan_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          locked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS delivery_plan_items (
          plan_id TEXT NOT NULL REFERENCES delivery_plans(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          subtitle_enabled INTEGER NOT NULL,
          subtitle_start_seconds REAL,
          transition TEXT NOT NULL DEFAULT 'cut' CHECK(transition = 'cut'),
          in_point_seconds REAL NOT NULL DEFAULT 0,
          out_point_seconds REAL,
          dialogue_mode TEXT NOT NULL DEFAULT 'original' CHECK(dialogue_mode IN ('original', 'mute')),
          section_id TEXT,
          candidate_id TEXT,
          master_version_id TEXT,
          hd_artifact_id TEXT,
          hd_master_version_id TEXT,
          source_snapshot TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(plan_id, shot_id),
          UNIQUE(plan_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS delivery_plan_versions (
          id TEXT PRIMARY KEY,
          plan_id TEXT NOT NULL REFERENCES delivery_plans(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          status TEXT NOT NULL,
          plan_hash TEXT NOT NULL,
          source TEXT NOT NULL,
          snapshot TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(plan_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_versions_plan
          ON delivery_plan_versions(plan_id, revision DESC);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(delivery_plan_items)").fetchall()}
    migrations = (
        ("in_point_seconds", "REAL NOT NULL DEFAULT 0"),
        ("out_point_seconds", "REAL"),
        ("dialogue_mode", "TEXT NOT NULL DEFAULT 'original'"),
        ("section_id", "TEXT"),
        ("candidate_id", "TEXT"),
        ("master_version_id", "TEXT"),
        ("hd_artifact_id", "TEXT"),
        ("hd_master_version_id", "TEXT"),
        ("source_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
    )
    for column, definition in migrations:
        if column not in columns:
            db.execute(f"ALTER TABLE delivery_plan_items ADD COLUMN {column} {definition}")


class DeliveryPlanItemInput(BaseModel):
    shot_id: str = Field(min_length=1, max_length=200)
    subtitle_enabled: bool = True
    subtitle_start_seconds: float | None = Field(None, ge=0, le=30)
    transition: Literal["cut"] = "cut"
    in_point_seconds: float = Field(0, ge=0, le=30)
    out_point_seconds: float | None = Field(None, gt=0, le=30)
    dialogue_mode: Literal["original", "mute"] = "original"


class DeliveryPlanPatch(BaseModel):
    base_revision: int = Field(ge=0)
    items: list[DeliveryPlanItemInput] = Field(min_length=1, max_length=300)


class DeliveryPlanRevision(BaseModel):
    base_revision: int = Field(ge=1)


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    setting = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
    if not setting:
        raise HTTPException(404, "当前没有已激活的项目")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (setting["value"],)).fetchone()
    if not project:
        raise HTTPException(404, "当前项目不存在")
    return dict(project)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone())


def _file_snapshot(path_value: str | None, output_root: Path | None) -> dict[str, Any]:
    if not path_value:
        return {"status": "missing", "reason": "候选没有持久化媒体路径"}
    path = Path(path_value).resolve()
    if output_root is not None:
        try:
            path.relative_to(output_root.resolve())
        except ValueError:
            return {"status": "invalid", "reason": "候选媒体不在允许输出目录"}
    if not path.is_file():
        return {"status": "missing", "reason": "候选媒体文件不存在", "output_file": str(path)}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "status": "verified", "output_file": str(path), "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns, "checksum_sha256": digest.hexdigest(),
    }


def _shot_snapshot(shot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: shot.get(key)
        for key in (
            "id", "ordinal", "scene_code", "title", "description", "prompt", "dialogue",
            "seconds", "width", "height", "subtitle_enabled", "subtitle_start_seconds", "updated_at",
        )
        if key in shot
    }


def _source_for_shot(db: sqlite3.Connection, shot_id: str, output_root: Path | None = None) -> dict[str, Any]:
    if not _table_exists(db, "candidates"):
        return {
            "candidate_id": None, "master_version_id": None, "section_id": None,
            "source_status": "unavailable", "source_reason": "旧数据库没有候选追溯表",
        }
    shot_row = db.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
    shot = dict(shot_row) if shot_row else {}
    hd_source = None
    if output_root is not None and _table_exists(db, "hd_master_versions"):
        try:
            try:
                from .hd_delivery import selected_hd_source
            except ImportError:
                from hd_delivery import selected_hd_source
            hd_source = selected_hd_source(db, shot_id, output_root)
        except HTTPException:
            raise
    candidate = db.execute(
        "SELECT * FROM candidates WHERE shot_id = ? AND selected = 1 AND archived = 0", (shot_id,),
    ).fetchone()
    mapping = None
    if _table_exists(db, "creative_storyboard_links"):
        mapping = db.execute(
            "SELECT section_id, last_synced_revision FROM creative_storyboard_links WHERE shot_id = ?", (shot_id,),
        ).fetchone()
    master = None
    if candidate and _table_exists(db, "candidate_master_versions"):
        master = db.execute(
            """SELECT id, revision, review_id, review_revision, candidate_snapshot, created_at
            FROM candidate_master_versions WHERE shot_id = ? AND candidate_id = ? ORDER BY revision DESC LIMIT 1""",
            (shot_id, candidate["id"]),
        ).fetchone()
    media = _file_snapshot(candidate["output_file"] if candidate and "output_file" in candidate.keys() else None, output_root)
    master_snapshot = {}
    if master:
        try:
            master_snapshot = json.loads(master["candidate_snapshot"] or "{}")
        except (TypeError, json.JSONDecodeError):
            master_snapshot = {}
    master_media_matches = bool(
        master_snapshot.get("output_file") == media.get("output_file")
        and master_snapshot.get("checksum_sha256") == media.get("checksum_sha256")
        and master_snapshot.get("size_bytes") == media.get("size_bytes")
    )
    candidate_completed = bool(candidate and ("status" not in candidate.keys() or candidate["status"] == "completed"))
    source_status = "ready" if candidate_completed and master and media.get("status") == "verified" and master_media_matches else "historical_debt" if candidate else "missing"
    source_reason = None
    if not candidate:
        source_reason = "镜头尚未选择草稿母版"
    elif not master:
        source_reason = "历史已选候选没有 append-only 母版版本"
    elif not candidate_completed:
        source_reason = "所选候选尚未生成完成"
    elif media.get("status") != "verified":
        source_reason = media.get("reason")
    elif not master_media_matches:
        source_reason = "母版版本缺少当前媒体校验和凭证或媒体已变化"
    section_revision = mapping["last_synced_revision"] if mapping else None
    if mapping and _table_exists(db, "creative_sections"):
        section = db.execute("SELECT revision FROM creative_sections WHERE id = ?", (mapping["section_id"],)).fetchone()
        section_revision = section["revision"] if section else None
    base_source = {
        "section_id": mapping["section_id"] if mapping else None,
        "section_revision": section_revision,
        "storyboard_revision": mapping["last_synced_revision"] if mapping else None,
        "shot_id": shot_id,
        "shot_snapshot": _shot_snapshot(shot),
        "candidate_id": candidate["id"] if candidate else None,
        "candidate_external_id": candidate["external_id"] if candidate and "external_id" in candidate.keys() else None,
        "candidate_prompt_id": candidate["prompt_id"] if candidate and "prompt_id" in candidate.keys() else None,
        "master_version_id": master["id"] if master else None,
        "master_revision": master["revision"] if master else None,
        "review_id": master["review_id"] if master else None,
        "review_revision": master["review_revision"] if master else None,
        "master_snapshot": master_snapshot,
        "media": media,
        "source_status": source_status,
        "source_reason": source_reason,
    }
    if not hd_source:
        return {**base_source, "source_type": "candidate", "hd_artifact_id": None, "hd_master_version_id": None}
    return {
        **base_source,
        "candidate_id": hd_source["source_candidate_id"],
        "master_version_id": hd_source["source_master_version_id"],
        "media": {
            "status": "verified", "output_file": hd_source["media"]["path"],
            "size_bytes": hd_source["media"]["size_bytes"], "modified_ns": hd_source["media"]["modified_ns"],
            "checksum_sha256": hd_source["media"]["checksum_sha256"],
        },
        "source_status": "ready", "source_reason": None, "source_type": "hd_artifact",
        "hd_artifact_id": hd_source["hd_artifact_id"],
        "hd_master_version_id": hd_source["hd_master_version_id"],
        "hd_master_revision": hd_source["hd_master_revision"],
        "hd_review_id": hd_source["hd_review_id"], "hd_review_revision": hd_source["hd_review_revision"],
        "hd_strategy_type": hd_source["strategy_type"], "hd_strategy_kind": hd_source["strategy_kind"],
        "hd_plan_id": hd_source["plan_id"], "hd_plan_hash": hd_source["plan_hash"],
        "hd_model_id": hd_source["model_id"], "hd_workflow_id": hd_source["workflow_id"],
        "hd_provenance": hd_source["provenance"],
    }


def _bind_sources(
    db: sqlite3.Connection, items: list[dict[str, Any]], output_root: Path | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            **item,
            **{
                "section_id": source["section_id"],
                "candidate_id": source["candidate_id"],
                "master_version_id": source["master_version_id"],
                "hd_artifact_id": source.get("hd_artifact_id"),
                "hd_master_version_id": source.get("hd_master_version_id"),
                "source_snapshot": source,
            },
        }
        for item in items
        for source in [_source_for_shot(db, item["shot_id"], output_root)]
    ]


def _default_items(db: sqlite3.Connection, project_id: str, output_root: Path | None = None) -> list[dict[str, Any]]:
    items = [
        {
            "shot_id": shot["id"], "ordinal": index,
            "subtitle_enabled": bool(shot["dialogue"] and shot["subtitle_enabled"]),
            "subtitle_start_seconds": shot["subtitle_start_seconds"] if shot["dialogue"] else None, "transition": "cut",
            "title": shot["title"], "scene_code": shot["scene_code"], "dialogue": shot["dialogue"],
            "seconds": shot["seconds"], "shot_status": shot["status"],
            "in_point_seconds": 0.0, "out_point_seconds": shot["seconds"], "dialogue_mode": "original",
        }
        for index, shot in enumerate(
            db.execute("SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal, id", (project_id,)).fetchall(),
            start=1,
        )
    ]
    return _bind_sources(db, items, output_root)


def _plan_items(db: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    items = []
    for row in db.execute(
            """SELECT items.*, shots.title, shots.scene_code, shots.dialogue, shots.seconds, shots.status AS shot_status
            FROM delivery_plan_items items JOIN shots ON shots.id = items.shot_id
            WHERE items.plan_id = ? ORDER BY items.ordinal""",
            (plan_id,),
        ).fetchall():
        item = {**dict(row), "subtitle_enabled": bool(row["subtitle_enabled"])}
        try:
            item["source_snapshot"] = json.loads(item.get("source_snapshot") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["source_snapshot"] = {}
        items.append(item)
    return items


def _hash_items(items: list[dict[str, Any]]) -> str:
    stable = [
        {
            "shot_id": item["shot_id"], "ordinal": item["ordinal"],
            "subtitle_enabled": bool(item["subtitle_enabled"]),
            "subtitle_start_seconds": item.get("subtitle_start_seconds"), "transition": item.get("transition", "cut"),
            "in_point_seconds": item.get("in_point_seconds", 0),
            "out_point_seconds": item.get("out_point_seconds"),
            "dialogue_mode": item.get("dialogue_mode", "original"),
            "section_id": item.get("section_id"),
            "candidate_id": item.get("candidate_id"),
            "master_version_id": item.get("master_version_id"),
            "hd_artifact_id": item.get("hd_artifact_id"),
            "hd_master_version_id": item.get("hd_master_version_id"),
            "source_snapshot": item.get("source_snapshot") or {},
        }
        for item in items
    ]
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def delivery_plan_hash(items: list[dict[str, Any]]) -> str:
    """Public canonical hash contract shared by lock and export gates."""
    return _hash_items(items)


def _validate_items(db: sqlite3.Connection, project_id: str, items: list[dict[str, Any]]) -> None:
    current = {
        row["id"]: dict(row)
        for row in db.execute("SELECT * FROM shots WHERE project_id = ?", (project_id,)).fetchall()
    }
    item_ids = [item["shot_id"] for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise HTTPException(422, "装配清单不能重复镜头")
    if set(item_ids) != set(current):
        missing = set(current) - set(item_ids)
        extra = set(item_ids) - set(current)
        detail = []
        if missing:
            detail.append("缺少：" + "、".join(sorted(missing)))
        if extra:
            detail.append("不属于项目：" + "、".join(sorted(extra)))
        raise HTTPException(422, "装配清单必须完整覆盖当前项目镜头；" + "；".join(detail))
    for item in items:
        if not current[item["shot_id"]]["dialogue"]:
            item["subtitle_enabled"] = False
            item["subtitle_start_seconds"] = None
        start = item.get("subtitle_start_seconds")
        if start is not None and float(start) >= float(current[item["shot_id"]]["seconds"]):
            raise HTTPException(422, f"镜头“{current[item['shot_id']]['title']}”字幕起点必须早于镜头时长")
        in_point = float(item.get("in_point_seconds") or 0)
        out_point = float(item.get("out_point_seconds") or current[item["shot_id"]]["seconds"])
        if in_point >= out_point or out_point > float(current[item["shot_id"]]["seconds"]) + 0.001:
            raise HTTPException(422, f"镜头“{current[item['shot_id']]['title']}”入出点必须位于候选时长内且入点早于出点")
        item["in_point_seconds"] = in_point
        item["out_point_seconds"] = out_point


def _validate_locked_sources(
    db: sqlite3.Connection, items: list[dict[str, Any]], output_root: Path | None = None,
) -> None:
    if not _table_exists(db, "candidate_master_versions"):
        return
    issues: list[str] = []
    for item in items:
        current = _source_for_shot(db, item["shot_id"], output_root)
        if current["source_status"] != "ready":
            issues.append(f"{item['title']}：{current['source_reason']}")
            continue
        if item.get("source_snapshot") != current:
            issues.append(f"{item['title']}：小节、镜头、审片、母版或媒体凭证已变化，请重新保存装配草稿")
    if issues:
        raise HTTPException(409, {"message": "装配来源门禁未通过", "issues": issues})


def _snapshot(plan: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": plan["id"], "project_id": plan["project_id"], "status": plan["status"],
        "revision": plan["revision"], "plan_hash": plan["plan_hash"], "items": items,
    }


def _record_version(db: sqlite3.Connection, plan: dict[str, Any], items: list[dict[str, Any]], source: str) -> None:
    snapshot = _snapshot(plan, items)
    db.execute(
        """INSERT INTO delivery_plan_versions
        (id, plan_id, revision, status, plan_hash, source, snapshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"delivery-version-{uuid.uuid4().hex[:12]}", plan["id"], plan["revision"], plan["status"],
            plan["plan_hash"], source, json.dumps(snapshot, ensure_ascii=False), utc_now(),
        ),
    )


def delivery_workspace(db_path: Path, output_root: Path | None = None) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        row = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        if row:
            plan = dict(row)
            items = _plan_items(db, plan["id"])
            versions = [dict(version) for version in db.execute(
                "SELECT id, revision, status, plan_hash, source, created_at FROM delivery_plan_versions WHERE plan_id = ? ORDER BY revision DESC",
                (plan["id"],),
            ).fetchall()]
        else:
            items = _default_items(db, project["id"], output_root)
            plan = {
                "id": None, "project_id": project["id"], "status": "draft", "revision": 0,
                "plan_hash": _hash_items(items), "created_at": None, "updated_at": None, "locked_at": None,
            }
            versions = []
        return {
            "project": {"id": project["id"], "title": project["title"], "episode": project["episode"]},
            "plan": {**plan, "items": items},
            "versions": versions,
            "summary": {
                "item_count": len(items),
                "subtitle_count": sum(bool(item["subtitle_enabled"] and item.get("dialogue")) for item in items),
                "planned_seconds": round(sum(float(item.get("seconds") or 0) for item in items), 3),
            },
        }


def save_delivery_plan(
    db_path: Path, payload: DeliveryPlanPatch, output_root: Path | None = None,
) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        current = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        expected = current["revision"] if current else 0
        if payload.base_revision != expected:
            raise HTTPException(409, f"装配计划已更新，当前修订为 {expected}")
        if current and current["status"] == "locked":
            raise HTTPException(409, "已锁定装配不能直接修改，请先创建新修订")
        raw_items = [item.model_dump() for item in payload.items]
        _validate_items(db, project["id"], raw_items)
        items = _bind_sources(
            db, [{**item, "ordinal": index} for index, item in enumerate(raw_items, start=1)], output_root,
        )
        plan_hash = _hash_items(items)
        now = utc_now()
        if current:
            plan_id = current["id"]
            revision = expected + 1
            db.execute(
                "UPDATE delivery_plans SET revision = ?, plan_hash = ?, updated_at = ? WHERE id = ?",
                (revision, plan_hash, now, plan_id),
            )
            db.execute("DELETE FROM delivery_plan_items WHERE plan_id = ?", (plan_id,))
        else:
            plan_id = f"delivery-plan-{uuid.uuid4().hex[:12]}"
            revision = 1
            db.execute(
                """INSERT INTO delivery_plans
                (id, project_id, status, revision, plan_hash, created_at, updated_at)
                VALUES (?, ?, 'draft', ?, ?, ?, ?)""",
                (plan_id, project["id"], revision, plan_hash, now, now),
            )
        for item in items:
            db.execute(
                """INSERT INTO delivery_plan_items
                (plan_id, shot_id, ordinal, subtitle_enabled, subtitle_start_seconds, transition,
                 in_point_seconds, out_point_seconds, dialogue_mode, section_id, candidate_id,
                  master_version_id, hd_artifact_id, hd_master_version_id, source_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, item["shot_id"], item["ordinal"], int(item["subtitle_enabled"]),
                    item["subtitle_start_seconds"], item["transition"], item["in_point_seconds"],
                    item["out_point_seconds"], item["dialogue_mode"], item.get("section_id"),
                    item.get("candidate_id"), item.get("master_version_id"),
                    item.get("hd_artifact_id"), item.get("hd_master_version_id"),
                    json.dumps(item.get("source_snapshot") or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        plan = dict(db.execute("SELECT * FROM delivery_plans WHERE id = ?", (plan_id,)).fetchone())
        _record_version(db, plan, _plan_items(db, plan_id), "save")
        db.commit()
    return delivery_workspace(db_path, output_root)


def lock_delivery_plan(
    db_path: Path, base_revision: int, output_root: Path | None = None,
) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        current = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        if not current:
            raise HTTPException(409, "请先保存装配草稿")
        if current["revision"] != base_revision:
            raise HTTPException(409, f"装配计划已更新，当前修订为 {current['revision']}")
        if current["status"] == "locked":
            return delivery_workspace(db_path, output_root)
        items = _plan_items(db, current["id"])
        _validate_items(db, project["id"], items)
        _validate_locked_sources(db, items, output_root)
        if current["plan_hash"] != _hash_items(items):
            raise HTTPException(409, "装配计划冻结哈希损坏，请重新保存")
        _validate_locked_sources(db, items, output_root)
        now = utc_now()
        revision = current["revision"] + 1
        db.execute(
            "UPDATE delivery_plans SET status = 'locked', revision = ?, updated_at = ?, locked_at = ? WHERE id = ?",
            (revision, now, now, current["id"]),
        )
        plan = dict(db.execute("SELECT * FROM delivery_plans WHERE id = ?", (current["id"],)).fetchone())
        _record_version(db, plan, items, "lock")
        db.commit()
    return delivery_workspace(db_path, output_root)


def reopen_delivery_plan(db_path: Path, base_revision: int, output_root: Path | None = None) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        current = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        if not current:
            raise HTTPException(404, "装配计划不存在")
        if current["revision"] != base_revision:
            raise HTTPException(409, f"装配计划已更新，当前修订为 {current['revision']}")
        if current["status"] != "locked":
            raise HTTPException(409, "装配计划已经是草稿")
        now = utc_now()
        revision = current["revision"] + 1
        db.execute(
            "UPDATE delivery_plans SET status = 'draft', revision = ?, updated_at = ?, locked_at = NULL WHERE id = ?",
            (revision, now, current["id"]),
        )
        plan = dict(db.execute("SELECT * FROM delivery_plans WHERE id = ?", (current["id"],)).fetchone())
        _record_version(db, plan, _plan_items(db, current["id"]), "reopen")
        db.commit()
    return delivery_workspace(db_path, output_root)


def locked_delivery_plan(db_path: Path, project_id: str) -> dict[str, Any] | None:
    with closing(connect(db_path)) as db:
        row = db.execute(
            "SELECT * FROM delivery_plans WHERE project_id = ? AND status = 'locked'",
            (project_id,),
        ).fetchone()
        if not row:
            return None
        plan = dict(row)
        return {**plan, "items": _plan_items(db, plan["id"])}


def create_delivery_router(db_path: Path, output_root: Path | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/delivery-plan")
    def api_delivery_workspace() -> dict[str, Any]:
        return delivery_workspace(db_path, output_root)

    @router.put("/api/delivery-plan")
    def api_save_delivery_plan(payload: DeliveryPlanPatch) -> dict[str, Any]:
        return save_delivery_plan(db_path, payload, output_root)

    @router.post("/api/delivery-plan/lock")
    def api_lock_delivery_plan(payload: DeliveryPlanRevision) -> dict[str, Any]:
        return lock_delivery_plan(db_path, payload.base_revision, output_root)

    @router.post("/api/delivery-plan/reopen")
    def api_reopen_delivery_plan(payload: DeliveryPlanRevision) -> dict[str, Any]:
        return reopen_delivery_plan(db_path, payload.base_revision, output_root)

    return router
