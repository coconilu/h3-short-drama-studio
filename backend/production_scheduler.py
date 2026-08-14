from __future__ import annotations

import hashlib
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
LOW_RESOLUTION_LIMIT = 768

PlanProvider = Callable[[str], dict[str, Any]]
Submitter = Callable[[dict[str, Any]], dict[str, Any]]
Syncer = Callable[[dict[str, Any]], dict[str, Any]]

_DB_PATH: Path | None = None
_PLAN_PROVIDER: PlanProvider | None = None
_SUBMITTER: Submitter | None = None
_SYNCER: Syncer | None = None
_OUTPUT_ROOT: Path | None = None
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
        CREATE TABLE IF NOT EXISTS production_item_attempts (
          id TEXT PRIMARY KEY,
          item_id TEXT NOT NULL REFERENCES production_batch_items(id) ON DELETE CASCADE,
          batch_id TEXT NOT NULL REFERENCES production_batches(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          attempt INTEGER NOT NULL,
          state TEXT NOT NULL CHECK(state IN (
            'submitting', 'running', 'completed', 'failed', 'submission_unknown'
          )),
          plan_hash TEXT NOT NULL,
          plan_snapshot TEXT NOT NULL,
          h3_project TEXT,
          prompt_ids TEXT NOT NULL DEFAULT '[]',
          candidate_ids TEXT NOT NULL DEFAULT '[]',
          source_snapshot TEXT NOT NULL DEFAULT '{}',
          media_evidence TEXT NOT NULL DEFAULT '[]',
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          UNIQUE(item_id, attempt)
        );
        CREATE INDEX IF NOT EXISTS idx_production_attempts_item
          ON production_item_attempts(item_id, attempt DESC);
        CREATE TABLE IF NOT EXISTS production_shot_leases (
          shot_id TEXT PRIMARY KEY REFERENCES shots(id) ON DELETE CASCADE,
          batch_id TEXT NOT NULL REFERENCES production_batches(id) ON DELETE CASCADE,
          item_id TEXT NOT NULL UNIQUE REFERENCES production_batch_items(id) ON DELETE CASCADE,
          state TEXT NOT NULL CHECK(state IN ('queued', 'submitting', 'running', 'unknown')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS production_shot_conflicts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          item_id TEXT NOT NULL UNIQUE REFERENCES production_batch_items(id) ON DELETE CASCADE,
          original_state TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('unresolved', 'resolved')) DEFAULT 'unresolved',
          reason TEXT NOT NULL,
          evidence TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_production_shot_conflicts_open
          ON production_shot_conflicts(shot_id, state);
        """
    )
    batch_columns = {row[1] for row in db.execute("PRAGMA table_info(production_batches)").fetchall()}
    for column, definition in (
        ("idempotency_key", "TEXT"),
        ("preflight_hash", "TEXT"),
    ):
        if column not in batch_columns:
            db.execute(f"ALTER TABLE production_batches ADD COLUMN {column} {definition}")
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_production_batch_idempotency
        ON production_batches(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL"""
    )
    item_columns = {row[1] for row in db.execute("PRAGMA table_info(production_batch_items)").fetchall()}
    attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(production_item_attempts)").fetchall()}
    credential_columns = (
        ("validation_job_id", "INTEGER"),
        ("validation_hash", "TEXT"),
        ("validation_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
        ("draft_job_id", "INTEGER"),
        ("draft_job_revision", "INTEGER"),
        ("draft_job_floor", "INTEGER NOT NULL DEFAULT 0"),
    )
    for column, definition in credential_columns:
        if column not in item_columns:
            db.execute(f"ALTER TABLE production_batch_items ADD COLUMN {column} {definition}")
        if column not in attempt_columns:
            db.execute(f"ALTER TABLE production_item_attempts ADD COLUMN {column} {definition}")
    conflict_columns = {row[1] for row in db.execute("PRAGMA table_info(production_shot_conflicts)").fetchall()}
    for column, definition in (
        ("resolved_by", "TEXT"),
        ("resolution_note", "TEXT"),
        ("revision", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in conflict_columns:
            db.execute(f"ALTER TABLE production_shot_conflicts ADD COLUMN {column} {definition}")
    # Upgrade safety: older databases had no cross-batch ownership row.  The
    # migration is one atomic unit: a shot with more than one actionable or
    # outcome-unknown item is never assigned an arbitrary winner.  Every
    # conflicting item receives durable audit evidence and the shot remains
    # blocked until those historical attempts are explicitly reconciled.
    db.execute("SAVEPOINT production_lease_migration")
    actionable = db.execute(
        """SELECT items.*, batches.created_at AS batch_created_at
        FROM production_batch_items items JOIN production_batches batches ON batches.id = items.batch_id
        WHERE items.state IN ('queued', 'submitting', 'running')
           OR (items.state = 'failed' AND items.error = 'submission_outcome_unknown')
        ORDER BY batches.created_at, items.ordinal, items.created_at"""
    ).fetchall()
    by_shot: dict[str, list[sqlite3.Row]] = {}
    for item in actionable:
        by_shot.setdefault(str(item["shot_id"]), []).append(item)
    for shot_id, items in by_shot.items():
        existing_lease = db.execute(
            "SELECT * FROM production_shot_leases WHERE shot_id = ?", (shot_id,),
        ).fetchone()
        mismatched_lease = bool(existing_lease and all(existing_lease["item_id"] != item["id"] for item in items))
        if len(items) > 1 or mismatched_lease:
            now = utc_now()
            conflict_items = list(items)
            if mismatched_lease:
                owner = db.execute(
                    "SELECT * FROM production_batch_items WHERE id = ?", (existing_lease["item_id"],),
                ).fetchone()
                if owner is not None and all(owner["id"] != item["id"] for item in conflict_items):
                    conflict_items.append(owner)
            evidence = {
                "shot_id": shot_id,
                "item_ids": [str(item["id"]) for item in conflict_items],
                "states": {str(item["id"]): str(item["state"]) for item in conflict_items},
                "detected_at": now,
            }
            for item in conflict_items:
                db.execute(
                    """INSERT OR IGNORE INTO production_shot_conflicts
                    (shot_id, item_id, original_state, state, reason, evidence, created_at)
                    VALUES (?, ?, ?, 'unresolved', 'migration_multiple_active_attempts', ?, ?)""",
                    (shot_id, item["id"], item["state"], json.dumps(evidence, sort_keys=True), now),
                )
                db.execute(
                    """UPDATE production_item_attempts SET state = 'submission_unknown',
                    error = 'migration_shot_ownership_conflict', updated_at = ?, completed_at = ?
                    WHERE item_id = ? AND state IN ('submitting', 'running')""",
                    (now, now, item["id"]),
                )
                db.execute(
                    """UPDATE production_batch_items SET state = 'failed',
                    error = 'migration_shot_ownership_conflict',
                    message = '升级时发现同一镜头存在多个未终结生产尝试；已失败关闭并等待人工对账',
                    updated_at = ?, completed_at = ?
                    WHERE id = ? AND (state IN ('queued', 'submitting', 'running')
                      OR (state = 'failed' AND error = 'submission_outcome_unknown'))""",
                    (now, now, item["id"]),
                )
                db.execute(
                    """UPDATE production_batches SET state = 'paused',
                    message = '升级发现重复镜头生产尝试；人工对账前保持镜头级阻断', updated_at = ?
                    WHERE id = ? AND state = 'running'""",
                    (now, item["batch_id"]),
                )
                db.execute(
                    """INSERT INTO production_batch_events
                    (batch_id, item_id, event, level, message, created_at)
                    VALUES (?, ?, 'migration_ownership_conflict', 'error', ?, ?)""",
                    (item["batch_id"], item["id"], "同一镜头存在多个旧活动或结果未知尝试，禁止自动选定所有者", now),
                )
            db.execute("DELETE FROM production_shot_leases WHERE shot_id = ?", (shot_id,))
            continue
        item = items[0]
        state = "unknown" if item["state"] == "failed" else item["state"]
        if existing_lease:
            db.execute(
                """UPDATE production_shot_leases SET state = ?, updated_at = ?
                WHERE shot_id = ? AND item_id = ?""",
                (state, utc_now(), shot_id, item["id"]),
            )
        else:
            db.execute(
                """INSERT INTO production_shot_leases
                (shot_id, batch_id, item_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (shot_id, item["batch_id"], item["id"], state, item["created_at"], item["updated_at"]),
            )
    db.execute("RELEASE production_lease_migration")


def configure_production_scheduler(
    db_path: Path,
    plan_provider: PlanProvider,
    submitter: Submitter,
    syncer: Syncer,
    *,
    poll_seconds: float = 2.0,
    output_root: Path | None = None,
) -> None:
    global _DB_PATH, _PLAN_PROVIDER, _SUBMITTER, _SYNCER, _POLL_SECONDS, _OUTPUT_ROOT
    _DB_PATH = db_path
    _PLAN_PROVIDER = plan_provider
    _SUBMITTER = submitter
    _SYNCER = syncer
    _POLL_SECONDS = max(0.02, poll_seconds)
    _OUTPUT_ROOT = output_root.resolve() if output_root else None


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


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone())


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validation_matches_plan(snapshot: dict[str, Any], plan: dict[str, Any]) -> bool:
    arguments = snapshot.get("arguments") if isinstance(snapshot, dict) else None
    if not isinstance(arguments, list):
        return False

    def value(flag: str) -> str | None:
        try:
            return str(arguments[arguments.index(flag) + 1])
        except (ValueError, IndexError):
            return None

    spec = plan.get("spec") or {}
    try:
        width, height = str(spec.get("resolution") or "").replace("×", "x").split("x", 1)
    except ValueError:
        return False
    return all((
        value("--prompt") == str(plan.get("compiled_prompt") or ""),
        value("--count") == str(spec.get("candidate_count")),
        value("--width") == width,
        value("--height") == height,
        value("--seconds") == str(float(spec.get("adapter_seconds"))),
        value("--steps") == str(spec.get("steps")),
        value("--mode") == str(plan.get("mode") or "").lower(),
    ))


def _safe_output(path_value: str | None) -> tuple[Path | None, str | None]:
    if not path_value:
        return None, "missing_output"
    path = Path(path_value).resolve()
    if _OUTPUT_ROOT is not None:
        try:
            path.relative_to(_OUTPUT_ROOT)
        except ValueError:
            return None, "outside_allowed_output_root"
    if not path.is_file():
        return None, "missing_file"
    return path, None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_evidence(
    db: sqlite3.Connection, shot_id: str, candidate_ids: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    # An empty attempt-owned set is evidence of zero candidates, never a
    # request to fall back to every historical candidate on the shot.
    if not candidate_ids or not _table_exists(db, "candidates"):
        return [], []
    placeholders = ",".join("?" for _ in candidate_ids)
    candidates = {
        str(item.get("external_id") or item["id"]): item
        for item in (
            dict(row)
            for row in db.execute(
                f"""SELECT id, external_id, prompt_id, seed, status, output_file, elapsed_seconds, metadata
                FROM candidates WHERE shot_id = ? AND archived = 0
                AND COALESCE(external_id, id) IN ({placeholders})""",
                (shot_id, *candidate_ids),
            ).fetchall()
        )
    }
    media: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        item = candidates.get(candidate_id)
        if item is None:
            media.append({"candidate_id": candidate_id, "evidence_status": "missing_record"})
            continue
        output = str(item.get("output_file") or "")
        path, reason = _safe_output(output)
        media.append({
            "candidate_id": candidate_id,
            "record_id": item["id"],
            "prompt_id": item.get("prompt_id"),
            "seed": item.get("seed"),
            "status": item.get("status"),
            "output_file": output or None,
            "file_exists": path is not None,
            "size_bytes": path.stat().st_size if path else None,
            "checksum_sha256": _file_sha256(path) if path else None,
            "evidence_status": "verified" if path else reason,
            "elapsed_seconds": item.get("elapsed_seconds"),
            "metadata": _decode_json(item.get("metadata"), {}),
        })
    return candidate_ids, media


def _draft_job(db: sqlite3.Connection, job_id: int | None) -> dict[str, Any] | None:
    if not _table_exists(db, "jobs"):
        return None
    if not job_id:
        return None
    row = db.execute("SELECT * FROM jobs WHERE id = ? AND kind = 'draft'", (job_id,)).fetchone()
    return dict(row) if row else None


def _has_unresolved_shot_conflict(db: sqlite3.Connection, shot_id: str) -> bool:
    return bool(db.execute(
        """SELECT 1 FROM production_shot_conflicts
        WHERE shot_id = ? AND state = 'unresolved' LIMIT 1""",
        (shot_id,),
    ).fetchone())


def _record_poll_reconciliation_conflict(
    db: sqlite3.Connection,
    item: dict[str, Any],
    result: dict[str, Any],
    *,
    now: str,
) -> None:
    """Persist a project-visible recovery path for an unsafe poll result."""
    attempt_number = int(item.get("attempts") or 0)
    attempt = db.execute(
        "SELECT * FROM production_item_attempts WHERE item_id = ? AND attempt = ?",
        (item["id"], attempt_number),
    ).fetchone()
    target = {
        "item_id": item["id"],
        "attempt": attempt_number,
        "draft_job_id": int(item.get("draft_job_id") or 0) or None,
        "item_job_revision": int(item.get("draft_job_revision") or 0),
        "attempt_job_revision": int(attempt["draft_job_revision"] or 0) if attempt else None,
    }
    observation = {
        "authoritative": result.get("authoritative"),
        "job_id": result.get("job_id"),
        "base_job_revision": result.get("base_job_revision"),
        "job_revision": result.get("job_revision"),
        "state": result.get("state"),
        "prompt_ids": list(result.get("prompt_ids") or []),
        "candidate_ids": list(result.get("candidate_ids") or []),
        "message": str(result.get("message") or "")[:1000],
        "detected_at": now,
    }
    existing = db.execute(
        "SELECT * FROM production_shot_conflicts WHERE item_id = ?", (item["id"],),
    ).fetchone()
    if existing:
        evidence = _decode_json(existing["evidence"], {})
        detections = list(evidence.get("detections") or [])
        detections.append(observation)
        evidence = {
            "kind": "draft_job_reconciliation_conflict",
            "target_attempt": target,
            "detections": detections[-20:],
            "prior_audit": evidence,
        }
        db.execute(
            """UPDATE production_shot_conflicts SET original_state = 'running', state = 'unresolved',
            reason = 'draft_job_reconciliation_conflict', evidence = ?, resolved_at = NULL,
            resolved_by = NULL, resolution_note = NULL, revision = revision + 1
            WHERE id = ?""",
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True), existing["id"]),
        )
    else:
        evidence = {
            "kind": "draft_job_reconciliation_conflict",
            "target_attempt": target,
            "detections": [observation],
        }
        db.execute(
            """INSERT INTO production_shot_conflicts
            (shot_id, item_id, original_state, state, reason, evidence, created_at)
            VALUES (?, ?, 'running', 'unresolved', 'draft_job_reconciliation_conflict', ?, ?)""",
            (item["shot_id"], item["id"], json.dumps(evidence, ensure_ascii=False, sort_keys=True), now),
        )


def _bind_draft_job(
    db: sqlite3.Connection, item: dict[str, Any], result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    result = result or {}
    explicit = result.get("job_id") or item.get("draft_job_id")
    if explicit:
        job = _draft_job(db, int(explicit))
        if not job or job.get("shot_id") != item["shot_id"]:
            return None
        return job
    floor = int(item.get("draft_job_floor") or 0)
    rows = db.execute(
        """SELECT * FROM jobs WHERE shot_id = ? AND kind = 'draft' AND id > ?
        AND COALESCE(plan_hash, '') = COALESCE(?, '') ORDER BY id""",
        (item["shot_id"], floor, item.get("validation_hash")),
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def _authoritative_draft_job(
    db: sqlite3.Connection, item: dict[str, Any], result: dict[str, Any],
) -> dict[str, Any] | None:
    """Verify that a sync result describes exactly the attempt-owned #4 job."""
    job_id = result.get("job_id")
    if not job_id:
        return None
    if item.get("draft_job_id") and int(item["draft_job_id"]) != int(job_id):
        return None
    job = _draft_job(db, int(job_id))
    if not job or job.get("shot_id") != item["shot_id"]:
        return None
    if result.get("base_job_revision") is not None and int(result["base_job_revision"]) != int(item.get("draft_job_revision") or 0):
        return None
    if result.get("job_revision") is None or int(result["job_revision"]) != int(job.get("reconciliation_revision") or 0):
        return None
    if result.get("state") != job.get("state"):
        return None
    if "prompt_ids" not in result or list(result["prompt_ids"] or []) != _decode_json(job.get("prompt_ids"), []):
        return None
    if "candidate_ids" not in result or list(result["candidate_ids"] or []) != _decode_json(job.get("candidate_ids"), []):
        return None
    if int(job.get("reconciliation_revision") or 0) < int(item.get("draft_job_revision") or 0):
        return None
    return job


def _attempt_owns_current_job(
    db: sqlite3.Connection, item: dict[str, Any], job: dict[str, Any],
) -> bool:
    """Check immutable attempt-owned records after the initial submit bind."""
    attempt = db.execute(
        """SELECT * FROM production_item_attempts
        WHERE item_id = ? AND attempt = ? AND draft_job_id = ?""",
        (item["id"], int(item.get("attempts") or 0), int(job["id"])),
    ).fetchone()
    if not attempt:
        return False
    # Job identity alone is insufficient: an external/manual sync may have
    # accidentally attached candidates from another attempt.
    return (
        _decode_json(attempt["prompt_ids"], []) == _decode_json(job.get("prompt_ids"), [])
        and _decode_json(attempt["candidate_ids"], []) == _decode_json(job.get("candidate_ids"), [])
    )


def _fail_poll_reconciliation(
    db: sqlite3.Connection,
    item: dict[str, Any],
    result: dict[str, Any],
    *,
    now: str,
) -> None:
    """Fail closed when a running attempt can no longer prove job ownership."""
    message = str(result.get("message") or "draft job 修订或候选所有权冲突，已失败关闭并等待人工审计")
    job = _draft_job(db, item.get("draft_job_id"))
    if job and not bool(job.get("retry_safe")) and job.get("state") != "待人工对账":
        job_evidence = _decode_json(job.get("reconciliation_snapshot"), {})
        poll_conflicts = list(job_evidence.get("production_poll_conflicts") or [])
        poll_conflicts.append({
            "item_id": item["id"],
            "attempt": int(item.get("attempts") or 0),
            "observed_job_revision": int(job.get("reconciliation_revision") or 0),
            "message": message[:1000],
            "detected_at": now,
        })
        job_evidence["production_poll_conflicts"] = poll_conflicts[-20:]
        db.execute(
            """UPDATE jobs SET state = '待人工对账', message = ?, retry_safe = 0,
            reconciliation_snapshot = ?, reconciliation_revision = reconciliation_revision + 1
            WHERE id = ? AND reconciliation_revision = ?""",
            (
                "生产调度检测到 attempt/job 所有权冲突；请核对精确任务后确认是否零提交",
                json.dumps(job_evidence, ensure_ascii=False, sort_keys=True),
                int(job["id"]),
                int(job.get("reconciliation_revision") or 0),
            ),
        )
    updated = db.execute(
        """UPDATE production_batch_items SET state = 'failed', message = ?,
        error = 'draft_job_reconciliation_conflict', updated_at = ?, completed_at = ?
        WHERE id = ? AND state = 'running'""",
        (message, now, now, item["id"]),
    )
    if updated.rowcount != 1:
        raise HTTPException(409, "生产条目已被另一同步请求更新")
    db.execute(
        """UPDATE production_item_attempts SET state = 'submission_unknown',
        error = 'draft_job_reconciliation_conflict', updated_at = ?, completed_at = ?
        WHERE item_id = ? AND attempt = ? AND state IN ('running', 'submission_unknown')""",
        (now, now, item["id"], int(item.get("attempts") or 0)),
    )
    lease = db.execute(
        """UPDATE production_shot_leases SET state = 'unknown', updated_at = ?
        WHERE item_id = ?""",
        (now, item["id"]),
    )
    if lease.rowcount != 1 and not db.execute(
        "SELECT 1 FROM production_shot_leases WHERE shot_id = ?", (item["shot_id"],),
    ).fetchone():
        db.execute(
            """INSERT INTO production_shot_leases
            (shot_id, batch_id, item_id, state, created_at, updated_at)
            VALUES (?, ?, ?, 'unknown', ?, ?)""",
            (item["shot_id"], item["batch_id"], item["id"], now, now),
        )
    _record_poll_reconciliation_conflict(db, item, result, now=now)
    _event(
        db,
        item["batch_id"],
        "draft_job_reconciliation_conflict",
        message,
        item_id=item["id"],
        level="error",
    )
    _refresh_batch(db, item["batch_id"])


def _refresh_attempt_evidence(
    db: sqlite3.Connection,
    item: dict[str, Any],
    *,
    state: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    result = result or {}
    if result.get("job_id"):
        job = _authoritative_draft_job(db, item, result)
        if job is None:
            raise HTTPException(409, "生产同步结果与条目冻结的 draft job 所有权不一致")
    else:
        job = _bind_draft_job(db, item, result)
    job = job or {}
    prompt_ids = (
        list(result.get("prompt_ids") or [])
        if "prompt_ids" in result
        else _decode_json(job.get("prompt_ids"), []) or _decode_json(item.get("prompt_ids"), [])
    )
    candidate_ids = (
        list(result.get("candidate_ids") or [])
        if "candidate_ids" in result
        else _decode_json(job.get("candidate_ids"), [])
    )
    candidate_ids, media = _candidate_evidence(db, item["shot_id"], list(candidate_ids))
    source_snapshot = _decode_json(job.get("source_snapshot"), {}) or {
        "plan_hash": item["plan_hash"],
        "plan": _decode_json(item.get("plan_snapshot"), {}),
    }
    job_id = int(job["id"]) if job else item.get("draft_job_id")
    job_revision = int(job.get("reconciliation_revision") or 0) if job else item.get("draft_job_revision")
    completed_at = utc_now() if state in {"completed", "failed", "submission_unknown"} else None
    db.execute(
        """UPDATE production_item_attempts SET state = ?, h3_project = ?, prompt_ids = ?,
        candidate_ids = ?, source_snapshot = ?, media_evidence = ?, draft_job_id = ?, draft_job_revision = ?,
        error = ?, updated_at = ?, completed_at = ?
        WHERE item_id = ? AND attempt = ?""",
        (
            state,
            result.get("h3_project") or job.get("h3_project") or item.get("h3_project"),
            json.dumps(prompt_ids, ensure_ascii=False),
            json.dumps(candidate_ids, ensure_ascii=False),
            json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True),
            json.dumps(media, ensure_ascii=False, sort_keys=True),
            job_id,
            job_revision,
            error,
            utc_now(),
            completed_at,
            item["id"],
            int(item.get("attempts") or 0),
        ),
    )
    if job_id:
        db.execute(
            """UPDATE production_batch_items SET draft_job_id = ?, draft_job_revision = ?,
            prompt_ids = ?, h3_project = COALESCE(?, h3_project), updated_at = ? WHERE id = ?""",
            (
                job_id, job_revision, json.dumps(prompt_ids, ensure_ascii=False),
                job.get("h3_project") or result.get("h3_project"), utc_now(), item["id"],
            ),
        )


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
        item["validation_snapshot"] = _decode_json(item.get("validation_snapshot"), {})
        item["attempt_history"] = []
        for attempt_row in db.execute(
            "SELECT * FROM production_item_attempts WHERE item_id = ? ORDER BY attempt DESC", (item["id"],),
        ).fetchall():
            attempt = dict(attempt_row)
            for field, fallback in (
                ("plan_snapshot", {}), ("prompt_ids", []), ("candidate_ids", []),
                ("source_snapshot", {}), ("media_evidence", []),
                ("validation_snapshot", {}),
            ):
                attempt[field] = _decode_json(attempt.get(field), fallback)
            item["attempt_history"].append(attempt)
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


def _job_zero_submit_basis(job: dict[str, Any], *, shot_id: str) -> tuple[str | None, str | None]:
    if job.get("shot_id") != shot_id:
        return None, "draft job 与冲突镜头不匹配"
    if job.get("state") != "提交失败" or not bool(job.get("retry_safe")):
        return None, "draft job 尚未被可信地确认成零提交"
    if _decode_json(job.get("candidate_ids"), []):
        return None, "draft job 已记录候选，不能证明零提交"
    evidence = _decode_json(job.get("reconciliation_snapshot"), {})
    manual = any(
        isinstance(value, dict) and value.get("action") == "confirm_not_submitted"
        for value in evidence.get("manual_resolutions") or []
    )
    frozen_pre_spawn = bool(
        evidence.get("adapter_returned_success") is False
        and isinstance(evidence.get("frozen_command"), dict)
        and len(str(evidence.get("input_hash") or "")) == 64
        and isinstance(evidence.get("manifest_before"), dict)
        and evidence["manifest_before"].get("trusted") is True
    )
    if manual:
        return "manual_confirm_not_submitted", None
    if frozen_pre_spawn:
        return "audited_pre_spawn_failure", None
    return None, "retry_safe 缺少人工确认或受信任的进程未启动冻结证据"


def _poll_conflict_zero_submit_proof(
    db: sqlite3.Connection, conflict: dict[str, Any], item: dict[str, Any],
) -> dict[str, Any]:
    evidence = _decode_json(conflict.get("evidence"), {})
    target = evidence.get("target_attempt") if isinstance(evidence, dict) else None
    if not isinstance(target, dict) or target.get("item_id") != item["id"]:
        return {"verified": False, "reason": "冲突缺少精确 attempt 审计目标"}
    attempt_number = int(target.get("attempt") or 0)
    job_id = int(target.get("draft_job_id") or 0)
    if attempt_number <= 0 or job_id <= 0:
        return {"verified": False, "reason": "冲突未绑定精确 attempt 或 draft job"}
    attempt = db.execute(
        """SELECT * FROM production_item_attempts
        WHERE item_id = ? AND attempt = ?""",
        (item["id"], attempt_number),
    ).fetchone()
    if not attempt or int(attempt["draft_job_id"] or 0) != job_id:
        return {"verified": False, "reason": f"attempt {attempt_number} 的 draft job 所有权与冲突证据不一致"}
    if attempt["state"] != "submission_unknown" or attempt["error"] != "draft_job_reconciliation_conflict":
        return {"verified": False, "reason": f"attempt {attempt_number} 未处于本次冲突的 submission_unknown 审计状态"}
    if _decode_json(attempt["candidate_ids"], []) or _decode_json(attempt["media_evidence"], []):
        return {"verified": False, "reason": f"attempt {attempt_number} 已记录候选或媒体证据，不能证明零提交"}
    job = _draft_job(db, job_id)
    if not job:
        return {"verified": False, "reason": f"attempt {attempt_number} 的精确 draft job 不存在"}
    basis, reason = _job_zero_submit_basis(job, shot_id=item["shot_id"])
    if not basis:
        return {"verified": False, "reason": f"attempt {attempt_number}：{reason}"}
    return {
        "verified": True,
        "basis": basis,
        "attempt": attempt_number,
        "job_id": job_id,
        "job_revision": int(job.get("reconciliation_revision") or 0),
    }


def _zero_submit_proof(
    db: sqlite3.Connection, conflict: dict[str, Any], item: dict[str, Any],
) -> dict[str, Any]:
    if conflict.get("reason") == "draft_job_reconciliation_conflict":
        return _poll_conflict_zero_submit_proof(db, conflict, item)
    attempts = [
        dict(value)
        for value in db.execute(
            "SELECT * FROM production_item_attempts WHERE item_id = ? ORDER BY attempt",
            (item["id"],),
        ).fetchall()
    ]
    claimed_count = int(item.get("attempts") or 0)
    if conflict["original_state"] == "queued" and claimed_count == 0 and not attempts and not item.get("draft_job_id"):
        return {"verified": True, "basis": "queued_never_claimed", "job_id": None}
    expected_numbers = list(range(1, claimed_count + 1))
    actual_numbers = [int(attempt.get("attempt") or 0) for attempt in attempts]
    if claimed_count <= 0 or actual_numbers != expected_numbers:
        return {
            "verified": False,
            "reason": f"历史 attempt 行不完整：期望 {expected_numbers}，实际 {actual_numbers}",
        }
    owned_job_ids = [int(attempt.get("draft_job_id") or 0) for attempt in attempts]
    if any(job_id <= 0 for job_id in owned_job_ids):
        return {"verified": False, "reason": "每个已领取 attempt 都必须独立绑定精确 draft job"}
    if len(set(owned_job_ids)) != claimed_count:
        return {"verified": False, "reason": "多个历史 attempt 复用了同一 draft job，不能独立证明零提交"}
    if int(item.get("draft_job_id") or 0) != owned_job_ids[-1]:
        return {"verified": False, "reason": "条目当前 draft job 与最后一次实际 attempt 不一致"}
    attempt_proofs: list[dict[str, Any]] = []
    for attempt, job_id in zip(attempts, owned_job_ids, strict=True):
        attempt_number = int(attempt["attempt"])
        if _decode_json(attempt.get("candidate_ids"), []) or _decode_json(attempt.get("media_evidence"), []):
            return {"verified": False, "reason": f"attempt {attempt_number} 已记录候选或媒体证据，不能证明零提交"}
        job = _draft_job(db, job_id)
        if not job or job.get("shot_id") != item["shot_id"]:
            return {"verified": False, "reason": f"attempt {attempt_number} 的精确 draft job 缺失或镜头不匹配"}
        basis, reason = _job_zero_submit_basis(job, shot_id=item["shot_id"])
        if not basis:
            return {
                "verified": False,
                "reason": f"attempt {attempt_number}：{reason}",
            }
        attempt_proofs.append({
            "attempt": attempt_number,
            "basis": basis,
            "job_id": int(job["id"]),
            "job_revision": int(job.get("reconciliation_revision") or 0),
        })
    return {
        "verified": True,
        "basis": "all_claimed_attempts_zero_submit",
        "attempts": attempt_proofs,
    }


def _conflict_groups(db: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    conflict_rows = db.execute(
        """SELECT conflicts.*, items.batch_id, items.shot_id, items.title AS item_title,
        items.state AS item_state, items.error AS item_error, items.attempts, items.draft_job_id,
        shots.title AS shot_title
        FROM production_shot_conflicts conflicts
        JOIN production_batch_items items ON items.id = conflicts.item_id
        JOIN production_batches batches ON batches.id = items.batch_id
        JOIN shots ON shots.id = conflicts.shot_id
        WHERE batches.project_id = ?
        ORDER BY conflicts.created_at DESC, conflicts.id""",
        (project_id,),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for raw in conflict_rows:
        conflict = dict(raw)
        item = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (conflict["item_id"],)).fetchone()
        proof = _zero_submit_proof(db, conflict, dict(item)) if conflict["state"] == "unresolved" and item else {
            "verified": conflict["state"] == "resolved",
            "basis": "resolved_audit" if conflict["state"] == "resolved" else None,
        }
        public = {
            "id": int(conflict["id"]),
            "item_id": conflict["item_id"],
            "batch_id": conflict["batch_id"],
            "item_title": conflict["item_title"],
            "original_state": conflict["original_state"],
            "item_state": conflict["item_state"],
            "item_error": conflict["item_error"],
            "state": conflict["state"],
            "reason": conflict["reason"],
            "evidence": _decode_json(conflict.get("evidence"), {}),
            "proof": proof,
            "created_at": conflict["created_at"],
            "resolved_at": conflict.get("resolved_at"),
            "resolved_by": conflict.get("resolved_by"),
            "resolution_note": conflict.get("resolution_note"),
            "revision": int(conflict.get("revision") or 0),
        }
        group = grouped.setdefault(conflict["shot_id"], {
            "shot_id": conflict["shot_id"], "shot_title": conflict["shot_title"], "items": [],
        })
        group["items"].append(public)
    groups = []
    for group in grouped.values():
        unresolved = [item for item in group["items"] if item["state"] == "unresolved"]
        group["state"] = "unresolved" if unresolved else "resolved"
        group["can_resolve"] = bool(unresolved) and all(item["proof"].get("verified") for item in unresolved)
        group["issues"] = [item["proof"].get("reason") for item in unresolved if not item["proof"].get("verified")]
        group["expected_revisions"] = {str(item["id"]): item["revision"] for item in unresolved}
        groups.append(group)
    return groups


def list_ownership_conflicts(db_path: Path, *, include_resolved: bool = True) -> list[dict[str, Any]]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        groups = _conflict_groups(db, project["id"])
        return groups if include_resolved else [group for group in groups if group["state"] == "unresolved"]


def resolve_ownership_conflicts(
    db_path: Path,
    shot_id: str,
    *,
    expected_revisions: dict[str, int],
    resolved_by: str,
    note: str,
) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        shot = db.execute("SELECT id FROM shots WHERE id = ? AND project_id = ?", (shot_id, project["id"])).fetchone()
        if not shot:
            raise HTTPException(404, "生产冲突不属于当前项目")
        rows_found = db.execute(
            """SELECT conflicts.*, items.batch_id, items.attempts, items.draft_job_id,
            items.state AS item_state, items.shot_id
            FROM production_shot_conflicts conflicts
            JOIN production_batch_items items ON items.id = conflicts.item_id
            JOIN production_batches batches ON batches.id = items.batch_id
            WHERE conflicts.shot_id = ? AND conflicts.state = 'unresolved' AND batches.project_id = ?
            ORDER BY conflicts.id""",
            (shot_id, project["id"]),
        ).fetchall()
        if not rows_found:
            raise HTTPException(409, "该镜头没有尚未解决的生产所有权冲突")
        expected = {str(row["id"]): int(row["revision"] or 0) for row in rows_found}
        if expected != {str(key): int(value) for key, value in expected_revisions.items()}:
            raise HTTPException(409, "冲突审计修订已变化，请刷新后重新确认")
        proofs: dict[str, dict[str, Any]] = {}
        issues: list[dict[str, Any]] = []
        for raw in rows_found:
            conflict = dict(raw)
            item = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (conflict["item_id"],)).fetchone()
            proof = _zero_submit_proof(db, conflict, dict(item)) if item else {"verified": False, "reason": "冲突条目已不存在"}
            proofs[str(conflict["id"])] = proof
            if not proof.get("verified"):
                issues.append({"conflict_id": int(conflict["id"]), "item_id": conflict["item_id"], "reason": proof.get("reason")})
        if issues:
            raise HTTPException(409, {"message": "整组冲突尚未全部证明零提交", "issues": issues})
        now = utc_now()
        batch_ids: set[str] = set()
        for raw in rows_found:
            conflict = dict(raw)
            evidence = _decode_json(conflict.get("evidence"), {})
            evidence["resolution"] = {
                "proof": proofs[str(conflict["id"])], "resolved_at": now,
                "resolved_by": resolved_by, "note": note,
            }
            changed = db.execute(
                """UPDATE production_shot_conflicts SET state = 'resolved', resolved_at = ?, resolved_by = ?,
                resolution_note = ?, evidence = ?, revision = revision + 1
                WHERE id = ? AND state = 'unresolved' AND revision = ?""",
                (
                    now, resolved_by, note, json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    conflict["id"], expected[str(conflict["id"])],
                ),
            )
            if changed.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "冲突已被另一解决请求更新")
            db.execute("DELETE FROM production_shot_leases WHERE item_id = ?", (conflict["item_id"],))
            _event(
                db, conflict["batch_id"], "migration_conflict_resolved",
                f"{resolved_by} 已确认整组旧尝试均未提交：{note}", item_id=conflict["item_id"], level="warning",
            )
            batch_ids.add(str(conflict["batch_id"]))
        for batch_id in batch_ids:
            _refresh_batch(db, batch_id)
        db.commit()
    return next(group for group in list_ownership_conflicts(db_path) if group["shot_id"] == shot_id)


def batch_preflight(db_path: Path, plan_provider: PlanProvider, shot_ids: list[str]) -> dict[str, Any]:
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
        frozen_items: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for shot_row in shots:
            shot = dict(shot_row)
            plan = plan_provider(shot["id"])
            spec = plan.get("spec") or {}
            resolution_numbers = [int(value) for value in str(spec.get("resolution") or "").replace("x", "×").split("×") if value.isdigit()]
            reasons: list[str] = []
            if plan.get("status") != "approved":
                reasons.append("当前生成计划尚未批准")
            if not plan.get("ready"):
                reasons.append("当前生成计划存在阻断项")
            if int(spec.get("candidate_count") or 0) < 2:
                reasons.append("低清生产批次每镜至少需要 2 条候选")
            if resolution_numbers and max(resolution_numbers) > LOW_RESOLUTION_LIMIT:
                reasons.append("生产批次只接受最长边不超过 768 的低清候选规格")
            if shot.get("status") in ("生成中", "已定稿"):
                reasons.append(f"镜头当前状态为{shot['status']}")
            lease = db.execute(
                "SELECT batch_id, item_id, state FROM production_shot_leases WHERE shot_id = ?", (shot["id"],),
            ).fetchone()
            if lease:
                reasons.append("镜头已被另一个活动生产批次占用")
            if _has_unresolved_shot_conflict(db, shot["id"]):
                reasons.append("镜头存在升级前的生产所有权冲突，必须先完成人工对账")
            validation = db.execute(
                "SELECT * FROM jobs WHERE shot_id = ? AND kind = 'validation' ORDER BY id DESC LIMIT 1",
                (shot["id"],),
            ).fetchone() if _table_exists(db, "jobs") else None
            validation_snapshot = _decode_json(validation["source_snapshot"], {}) if validation else {}
            if not validation or validation["state"] != "校验通过" or not validation["plan_hash"] or not validation_snapshot:
                reasons.append("缺少当前 H3 输入的可信 dry-run 校验凭证")
            elif not _validation_matches_plan(validation_snapshot, plan):
                reasons.append("最新 dry-run 凭证与当前批准计划的实际适配器输入不一致")
            frozen = {
                "shot_id": shot["id"], "title": shot["title"], "ordinal": shot["ordinal"],
                "plan_hash": plan.get("plan_hash"),
                "plan_snapshot": {key: value for key, value in plan.items() if not key.startswith("_")},
                "validation_job_id": int(validation["id"]) if validation else None,
                "validation_hash": validation["plan_hash"] if validation else None,
                "validation_snapshot": validation_snapshot,
            }
            frozen_items.append(frozen)
            results.append({
                "shot_id": shot["id"], "title": shot["title"], "ok": not reasons,
                "reasons": reasons, "plan_hash": frozen["plan_hash"],
                "validation_job_id": frozen["validation_job_id"], "validation_hash": frozen["validation_hash"],
            })
        credential = {"project_id": project["id"], "shot_ids": [item["shot_id"] for item in frozen_items], "items": frozen_items}
        return {
            "ok": all(item["ok"] for item in results), "gpu_submitted": False,
            "requested_count": len(results), "passed_count": sum(item["ok"] for item in results),
            "failed_count": sum(not item["ok"] for item in results), "results": results,
            "preflight_hash": _canonical_hash(credential), "frozen_items": frozen_items,
        }


def _verify_frozen_item(db: sqlite3.Connection, project_id: str, frozen: dict[str, Any]) -> None:
    shot = db.execute("SELECT * FROM shots WHERE id = ? AND project_id = ?", (frozen["shot_id"], project_id)).fetchone()
    if not shot:
        raise HTTPException(409, "预检后镜头已删除或移出当前项目")
    if db.execute("SELECT 1 FROM production_shot_leases WHERE shot_id = ?", (frozen["shot_id"],)).fetchone():
        raise HTTPException(409, "预检后镜头已被另一个活动生产批次占用")
    if _has_unresolved_shot_conflict(db, frozen["shot_id"]):
        raise HTTPException(409, "镜头存在尚未解决的历史生产所有权冲突")
    plan = db.execute(
        "SELECT plan_hash, status FROM h3_prompt_plans WHERE shot_id = ? ORDER BY rowid DESC LIMIT 1",
        (frozen["shot_id"],),
    ).fetchone()
    if not plan or plan["status"] != "approved" or plan["plan_hash"] != frozen["plan_hash"]:
        raise HTTPException(409, "预检后批准计划已变化")
    validation = db.execute(
        "SELECT * FROM jobs WHERE id = ? AND shot_id = ? AND kind = 'validation'",
        (frozen["validation_job_id"], frozen["shot_id"]),
    ).fetchone()
    if (
        not validation or validation["state"] != "校验通过" or validation["plan_hash"] != frozen["validation_hash"]
        or _decode_json(validation["source_snapshot"], {}) != frozen["validation_snapshot"]
    ):
        raise HTTPException(409, "预检后的 dry-run 校验凭证已变化")


def create_batch(
    db_path: Path,
    plan_provider: PlanProvider,
    shot_ids: list[str],
    *,
    name: str,
    max_attempts: int,
    preflight_hash: str,
    idempotency_key: str,
) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        existing = db.execute(
            "SELECT * FROM production_batches WHERE project_id = ? AND idempotency_key = ?",
            (project["id"], idempotency_key),
        ).fetchone()
        if existing:
            if existing["preflight_hash"] != preflight_hash:
                raise HTTPException(409, "幂等键已用于不同的生产预检")
            existing_shots = [
                row["shot_id"] for row in db.execute(
                    "SELECT shot_id FROM production_batch_items WHERE batch_id = ? ORDER BY ordinal, created_at",
                    (existing["id"],),
                ).fetchall()
            ]
            if len(existing_shots) != len(shot_ids) or set(existing_shots) != set(shot_ids):
                raise HTTPException(409, "幂等键已用于不同的镜头集合")
            return _batch_public(db, existing)
    preflight = batch_preflight(db_path, plan_provider, shot_ids)
    if not preflight["ok"]:
        raise HTTPException(409, {"message": "生产批次门禁未通过", "issues": preflight["results"]})
    if preflight_hash != preflight["preflight_hash"]:
        raise HTTPException(409, "批次预检已过期，请重新预检")
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        existing = db.execute(
            "SELECT * FROM production_batches WHERE project_id = ? AND idempotency_key = ?",
            (project["id"], idempotency_key),
        ).fetchone()
        if existing:
            if existing["preflight_hash"] != preflight_hash:
                raise HTTPException(409, "幂等键已用于不同的生产预检")
            existing_shots = [
                row["shot_id"] for row in db.execute(
                    "SELECT shot_id FROM production_batch_items WHERE batch_id = ?", (existing["id"],),
                ).fetchall()
            ]
            if len(existing_shots) != len(shot_ids) or set(existing_shots) != set(shot_ids):
                raise HTTPException(409, "幂等键已用于不同的镜头集合")
            db.commit()
            return _batch_public(db, existing)
        for frozen in preflight["frozen_items"]:
            _verify_frozen_item(db, project["id"], frozen)

        now = utc_now()
        batch_id = f"production-{uuid.uuid4().hex[:12]}"
        candidate_total = sum(int(item["plan_snapshot"]["spec"]["candidate_count"]) for item in preflight["frozen_items"])
        db.execute(
            """INSERT INTO production_batches
            (id, project_id, name, state, item_count, config, message, created_at, updated_at, started_at,
             idempotency_key, preflight_hash)
            VALUES (?, ?, ?, 'running', ?, ?, '等待单 GPU 调度器提交首个镜头', ?, ?, ?, ?, ?)""",
            (
                batch_id,
                project["id"],
                name.strip() or f"{project['episode']} 生产批次",
                len(preflight["frozen_items"]),
                json.dumps({
                    "concurrency": 1,
                    "candidate_total": candidate_total,
                    "snapshot_policy": "immutable",
                    "minimum_candidates_per_shot": 2,
                    "resolution_policy": "low_resolution_max_768",
                }, ensure_ascii=False),
                now,
                now,
                now,
                idempotency_key,
                preflight_hash,
            ),
        )
        for frozen in preflight["frozen_items"]:
            item_id = f"production-item-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO production_batch_items
                (id, batch_id, shot_id, ordinal, title, state, plan_hash, plan_snapshot,
                 max_attempts, message, created_at, updated_at, validation_job_id, validation_hash, validation_snapshot)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, '等待提交', ?, ?, ?, ?, ?)""",
                (
                    item_id, batch_id, frozen["shot_id"], frozen["ordinal"], frozen["title"], frozen["plan_hash"],
                    json.dumps(frozen["plan_snapshot"], ensure_ascii=False, sort_keys=True), max_attempts, now, now,
                    frozen["validation_job_id"], frozen["validation_hash"],
                    json.dumps(frozen["validation_snapshot"], ensure_ascii=False, sort_keys=True),
                ),
            )
            db.execute(
                """INSERT INTO production_shot_leases
                (shot_id, batch_id, item_id, state, created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)""",
                (frozen["shot_id"], batch_id, item_id, now, now),
            )
        _event(db, batch_id, "created", f"已冻结 {len(preflight['frozen_items'])} 个镜头、{candidate_total} 条候选的提交凭证")
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
            _refresh_attempt_evidence(
                db, dict(item), state="submission_unknown", error="submission_outcome_unknown",
            )
            db.execute(
                "UPDATE production_shot_leases SET state = 'unknown', updated_at = ? WHERE item_id = ?",
                (now, item["id"]),
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
        draft_floor = int(db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM jobs WHERE shot_id = ? AND kind = 'draft'", (item["shot_id"],),
        ).fetchone()[0])
        cursor = db.execute(
            """UPDATE production_batch_items SET state = 'submitting', attempts = attempts + 1,
            message = '正在提交到 ComfyUI', error = NULL, started_at = COALESCE(started_at, ?),
            updated_at = ?, draft_job_floor = ?, draft_job_id = NULL, draft_job_revision = NULL
            WHERE id = ? AND state = 'queued'""",
            (now, now, draft_floor, item["id"]),
        )
        if cursor.rowcount != 1:
            db.rollback()
            return None
        attempt = int(item["attempts"]) + 1
        db.execute(
            """INSERT INTO production_item_attempts
            (id, item_id, batch_id, shot_id, attempt, state, plan_hash, plan_snapshot,
             validation_job_id, validation_hash, validation_snapshot, draft_job_floor, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'submitting', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"production-attempt-{uuid.uuid4().hex[:12]}", item["id"], item["batch_id"], item["shot_id"],
                attempt, item["plan_hash"], item["plan_snapshot"], item["validation_job_id"],
                item["validation_hash"], item["validation_snapshot"], draft_floor, now, now,
            ),
        )
        lease = db.execute(
            """UPDATE production_shot_leases SET state = 'submitting', updated_at = ?
            WHERE shot_id = ? AND item_id = ? AND state = 'queued'""",
            (now, item["shot_id"], item["id"]),
        )
        if lease.rowcount != 1:
            db.rollback()
            return None
        _event(db, item["batch_id"], "submitting", f"开始提交镜头 {item['title']}", item_id=item["id"])
        db.commit()
        claimed = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (item["id"],)).fetchone()
        return dict(claimed)


def _fail_item(
    db_path: Path, item: dict[str, Any], message: str, error: str, *, submission_unknown: bool = False,
) -> None:
    with closing(connect(db_path)) as db:
        now = utc_now()
        db.execute(
            """UPDATE production_batch_items SET state = 'failed', message = ?, error = ?,
            updated_at = ?, completed_at = ? WHERE id = ?""",
            (message, "submission_outcome_unknown" if submission_unknown else error[-5000:], now, now, item["id"]),
        )
        _refresh_attempt_evidence(
            db, item, state="submission_unknown" if submission_unknown else "failed", error=error[-5000:],
        )
        if submission_unknown:
            db.execute(
                "UPDATE production_shot_leases SET state = 'unknown', updated_at = ? WHERE item_id = ?",
                (now, item["id"]),
            )
        else:
            db.execute("DELETE FROM production_shot_leases WHERE item_id = ?", (item["id"],))
        _event(db, item["batch_id"], "failed", message, item_id=item["id"], level="error")
        _refresh_batch(db, item["batch_id"])
        db.commit()


def submit_claimed_item(db_path: Path, item: dict[str, Any], plan_provider: PlanProvider, submitter: Submitter) -> None:
    try:
        current = plan_provider(item["shot_id"])
        if current.get("status") != "approved" or current.get("plan_hash") != item["plan_hash"]:
            _fail_item(db_path, item, "批准计划已变化，未提交 GPU", "approved_plan_changed")
            return
        result = submitter(item)
        result_state = str(result.get("state") or "已提交")
        completed = result_state == "完成"
        now = utc_now()
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            current_item = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (item["id"],)).fetchone()
            if not current_item or current_item["state"] != "submitting":
                raise HTTPException(409, "生产条目提交状态已变化")
            item = dict(current_item)
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
            _refresh_attempt_evidence(db, item, state="completed" if completed else "running", result=result)
            if completed:
                db.execute("DELETE FROM production_shot_leases WHERE item_id = ?", (item["id"],))
            else:
                db.execute(
                    "UPDATE production_shot_leases SET state = 'running', updated_at = ? WHERE item_id = ?",
                    (now, item["id"]),
                )
            _event(
                db, item["batch_id"], "completed" if completed else "submitted",
                result.get("message") or result_state, item_id=item["id"],
            )
            _refresh_batch(db, item["batch_id"])
            db.commit()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        with closing(connect(db_path)) as db:
            current = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (item["id"],)).fetchone()
            bound = _bind_draft_job(db, dict(current or item)) if current else None
            retry_safe = bool(bound and bound.get("retry_safe"))
        _fail_item(
            db_path, dict(current or item), "镜头提交失败，等待人工处理", detail,
            submission_unknown=not retry_safe,
        )
    except Exception as exc:
        with closing(connect(db_path)) as db:
            current = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (item["id"],)).fetchone()
            bound = _bind_draft_job(db, dict(current or item)) if current else None
            retry_safe = bool(bound and bound.get("retry_safe"))
        _fail_item(
            db_path, dict(current or item), "镜头提交失败，等待人工处理", str(exc),
            submission_unknown=not retry_safe,
        )


