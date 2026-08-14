from __future__ import annotations

import hashlib
import json
import os
import socket
import shutil
import sqlite3
import sys
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


ARCHIVE_SCHEMA_VERSION = 2
MEDIA_SUFFIXES = {".aac", ".flac", ".jpeg", ".jpg", ".json", ".m4a", ".mov", ".mp3", ".mp4", ".png", ".srt", ".vtt", ".wav", ".webm", ".webp"}
ARCHIVE_OWNER_INSTANCE = f"archive-instance-{uuid.uuid4().hex}"
ARCHIVE_OWNER_HOST = socket.gethostname().casefold()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_archive_schema(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(projects)").fetchall()}
    if "archived" not in columns:
        db.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "archived_at" not in columns:
        db.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_archives (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('ready', 'invalid')),
          package_path TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          checksum_sha256 TEXT NOT NULL,
          manifest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          verified_at TEXT,
          UNIQUE(project_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_project_archives_project
          ON project_archives(project_id, revision DESC);
        CREATE TABLE IF NOT EXISTS project_archive_tasks (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          archive_id TEXT NOT NULL UNIQUE,
          operation TEXT NOT NULL CHECK(operation IN ('snapshot','archive')),
          state TEXT NOT NULL CHECK(state IN ('running','completed','failed')),
          stage TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 0,
          archive_revision INTEGER NOT NULL,
          owner_instance TEXT NOT NULL,
          owner_host TEXT NOT NULL,
          owner_pid INTEGER NOT NULL,
          owner_process_identity TEXT NOT NULL,
          heartbeat_at TEXT NOT NULL,
          staging_path TEXT NOT NULL,
          partial_path TEXT NOT NULL,
          final_path TEXT NOT NULL,
          error TEXT,
          audit TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_project_archive_tasks_project
          ON project_archive_tasks(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS project_archive_leases (
          project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
          lease_id TEXT NOT NULL UNIQUE,
          operation TEXT NOT NULL CHECK(operation IN ('snapshot','archive')),
          created_at TEXT NOT NULL,
          task_id TEXT REFERENCES project_archive_tasks(id),
          owner_instance TEXT,
          owner_host TEXT,
          owner_pid INTEGER,
          owner_process_identity TEXT,
          heartbeat_at TEXT
        );
        CREATE TABLE IF NOT EXISTS project_archive_reconciliations (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          lease_id TEXT NOT NULL UNIQUE,
          task_id TEXT,
          reason TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('unresolved','resolved')) DEFAULT 'unresolved',
          revision INTEGER NOT NULL DEFAULT 0,
          evidence TEXT NOT NULL DEFAULT '{}',
          confirmed_no_live_process INTEGER NOT NULL DEFAULT 0,
          resolved_by TEXT,
          resolution_note TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_project_archive_reconciliations_open
          ON project_archive_reconciliations(project_id, state, updated_at DESC);
        """
    )
    lease_columns = {row[1] for row in db.execute("PRAGMA table_info(project_archive_leases)").fetchall()}
    for name, definition in (
        ("task_id", "TEXT REFERENCES project_archive_tasks(id)"),
        ("owner_instance", "TEXT"),
        ("owner_host", "TEXT"),
        ("owner_pid", "INTEGER"),
        ("owner_process_identity", "TEXT"),
        ("heartbeat_at", "TEXT"),
    ):
        if name not in lease_columns:
            db.execute(f"ALTER TABLE project_archive_leases ADD COLUMN {name} {definition}")
    _audit_archive_lease_reconciliations(db)


def _process_identity(pid: int) -> tuple[bool | None, str | None]:
    """Return whether a local process is alive and a stable start identity when available.

    ``None`` means that the OS would not let us prove either outcome. Recovery
    must retain the lease in that case; false negatives are safer than clearing
    work owned by another live server instance.
    """
    if pid <= 0:
        return False, None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in {87, 1168}:  # ERROR_INVALID_PARAMETER / ERROR_NOT_FOUND
                return False, None
            return None, None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
            ):
                return None, None
            ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return True, f"win-filetime:{ticks}"
        finally:
            kernel32.CloseHandle(handle)
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 is the process start time in clock ticks since boot. The
        # command name can contain spaces, so split only after the final ')'.
        payload = proc_stat.read_text(encoding="utf-8")
        tail = payload[payload.rfind(")") + 2 :].split()
        return True, f"proc-start:{tail[19]}"
    except FileNotFoundError:
        return False, None
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, None
        except (PermissionError, OSError):
            return None, None
        return True, None


def _current_owner() -> dict[str, Any]:
    alive, identity = _process_identity(os.getpid())
    if not alive or not identity:
        # This value remains unique for the process lifetime. Recovery will
        # never clear it unless the PID is proven dead.
        identity = f"runtime:{os.getpid()}:{ARCHIVE_OWNER_INSTANCE}"
    return {
        "owner_instance": ARCHIVE_OWNER_INSTANCE,
        "owner_host": ARCHIVE_OWNER_HOST,
        "owner_pid": os.getpid(),
        "owner_process_identity": identity,
    }


class ArchiveReconciliationResolve(BaseModel):
    expected_revision: int = Field(ge=0)
    confirmed_by: str = Field(min_length=2, max_length=80)
    note: str = Field(min_length=8, max_length=1000)
    confirm_no_live_archive_process: bool


def _lease_anomaly(
    lease: dict[str, Any], task: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]] | None:
    lease_owner = {
        key: lease.get(key)
        for key in ("owner_instance", "owner_host", "owner_pid", "owner_process_identity")
    }
    evidence: dict[str, Any] = {
        "lease": {
            "project_id": lease.get("project_id"),
            "lease_id": lease.get("lease_id"),
            "task_id": lease.get("task_id"),
            "operation": lease.get("operation"),
            "created_at": lease.get("created_at"),
            "heartbeat_at": lease.get("heartbeat_at"),
            "owner": lease_owner,
        }
    }
    if not lease.get("task_id") or task is None:
        evidence["task"] = None
        return "taskless_lease", evidence
    task_owner = {
        key: task.get(key)
        for key in ("owner_instance", "owner_host", "owner_pid", "owner_process_identity")
    }
    evidence["task"] = {
        "id": task.get("id"),
        "project_id": task.get("project_id"),
        "state": task.get("state"),
        "stage": task.get("stage"),
        "revision": task.get("revision"),
        "operation": task.get("operation"),
        "owner": task_owner,
    }
    if task.get("project_id") != lease.get("project_id"):
        return "task_project_mismatch", evidence
    if task.get("state") != "running":
        return "terminal_task_lease", evidence
    owner_mismatches = [key for key in task_owner if task_owner[key] != lease_owner[key]]
    if owner_mismatches or task.get("operation") != lease.get("operation"):
        evidence["mismatches"] = owner_mismatches + (
            ["operation"] if task.get("operation") != lease.get("operation") else []
        )
        return "task_lease_owner_mismatch", evidence
    return None


def _audit_archive_lease_reconciliations(db: sqlite3.Connection) -> int:
    """Persist abnormal leases without ever releasing their project freeze."""
    if not _table_exists(db, "project_archive_leases") or not _table_exists(db, "project_archive_reconciliations"):
        return 0
    now = utc_now()
    changed = 0
    leases = [dict(row) for row in db.execute("SELECT * FROM project_archive_leases ORDER BY project_id").fetchall()]
    for lease in leases:
        task_row = None
        if lease.get("task_id") and _table_exists(db, "project_archive_tasks"):
            task_row = db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (lease["task_id"],)).fetchone()
        anomaly = _lease_anomaly(lease, dict(task_row) if task_row else None)
        if anomaly is None:
            continue
        reason, evidence = anomaly
        serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        existing = db.execute(
            "SELECT * FROM project_archive_reconciliations WHERE lease_id = ?", (lease["lease_id"],),
        ).fetchone()
        if existing is None:
            db.execute(
                """INSERT INTO project_archive_reconciliations
                (id, project_id, lease_id, task_id, reason, state, revision, evidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'unresolved', 0, ?, ?, ?)""",
                (
                    f"archive-reconciliation-{uuid.uuid4().hex[:12]}", lease["project_id"], lease["lease_id"],
                    lease.get("task_id"), reason, serialized, now, now,
                ),
            )
            changed += 1
            continue
        if (
            existing["state"] != "unresolved"
            or existing["project_id"] != lease["project_id"]
            or existing["task_id"] != lease.get("task_id")
            or existing["reason"] != reason
            or existing["evidence"] != serialized
        ):
            db.execute(
                """UPDATE project_archive_reconciliations
                SET project_id = ?, task_id = ?, reason = ?, state = 'unresolved', evidence = ?,
                    confirmed_no_live_process = 0, resolved_by = NULL, resolution_note = NULL,
                    resolved_at = NULL, updated_at = ?, revision = revision + 1
                WHERE id = ? AND revision = ?""",
                (
                    lease["project_id"], lease.get("task_id"), reason, serialized, now,
                    existing["id"], int(existing["revision"]),
                ),
            )
            changed += 1
    return changed


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def project_has_active_work(db: sqlite3.Connection, project_id: str) -> list[str]:
    """Return durable blockers that make a project snapshot unsafe."""
    blockers: list[str] = []
    checks = (
        ("hd_generation_jobs", "SELECT 1 FROM hd_generation_jobs WHERE project_id = ? AND state IN ('queued','running','submission_outcome_unknown') LIMIT 1", "高清任务"),
        ("hd_validation_leases", "SELECT 1 FROM hd_validation_leases WHERE project_id = ? LIMIT 1", "高清 dry-run"),
        ("production_batches", "SELECT 1 FROM production_batches WHERE project_id = ? AND state IN ('running','paused','cancelling') LIMIT 1", "生产批次"),
        ("export_runs", "SELECT 1 FROM export_runs WHERE project_id = ? AND state IN ('排队中','恢复排队','导出中','取消中') LIMIT 1", "导出任务"),
        ("jobs", """SELECT 1 FROM jobs JOIN shots ON shots.id = jobs.shot_id
            WHERE shots.project_id = ? AND jobs.state IN ('提交中','已提交待对账','提交状态未知','已提交','排队中','运行中','待人工对账') LIMIT 1""", "H3 任务"),
        ("production_shot_leases", """SELECT 1 FROM production_shot_leases leases
            JOIN production_batches batches ON batches.id = leases.batch_id WHERE batches.project_id = ? LIMIT 1""", "生产镜头占用"),
        ("hd_shot_leases", "SELECT 1 FROM hd_shot_leases WHERE project_id = ? LIMIT 1", "高清镜头占用"),
        ("h3_generation_leases", """SELECT 1 FROM h3_generation_leases leases
            JOIN shots ON shots.id = leases.shot_id WHERE shots.project_id = ? LIMIT 1""", "H3 提交占用"),
        ("h3_validation_leases", """SELECT 1 FROM h3_validation_leases leases
            JOIN shots ON shots.id = leases.shot_id WHERE shots.project_id = ? LIMIT 1""", "H3 校验占用"),
    )
    for table, sql, label in checks:
        if _table_exists(db, table) and db.execute(sql, (project_id,)).fetchone():
            blockers.append(label)
    return blockers


def _rows(db: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _ids(records: list[dict[str, Any]]) -> list[Any]:
    return [record["id"] for record in records]


def _by_ids(db: sqlite3.Connection, table: str, column: str, values: list[Any]) -> list[dict[str, Any]]:
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    return _rows(db, f"SELECT * FROM {table} WHERE {column} IN ({placeholders})", tuple(values))


def project_snapshot(db: sqlite3.Connection, project_id: str) -> dict[str, list[dict[str, Any]]]:
    projects = _rows(db, "SELECT * FROM projects WHERE id = ?", (project_id,))
    if not projects:
        raise HTTPException(404, "项目不存在")
    shots = _rows(db, "SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal", (project_id,))
    shot_ids = _ids(shots)
    assets = _rows(db, "SELECT * FROM assets WHERE project_id = ? ORDER BY id", (project_id,))
    candidates = _by_ids(db, "candidates", "shot_id", shot_ids)
    promotions = _by_ids(db, "promotions", "shot_id", shot_ids)
    jobs = _by_ids(db, "jobs", "shot_id", shot_ids)
    shot_references = _by_ids(db, "shot_references", "shot_id", shot_ids)
    candidate_reviews = _rows(db, "SELECT * FROM candidate_reviews WHERE project_id = ? ORDER BY candidate_id, revision", (project_id,))
    prompt_plans = _rows(db, "SELECT * FROM h3_prompt_plans WHERE project_id = ? ORDER BY shot_id, created_at", (project_id,))

    bibles = _rows(db, "SELECT * FROM production_bible_entries WHERE project_id = ? ORDER BY id", (project_id,))
    bible_ids = _ids(bibles)
    batches = _rows(db, "SELECT * FROM production_batches WHERE project_id = ? ORDER BY created_at", (project_id,))
    batch_ids = _ids(batches)
    batch_items = _by_ids(db, "production_batch_items", "batch_id", batch_ids)
    batch_item_ids = _ids(batch_items)
    documents = _rows(db, "SELECT * FROM script_documents WHERE project_id = ?", (project_id,))
    document_ids = _ids(documents)
    delivery_plans = _rows(db, "SELECT * FROM delivery_plans WHERE project_id = ?", (project_id,))
    delivery_ids = _ids(delivery_plans)
    export_runs = _rows(db, "SELECT * FROM export_runs WHERE project_id = ? ORDER BY created_at", (project_id,))
    export_ids = _ids(export_runs)
    delivery_signoffs = _rows(db, "SELECT * FROM delivery_signoffs WHERE project_id = ? ORDER BY category, revision", (project_id,))
    acceptance_runs = _rows(db, "SELECT * FROM production_acceptance_runs WHERE project_id = ? ORDER BY created_at", (project_id,))
    hd_plans = _rows(db, "SELECT * FROM hd_strategy_versions WHERE project_id = ? ORDER BY shot_id, revision", (project_id,))
    hd_validations = _rows(db, "SELECT * FROM hd_validations WHERE project_id = ? ORDER BY created_at", (project_id,))
    hd_jobs = _rows(db, "SELECT * FROM hd_generation_jobs WHERE project_id = ? ORDER BY created_at", (project_id,))
    hd_artifacts = _rows(db, "SELECT * FROM hd_artifacts WHERE project_id = ? ORDER BY shot_id, version", (project_id,))
    hd_reviews = _rows(db, "SELECT * FROM hd_artifact_reviews WHERE project_id = ? ORDER BY artifact_id, revision", (project_id,))
    hd_masters = _rows(db, "SELECT * FROM hd_master_versions WHERE project_id = ? ORDER BY shot_id, revision", (project_id,))
    creative_briefs = _rows(db, "SELECT * FROM creative_briefs WHERE project_id = ?", (project_id,))
    creative_proposals = _rows(db, "SELECT * FROM creative_proposals WHERE project_id = ? ORDER BY ordinal", (project_id,))
    creative_characters = _rows(db, "SELECT * FROM creative_characters WHERE project_id = ? ORDER BY ordinal", (project_id,))
    creative_chapters = _rows(db, "SELECT * FROM creative_chapters WHERE project_id = ? ORDER BY ordinal", (project_id,))
    creative_sections = _rows(db, "SELECT * FROM creative_sections WHERE project_id = ? ORDER BY chapter_id, ordinal", (project_id,))
    creative_revisions = _rows(
        db,
        "SELECT * FROM creative_revisions WHERE project_id = ? ORDER BY entity_type, entity_id, revision",
        (project_id,),
    )
    creative_agent_runs = _rows(
        db,
        "SELECT * FROM creative_agent_runs WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    )

    return {
        "projects": projects,
        "shots": shots,
        "assets": assets,
        "shot_references": shot_references,
        "candidates": candidates,
        "candidate_reviews": candidate_reviews,
        "candidate_master_versions": _rows(
            db, "SELECT * FROM candidate_master_versions WHERE project_id = ? ORDER BY shot_id, revision", (project_id,)
        ),
        "promotions": promotions,
        "jobs": jobs,
        "h3_prompt_plans": prompt_plans,
        "production_bible_entries": bibles,
        "production_bible_assets": _by_ids(db, "production_bible_assets", "entry_id", bible_ids),
        "production_bible_shots": _by_ids(db, "production_bible_shots", "entry_id", bible_ids),
        "production_bible_versions": _by_ids(db, "production_bible_versions", "entry_id", bible_ids),
        "production_batches": batches,
        "production_batch_items": batch_items,
        "production_item_attempts": _by_ids(db, "production_item_attempts", "item_id", batch_item_ids),
        "production_shot_leases": _by_ids(db, "production_shot_leases", "batch_id", batch_ids),
        "production_shot_conflicts": _by_ids(db, "production_shot_conflicts", "item_id", batch_item_ids),
        "production_batch_events": _by_ids(db, "production_batch_events", "batch_id", batch_ids),
        "script_documents": documents,
        "script_sections": _by_ids(db, "script_sections", "document_id", document_ids),
        "script_versions": _by_ids(db, "script_versions", "document_id", document_ids),
        "script_agent_runs": _by_ids(db, "script_agent_runs", "document_id", document_ids),
        "script_storyboard_links": _by_ids(db, "script_storyboard_links", "document_id", document_ids),
        "script_storyboard_syncs": _rows(db, "SELECT * FROM script_storyboard_syncs WHERE project_id = ? ORDER BY created_at", (project_id,)),
        "delivery_plans": delivery_plans,
        "delivery_plan_items": _by_ids(db, "delivery_plan_items", "plan_id", delivery_ids),
        "delivery_plan_versions": _by_ids(db, "delivery_plan_versions", "plan_id", delivery_ids),
        "export_runs": export_runs,
        "export_events": _by_ids(db, "export_events", "run_id", export_ids),
        "delivery_signoffs": delivery_signoffs,
        "production_acceptance_runs": acceptance_runs,
        "hd_strategy_versions": hd_plans,
        "hd_validations": hd_validations,
        "hd_generation_jobs": hd_jobs,
        "hd_shot_leases": _rows(db, "SELECT * FROM hd_shot_leases WHERE project_id = ? ORDER BY shot_id", (project_id,)),
        "hd_artifacts": hd_artifacts,
        "hd_artifact_reviews": hd_reviews,
        "hd_master_versions": hd_masters,
        "creative_briefs": creative_briefs,
        "creative_proposals": creative_proposals,
        "creative_characters": creative_characters,
        "creative_chapters": creative_chapters,
        "creative_sections": creative_sections,
        "creative_revisions": creative_revisions,
        "creative_agent_runs": creative_agent_runs,
        "creative_storyboard_links": _rows(
            db, "SELECT * FROM creative_storyboard_links WHERE project_id = ? ORDER BY section_id", (project_id,)
        ),
        "creative_storyboard_syncs": _rows(
            db, "SELECT * FROM creative_storyboard_syncs WHERE project_id = ? ORDER BY applied_at", (project_id,)
        ),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_paths(snapshot: dict[str, list[dict[str, Any]]], export_root: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    for asset in snapshot["assets"]:
        if asset.get("source") == "managed" and asset.get("managed_path"):
            candidates.append(("assets", Path(asset["managed_path"])))
    for candidate in snapshot["candidates"]:
        if candidate.get("selected"):
            for field in ("output_file", "thumbnail_file"):
                if candidate.get(field):
                    candidates.append(("selected-candidates", Path(candidate[field])))
    for promotion in snapshot["promotions"]:
        if promotion.get("selected") and promotion.get("output_file"):
            candidates.append(("selected-promotions", Path(promotion["output_file"])))
    for artifact in snapshot.get("hd_artifacts", []):
        if artifact.get("output_path"):
            candidates.append(("hd-artifacts", Path(artifact["output_path"])))
    for run in snapshot["export_runs"]:
        if not run.get("is_current"):
            continue
        try:
            outputs = json.loads(run.get("outputs") or "{}")
        except (TypeError, json.JSONDecodeError):
            outputs = {}
        for value in outputs.values():
            if isinstance(value, str):
                path = Path(value)
                candidates.append(("current-export", path if path.is_absolute() else export_root / path))
    return candidates


def _collect_media(snapshot: dict[str, list[dict[str, Any]]], export_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    included: list[dict[str, Any]] = []
    omitted: list[dict[str, str]] = []
    seen: set[Path] = set()
    hd_checksums = {
        str(Path(item["output_path"]).expanduser().resolve()): str(item.get("output_sha256") or "").lower()
        for item in snapshot.get("hd_artifacts", []) if item.get("output_path")
    }
    for category, raw_path in _candidate_paths(snapshot, export_root):
        try:
            path = raw_path.expanduser().resolve(strict=True)
        except OSError:
            omitted.append({"category": category, "path": str(raw_path), "reason": "missing"})
            continue
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            omitted.append({"category": category, "path": str(path), "reason": "unsupported"})
            continue
        checksum = sha256_file(path)
        expected_hd = hd_checksums.get(str(path))
        if category == "hd-artifacts" and (len(expected_hd or "") != 64 or checksum.lower() != expected_hd):
            omitted.append({"category": category, "path": str(path), "reason": "sha256-mismatch"})
            continue
        included.append({
            "category": category,
            "source_path": str(path),
            "archive_path": f"media/{category}/{checksum[:12]}-{path.name}",
            "size_bytes": path.stat().st_size,
            "checksum_sha256": checksum,
            "path": path,
        })
    return included, omitted


def archive_public(record: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(record.get("manifest") or "{}")
    return {
        "id": record["id"], "project_id": record["project_id"], "revision": record["revision"],
        "state": record["state"], "size_bytes": record["size_bytes"],
        "checksum_sha256": record["checksum_sha256"], "created_at": record["created_at"],
        "verified_at": record.get("verified_at"), "row_counts": manifest.get("row_counts", {}),
        "media_count": len(manifest.get("media", [])), "omitted_count": len(manifest.get("omitted_media", [])),
        "download_url": f"/api/project-archives/{record['id']}/download",
    }


def archive_task_public(record: dict[str, Any]) -> dict[str, Any]:
    try:
        audit = json.loads(record.get("audit") or "{}")
    except (TypeError, json.JSONDecodeError):
        audit = {"parse_error": "stored audit is not valid JSON"}
    return {
        "id": record["id"],
        "project_id": record["project_id"],
        "archive_id": record["archive_id"],
        "operation": record["operation"],
        "state": record["state"],
        "stage": record["stage"],
        "revision": record["revision"],
        "archive_revision": record["archive_revision"],
        "owner_instance": record["owner_instance"],
        "owner_pid": record["owner_pid"],
        "heartbeat_at": record["heartbeat_at"],
        "error": record.get("error"),
        "audit": audit,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "completed_at": record.get("completed_at"),
    }


def archive_reconciliation_public(record: dict[str, Any]) -> dict[str, Any]:
    try:
        evidence = json.loads(record.get("evidence") or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {"parse_error": "stored evidence is not valid JSON"}
    return {
        "id": record["id"],
        "project_id": record["project_id"],
        "project_title": record.get("project_title"),
        "lease_id": record["lease_id"],
        "task_id": record.get("task_id"),
        "reason": record["reason"],
        "state": record["state"],
        "revision": int(record.get("revision") or 0),
        "evidence": evidence,
        "confirmed_no_live_process": bool(record.get("confirmed_no_live_process")),
        "resolved_by": record.get("resolved_by"),
        "resolution_note": record.get("resolution_note"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "resolved_at": record.get("resolved_at"),
    }


def list_archive_reconciliations(
    db_path: Path, project_id: str | None = None, *, include_resolved: bool = True,
) -> list[dict[str, Any]]:
    with closing(connect(db_path)) as db:
        if project_id is not None and not db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise HTTPException(404, "项目不存在")
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("reconciliations.project_id = ?")
            params.append(project_id)
        if not include_resolved:
            clauses.append("reconciliations.state = 'unresolved'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        records = _rows(
            db,
            f"""SELECT reconciliations.*, projects.title AS project_title
            FROM project_archive_reconciliations reconciliations
            JOIN projects ON projects.id = reconciliations.project_id
            {where} ORDER BY reconciliations.updated_at DESC, reconciliations.id""",
            tuple(params),
        )
    return [archive_reconciliation_public(record) for record in records]


def _owner_is_definitely_current(task: dict[str, Any]) -> bool:
    if task.get("owner_instance") == ARCHIVE_OWNER_INSTANCE:
        return True
    if str(task.get("owner_host") or "").casefold() != ARCHIVE_OWNER_HOST:
        return False
    try:
        pid = int(task.get("owner_pid") or 0)
    except (TypeError, ValueError):
        return False
    alive, identity = _process_identity(pid)
    if alive is not True:
        return False
    stored = str(task.get("owner_process_identity") or "")
    current = str(identity or "")
    stored_kind = stored.split(":", 1)[0] if ":" in stored else ""
    current_kind = current.split(":", 1)[0] if ":" in current else ""
    return stored_kind in {"win-filetime", "proc-start"} and current_kind == stored_kind and stored == current


def resolve_archive_reconciliation(
    db_path: Path,
    backup_root: Path,
    project_id: str,
    reconciliation_id: str,
    payload: ArchiveReconciliationResolve,
) -> dict[str, Any]:
    if not payload.confirm_no_live_archive_process:
        raise HTTPException(400, "必须明确确认没有存活的归档进程")
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        _audit_archive_lease_reconciliations(db)
        record = db.execute(
            """SELECT * FROM project_archive_reconciliations
            WHERE id = ? AND project_id = ?""",
            (reconciliation_id, project_id),
        ).fetchone()
        if not record:
            db.rollback()
            raise HTTPException(404, "归档冻结对账记录不存在")
        if record["state"] != "unresolved" or int(record["revision"]) != payload.expected_revision:
            db.rollback()
            raise HTTPException(409, "归档冻结对账记录已变化，请刷新后重试")
        lease_row = db.execute(
            "SELECT * FROM project_archive_leases WHERE project_id = ? AND lease_id = ?",
            (project_id, record["lease_id"]),
        ).fetchone()
        if not lease_row:
            db.rollback()
            raise HTTPException(409, "异常归档 lease 已变化，保持对账记录且未写入")
        task_row = None
        if lease_row["task_id"]:
            task_row = db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (lease_row["task_id"],)).fetchone()
        anomaly = _lease_anomaly(dict(lease_row), dict(task_row) if task_row else None)
        if anomaly is None:
            db.rollback()
            raise HTTPException(409, "当前 lease 与任务已恢复一致，不能按旧证据解除")
        if task_row and task_row["state"] == "running" and _owner_is_definitely_current(dict(task_row)):
            db.rollback()
            raise HTTPException(409, "检测到归档任务 owner 仍存活，不能解除冻结")
        task = dict(task_row) if task_row else None
        db.rollback()

    cleanup_errors = _cleanup_archive_task_paths(db_path, backup_root, task) if task and task["state"] == "running" else []
    if cleanup_errors:
        raise HTTPException(409, "无法安全清理异常归档任务文件，冻结保持：" + "；".join(cleanup_errors))

    now = utc_now()
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        _audit_archive_lease_reconciliations(db)
        record = db.execute(
            "SELECT * FROM project_archive_reconciliations WHERE id = ? AND project_id = ?",
            (reconciliation_id, project_id),
        ).fetchone()
        if not record or record["state"] != "unresolved" or int(record["revision"]) != payload.expected_revision:
            db.rollback()
            raise HTTPException(409, "归档冻结对账记录已被其他请求处理")
        lease_row = db.execute(
            "SELECT * FROM project_archive_leases WHERE project_id = ? AND lease_id = ?",
            (project_id, record["lease_id"]),
        ).fetchone()
        if not lease_row:
            db.rollback()
            raise HTTPException(409, "异常归档 lease 已变化，未解除其他冻结")
        task_row = None
        if lease_row["task_id"]:
            task_row = db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (lease_row["task_id"],)).fetchone()
        anomaly = _lease_anomaly(dict(lease_row), dict(task_row) if task_row else None)
        if anomaly is None:
            db.rollback()
            raise HTTPException(409, "当前 lease 与任务已恢复一致，未解除")
        reason, current_evidence = anomaly
        try:
            reconciliation_audit = json.loads(record["evidence"] or "{}")
        except (TypeError, json.JSONDecodeError):
            reconciliation_audit = {}
        reconciliation_audit["resolution"] = {
            "confirmed_no_live_archive_process": True,
            "confirmed_by": payload.confirmed_by.strip(),
            "note": payload.note.strip(),
            "reason_at_resolution": reason,
            "evidence_at_resolution": current_evidence,
            "resolved_at": now,
        }
        cursor = db.execute(
            """UPDATE project_archive_reconciliations
            SET state = 'resolved', confirmed_no_live_process = 1, resolved_by = ?, resolution_note = ?,
                evidence = ?, updated_at = ?, resolved_at = ?, revision = revision + 1
            WHERE id = ? AND project_id = ? AND state = 'unresolved' AND revision = ?""",
            (
                payload.confirmed_by.strip(), payload.note.strip(),
                json.dumps(reconciliation_audit, ensure_ascii=False, sort_keys=True), now, now,
                reconciliation_id, project_id, payload.expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "归档冻结对账记录已被其他请求处理")
        if task_row:
            task = dict(task_row)
            try:
                task_audit = json.loads(task["audit"] or "{}")
            except (TypeError, json.JSONDecodeError):
                task_audit = {}
            task_audit["manual_reconciliation"] = reconciliation_audit["resolution"]
            if task["state"] == "running":
                task_cursor = db.execute(
                    """UPDATE project_archive_tasks
                    SET state = 'failed', stage = 'reconciled_failed', error = ?, audit = ?,
                        heartbeat_at = ?, updated_at = ?, completed_at = ?, revision = revision + 1
                    WHERE id = ? AND project_id = ? AND state = 'running' AND revision = ?""",
                    (
                        "人工确认无存活归档进程并解除异常冻结",
                        json.dumps(task_audit, ensure_ascii=False, sort_keys=True), now, now, now,
                        task["id"], project_id, int(task["revision"]),
                    ),
                )
            else:
                task_cursor = db.execute(
                    """UPDATE project_archive_tasks
                    SET audit = ?, updated_at = ?, revision = revision + 1
                    WHERE id = ? AND project_id = ? AND state = ? AND revision = ?""",
                    (
                        json.dumps(task_audit, ensure_ascii=False, sort_keys=True), now,
                        task["id"], project_id, task["state"], int(task["revision"]),
                    ),
                )
            if task_cursor.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "归档任务在对账期间变化，零写入")
        lease_cursor = db.execute(
            "DELETE FROM project_archive_leases WHERE project_id = ? AND lease_id = ?",
            (project_id, record["lease_id"]),
        )
        if lease_cursor.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "异常归档 lease 在对账期间变化，零写入")
        db.commit()
    return next(
        item for item in list_archive_reconciliations(db_path, project_id) if item["id"] == reconciliation_id
    )


def _cleanup_tree(path: Path) -> None:
    if not path.exists():
        return

    def writable_then_retry(function: Any, raw_path: str, _error: Any) -> None:
        os.chmod(raw_path, 0o700)
        function(raw_path)

    shutil.rmtree(path, onerror=writable_then_retry)


def _stage_archive_media(staging_root: Path, media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    staged: list[dict[str, Any]] = []
    media_root = staging_root / "media"
    staging_root.mkdir(parents=True, exist_ok=False)
    media_root.mkdir(exist_ok=False)
    for index, item in enumerate(media, 1):
        source = Path(item["path"]).resolve()
        expected_sha = str(item["checksum_sha256"]).lower()
        before_size = source.stat().st_size
        before_sha = sha256_file(source).lower()
        if before_sha != expected_sha or before_size != int(item["size_bytes"]):
            raise HTTPException(409, f"归档媒体在冻结前已变化：{source.name}")
        suffix = source.suffix.lower() if source.suffix.lower() in MEDIA_SUFFIXES else ".media"
        staged_path = media_root / f"{index:04d}-{expected_sha}{suffix}"
        temporary = media_root / f".{index:04d}-{uuid.uuid4().hex}.tmp"
        try:
            with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            after_size = source.stat().st_size
            after_sha = sha256_file(source).lower()
            staged_sha = sha256_file(temporary).lower()
            if before_size != after_size or before_sha != after_sha or staged_sha != expected_sha:
                raise HTTPException(409, f"归档媒体在 staging 复制期间发生变化：{source.name}")
            temporary.replace(staged_path)
            os.chmod(staged_path, 0o444)
            staged.append({**item, "staged_path": staged_path})
        finally:
            temporary.unlink(missing_ok=True)
    return staged


def _verify_built_archive(path: Path, manifest_bytes: bytes, data_bytes: bytes, media: list[dict[str, Any]]) -> None:
    expected_names = {"archive-manifest.json", "project-data.json", *[item["archive_path"] for item in media]}
    try:
        with zipfile.ZipFile(path) as package:
            if package.testzip() is not None or set(package.namelist()) != expected_names:
                raise HTTPException(500, "归档 ZIP 目录或 CRC 校验失败")
            if package.read("archive-manifest.json") != manifest_bytes:
                raise HTTPException(500, "归档 manifest 与冻结凭证不一致")
            packaged_data = package.read("project-data.json")
            if packaged_data != data_bytes:
                raise HTTPException(500, "归档项目数据与冻结凭证不一致")
            for item in media:
                payload = package.read(item["archive_path"])
                if len(payload) != int(item["size_bytes"]) or hashlib.sha256(payload).hexdigest() != item["checksum_sha256"]:
                    raise HTTPException(500, f"归档媒体校验失败：{item['archive_path']}")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(500, f"归档 ZIP 完整验证失败：{exc}") from exc


def _archive_task_paths(backup_root: Path, project_id: str, archive_id: str, archive_revision: int, task_id: str) -> dict[str, Path]:
    root = backup_root.resolve()
    staging_path = (root / ".staging" / task_id).resolve()
    archive_dir = (root / project_id).resolve()
    try:
        staging_path.relative_to(root / ".staging")
        archive_dir.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "项目标识无法映射到归档目录") from exc
    return {
        "staging_path": staging_path,
        "partial_path": archive_dir / f".{archive_id}.partial",
        "final_path": archive_dir / f"{project_id}-R{archive_revision}-{archive_id}.jingchang.zip",
    }


def _acquire_archive_task(
    db_path: Path,
    backup_root: Path,
    project_id: str,
    *,
    mark_archived: bool,
    task_id: str,
    lease_id: str,
    archive_id: str,
    created_at: str,
) -> dict[str, Any]:
    owner = _current_owner()
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(404, "项目不存在")
        if project["archived"]:
            raise HTTPException(409, "项目已经归档")
        if db.execute("SELECT 1 FROM project_archive_leases WHERE project_id = ?", (project_id,)).fetchone():
            raise HTTPException(409, "项目已有归档冻结任务")
        blockers = project_has_active_work(db, project_id)
        if blockers:
            raise HTTPException(409, "项目仍有活动任务，不能归档：" + "、".join(blockers))
        if mark_archived:
            active = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
            if active and active["value"] == project_id:
                raise HTTPException(409, "当前项目不能归档，请先切换到另一个项目")
        previous = db.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM project_archives WHERE project_id = ?", (project_id,),
        ).fetchone()[0]
        archive_revision = int(previous) + 1
        paths = _archive_task_paths(backup_root, project_id, archive_id, archive_revision, task_id)
        snapshot = project_snapshot(db, project_id)
        db.execute(
            """INSERT INTO project_archive_tasks
            (id, project_id, archive_id, operation, state, stage, revision, archive_revision,
             owner_instance, owner_host, owner_pid, owner_process_identity, heartbeat_at,
             staging_path, partial_path, final_path, audit, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'running', 'snapshot_frozen', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
            (
                task_id, project_id, archive_id, "archive" if mark_archived else "snapshot", archive_revision,
                owner["owner_instance"], owner["owner_host"], owner["owner_pid"], owner["owner_process_identity"],
                created_at, str(paths["staging_path"]), str(paths["partial_path"]), str(paths["final_path"]),
                created_at, created_at,
            ),
        )
        db.execute(
            """INSERT INTO project_archive_leases
            (project_id, lease_id, operation, created_at, task_id, owner_instance, owner_host,
             owner_pid, owner_process_identity, heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, lease_id, "archive" if mark_archived else "snapshot", created_at, task_id,
                owner["owner_instance"], owner["owner_host"], owner["owner_pid"],
                owner["owner_process_identity"], created_at,
            ),
        )
        db.commit()
    return {"snapshot": snapshot, "archive_revision": archive_revision, "task_revision": 0, **paths}


def _advance_archive_task(db_path: Path, task_id: str, expected_revision: int, stage: str) -> int:
    now = utc_now()
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            """UPDATE project_archive_tasks
            SET stage = ?, heartbeat_at = ?, updated_at = ?, revision = revision + 1
            WHERE id = ? AND state = 'running' AND revision = ?""",
            (stage, now, now, task_id, expected_revision),
        )
        if cursor.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "归档任务所有权或阶段已变化，已停止发布")
        db.execute("UPDATE project_archive_leases SET heartbeat_at = ? WHERE task_id = ?", (now, task_id))
        db.commit()
    return expected_revision + 1


def _fail_archive_task(db_path: Path, task_id: str, error: str, cleanup_errors: list[str] | None = None) -> None:
    now = utc_now()
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row or row["state"] != "running":
            db.rollback()
            return
        try:
            audit = json.loads(row["audit"] or "{}")
        except (TypeError, json.JSONDecodeError):
            audit = {}
        audit["failure"] = {
            "stage": row["stage"], "error": error, "cleanup_errors": cleanup_errors or [], "at": now,
        }
        if cleanup_errors:
            db.execute(
                """UPDATE project_archive_tasks
                SET stage = 'failure_cleanup_failed', error = ?, audit = ?, heartbeat_at = ?,
                    updated_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'running' AND revision = ?""",
                (
                    "归档失败且私有文件无法安全清理，冻结仍保留",
                    json.dumps(audit, ensure_ascii=False), now, now, task_id, int(row["revision"]),
                ),
            )
            db.commit()
            return
        db.execute(
            """UPDATE project_archive_tasks
            SET state = 'failed', stage = 'failed', error = ?, audit = ?, heartbeat_at = ?,
                updated_at = ?, completed_at = ?, revision = revision + 1
            WHERE id = ? AND state = 'running' AND revision = ?""",
            (error, json.dumps(audit, ensure_ascii=False), now, now, now, task_id, int(row["revision"])),
        )
        db.execute("DELETE FROM project_archive_leases WHERE task_id = ?", (task_id,))
        db.commit()


def _cleanup_archive_task_paths(db_path: Path, backup_root: Path, task: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    root = backup_root.resolve()
    expected = _archive_task_paths(
        root, task["project_id"], task["archive_id"], int(task["archive_revision"]), task["id"],
    )
    for key, path in expected.items():
        if str(path) != str(Path(task[key]).resolve()):
            errors.append(f"unsafe-{key}")
            continue
        try:
            if key == "staging_path":
                _cleanup_tree(path)
            elif key == "partial_path":
                path.unlink(missing_ok=True)
            else:
                with closing(connect(db_path)) as db:
                    registered = db.execute(
                        "SELECT 1 FROM project_archives WHERE package_path = ?", (str(path),),
                    ).fetchone()
                if not registered:
                    path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{key}:{exc}")
    return errors


def _task_owner_is_dead(task: dict[str, Any]) -> bool:
    if task.get("owner_instance") == ARCHIVE_OWNER_INSTANCE:
        return False
    if str(task.get("owner_host") or "").casefold() != ARCHIVE_OWNER_HOST:
        return False
    try:
        pid = int(task.get("owner_pid") or 0)
    except (TypeError, ValueError):
        return False
    alive, current_identity = _process_identity(pid)
    if alive is False:
        return True
    stored_identity = str(task.get("owner_process_identity") or "")
    current_identity = str(current_identity or "")
    stored_kind = stored_identity.split(":", 1)[0] if ":" in stored_identity else ""
    current_kind = current_identity.split(":", 1)[0] if ":" in current_identity else ""
    verified_kinds = {"win-filetime", "proc-start"}
    if (
        alive is True
        and stored_kind in verified_kinds
        and current_kind == stored_kind
        and stored_identity
        and current_identity
    ):
        return current_identity != stored_identity
    return False


def recover_archive_tasks(db_path: Path, backup_root: Path) -> int:
    """Fail and clean only tasks whose local owner process is proven dead."""
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        _audit_archive_lease_reconciliations(db)
        db.commit()
        blocked_task_ids = {
            row[0]
            for row in db.execute(
                """SELECT task_id FROM project_archive_reconciliations
                WHERE state = 'unresolved' AND task_id IS NOT NULL"""
            ).fetchall()
        }
        tasks = [dict(row) for row in db.execute(
            "SELECT * FROM project_archive_tasks WHERE state = 'running' ORDER BY created_at",
        ).fetchall()]
    recovered = 0
    for task in tasks:
        if task["id"] in blocked_task_ids:
            continue
        if not _task_owner_is_dead(task):
            continue
        cleanup_errors = _cleanup_archive_task_paths(db_path, backup_root, task)
        if cleanup_errors:
            now = utc_now()
            with closing(connect(db_path)) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT state, revision, audit FROM project_archive_tasks WHERE id = ?", (task["id"],),
                ).fetchone()
                if current and current["state"] == "running" and int(current["revision"]) == int(task["revision"]):
                    try:
                        audit = json.loads(current["audit"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        audit = {}
                    audit["recovery_cleanup_failed"] = {"errors": cleanup_errors, "at": now}
                    db.execute(
                        """UPDATE project_archive_tasks SET stage = 'recovery_cleanup_failed', error = ?, audit = ?,
                        heartbeat_at = ?, updated_at = ?, revision = revision + 1
                        WHERE id = ? AND state = 'running' AND revision = ?""",
                        (
                            "无法安全清理崩溃归档任务，冻结仍保留", json.dumps(audit, ensure_ascii=False), now, now,
                            task["id"], int(task["revision"]),
                        ),
                    )
                    db.commit()
                else:
                    db.rollback()
            continue
        now = utc_now()
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task["id"],)).fetchone()
            if not current or current["state"] != "running" or int(current["revision"]) != int(task["revision"]):
                db.rollback()
                continue
            if db.execute("SELECT 1 FROM project_archives WHERE package_path = ?", (current["final_path"],)).fetchone():
                db.rollback()
                continue
            try:
                audit = json.loads(current["audit"] or "{}")
            except (TypeError, json.JSONDecodeError):
                audit = {}
            audit["recovery"] = {
                "reason": "owner_process_dead", "owner_instance": current["owner_instance"],
                "owner_pid": current["owner_pid"], "owner_process_identity": current["owner_process_identity"],
                "recovered_by": ARCHIVE_OWNER_INSTANCE, "at": now,
            }
            cursor = db.execute(
                """UPDATE project_archive_tasks
                SET state = 'failed', stage = 'recovered_failed', error = ?, audit = ?, heartbeat_at = ?,
                    updated_at = ?, completed_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'running' AND revision = ?""",
                (
                    "检测到归档进程异常退出；未发布归档并已释放冻结",
                    json.dumps(audit, ensure_ascii=False), now, now, now, task["id"], int(task["revision"]),
                ),
            )
            if cursor.rowcount != 1:
                db.rollback()
                continue
            db.execute("DELETE FROM project_archive_leases WHERE task_id = ?", (task["id"],))
            db.commit()
            recovered += 1
    return recovered


def create_project_archive(
    db_path: Path, backup_root: Path, project_id: str, export_root: Path | None = None, *, mark_archived: bool = False,
) -> dict[str, Any]:
    task_id = f"archive-task-{uuid.uuid4().hex[:12]}"
    lease_id = f"archive-lease-{uuid.uuid4().hex[:12]}"
    archive_id = f"project-archive-{uuid.uuid4().hex[:12]}"
    created_at = utc_now()
    archive_revision = 0
    task_revision = 0
    lease_acquired = False
    final_path: Path | None = None
    partial_path: Path | None = None
    staging_root: Path | None = None
    failure_error = "归档任务异常终止"
    try:
        acquired = _acquire_archive_task(
            db_path, backup_root, project_id, mark_archived=mark_archived, task_id=task_id,
            lease_id=lease_id, archive_id=archive_id, created_at=created_at,
        )
        snapshot = acquired["snapshot"]
        archive_revision = int(acquired["archive_revision"])
        task_revision = int(acquired["task_revision"])
        staging_root = Path(acquired["staging_path"])
        partial_path = Path(acquired["partial_path"])
        final_path = Path(acquired["final_path"])
        lease_acquired = True

        media, omitted = _collect_media(snapshot, (export_root or backup_root.parent / "exports").resolve())
        if omitted:
            details = "；".join(f"{item['reason']}:{item['path']}" for item in omitted[:5])
            raise HTTPException(409, f"归档包含 {len(omitted)} 个缺失或不可信媒体，未发布：{details}")
        task_revision = _advance_archive_task(db_path, task_id, task_revision, "sources_verified")
        staged_media = _stage_archive_media(staging_root, media)
        task_revision = _advance_archive_task(db_path, task_id, task_revision, "media_staged")
        project = snapshot["projects"][0]
        manifest = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "archive_id": archive_id,
            "project_id": project_id,
            "project_title": project["title"],
            "revision": archive_revision,
            "created_at": created_at,
            "row_counts": {table: len(records) for table, records in snapshot.items()},
            "media": [{key: value for key, value in item.items() if key not in {"path", "staged_path"}} for item in staged_media],
            "omitted_media": [],
            "model_weights_included": False,
        }
        data_bytes = json.dumps(
            {"schema_version": ARCHIVE_SCHEMA_VERSION, "tables": snapshot},
            ensure_ascii=False, sort_keys=True, indent=2,
        ).encode("utf-8")
        manifest["data_sha256"] = hashlib.sha256(data_bytes).hexdigest()
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")

        archive_dir = final_path.parent
        archive_dir.mkdir(parents=True, exist_ok=True)
        task_revision = _advance_archive_task(db_path, task_id, task_revision, "package_building")
        with zipfile.ZipFile(partial_path, "w", allowZip64=True) as package:
            package.writestr("archive-manifest.json", manifest_bytes, compress_type=zipfile.ZIP_DEFLATED)
            package.writestr("project-data.json", data_bytes, compress_type=zipfile.ZIP_DEFLATED)
            for item in staged_media:
                package.write(item["staged_path"], item["archive_path"], compress_type=zipfile.ZIP_STORED)
        _verify_built_archive(partial_path, manifest_bytes, data_bytes, staged_media)
        task_revision = _advance_archive_task(db_path, task_id, task_revision, "package_verified")
        partial_path.replace(final_path)
        _verify_built_archive(final_path, manifest_bytes, data_bytes, staged_media)
        checksum = sha256_file(final_path)
        size_bytes = final_path.stat().st_size
        task_revision = _advance_archive_task(db_path, task_id, task_revision, "final_verified")

        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute(
                "SELECT * FROM project_archive_leases WHERE project_id = ? AND lease_id = ? AND task_id = ?",
                (project_id, lease_id, task_id),
            ).fetchone()
            task = db.execute(
                "SELECT * FROM project_archive_tasks WHERE id = ? AND state = 'running' AND revision = ?",
                (task_id, task_revision),
            ).fetchone()
            project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not lease or not task or not project or project["archived"] or project_has_active_work(db, project_id):
                raise HTTPException(409, "归档冻结期间项目状态或任务发生变化，未发布")
            current_snapshot = project_snapshot(db, project_id)
            current_data = json.dumps(
                {"schema_version": ARCHIVE_SCHEMA_VERSION, "tables": current_snapshot},
                ensure_ascii=False, sort_keys=True, indent=2,
            ).encode("utf-8")
            if hashlib.sha256(current_data).hexdigest() != manifest["data_sha256"]:
                raise HTTPException(409, "归档冻结期间项目数据发生变化，未发布")
            if mark_archived:
                active = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
                if active and active["value"] == project_id:
                    raise HTTPException(409, "项目在归档期间被重新激活，未发布")
            verified_at = utc_now()
            db.execute(
                """INSERT INTO project_archives
                (id, project_id, revision, state, package_path, size_bytes, checksum_sha256, manifest, created_at, verified_at)
                VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?)""",
                (archive_id, project_id, archive_revision, str(final_path), size_bytes, checksum, json.dumps(manifest, ensure_ascii=False), created_at, verified_at),
            )
            if mark_archived:
                db.execute("UPDATE projects SET archived = 1, archived_at = ? WHERE id = ? AND archived = 0", (verified_at, project_id))
            cursor = db.execute(
                """UPDATE project_archive_tasks
                SET state = 'completed', stage = 'completed', error = NULL, heartbeat_at = ?, updated_at = ?,
                    completed_at = ?, revision = revision + 1
                WHERE id = ? AND state = 'running' AND revision = ?""",
                (verified_at, verified_at, verified_at, task_id, task_revision),
            )
            if cursor.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "归档任务状态已变化，未发布")
            db.execute(
                "DELETE FROM project_archive_leases WHERE project_id = ? AND lease_id = ? AND task_id = ?",
                (project_id, lease_id, task_id),
            )
            record = dict(db.execute("SELECT * FROM project_archives WHERE id = ?", (archive_id,)).fetchone())
            db.commit()
            lease_acquired = False
        return archive_public(record)
    except Exception as exc:
        failure_error = str(getattr(exc, "detail", exc))
        raise
    finally:
        if lease_acquired:
            with closing(connect(db_path)) as db:
                task_row = db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone()
            cleanup_errors = _cleanup_archive_task_paths(db_path, backup_root, dict(task_row)) if task_row else ["task-missing"]
            _fail_archive_task(db_path, task_id, failure_error, cleanup_errors)
        else:
            if partial_path is not None:
                partial_path.unlink(missing_ok=True)
            if staging_root is not None:
                _cleanup_tree(staging_root)


def verify_project_archive(db_path: Path, archive_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM project_archives WHERE id = ?", (archive_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归档包不存在")
        record = dict(row)
    path = Path(record["package_path"])
    errors: list[str] = []
    if not path.is_file():
        errors.append("归档文件缺失")
    elif sha256_file(path) != record["checksum_sha256"]:
        errors.append("归档文件 SHA-256 不匹配")
    if not errors:
        try:
            with zipfile.ZipFile(path) as package:
                manifest = json.loads(package.read("archive-manifest.json"))
                data_bytes = package.read("project-data.json")
                if hashlib.sha256(data_bytes).hexdigest() != manifest.get("data_sha256"):
                    errors.append("项目数据 SHA-256 不匹配")
                for media in manifest.get("media", []):
                    digest = hashlib.sha256(package.read(media["archive_path"])).hexdigest()
                    if digest != media["checksum_sha256"]:
                        errors.append(f"媒体校验失败：{media['archive_path']}")
        except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            errors.append(f"归档结构无效：{exc}")
    verified_at = utc_now()
    state = "invalid" if errors else "ready"
    with closing(connect(db_path)) as db:
        db.execute("UPDATE project_archives SET state = ?, verified_at = ? WHERE id = ?", (state, verified_at, archive_id))
        db.commit()
    return {"ok": not errors, "state": state, "errors": errors, "verified_at": verified_at, "archive": archive_public({**record, "state": state, "verified_at": verified_at})}


def create_archive_router(db_path: Path, backup_root: Path, export_root: Path | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project_id}/archives")
    def list_archives(project_id: str) -> list[dict[str, Any]]:
        with closing(connect(db_path)) as db:
            records = _rows(db, "SELECT * FROM project_archives WHERE project_id = ? ORDER BY revision DESC", (project_id,))
        return [archive_public(record) for record in records]

    @router.post("/api/projects/{project_id}/archives")
    def create_archive(project_id: str) -> dict[str, Any]:
        return create_project_archive(db_path, backup_root, project_id, export_root)

    @router.get("/api/projects/{project_id}/archive-tasks")
    def list_archive_tasks(project_id: str) -> list[dict[str, Any]]:
        with closing(connect(db_path)) as db:
            if not db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                raise HTTPException(404, "项目不存在")
            records = _rows(
                db,
                "SELECT * FROM project_archive_tasks WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            )
        return [archive_task_public(record) for record in records]

    @router.get("/api/projects/{project_id}/archive-reconciliations")
    def api_list_archive_reconciliations(
        project_id: str, include_resolved: bool = True,
    ) -> list[dict[str, Any]]:
        return list_archive_reconciliations(db_path, project_id, include_resolved=include_resolved)

    @router.post("/api/projects/{project_id}/archive-reconciliations/{reconciliation_id}/resolve")
    def api_resolve_archive_reconciliation(
        project_id: str, reconciliation_id: str, payload: ArchiveReconciliationResolve,
    ) -> dict[str, Any]:
        return resolve_archive_reconciliation(
            db_path, backup_root, project_id, reconciliation_id, payload,
        )

    @router.post("/api/project-archives/{archive_id}/verify")
    def verify_archive(archive_id: str) -> dict[str, Any]:
        return verify_project_archive(db_path, archive_id)

    @router.get("/api/project-archives/{archive_id}/download")
    def download_archive(archive_id: str) -> FileResponse:
        with closing(connect(db_path)) as db:
            row = db.execute("SELECT package_path, project_id, revision FROM project_archives WHERE id = ?", (archive_id,)).fetchone()
        if not row or not Path(row["package_path"]).is_file():
            raise HTTPException(404, "归档包不存在")
        return FileResponse(row["package_path"], filename=f"{row['project_id']}-R{row['revision']}.jingchang.zip", media_type="application/zip")

    @router.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: str) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            active = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
            project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not project:
                raise HTTPException(404, "项目不存在")
            if active and active["value"] == project_id:
                raise HTTPException(409, "当前项目不能归档，请先切换到另一个项目")
        archive = create_project_archive(db_path, backup_root, project_id, export_root, mark_archived=True)
        return {"project_id": project_id, "archived": True, "archive": archive}

    @router.post("/api/projects/{project_id}/restore")
    def restore_project(project_id: str) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            cursor = db.execute("UPDATE projects SET archived = 0, archived_at = NULL WHERE id = ?", (project_id,))
            if not cursor.rowcount:
                raise HTTPException(404, "项目不存在")
            db.commit()
        return {"project_id": project_id, "archived": False}

    return router
