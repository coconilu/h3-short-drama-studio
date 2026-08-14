from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


ARCHIVE_SCHEMA_VERSION = 2
MEDIA_SUFFIXES = {".aac", ".flac", ".jpeg", ".jpg", ".json", ".m4a", ".mov", ".mp3", ".mp4", ".png", ".srt", ".vtt", ".wav", ".webm", ".webp"}


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
        CREATE TABLE IF NOT EXISTS project_archive_leases (
          project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
          lease_id TEXT NOT NULL UNIQUE,
          operation TEXT NOT NULL CHECK(operation IN ('snapshot','archive')),
          created_at TEXT NOT NULL
        );
        """
    )


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


def _release_archive_lease(db_path: Path, project_id: str, lease_id: str) -> None:
    with closing(connect(db_path)) as db:
        db.execute("DELETE FROM project_archive_leases WHERE project_id = ? AND lease_id = ?", (project_id, lease_id))
        db.commit()


def create_project_archive(
    db_path: Path, backup_root: Path, project_id: str, export_root: Path | None = None, *, mark_archived: bool = False,
) -> dict[str, Any]:
    lease_id = f"archive-lease-{uuid.uuid4().hex[:12]}"
    archive_id = f"project-archive-{uuid.uuid4().hex[:12]}"
    created_at = utc_now()
    snapshot: dict[str, list[dict[str, Any]]]
    revision = 0
    lease_acquired = False
    final_path: Path | None = None
    partial_path: Path | None = None
    staging_root = (backup_root.resolve() / ".staging" / lease_id).resolve()
    try:
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
            db.execute(
                "INSERT INTO project_archive_leases (project_id, lease_id, operation, created_at) VALUES (?, ?, ?, ?)",
                (project_id, lease_id, "archive" if mark_archived else "snapshot", created_at),
            )
            lease_acquired = True
            snapshot = project_snapshot(db, project_id)
            previous = db.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM project_archives WHERE project_id = ?", (project_id,),
            ).fetchone()[0]
            revision = int(previous) + 1
            db.commit()

        media, omitted = _collect_media(snapshot, (export_root or backup_root.parent / "exports").resolve())
        if omitted:
            details = "；".join(f"{item['reason']}:{item['path']}" for item in omitted[:5])
            raise HTTPException(409, f"归档包含 {len(omitted)} 个缺失或不可信媒体，未发布：{details}")
        staged_media = _stage_archive_media(staging_root, media)
        project = snapshot["projects"][0]
        manifest = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "archive_id": archive_id,
            "project_id": project_id,
            "project_title": project["title"],
            "revision": revision,
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

        backup_root = backup_root.resolve()
        archive_dir = (backup_root / project_id).resolve()
        try:
            archive_dir.relative_to(backup_root)
        except ValueError as exc:
            raise HTTPException(400, "项目标识无法映射到归档目录") from exc
        archive_dir.mkdir(parents=True, exist_ok=True)
        final_path = archive_dir / f"{project_id}-R{revision}-{archive_id}.jingchang.zip"
        partial_path = archive_dir / f".{archive_id}.partial"
        with zipfile.ZipFile(partial_path, "w", allowZip64=True) as package:
            package.writestr("archive-manifest.json", manifest_bytes, compress_type=zipfile.ZIP_DEFLATED)
            package.writestr("project-data.json", data_bytes, compress_type=zipfile.ZIP_DEFLATED)
            for item in staged_media:
                package.write(item["staged_path"], item["archive_path"], compress_type=zipfile.ZIP_STORED)
        _verify_built_archive(partial_path, manifest_bytes, data_bytes, staged_media)
        partial_path.replace(final_path)
        _verify_built_archive(final_path, manifest_bytes, data_bytes, staged_media)
        checksum = sha256_file(final_path)
        size_bytes = final_path.stat().st_size

        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute(
                "SELECT * FROM project_archive_leases WHERE project_id = ? AND lease_id = ?", (project_id, lease_id),
            ).fetchone()
            project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not lease or not project or project["archived"] or project_has_active_work(db, project_id):
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
                (archive_id, project_id, revision, str(final_path), size_bytes, checksum, json.dumps(manifest, ensure_ascii=False), created_at, verified_at),
            )
            if mark_archived:
                db.execute("UPDATE projects SET archived = 1, archived_at = ? WHERE id = ? AND archived = 0", (verified_at, project_id))
            db.execute("DELETE FROM project_archive_leases WHERE project_id = ? AND lease_id = ?", (project_id, lease_id))
            record = dict(db.execute("SELECT * FROM project_archives WHERE id = ?", (archive_id,)).fetchone())
            db.commit()
            lease_acquired = False
        return archive_public(record)
    except Exception:
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        raise
    finally:
        if partial_path is not None:
            partial_path.unlink(missing_ok=True)
        _cleanup_tree(staging_root)
        if lease_acquired:
            _release_archive_lease(db_path, project_id, lease_id)


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
