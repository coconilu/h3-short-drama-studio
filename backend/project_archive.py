from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


ARCHIVE_SCHEMA_VERSION = 1
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
        """
    )


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


def create_project_archive(db_path: Path, backup_root: Path, project_id: str, export_root: Path | None = None) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN")
        snapshot = project_snapshot(db, project_id)
        previous = db.execute("SELECT COALESCE(MAX(revision), 0) FROM project_archives WHERE project_id = ?", (project_id,)).fetchone()[0]
        revision = int(previous) + 1
        project = snapshot["projects"][0]
        media, omitted = _collect_media(snapshot, (export_root or backup_root.parent / "exports").resolve())
        archive_id = f"project-archive-{uuid.uuid4().hex[:12]}"
        created_at = utc_now()
        manifest = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "archive_id": archive_id,
            "project_id": project_id,
            "project_title": project["title"],
            "revision": revision,
            "created_at": created_at,
            "row_counts": {table: len(records) for table, records in snapshot.items()},
            "media": [{key: value for key, value in item.items() if key != "path"} for item in media],
            "omitted_media": omitted,
            "model_weights_included": False,
        }
        data_bytes = json.dumps({"schema_version": ARCHIVE_SCHEMA_VERSION, "tables": snapshot}, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        manifest["data_sha256"] = hashlib.sha256(data_bytes).hexdigest()
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")

    archive_dir = (backup_root / project_id).resolve()
    backup_root = backup_root.resolve()
    try:
        archive_dir.relative_to(backup_root)
    except ValueError as exc:
        raise HTTPException(400, "项目标识无法映射到归档目录") from exc
    archive_dir.mkdir(parents=True, exist_ok=True)
    final_path = archive_dir / f"{project_id}-R{revision}.jingchang.zip"
    partial_path = archive_dir / f".{archive_id}.partial"
    try:
        with zipfile.ZipFile(partial_path, "w", allowZip64=True) as package:
            package.writestr("archive-manifest.json", manifest_bytes, compress_type=zipfile.ZIP_DEFLATED)
            package.writestr("project-data.json", data_bytes, compress_type=zipfile.ZIP_DEFLATED)
            for item in media:
                package.write(item["path"], item["archive_path"], compress_type=zipfile.ZIP_STORED)
        partial_path.replace(final_path)
    finally:
        partial_path.unlink(missing_ok=True)
    checksum = sha256_file(final_path)
    size_bytes = final_path.stat().st_size
    with closing(connect(db_path)) as db:
        db.execute(
            """INSERT INTO project_archives
            (id, project_id, revision, state, package_path, size_bytes, checksum_sha256, manifest, created_at, verified_at)
            VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?)""",
            (archive_id, project_id, revision, str(final_path), size_bytes, checksum, json.dumps(manifest, ensure_ascii=False), created_at, created_at),
        )
        db.commit()
        record = dict(db.execute("SELECT * FROM project_archives WHERE id = ?", (archive_id,)).fetchone())
    return archive_public(record)


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
        archive = create_project_archive(db_path, backup_root, project_id, export_root)
        with closing(connect(db_path)) as db:
            db.execute("UPDATE projects SET archived = 1, archived_at = ? WHERE id = ?", (utc_now(), project_id))
            db.commit()
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