def poll_running_items(db_path: Path, syncer: Syncer) -> int:
    with closing(connect(db_path)) as db:
        items = [dict(item) for item in db.execute("SELECT * FROM production_batch_items WHERE state = 'running'").fetchall()]
    changed = 0
    for item in items:
        try:
            result = syncer(item)
        except Exception:
            continue
        state = str(result.get("state") or "")
        now = utc_now()
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM production_batch_items WHERE id = ? AND state = 'running'", (item["id"],),
            ).fetchone()
            if not current:
                db.rollback()
                continue
            current_item = dict(current)
            job = (
                None
                if result.get("authoritative") is False or result.get("base_job_revision") is None
                else _authoritative_draft_job(db, current_item, result)
            )
            if job is not None and not _attempt_owns_current_job(db, current_item, job):
                job = None
            if job is None:
                _fail_poll_reconciliation(db, current_item, result, now=now)
                db.commit()
                changed += 1
                continue
            old_revision = int(current_item.get("draft_job_revision") or 0)
            job_revision = int(job.get("reconciliation_revision") or 0)
            if job_revision > old_revision:
                advanced = db.execute(
                    """UPDATE production_batch_items SET draft_job_revision = ?, updated_at = ?
                    WHERE id = ? AND state = 'running' AND COALESCE(draft_job_revision, 0) = ?""",
                    (job_revision, now, item["id"], old_revision),
                )
                attempt_advanced = db.execute(
                    """UPDATE production_item_attempts SET draft_job_revision = ?, updated_at = ?
                    WHERE item_id = ? AND attempt = ? AND draft_job_id = ?
                    AND COALESCE(draft_job_revision, 0) = ? AND state = 'running'""",
                    (
                        job_revision,
                        now,
                        item["id"],
                        int(current_item.get("attempts") or 0),
                        int(job["id"]),
                        old_revision,
                    ),
                )
                if advanced.rowcount != 1 or attempt_advanced.rowcount != 1:
                    authority_item = db.execute(
                        "SELECT * FROM production_batch_items WHERE id = ?", (item["id"],),
                    ).fetchone()
                    authority_attempt = db.execute(
                        """SELECT * FROM production_item_attempts
                        WHERE item_id = ? AND attempt = ? AND draft_job_id = ?""",
                        (item["id"], int(current_item.get("attempts") or 0), int(job["id"])),
                    ).fetchone()
                    already_consumed = bool(
                        authority_item
                        and authority_attempt
                        and authority_item["state"] != "running"
                        and int(authority_item["draft_job_revision"] or 0) >= job_revision
                        and int(authority_attempt["draft_job_revision"] or 0) >= job_revision
                        and authority_attempt["state"] in ITEM_TERMINAL_STATES + ("submission_unknown",)
                    )
                    if already_consumed:
                        db.commit()
                        continue
                    failure_result = {
                        **result,
                        "message": "draft job revision CAS 未能同时推进条目与 attempt；已失败关闭并保留审计证据",
                    }
                    _fail_poll_reconciliation(
                        db,
                        dict(authority_item or current_item),
                        failure_result,
                        now=now,
                    )
                    db.commit()
                    changed += 1
                    continue
                current_item["draft_job_revision"] = job_revision
                result = {**result, "base_job_revision": job_revision, "job_revision": job_revision}
            state = str(job.get("state") or "")
            next_state = "completed" if state == "完成" else "failed" if state in {"失败", "提交失败"} else "running"
            updated = db.execute(
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
            if updated.rowcount != 1:
                db.rollback()
                continue
            _refresh_attempt_evidence(
                db,
                current_item,
                state=next_state,
                result=result,
                error=(result.get("message") or "H3 生成失败") if next_state == "failed" else None,
            )
            if next_state != "running":
                changed += 1
                db.execute("DELETE FROM production_shot_leases WHERE item_id = ?", (item["id"],))
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
            db.execute(
                "DELETE FROM production_shot_leases WHERE batch_id = ? AND state = 'queued'", (batch_id,),
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
        db.execute("BEGIN IMMEDIATE")
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
        if _has_unresolved_shot_conflict(db, item["shot_id"]):
            raise HTTPException(409, "镜头存在尚未解决的历史生产所有权冲突")
        unknown_retry = item["error"] == "submission_outcome_unknown"
        audited_job_revision = item["draft_job_revision"]
        if unknown_retry:
            job = _draft_job(db, item["draft_job_id"])
            if (
                not job
                or job.get("shot_id") != item["shot_id"]
                or not bool(job.get("retry_safe"))
                or _decode_json(job.get("candidate_ids"), [])
            ):
                raise HTTPException(409, "提交结果未知只能先完成人工对账，禁止直接重试或再次提交")
            audited_job_revision = int(job.get("reconciliation_revision") or 0)
        current = plan_provider(item["shot_id"])
        if current.get("status") != "approved" or current.get("plan_hash") != item["plan_hash"]:
            raise HTTPException(409, "当前批准计划与批次快照不同，请新建生产批次")
        now = utc_now()
        if unknown_retry:
            lease = db.execute(
                "SELECT * FROM production_shot_leases WHERE shot_id = ?", (item["shot_id"],),
            ).fetchone()
            if not lease or lease["item_id"] != item_id or lease["state"] != "unknown":
                raise HTTPException(409, "结果未知条目的镜头所有权凭证缺失或已被其他条目占用")
            lease_cursor = db.execute(
                """UPDATE production_shot_leases SET state = 'queued', updated_at = ?
                WHERE shot_id = ? AND item_id = ? AND state = 'unknown'""",
                (now, item["shot_id"], item_id),
            )
            if lease_cursor.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "结果未知条目的重试所有权已被另一请求更新")
        else:
            try:
                db.execute(
                    """INSERT INTO production_shot_leases
                    (shot_id, batch_id, item_id, state, created_at, updated_at)
                    VALUES (?, ?, ?, 'queued', ?, ?)""",
                    (item["shot_id"], item["batch_id"], item_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "镜头已被另一个活动生产批次占用") from exc
        updated = db.execute(
            """UPDATE production_batch_items SET state = 'queued', message = '人工重试，等待提交',
            error = NULL, completed_at = NULL, updated_at = ?, draft_job_revision = ?
            WHERE id = ? AND state = 'failed'""",
            (now, audited_job_revision, item_id),
        )
        if updated.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "条目已被另一重试请求更新")
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
    preflight_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ProductionBatchPreflight(BaseModel):
    shot_ids: list[str] = Field(min_length=1, max_length=200)


class ProductionConflictResolution(BaseModel):
    confirm: bool = False
    expected_revisions: dict[str, int]
    resolved_by: str = Field(min_length=2, max_length=80)
    note: str = Field(min_length=2, max_length=500)


def create_production_router(db_path: Path, plan_provider: PlanProvider) -> APIRouter:
    router = APIRouter()

    @router.get("/api/production-batches")
    def api_list_batches() -> list[dict[str, Any]]:
        return list_batches(db_path)

    @router.get("/api/production-batches/{batch_id}")
    def api_get_batch(batch_id: str) -> dict[str, Any]:
        return get_batch(db_path, batch_id)

    @router.post("/api/production-batches/preflight")
    def api_preflight_batch(payload: ProductionBatchPreflight) -> dict[str, Any]:
        return batch_preflight(db_path, plan_provider, payload.shot_ids)

    @router.post("/api/production-batches")
    def api_create_batch(payload: ProductionBatchCreate) -> dict[str, Any]:
        if not payload.confirm:
            raise HTTPException(400, "实际提交 GPU 的生产批次必须显式确认")
        return create_batch(
            db_path, plan_provider, payload.shot_ids,
            name=payload.name, max_attempts=payload.max_attempts,
            preflight_hash=payload.preflight_hash, idempotency_key=payload.idempotency_key,
        )

    @router.post("/api/production-batches/{batch_id}/{action}")
    def api_mutate_batch(batch_id: str, action: str) -> dict[str, Any]:
        return mutate_batch(db_path, batch_id, action)

    @router.post("/api/production-items/{item_id}/retry")
    def api_retry_item(item_id: str) -> dict[str, Any]:
        return retry_item(db_path, item_id, plan_provider)

    @router.get("/api/production-conflicts")
    def api_list_conflicts(include_resolved: bool = True) -> list[dict[str, Any]]:
        return list_ownership_conflicts(db_path, include_resolved=include_resolved)

    @router.post("/api/production-conflicts/{shot_id}/resolve")
    def api_resolve_conflicts(shot_id: str, payload: ProductionConflictResolution) -> dict[str, Any]:
        if not payload.confirm:
            raise HTTPException(400, "解决生产所有权冲突必须显式确认")
        return resolve_ownership_conflicts(
            db_path,
            shot_id,
            expected_revisions=payload.expected_revisions,
            resolved_by=payload.resolved_by,
            note=payload.note,
        )

    return router
