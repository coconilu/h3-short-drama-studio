from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


HDStrategy = Literal["ref2va_regenerate", "original_model_regenerate", "deterministic_scale"]
HDRunner = Callable[[dict[str, Any], bool], dict[str, Any]]
MediaProbe = Callable[[Path], dict[str, Any]]

STRATEGY_DETAILS: dict[str, dict[str, Any]] = {
    "ref2va_regenerate": {
        "label": "Ref2VA 高清精修",
        "operation": "把低清母版作为视频参考，在目标分辨率重新扩散；会生成新细节，也可能漂移。",
        "kind": "model_regeneration",
        "gpu_required": True,
        "drift_review_required": True,
    },
    "original_model_regenerate": {
        "label": "原策略高清重生成",
        "operation": "沿用母版的 FL2VA / Ref2VA 路线、prompt、seed 与参考素材重新采样；不保证复现同一镜头。",
        "kind": "model_regeneration",
        "gpu_required": True,
        "drift_review_required": True,
    },
    "deterministic_scale": {
        "label": "确定性保真放大",
        "operation": "使用 FFmpeg Lanczos 放大并保留原动作、时长、构图和声音；不会生成新细节。",
        "kind": "pixel_scaling",
        "gpu_required": False,
        "drift_review_required": False,
    },
}

ACTIVE_JOB_STATES = ("queued", "running", "submission_outcome_unknown")


class HDRunnerError(RuntimeError):
    def __init__(self, message: str, *, process_started: bool) -> None:
        super().__init__(message)
        self.process_started = process_started


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def init_hd_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS hd_strategy_versions (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          strategy_type TEXT NOT NULL CHECK(strategy_type IN ('ref2va_regenerate','original_model_regenerate','deterministic_scale')),
          target_width INTEGER NOT NULL,
          target_height INTEGER NOT NULL,
          source_master_version_id TEXT NOT NULL REFERENCES candidate_master_versions(id) ON DELETE RESTRICT,
          source_candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE RESTRICT,
          source_snapshot TEXT NOT NULL,
          plan_hash TEXT NOT NULL,
          estimated_operation TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(shot_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_hd_strategy_project ON hd_strategy_versions(project_id, shot_id, revision DESC);

        CREATE TABLE IF NOT EXISTS hd_validations (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          plan_id TEXT NOT NULL REFERENCES hd_strategy_versions(id) ON DELETE RESTRICT,
          plan_hash TEXT NOT NULL,
          validation_hash TEXT NOT NULL,
          adapter TEXT NOT NULL,
          model_id TEXT NOT NULL,
          workflow_id TEXT NOT NULL,
          command_snapshot TEXT NOT NULL,
          evidence TEXT NOT NULL,
          gpu_submitted INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hd_validations_plan ON hd_validations(plan_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS hd_validation_leases (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL UNIQUE REFERENCES shots(id) ON DELETE CASCADE,
          plan_id TEXT NOT NULL REFERENCES hd_strategy_versions(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hd_generation_jobs (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          plan_id TEXT NOT NULL REFERENCES hd_strategy_versions(id) ON DELETE RESTRICT,
          validation_id TEXT NOT NULL REFERENCES hd_validations(id) ON DELETE RESTRICT,
          attempt INTEGER NOT NULL,
          idempotency_key TEXT NOT NULL,
          state TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 0,
          plan_hash TEXT NOT NULL,
          validation_hash TEXT NOT NULL,
          command_snapshot TEXT NOT NULL,
          expected_artifact_id TEXT NOT NULL,
          gpu_submitted INTEGER NOT NULL DEFAULT 0,
          retry_safe INTEGER NOT NULL DEFAULT 0,
          message TEXT NOT NULL,
          evidence TEXT NOT NULL DEFAULT '{}',
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT,
          UNIQUE(project_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_hd_jobs_queue ON hd_generation_jobs(state, created_at);

        CREATE TABLE IF NOT EXISTS hd_shot_leases (
          shot_id TEXT PRIMARY KEY REFERENCES shots(id) ON DELETE CASCADE,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          job_id TEXT NOT NULL UNIQUE REFERENCES hd_generation_jobs(id) ON DELETE CASCADE,
          state TEXT NOT NULL CHECK(state IN ('active','unknown')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hd_artifacts (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          job_id TEXT NOT NULL UNIQUE REFERENCES hd_generation_jobs(id) ON DELETE RESTRICT,
          plan_id TEXT NOT NULL REFERENCES hd_strategy_versions(id) ON DELETE RESTRICT,
          version INTEGER NOT NULL,
          strategy_type TEXT NOT NULL,
          strategy_kind TEXT NOT NULL,
          source_candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE RESTRICT,
          source_master_version_id TEXT NOT NULL REFERENCES candidate_master_versions(id) ON DELETE RESTRICT,
          prompt TEXT NOT NULL,
          seed INTEGER,
          spec TEXT NOT NULL,
          plan_hash TEXT NOT NULL,
          model_id TEXT NOT NULL,
          workflow_id TEXT NOT NULL,
          reference_snapshot TEXT NOT NULL,
          source_path TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          output_path TEXT NOT NULL,
          output_sha256 TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          width INTEGER NOT NULL,
          height INTEGER NOT NULL,
          duration_seconds REAL NOT NULL,
          has_audio INTEGER NOT NULL,
          prompt_id TEXT,
          comfy_task_id TEXT,
          provenance TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(shot_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_hd_artifacts_shot ON hd_artifacts(shot_id, version DESC);

        CREATE TABLE IF NOT EXISTS hd_artifact_reviews (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          artifact_id TEXT NOT NULL REFERENCES hd_artifacts(id) ON DELETE RESTRICT,
          revision INTEGER NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('pass','needs_changes','reject')),
          scores TEXT NOT NULL,
          drift_confirmed INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL,
          watched_seconds REAL NOT NULL,
          artifact_snapshot TEXT NOT NULL,
          media_probe TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(artifact_id, revision)
        );

        CREATE TABLE IF NOT EXISTS hd_master_versions (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          artifact_id TEXT NOT NULL REFERENCES hd_artifacts(id) ON DELETE RESTRICT,
          action TEXT NOT NULL CHECK(action IN ('select','rollback')),
          rollback_of_revision INTEGER,
          review_id TEXT NOT NULL REFERENCES hd_artifact_reviews(id) ON DELETE RESTRICT,
          review_revision INTEGER NOT NULL,
          note TEXT NOT NULL,
          artifact_snapshot TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(shot_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_hd_master_shot ON hd_master_versions(shot_id, revision DESC);
        """
    )


class HDPlanCreate(BaseModel):
    strategy_type: HDStrategy
    target_width: int = Field(1344, ge=640, le=3840)
    target_height: int = Field(768, ge=352, le=2160)


class HDValidationRequest(BaseModel):
    plan_id: str = Field(min_length=3, max_length=200)
    expected_plan_hash: str = Field(min_length=64, max_length=64)


class HDSubmitRequest(BaseModel):
    validation_id: str = Field(min_length=3, max_length=200)
    expected_validation_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=200)
    confirm: bool = False


class HDScores(BaseModel):
    story_match: int = Field(ge=1, le=5)
    identity_continuity: int = Field(ge=1, le=5)
    temporal_stability: int = Field(ge=1, le=5)
    visual_detail: int = Field(ge=1, le=5)
    audio_quality: int = Field(ge=1, le=5)


class HDReviewRequest(BaseModel):
    artifact_id: str
    decision: Literal["pass", "needs_changes", "reject"]
    scores: HDScores
    drift_confirmed: bool = False
    note: str = Field("", max_length=1000)
    watched_seconds: float = Field(ge=0, le=3600)


class HDSelectRequest(BaseModel):
    artifact_id: str
    base_revision: int = Field(ge=0)
    note: str = Field("", max_length=500)


class HDRollbackRequest(BaseModel):
    target_revision: int = Field(ge=1)
    base_revision: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=500)
    confirm: bool = False


class HDUnknownResolution(BaseModel):
    expected_revision: int = Field(ge=0)
    note: str = Field(min_length=2, max_length=500)
    confirm_no_external_submission: bool = False


_runner: HDRunner | None = None
_probe: MediaProbe | None = None
_db_path: Path | None = None
_output_root: Path | None = None
_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()
_worker_wake = threading.Event()


def configure_hd_delivery(db_path: Path, output_root: Path, runner: HDRunner, probe: MediaProbe | None = None) -> None:
    global _db_path, _output_root, _runner, _probe
    _db_path = db_path
    _output_root = output_root.resolve()
    _runner = runner
    _probe = probe


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    setting = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
    if not setting:
        raise HTTPException(404, "当前没有已激活的项目")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (setting["value"],)).fetchone()
    if not project or ("archived" in project.keys() and project["archived"]):
        raise HTTPException(404, "当前项目不存在或已归档")
    return dict(project)


def _json(value: str | None, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed
    except (TypeError, json.JSONDecodeError):
        return fallback


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _allowed_media(path_value: str | None, output_root: Path) -> Path:
    if not path_value:
        raise HTTPException(409, "媒体没有持久化输出路径")
    path = Path(path_value).resolve()
    try:
        path.relative_to(output_root.resolve())
    except ValueError as exc:
        raise HTTPException(403, "媒体不在受管输出目录") from exc
    if not path.is_file():
        raise HTTPException(409, "媒体文件不存在")
    return path


def _file_snapshot(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "checksum_sha256": _sha256(path),
    }


def _references(db: sqlite3.Connection, shot_id: str) -> list[dict[str, Any]]:
    if not _table_exists(db, "shot_references") or not _table_exists(db, "assets"):
        return []
    records = []
    for row in db.execute(
        """SELECT refs.id, refs.asset_id, refs.reference_type, refs.ordinal, refs.role,
        assets.name, assets.managed_path, assets.checksum_sha256
        FROM shot_references refs JOIN assets ON assets.id = refs.asset_id
        WHERE refs.shot_id = ? ORDER BY refs.reference_type, refs.ordinal""",
        (shot_id,),
    ).fetchall():
        item = dict(row)
        raw_path = str(item.get("managed_path") or "")
        expected_sha = str(item.get("checksum_sha256") or "").lower()
        if not raw_path or not Path(raw_path).is_absolute() or len(expected_sha) != 64:
            raise HTTPException(409, f"参考素材 {item.get('asset_id')} 缺少规范化受管路径或 SHA-256")
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise HTTPException(409, f"参考素材 {item.get('asset_id')} 文件不存在")
        current_sha = _sha256(path).lower()
        if current_sha != expected_sha:
            raise HTTPException(409, f"参考素材 {item.get('asset_id')} 的文件与冻结 SHA-256 不一致")
        item["managed_path"] = str(path)
        item["size_bytes"] = path.stat().st_size
        records.append(item)
    return records


def _current_master_source(db: sqlite3.Connection, project_id: str, shot_id: str, output_root: Path) -> dict[str, Any]:
    shot = db.execute("SELECT * FROM shots WHERE id = ? AND project_id = ?", (shot_id, project_id)).fetchone()
    if not shot:
        raise HTTPException(404, "当前项目中没有这个镜头")
    master = db.execute(
        """SELECT versions.*, candidates.output_file, candidates.prompt_id, candidates.external_id,
        candidates.seed, candidates.metadata, candidates.status AS candidate_status,
        candidates.selected AS candidate_selected
        FROM candidate_master_versions versions
        JOIN candidates ON candidates.id = versions.candidate_id
        WHERE versions.shot_id = ? AND versions.project_id = ?
        ORDER BY versions.revision DESC LIMIT 1""",
        (shot_id, project_id),
    ).fetchone()
    if not master or not master["candidate_selected"]:
        raise HTTPException(409, "镜头尚未锁定可信草稿母版")
    if master["candidate_status"] != "completed":
        raise HTTPException(409, "草稿母版尚未完成")
    media_path = _allowed_media(master["output_file"], output_root)
    media = _file_snapshot(media_path)
    stored = _json(master["candidate_snapshot"], {})
    if stored.get("checksum_sha256") != media["checksum_sha256"] or stored.get("size_bytes") != media["size_bytes"]:
        raise HTTPException(409, "草稿母版媒体与锁定凭证不一致")
    metadata = _json(master["metadata"], {})
    trace = stored.get("trace") if isinstance(stored.get("trace"), dict) else {}
    plan_snapshot = trace.get("plan_snapshot") if isinstance(trace.get("plan_snapshot"), dict) else {}
    source_snapshot = trace.get("source_snapshot") if isinstance(trace.get("source_snapshot"), dict) else {}
    original_mode = str(
        plan_snapshot.get("mode") or source_snapshot.get("mode") or metadata.get("mode")
        or ("ref2va" if _references(db, shot_id) else "fl2va")
    ).lower()
    if original_mode not in ("fl2va", "ref2va"):
        original_mode = "ref2va" if _references(db, shot_id) else "fl2va"
    prompt = str(metadata.get("prompt") or (shot["prompt"] if "prompt" in shot.keys() else ""))
    spec = {
        "width": int(metadata.get("width") or (shot["width"] if "width" in shot.keys() else 0) or 0),
        "height": int(metadata.get("height") or (shot["height"] if "height" in shot.keys() else 0) or 0),
        "duration_seconds": float(metadata.get("actual_seconds") or (shot["seconds"] if "seconds" in shot.keys() else 0) or 0),
    }
    return {
        "project_id": project_id,
        "shot_id": shot_id,
        "shot_title": shot["title"],
        "master_version_id": master["id"],
        "master_revision": int(master["revision"]),
        "candidate_id": master["candidate_id"],
        "candidate_external_id": master["external_id"],
        "h3_project": trace.get("h3_project"),
        "prompt_id": master["prompt_id"],
        "prompt": prompt,
        "seed": master["seed"],
        "spec": spec,
        "original_mode": original_mode,
        "plan_hash": trace.get("plan_hash") or stored.get("plan_hash"),
        "references": _references(db, shot_id),
        "media": media,
        "locked_snapshot": stored,
    }


def _source_matches(current: dict[str, Any], frozen: dict[str, Any]) -> bool:
    return all(
        current.get(key) == frozen.get(key)
        for key in ("project_id", "shot_id", "master_version_id", "master_revision", "candidate_id", "prompt_id", "prompt", "seed", "original_mode", "plan_hash", "references", "media")
    )


def _safe_component(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", value) or value in {".", ".."}:
        raise HDRunnerError(f"{label} 不是安全路径标识", process_started=False)
    return value


def _attempt_root(output_root: Path, job_id: str) -> Path:
    root = (output_root.resolve() / "hd-delivery" / "attempts").resolve()
    path = (root / _safe_component(job_id, "高清任务 ID")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HDRunnerError("高清任务 staging 越过受管目录", process_started=False) from exc
    return path


def _cleanup_attempt_staging(output_root: Path, job_id: str) -> None:
    path = _attempt_root(output_root, job_id)
    if not path.exists():
        return

    def writable_then_retry(function: Any, raw_path: str, _error: Any) -> None:
        os.chmod(raw_path, 0o700)
        function(raw_path)

    shutil.rmtree(path, onerror=writable_then_retry)


def _copy_stable(source: Path, destination: Path, expected_sha: str) -> dict[str, Any]:
    before_size = source.stat().st_size
    before_sha = _sha256(source).lower()
    if len(expected_sha) != 64 or before_sha != expected_sha.lower():
        raise HDRunnerError("高清输入在 staging 前已变化", process_started=False)
    temporary = destination.parent / f".{destination.name}-{uuid.uuid4().hex}.tmp"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        after_size = source.stat().st_size
        after_sha = _sha256(source).lower()
        staged_sha = _sha256(temporary).lower()
        if before_size != after_size or before_sha != after_sha or staged_sha != expected_sha.lower():
            raise HDRunnerError("高清输入在 staging 复制期间发生变化", process_started=False)
        temporary.replace(destination)
        os.chmod(destination, 0o444)
        return {
            "path": str(destination.resolve()), "size_bytes": destination.stat().st_size,
            "checksum_sha256": staged_sha, "original_path": str(source.resolve()),
        }
    finally:
        temporary.unlink(missing_ok=True)


def _stage_execution_inputs(output_root: Path, job: dict[str, Any], request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _attempt_root(output_root, job["id"])
    _cleanup_attempt_staging(output_root, job["id"])
    input_root = root / "inputs"
    output_path = root / "output"
    root.mkdir(parents=True, exist_ok=False)
    input_root.mkdir(exist_ok=False)
    output_path.mkdir(exist_ok=False)
    try:
        frozen_source = request.get("source") if isinstance(request.get("source"), dict) else {}
        media = frozen_source.get("media") if isinstance(frozen_source.get("media"), dict) else {}
        source_path = Path(str(media.get("path") or "")).resolve()
        source_suffix = source_path.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,8}", source_path.suffix.lower()) else ".media"
        source_copy = _copy_stable(
            source_path, input_root / f"source-{str(media.get('checksum_sha256')).lower()}{source_suffix}",
            str(media.get("checksum_sha256") or ""),
        )
        staged_references: list[dict[str, Any]] = []
        for index, reference in enumerate(frozen_source.get("references") or [], 1):
            reference_path = Path(str(reference.get("managed_path") or "")).resolve()
            suffix = reference_path.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,8}", reference_path.suffix.lower()) else ".media"
            copy = _copy_stable(
                reference_path,
                input_root / f"ref-{index:03d}-{str(reference.get('checksum_sha256')).lower()}{suffix}",
                str(reference.get("checksum_sha256") or ""),
            )
            staged_references.append({**reference, "managed_path": copy["path"], "staged": copy})
        staged_source = {
            **frozen_source,
            "media": {**media, **source_copy, "staged": True},
            "references": staged_references,
        }
        staged_request = {
            **request, "source": staged_source, "attempt_output_root": str(output_path.resolve()),
            "attempt_id": job["id"],
        }
        staging = {
            "root": str(root.resolve()), "source": source_copy,
            "references": [item["staged"] for item in staged_references],
            "output_root": str(output_path.resolve()),
        }
        return staged_request, staging
    except Exception:
        _cleanup_attempt_staging(output_root, job["id"])
        raise


def _verify_staging_snapshot(output_root: Path, job_id: str, staging: dict[str, Any]) -> None:
    root = _attempt_root(output_root, job_id)
    if Path(str(staging.get("root") or "")).resolve() != root:
        raise HDRunnerError("高清任务 staging 根目录凭证不一致", process_started=False)
    records = [staging.get("source"), *(staging.get("references") or [])]
    for record in records:
        if not isinstance(record, dict):
            raise HDRunnerError("高清任务 staging 输入凭证不完整", process_started=False)
        path = Path(str(record.get("path") or "")).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HDRunnerError("高清任务 staging 输入越过受管目录", process_started=False) from exc
        current = _file_snapshot(path)
        if current["checksum_sha256"] != record.get("checksum_sha256") or current["size_bytes"] != record.get("size_bytes"):
            raise HDRunnerError("高清任务 staged 输入已变化", process_started=False)


def _execution_gate(
    db_path: Path, output_root: Path, job: dict[str, Any], staging: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        current_job = db.execute(
            "SELECT * FROM hd_generation_jobs WHERE id = ? AND state = 'running' AND revision = ?",
            (job["id"], job["revision"]),
        ).fetchone()
        lease = db.execute(
            "SELECT * FROM hd_shot_leases WHERE shot_id = ? AND job_id = ? AND state = 'active'",
            (job["shot_id"], job["id"]),
        ).fetchone()
        project = db.execute("SELECT * FROM projects WHERE id = ?", (job["project_id"],)).fetchone()
        frozen = db.execute(
            "SELECT 1 FROM project_archive_leases WHERE project_id = ?",
            (job["project_id"],),
        ).fetchone() if _table_exists(db, "project_archive_leases") else None
        if not current_job or not lease:
            raise HDRunnerError("高清任务 claim/lease 已变化", process_started=False)
        if not project or ("archived" in project.keys() and project["archived"]):
            raise HDRunnerError("高清任务所属项目已归档", process_started=False)
        if frozen:
            raise HDRunnerError("项目正在冻结归档，高清任务禁止启动", process_started=False)
        plan, current_source = _require_latest_plan(db, job["project_id"], job["plan_id"], output_root)
        if plan["plan_hash"] != job["plan_hash"]:
            raise HDRunnerError("高清任务 plan hash 已变化", process_started=False)
        command = _json(current_job["command_snapshot"], {})
        request = command.get("request") if isinstance(command.get("request"), dict) else command
        if (
            request.get("plan_hash") != plan["plan_hash"]
            or request.get("source") != plan["source_snapshot"]
            or not _source_matches(current_source, plan["source_snapshot"])
        ):
            raise HDRunnerError("高清任务冻结 master/source/reference 已变化", process_started=False)
        validation = db.execute(
            "SELECT * FROM hd_validations WHERE id = ? AND plan_id = ? AND validation_hash = ?",
            (job["validation_id"], job["plan_id"], job["validation_hash"]),
        ).fetchone()
        if not validation:
            raise HDRunnerError("高清任务 dry-run 凭证已变化", process_started=False)
        if staging is not None:
            _verify_staging_snapshot(output_root, job["id"], staging)
            evidence = _json(current_job["evidence"], {})
            evidence["execution_gate"] = {"checked_at": utc_now(), "staging": staging}
            db.execute(
                "UPDATE hd_generation_jobs SET evidence = ?, updated_at = ? WHERE id = ? AND state = 'running' AND revision = ?",
                (json.dumps(evidence, ensure_ascii=False, sort_keys=True), utc_now(), job["id"], job["revision"]),
            )
        db.commit()
        return plan


def _validate_target(width: int, height: int) -> None:
    if width % 32 or height % 32:
        raise HTTPException(422, "H3 目标宽高都必须能被 32 整除")
    if width < 1344 or height < 768 or width <= height:
        raise HTTPException(422, "高清交付规格必须为横屏且至少 1344×768")


def _strategy_public(strategy: str) -> dict[str, Any]:
    return {"type": strategy, **STRATEGY_DETAILS[strategy]}


def _plan_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["source_snapshot"] = _json(item.get("source_snapshot"), {})
    item["strategy"] = _strategy_public(item["strategy_type"])
    return item


def _validation_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["command_snapshot"] = _json(item.get("command_snapshot"), {})
    item["evidence"] = _json(item.get("evidence"), {})
    item["gpu_submitted"] = bool(item.get("gpu_submitted"))
    return item


def _job_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["command_snapshot"] = _json(item.get("command_snapshot"), {})
    item["evidence"] = _json(item.get("evidence"), {})
    item["gpu_submitted"] = bool(item.get("gpu_submitted"))
    item["retry_safe"] = bool(item.get("retry_safe"))
    return item


def _artifact_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("spec", "reference_snapshot", "provenance"):
        item[key] = _json(item.get(key), {}) if key != "reference_snapshot" else _json(item.get(key), [])
    item["has_audio"] = bool(item.get("has_audio"))
    item["strategy"] = _strategy_public(item["strategy_type"])
    item["video_url"] = f"/api/hd/artifacts/{item['id']}/video"
    return item


def create_hd_plan(db_path: Path, output_root: Path, shot_id: str, payload: HDPlanCreate) -> dict[str, Any]:
    _validate_target(payload.target_width, payload.target_height)
    if payload.strategy_type != "deterministic_scale" and (payload.target_width > 1344 or payload.target_height > 768):
        raise HTTPException(422, "当前本机 H3 模型重生成上限为 1344×768；更大画布只能选择确定性缩放并如实标注")
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        source = _current_master_source(db, project["id"], shot_id, output_root)
        if payload.strategy_type != "deterministic_scale":
            # A model regeneration must carry the exact seed that will be sent
            # to the adapter.  If the historical master has no seed, freeze a
            # new one now instead of later claiming that a random run reused it.
            source["generation_seed"] = int(source["seed"]) if source.get("seed") is not None else int.from_bytes(os.urandom(8), "big") % (2**63)
        revision = int(db.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM hd_strategy_versions WHERE shot_id = ?", (shot_id,),
        ).fetchone()[0])
        stable = {
            "schema_version": 1,
            "project_id": project["id"], "shot_id": shot_id, "revision": revision,
            "strategy_type": payload.strategy_type,
            "target": {"width": payload.target_width, "height": payload.target_height},
            "source": source,
        }
        plan_hash = _canonical_hash(stable)
        plan_id = f"hd-plan-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        db.execute(
            """INSERT INTO hd_strategy_versions
            (id, project_id, shot_id, revision, strategy_type, target_width, target_height,
             source_master_version_id, source_candidate_id, source_snapshot, plan_hash, estimated_operation, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan_id, project["id"], shot_id, revision, payload.strategy_type,
                payload.target_width, payload.target_height, source["master_version_id"], source["candidate_id"],
                json.dumps(source, ensure_ascii=False, sort_keys=True), plan_hash,
                STRATEGY_DETAILS[payload.strategy_type]["operation"], now,
            ),
        )
        plan = db.execute("SELECT * FROM hd_strategy_versions WHERE id = ?", (plan_id,)).fetchone()
        db.commit()
        return _plan_public(plan)


def _require_plan(db: sqlite3.Connection, project_id: str, plan_id: str, output_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    row = db.execute("SELECT * FROM hd_strategy_versions WHERE id = ? AND project_id = ?", (plan_id, project_id)).fetchone()
    if not row:
        raise HTTPException(404, "当前项目中没有这个高清策略版本")
    plan = _plan_public(row)
    current = _current_master_source(db, project_id, plan["shot_id"], output_root)
    if not _source_matches(current, plan["source_snapshot"]):
        raise HTTPException(409, "草稿母版、prompt、参考素材或源媒体已变化，请创建新的高清策略版本")
    return plan, current


def _require_latest_plan(db: sqlite3.Connection, project_id: str, plan_id: str, output_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    plan, current = _require_plan(db, project_id, plan_id, output_root)
    latest = db.execute(
        "SELECT id FROM hd_strategy_versions WHERE project_id = ? AND shot_id = ? ORDER BY revision DESC LIMIT 1",
        (project_id, plan["shot_id"]),
    ).fetchone()
    if not latest or latest["id"] != plan_id:
        raise HTTPException(409, "该镜头已有更新的高清策略版本，请对最新版本重新 dry-run")
    return plan, current


def validate_hd_plan(
    db_path: Path, output_root: Path, payload: HDValidationRequest, runner: HDRunner,
) -> dict[str, Any]:
    lease_id = f"hd-validation-lease-{uuid.uuid4().hex[:12]}"
    lease_acquired = False
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        if _table_exists(db, "project_archive_leases") and db.execute(
            "SELECT 1 FROM project_archive_leases WHERE project_id = ?", (project["id"],),
        ).fetchone():
            raise HTTPException(409, "项目正在冻结归档，禁止启动高清 dry-run")
        plan, _ = _require_latest_plan(db, project["id"], payload.plan_id, output_root)
        if plan["plan_hash"] != payload.expected_plan_hash:
            raise HTTPException(409, "高清策略 plan hash 已变化")
        try:
            db.execute(
                "INSERT INTO hd_validation_leases (id, project_id, shot_id, plan_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (lease_id, project["id"], plan["shot_id"], plan["id"], utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "这个镜头已有进行中的高清 dry-run") from exc
        db.commit()
        lease_acquired = True
    try:
        request = {
            "operation": "hd_promote", "plan_id": plan["id"], "plan_hash": plan["plan_hash"],
            "strategy_type": plan["strategy_type"], "target_width": plan["target_width"],
            "target_height": plan["target_height"], "source": plan["source_snapshot"],
        }
        result = runner(request, True)
        if result.get("gpu_submitted"):
            raise HTTPException(502, "高清 dry-run 适配器错误地提交了 GPU 任务")
        adapter_command = result.get("command") if isinstance(result.get("command"), dict) else request
        command = {"request": request, "adapter_command": adapter_command}
        adapter = str(result.get("adapter") or "unknown")
        model_id = str(result.get("model_id") or ("ffmpeg-lanczos" if plan["strategy_type"] == "deterministic_scale" else "unverified"))
        workflow_id = str(result.get("workflow_id") or "unverified")
        evidence = {**result, "gpu_submitted": False, "plan_hash": plan["plan_hash"]}
        validation_hash = _canonical_hash({"plan": plan, "command": command, "adapter": adapter, "model_id": model_id, "workflow_id": workflow_id})
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            lease = db.execute(
                "SELECT 1 FROM hd_validation_leases WHERE id = ? AND project_id = ? AND plan_id = ?",
                (lease_id, project["id"], payload.plan_id),
            ).fetchone()
            frozen = _table_exists(db, "project_archive_leases") and db.execute(
                "SELECT 1 FROM project_archive_leases WHERE project_id = ?", (project["id"],),
            ).fetchone()
            if not lease or frozen:
                raise HTTPException(409, "高清 dry-run 返回时项目归档冻结或 lease 已变化，结果未保存")
            current_plan, _ = _require_latest_plan(db, project["id"], payload.plan_id, output_root)
            if current_plan["plan_hash"] != payload.expected_plan_hash:
                raise HTTPException(409, "dry-run 期间高清策略发生变化，结果未保存")
            validation_id = f"hd-validation-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO hd_validations
                (id, project_id, shot_id, plan_id, plan_hash, validation_hash, adapter, model_id,
                 workflow_id, command_snapshot, evidence, gpu_submitted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    validation_id, project["id"], current_plan["shot_id"], current_plan["id"], current_plan["plan_hash"],
                    validation_hash, adapter, model_id, workflow_id, json.dumps(command, ensure_ascii=False, sort_keys=True),
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True), utc_now(),
                ),
            )
            db.execute("DELETE FROM hd_validation_leases WHERE id = ?", (lease_id,))
            row = db.execute("SELECT * FROM hd_validations WHERE id = ?", (validation_id,)).fetchone()
            db.commit()
            lease_acquired = False
            return _validation_public(row)
    finally:
        if lease_acquired:
            with closing(connect(db_path)) as db:
                db.execute("DELETE FROM hd_validation_leases WHERE id = ?", (lease_id,))
                db.commit()


def submit_hd_job(db_path: Path, output_root: Path, shot_id: str, payload: HDSubmitRequest) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(400, "提交高清处理前必须明确确认")
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        existing = db.execute(
            "SELECT * FROM hd_generation_jobs WHERE project_id = ? AND idempotency_key = ?",
            (project["id"], payload.idempotency_key),
        ).fetchone()
        if existing:
            if existing["shot_id"] != shot_id or existing["validation_id"] != payload.validation_id:
                raise HTTPException(409, "幂等键已用于其他高清提交")
            db.commit()
            return _job_public(existing)
        if _table_exists(db, "project_archive_leases") and db.execute(
            "SELECT 1 FROM project_archive_leases WHERE project_id = ?", (project["id"],),
        ).fetchone():
            raise HTTPException(409, "项目正在冻结归档，禁止提交高清任务")
        validation_row = db.execute(
            "SELECT * FROM hd_validations WHERE id = ? AND project_id = ? AND shot_id = ?",
            (payload.validation_id, project["id"], shot_id),
        ).fetchone()
        if not validation_row:
            raise HTTPException(404, "当前镜头没有这个高清 dry-run 记录")
        validation = _validation_public(validation_row)
        if validation["validation_hash"] != payload.expected_validation_hash:
            raise HTTPException(409, "高清 dry-run 凭证已变化")
        plan, _ = _require_latest_plan(db, project["id"], validation["plan_id"], output_root)
        if plan["plan_hash"] != validation["plan_hash"]:
            raise HTTPException(409, "高清策略与 dry-run plan hash 不一致")
        lease = db.execute("SELECT * FROM hd_shot_leases WHERE shot_id = ?", (shot_id,)).fetchone()
        if lease:
            raise HTTPException(409, "这个镜头已有高清任务或待人工对账任务")
        attempt = int(db.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 FROM hd_generation_jobs WHERE shot_id = ?", (shot_id,),
        ).fetchone()[0])
        job_id = f"hd-job-{uuid.uuid4().hex[:12]}"
        artifact_id = f"hd-artifact-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        command = {
            **validation["command_snapshot"], "plan_id": plan["id"], "plan_hash": plan["plan_hash"],
            "validation_id": validation["id"], "validation_hash": validation["validation_hash"],
            "expected_artifact_id": artifact_id,
        }
        db.execute(
            """INSERT INTO hd_generation_jobs
            (id, project_id, shot_id, plan_id, validation_id, attempt, idempotency_key, state, revision,
             plan_hash, validation_hash, command_snapshot, expected_artifact_id, message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, '已确认，等待高清工作进程', ?, ?)""",
            (
                job_id, project["id"], shot_id, plan["id"], validation["id"], attempt, payload.idempotency_key,
                plan["plan_hash"], validation["validation_hash"], json.dumps(command, ensure_ascii=False, sort_keys=True),
                artifact_id, now, now,
            ),
        )
        db.execute(
            "INSERT INTO hd_shot_leases (shot_id, project_id, job_id, state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (shot_id, project["id"], job_id, now, now),
        )
        job = db.execute("SELECT * FROM hd_generation_jobs WHERE id = ?", (job_id,)).fetchone()
        db.commit()
    _worker_wake.set()
    return _job_public(job)


def _default_probe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,duration,channels,sample_rate",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "issues": [f"ffprobe 不可用：{exc}"]}
    if completed.returncode != 0:
        return {"ok": False, "issues": [completed.stderr.strip() or "媒体不可解码"]}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "issues": ["ffprobe 返回无效 JSON"]}
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration = (payload.get("format") or {}).get("duration") or video.get("duration") or 0
    issues = []
    if not video:
        issues.append("缺少视频流")
    if not audio:
        issues.append("缺少音轨")
    return {
        "ok": not issues, "issues": issues, "duration_seconds": round(float(duration), 3),
        "video": {"codec": video.get("codec_name"), "width": video.get("width"), "height": video.get("height")},
        "audio": {"present": bool(audio), "codec": audio.get("codec_name"), "channels": audio.get("channels"), "sample_rate": audio.get("sample_rate")},
    }


@contextmanager
def _hold_no_write_or_delete(path: Path):
    """Hold a Windows share-mode lock while evidence is read and committed."""
    if os.name != "nt":
        raise HDRunnerError("当前平台无法提供高清产物的 OS 级禁写/禁删锁", process_started=True)
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(str(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None)
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        raise HDRunnerError(
            f"无法锁定高清产物 {path.name}（Windows {error}: {ctypes.FormatError(error).strip()}）",
            process_started=True,
        )
    try:
        yield
    finally:
        close_handle(handle)


def _allowed_attempt_output(raw_path: str | None, output_root: Path, job_id: str) -> Path:
    path = Path(str(raw_path or "")).resolve()
    root = (_attempt_root(output_root, job_id) / "output").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HDRunnerError("适配器输出不在本次高清任务私有 staging", process_started=True) from exc
    if not path.is_file():
        raise HDRunnerError("高清 staging 产物不存在", process_started=True)
    return path


def _publish_hd_output(output_root: Path, job: dict[str, Any], staged: Path) -> tuple[Path, dict[str, Any]]:
    artifact_root = (output_root.resolve() / "hd-delivery" / "artifacts").resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    with _hold_no_write_or_delete(staged):
        staged_snapshot = _file_snapshot(staged)
        suffix = staged.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,8}", staged.suffix.lower()) else ".media"
        artifact_name = f"{_safe_component(job['expected_artifact_id'], '高清产物 ID')}-{staged_snapshot['checksum_sha256']}{suffix}"
        final_path = (artifact_root / artifact_name).resolve()
        try:
            final_path.relative_to(artifact_root)
        except ValueError as exc:
            raise HDRunnerError("高清产物发布路径越界", process_started=True) from exc
        temporary = artifact_root / f".{artifact_name}-{uuid.uuid4().hex}.tmp"
        try:
            with staged.open("rb") as source_handle, temporary.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            copied = _file_snapshot(temporary)
            staged_after = _file_snapshot(staged)
            if staged_after != staged_snapshot or copied["checksum_sha256"] != staged_snapshot["checksum_sha256"] or copied["size_bytes"] != staged_snapshot["size_bytes"]:
                raise HDRunnerError("高清 staging 产物在发布复制期间发生变化", process_started=True)
            if final_path.exists():
                existing = _file_snapshot(final_path)
                if existing["checksum_sha256"] != staged_snapshot["checksum_sha256"]:
                    raise HDRunnerError("高清内容寻址发布路径已被其他内容占用", process_started=True)
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(final_path)
            os.chmod(final_path, 0o444)
            return final_path, staged_snapshot
        finally:
            temporary.unlink(missing_ok=True)


def _claim_job(db_path: Path) -> dict[str, Any] | None:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        archive_filter = (
            "AND NOT EXISTS (SELECT 1 FROM project_archive_leases leases WHERE leases.project_id = jobs.project_id)"
            if _table_exists(db, "project_archive_leases") else ""
        )
        pending = db.execute(
            f"""SELECT jobs.* FROM hd_generation_jobs jobs
            JOIN projects ON projects.id = jobs.project_id AND COALESCE(projects.archived, 0) = 0
            WHERE jobs.state = 'queued' {archive_filter}
            ORDER BY jobs.created_at LIMIT 1"""
        ).fetchone()
        if not pending:
            db.commit()
            return None
        now = utc_now()
        changed = db.execute(
            """UPDATE hd_generation_jobs SET state = 'running', revision = revision + 1,
            message = '正在执行冻结的高清操作', started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE id = ? AND state = 'queued' AND revision = ?""",
            (now, now, pending["id"], pending["revision"]),
        )
        if changed.rowcount != 1:
            db.rollback()
            return None
        row = db.execute("SELECT * FROM hd_generation_jobs WHERE id = ?", (pending["id"],)).fetchone()
        db.commit()
        return _job_public(row)


def _verify_completion_evidence(plan: dict[str, Any], result: dict[str, Any]) -> tuple[str, str]:
    strategy = plan["strategy_type"]
    source = plan["source_snapshot"]
    model_id = str(result.get("model_id") or "")
    workflow_id = str(result.get("workflow_id") or "")
    if strategy == "deterministic_scale":
        if result.get("gpu_submitted") or model_id != "ffmpeg-lanczos" or workflow_id != "deterministic-scale-contain-v1":
            raise HDRunnerError("确定性缩放返回了错误的模型、工作流或 GPU 证据", process_started=True)
        return model_id, workflow_id
    expected_model = (
        "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
        if strategy == "ref2va_regenerate" or source.get("original_mode") == "ref2va"
        else "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    )
    expected_workflow = "h3-ref2va-regenerate-v2" if strategy == "ref2va_regenerate" else f"h3-draft-{source.get('original_mode')}-target-v2"
    adapter_input = result.get("adapter_input")
    record = result.get("manifest_record")
    if not result.get("gpu_submitted") or model_id != expected_model or workflow_id != expected_workflow:
        raise HDRunnerError("模型重生成返回的 model/workflow/GPU 证据不一致", process_started=True)
    if not isinstance(adapter_input, dict) or not isinstance(record, dict):
        raise HDRunnerError("模型重生成缺少精确 adapter input 或 manifest 记录", process_started=True)
    expected_seed = source.get("generation_seed")
    expected_prompt_sha = hashlib.sha256(str(source.get("prompt") or "").encode("utf-8")).hexdigest()
    if (
        adapter_input.get("seed") != expected_seed
        or adapter_input.get("prompt_sha256") != expected_prompt_sha
        or adapter_input.get("model_id") != expected_model
        or adapter_input.get("workflow_id") != expected_workflow
        or record.get("seed") != expected_seed
        or hashlib.sha256(str(record.get("prompt") or "").encode("utf-8")).hexdigest() != expected_prompt_sha
        or int(record.get("width") or 0) != int(plan["target_width"])
        or int(record.get("height") or 0) != int(plan["target_height"])
        or record.get("status") != "completed"
        or not record.get("prompt_id")
    ):
        raise HDRunnerError("模型重生成 manifest 的 seed/prompt/spec/task 与冻结输入不一致", process_started=True)
    if adapter_input.get("references") != record.get("references"):
        raise HDRunnerError("模型重生成 manifest 的参考素材与实际 adapter input 不一致", process_started=True)
    return model_id, workflow_id


def _remove_published(path: Path) -> None:
    if not path.exists():
        return
    try:
        os.chmod(path, 0o600)
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _complete_hd_job(db_path: Path, output_root: Path, job: dict[str, Any], result: dict[str, Any], probe: MediaProbe) -> None:
    staged = _allowed_attempt_output(result.get("output_file"), output_root, job["id"])
    published, staged_snapshot = _publish_hd_output(output_root, job, staged)
    committed = False
    try:
        with _hold_no_write_or_delete(published):
            media = _file_snapshot(published)
            if media["checksum_sha256"] != staged_snapshot["checksum_sha256"] or media["size_bytes"] != staged_snapshot["size_bytes"]:
                raise HDRunnerError("高清发布产物与私有 staging 不一致", process_started=True)
            technical = probe(published)
            if not technical.get("ok"):
                raise HDRunnerError("高清产物技术检查未通过：" + "；".join(technical.get("issues") or []), process_started=True)
            with closing(connect(db_path)) as db:
                db.execute("BEGIN IMMEDIATE")
                current_job = db.execute(
                    "SELECT * FROM hd_generation_jobs WHERE id = ? AND state = 'running' AND revision = ?",
                    (job["id"], job["revision"]),
                ).fetchone()
                lease = db.execute(
                    "SELECT 1 FROM hd_shot_leases WHERE shot_id = ? AND job_id = ? AND state = 'active'",
                    (job["shot_id"], job["id"]),
                ).fetchone()
                project = db.execute("SELECT * FROM projects WHERE id = ?", (job["project_id"],)).fetchone()
                archive_lease = db.execute(
                    "SELECT 1 FROM project_archive_leases WHERE project_id = ?", (job["project_id"],),
                ).fetchone() if _table_exists(db, "project_archive_leases") else None
                if not current_job or not lease:
                    raise HDRunnerError("高清任务完成 CAS/lease 失败，保留为待人工对账", process_started=True)
                if not project or ("archived" in project.keys() and project["archived"]) or archive_lease:
                    raise HDRunnerError("项目已归档或正在冻结，高清产物不能发布", process_started=True)
                plan, _ = _require_latest_plan(db, job["project_id"], job["plan_id"], output_root)
                if plan["plan_hash"] != job["plan_hash"]:
                    raise HDRunnerError("高清任务运行期间 plan hash 变化", process_started=True)
                model_id, workflow_id = _verify_completion_evidence(plan, result)
                # Recompute protected path/size/SHA inside the publication transaction.
                final_media = _file_snapshot(published)
                if final_media != media:
                    raise HDRunnerError("高清产物在最终发布事务前发生变化", process_started=True)
                source = plan["source_snapshot"]
                version = int(db.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM hd_artifacts WHERE shot_id = ?", (job["shot_id"],),
                ).fetchone()[0])
                artifact_id = current_job["expected_artifact_id"]
                spec = {
                    "requested": {"width": plan["target_width"], "height": plan["target_height"]},
                    "actual": {
                        "width": int((technical.get("video") or {}).get("width") or 0),
                        "height": int((technical.get("video") or {}).get("height") or 0),
                        "duration_seconds": float(technical.get("duration_seconds") or 0),
                    },
                }
                if spec["actual"]["width"] < plan["target_width"] or spec["actual"]["height"] < plan["target_height"]:
                    raise HDRunnerError("高清产物未达到目标规格", process_started=True)
                provenance = {
                    "schema_version": 2, "strategy": _strategy_public(plan["strategy_type"]),
                    "plan_id": plan["id"], "plan_hash": plan["plan_hash"], "validation_id": job["validation_id"],
                    "job_id": job["id"], "attempt": job["attempt"], "model_id": model_id, "workflow_id": workflow_id,
                    "prompt": source["prompt"], "seed": source.get("generation_seed", source.get("seed")),
                    "source_seed": source.get("seed"), "source_spec": source.get("spec"), "target_spec": spec,
                    "references": source.get("references", []), "source_media": source["media"],
                    "staged_output_media": staged_snapshot, "output_media": final_media,
                    "adapter_result": result, "technical_probe": technical,
                }
                db.execute(
                    """INSERT INTO hd_artifacts
                    (id, project_id, shot_id, job_id, plan_id, version, strategy_type, strategy_kind,
                     source_candidate_id, source_master_version_id, prompt, seed, spec, plan_hash, model_id, workflow_id,
                     reference_snapshot, source_path, source_sha256, output_path, output_sha256, size_bytes,
                     width, height, duration_seconds, has_audio, prompt_id, comfy_task_id, provenance, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact_id, job["project_id"], job["shot_id"], job["id"], plan["id"], version,
                        plan["strategy_type"], STRATEGY_DETAILS[plan["strategy_type"]]["kind"], source["candidate_id"],
                        source["master_version_id"], source["prompt"], source.get("generation_seed", source.get("seed")),
                        json.dumps(spec, ensure_ascii=False, sort_keys=True), plan["plan_hash"], model_id, workflow_id,
                        json.dumps(source.get("references", []), ensure_ascii=False, sort_keys=True),
                        source["media"]["path"], source["media"]["checksum_sha256"], final_media["path"],
                        final_media["checksum_sha256"], final_media["size_bytes"], spec["actual"]["width"],
                        spec["actual"]["height"], spec["actual"]["duration_seconds"],
                        int(bool((technical.get("audio") or {}).get("present"))), result.get("prompt_id"), result.get("comfy_task_id"),
                        json.dumps(provenance, ensure_ascii=False, sort_keys=True), utc_now(),
                    ),
                )
                now = utc_now()
                changed = db.execute(
                    """UPDATE hd_generation_jobs SET state = 'completed', revision = revision + 1, gpu_submitted = ?,
                    message = '高清产物已完成并写入不可变证据', evidence = ?, updated_at = ?, completed_at = ?, error = NULL
                    WHERE id = ? AND state = 'running' AND revision = ?""",
                    (int(bool(result.get("gpu_submitted"))), json.dumps(result, ensure_ascii=False, sort_keys=True), now, now, job["id"], job["revision"]),
                )
                if changed.rowcount != 1:
                    raise HDRunnerError("高清任务完成写入 CAS 失败", process_started=True)
                db.execute("DELETE FROM hd_shot_leases WHERE job_id = ?", (job["id"],))
                db.commit()
                committed = True
    finally:
        if not committed:
            _remove_published(published)


def process_next_hd_job(
    db_path: Path | None = None, output_root: Path | None = None,
    runner: HDRunner | None = None, probe: MediaProbe | None = None,
) -> bool:
    active_db = db_path or _db_path
    active_root = (output_root or _output_root)
    active_runner = runner or _runner
    active_probe = probe or _probe or _default_probe
    if not active_db or not active_root or not active_runner:
        return False
    job = _claim_job(active_db)
    if not job:
        return False
    runner_called = False
    staging: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    try:
        frozen_command = job["command_snapshot"]
        frozen_request = frozen_command.get("request") if isinstance(frozen_command.get("request"), dict) else frozen_command
        base_request = {
            **frozen_request,
            "validation_id": job["validation_id"], "validation_hash": job["validation_hash"],
            "expected_artifact_id": job["expected_artifact_id"],
        }
        _execution_gate(active_db, active_root, job)
        execution_request, staging = _stage_execution_inputs(active_root, job, base_request)
        # The first check protects staging; this second short transaction is
        # the final side-effect gate immediately before invoking the adapter.
        _execution_gate(active_db, active_root, job, staging)
        runner_called = True
        result = active_runner(execution_request, False)
        _complete_hd_job(active_db, active_root, job, result, active_probe)
        _cleanup_attempt_staging(active_root, job["id"])
    except Exception as exc:
        process_started = bool(getattr(exc, "process_started", runner_called))
        now = utc_now()
        with closing(connect(active_db)) as db:
            db.execute("BEGIN IMMEDIATE")
            state = "failed" if not process_started else "submission_outcome_unknown"
            retry_safe = 1 if not process_started else 0
            message = "高清适配器确认未启动，可安全重试" if retry_safe else "外部提交结果未知，必须人工对账"
            current = db.execute("SELECT evidence FROM hd_generation_jobs WHERE id = ?", (job["id"],)).fetchone()
            evidence = _json(current["evidence"], {}) if current else {}
            evidence.update({
                "process_started": process_started,
                "runner_called": runner_called,
                "error": str(exc),
                "staging": staging,
                "runner_result": result,
                "failed_at": now,
            })
            db.execute(
                """UPDATE hd_generation_jobs SET state = ?, revision = revision + 1, retry_safe = ?, message = ?,
                error = ?, evidence = ?, updated_at = ?, completed_at = ? WHERE id = ? AND state = 'running'""",
                (state, retry_safe, message, str(exc), json.dumps(evidence, ensure_ascii=False, sort_keys=True), now, now, job["id"]),
            )
            if retry_safe:
                db.execute("DELETE FROM hd_shot_leases WHERE job_id = ?", (job["id"],))
            else:
                db.execute("UPDATE hd_shot_leases SET state = 'unknown', updated_at = ? WHERE job_id = ?", (now, job["id"]))
            db.commit()
        if retry_safe:
            _cleanup_attempt_staging(active_root, job["id"])
    return True


def recover_hd_jobs(db_path: Path) -> int:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        stranded = db.execute("SELECT id, shot_id, project_id FROM hd_generation_jobs WHERE state = 'running'").fetchall()
        now = utc_now()
        for job in stranded:
            db.execute(
                """UPDATE hd_generation_jobs SET state = 'submission_outcome_unknown', revision = revision + 1,
                message = '服务重启后外部提交结果未知，等待人工对账', updated_at = ? WHERE id = ? AND state = 'running'""",
                (now, job["id"]),
            )
            db.execute(
                """INSERT INTO hd_shot_leases (shot_id, project_id, job_id, state, created_at, updated_at)
                VALUES (?, ?, ?, 'unknown', ?, ?)
                ON CONFLICT(shot_id) DO UPDATE SET job_id = excluded.job_id, state = 'unknown', updated_at = excluded.updated_at""",
                (job["shot_id"], job["project_id"], job["id"], now, now),
            )
        db.commit()
        return len(stranded)


def _worker_loop() -> None:
    while not _worker_stop.is_set():
        processed = process_next_hd_job()
        if not processed:
            _worker_wake.wait(0.5)
            _worker_wake.clear()


def start_hd_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="hd-delivery-worker", daemon=True)
    _worker_thread.start()


def stop_hd_worker() -> None:
    _worker_stop.set()
    _worker_wake.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=3)


def resolve_unknown_job(db_path: Path, job_id: str, payload: HDUnknownResolution) -> dict[str, Any]:
    if not payload.confirm_no_external_submission:
        raise HTTPException(400, "只有人工确认未发生外部提交，才能解除未知占用")
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        job = db.execute(
            "SELECT * FROM hd_generation_jobs WHERE id = ? AND project_id = ?", (job_id, project["id"]),
        ).fetchone()
        if not job:
            raise HTTPException(404, "当前项目中没有这个高清任务")
        if job["state"] != "submission_outcome_unknown" or int(job["revision"]) != payload.expected_revision:
            raise HTTPException(409, "高清任务状态或修订已变化")
        now = utc_now()
        evidence = _json(job["evidence"], {})
        evidence["manual_zero_submit"] = {"confirmed": True, "note": payload.note.strip(), "created_at": now}
        changed = db.execute(
            """UPDATE hd_generation_jobs SET state = 'failed', revision = revision + 1, retry_safe = 1,
            message = '人工确认未发生外部提交，可安全重试', evidence = ?, updated_at = ?, completed_at = ?
            WHERE id = ? AND state = 'submission_outcome_unknown' AND revision = ?""",
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True), now, now, job_id, payload.expected_revision),
        )
        if changed.rowcount != 1:
            raise HTTPException(409, "人工对账 CAS 失败")
        db.execute("DELETE FROM hd_shot_leases WHERE job_id = ? AND state = 'unknown'", (job_id,))
        current = db.execute("SELECT * FROM hd_generation_jobs WHERE id = ?", (job_id,)).fetchone()
        db.commit()
        return _job_public(current)


def retry_hd_job(db_path: Path, output_root: Path, job_id: str, idempotency_key: str, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(400, "重试高清任务前必须明确确认")
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        job = db.execute("SELECT * FROM hd_generation_jobs WHERE id = ? AND project_id = ?", (job_id, project["id"])).fetchone()
        if not job:
            raise HTTPException(404, "当前项目中没有这个高清任务")
        if job["state"] != "failed" or not job["retry_safe"]:
            raise HTTPException(409, "只有确认零提交的失败任务可以安全重试")
        validation = db.execute("SELECT * FROM hd_validations WHERE id = ?", (job["validation_id"],)).fetchone()
    return submit_hd_job(
        db_path, output_root, job["shot_id"],
        HDSubmitRequest(
            validation_id=job["validation_id"], expected_validation_hash=validation["validation_hash"],
            idempotency_key=idempotency_key, confirm=True,
        ),
    )


def save_hd_review(
    db_path: Path, output_root: Path, shot_id: str, payload: HDReviewRequest, probe: MediaProbe | None = None,
) -> dict[str, Any]:
    if payload.decision != "pass" and not payload.note.strip():
        raise HTTPException(422, "拒绝或保留修改时必须写明原因")
    scores = payload.scores.model_dump()
    if payload.decision == "pass" and min(scores.values()) < 3:
        raise HTTPException(422, "存在低于 3 分的维度，不能通过高清审片")
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        row = db.execute(
            "SELECT * FROM hd_artifacts WHERE id = ? AND shot_id = ? AND project_id = ?", (payload.artifact_id, shot_id, project["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "当前镜头中没有这个高清版本")
        artifact = _artifact_public(row)
        path = _allowed_media(artifact["output_path"], output_root)
        snapshot = _file_snapshot(path)
        if snapshot["checksum_sha256"] != artifact["output_sha256"]:
            raise HTTPException(409, "高清媒体已变化，请重新生成")
        technical = (probe or _probe or _default_probe)(path)
        if payload.decision == "pass" and not technical.get("ok"):
            raise HTTPException(422, "高清媒体技术检查未通过")
        duration = float(technical.get("duration_seconds") or artifact["duration_seconds"] or 0)
        watched_required = max(0.0, min(duration * 0.8, duration - 0.35))
        if payload.decision == "pass" and payload.watched_seconds + 0.01 < watched_required:
            raise HTTPException(422, f"通过前至少需观看 {watched_required:.2f} 秒")
        if payload.decision == "pass" and STRATEGY_DETAILS[artifact["strategy_type"]]["drift_review_required"] and not payload.drift_confirmed:
            raise HTTPException(422, "模型重生成存在漂移风险，必须人工确认与低清母版的差异")
        revision = int(db.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM hd_artifact_reviews WHERE artifact_id = ?", (artifact["id"],),
        ).fetchone()[0])
        review_id = f"hd-review-{uuid.uuid4().hex[:12]}"
        frozen = {"artifact_id": artifact["id"], "plan_hash": artifact["plan_hash"], **snapshot}
        db.execute(
            """INSERT INTO hd_artifact_reviews
            (id, project_id, shot_id, artifact_id, revision, decision, scores, drift_confirmed,
             note, watched_seconds, artifact_snapshot, media_probe, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                review_id, project["id"], shot_id, artifact["id"], revision, payload.decision,
                json.dumps(scores, ensure_ascii=False, sort_keys=True), int(payload.drift_confirmed), payload.note.strip(),
                payload.watched_seconds, json.dumps(frozen, ensure_ascii=False, sort_keys=True),
                json.dumps(technical, ensure_ascii=False, sort_keys=True), utc_now(),
            ),
        )
        saved = db.execute("SELECT * FROM hd_artifact_reviews WHERE id = ?", (review_id,)).fetchone()
        db.commit()
        result = dict(saved)
        result["scores"] = _json(result["scores"], {})
        result["drift_confirmed"] = bool(result["drift_confirmed"])
        result["artifact_snapshot"] = _json(result["artifact_snapshot"], {})
        result["media_probe"] = _json(result["media_probe"], {})
        return result


def _record_hd_selection(
    db_path: Path, output_root: Path, shot_id: str, artifact_id: str, base_revision: int,
    note: str, action: Literal["select", "rollback"], rollback_of_revision: int | None = None,
) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        artifact_row = db.execute(
            "SELECT * FROM hd_artifacts WHERE id = ? AND shot_id = ? AND project_id = ?", (artifact_id, shot_id, project["id"]),
        ).fetchone()
        if not artifact_row:
            raise HTTPException(404, "当前镜头中没有这个高清版本")
        artifact = _artifact_public(artifact_row)
        current_revision = int(db.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM hd_master_versions WHERE shot_id = ?", (shot_id,),
        ).fetchone()[0])
        if base_revision != current_revision:
            raise HTTPException(409, f"高清定稿已变化，当前修订为 {current_revision}")
        review = db.execute(
            "SELECT * FROM hd_artifact_reviews WHERE artifact_id = ? ORDER BY revision DESC LIMIT 1", (artifact_id,),
        ).fetchone()
        if not review or review["decision"] != "pass":
            raise HTTPException(409, "高清版本尚未通过最新审片")
        path = _allowed_media(artifact["output_path"], output_root)
        media = _file_snapshot(path)
        reviewed = _json(review["artifact_snapshot"], {})
        if reviewed.get("checksum_sha256") != media["checksum_sha256"] or artifact["output_sha256"] != media["checksum_sha256"]:
            raise HTTPException(409, "高清审片后的媒体已变化")
        version_id = f"hd-master-{uuid.uuid4().hex[:12]}"
        revision = current_revision + 1
        frozen = {"artifact": artifact, "review_revision": int(review["revision"]), "media": media}
        db.execute(
            """INSERT INTO hd_master_versions
            (id, project_id, shot_id, revision, artifact_id, action, rollback_of_revision, review_id,
             review_revision, note, artifact_snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id, project["id"], shot_id, revision, artifact_id, action, rollback_of_revision,
                review["id"], review["revision"], note.strip(), json.dumps(frozen, ensure_ascii=False, sort_keys=True), utc_now(),
            ),
        )
        row = db.execute("SELECT * FROM hd_master_versions WHERE id = ?", (version_id,)).fetchone()
        db.commit()
        result = dict(row)
        result["artifact_snapshot"] = _json(result["artifact_snapshot"], {})
        result["current"] = True
        return result


def select_hd_artifact(db_path: Path, output_root: Path, shot_id: str, payload: HDSelectRequest) -> dict[str, Any]:
    return _record_hd_selection(db_path, output_root, shot_id, payload.artifact_id, payload.base_revision, payload.note, "select")


def rollback_hd_master(db_path: Path, output_root: Path, shot_id: str, payload: HDRollbackRequest) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(400, "回滚高清定稿前必须明确确认")
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        target = db.execute(
            "SELECT artifact_id FROM hd_master_versions WHERE project_id = ? AND shot_id = ? AND revision = ?",
            (project["id"], shot_id, payload.target_revision),
        ).fetchone()
        if not target:
            raise HTTPException(404, "目标高清定稿版本不存在")
    return _record_hd_selection(
        db_path, output_root, shot_id, target["artifact_id"], payload.base_revision,
        payload.note, "rollback", payload.target_revision,
    )


def _review_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["scores"] = _json(item.get("scores"), {})
    item["drift_confirmed"] = bool(item.get("drift_confirmed"))
    item["artifact_snapshot"] = _json(item.get("artifact_snapshot"), {})
    item["media_probe"] = _json(item.get("media_probe"), {})
    return item


def hd_shot_workspace(db_path: Path, output_root: Path, shot_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        shot = db.execute("SELECT * FROM shots WHERE id = ? AND project_id = ?", (shot_id, project["id"])).fetchone()
        if not shot:
            raise HTTPException(404, "当前项目中没有这个镜头")
        try:
            master = _current_master_source(db, project["id"], shot_id, output_root)
            master_issue = None
        except HTTPException as exc:
            master = None
            master_issue = str(exc.detail)
        plans = [_plan_public(row) for row in db.execute(
            "SELECT * FROM hd_strategy_versions WHERE project_id = ? AND shot_id = ? ORDER BY revision DESC", (project["id"], shot_id),
        ).fetchall()]
        validations = [_validation_public(row) for row in db.execute(
            "SELECT * FROM hd_validations WHERE project_id = ? AND shot_id = ? ORDER BY created_at DESC", (project["id"], shot_id),
        ).fetchall()]
        jobs = [_job_public(row) for row in db.execute(
            "SELECT * FROM hd_generation_jobs WHERE project_id = ? AND shot_id = ? ORDER BY created_at DESC", (project["id"], shot_id),
        ).fetchall()]
        artifacts = [_artifact_public(row) for row in db.execute(
            "SELECT * FROM hd_artifacts WHERE project_id = ? AND shot_id = ? ORDER BY version DESC", (project["id"], shot_id),
        ).fetchall()]
        reviews = [_review_public(row) for row in db.execute(
            "SELECT * FROM hd_artifact_reviews WHERE project_id = ? AND shot_id = ? ORDER BY created_at DESC", (project["id"], shot_id),
        ).fetchall()]
        versions = []
        for row in db.execute(
            "SELECT * FROM hd_master_versions WHERE project_id = ? AND shot_id = ? ORDER BY revision DESC", (project["id"], shot_id),
        ).fetchall():
            item = dict(row)
            item["artifact_snapshot"] = _json(item["artifact_snapshot"], {})
            item["current"] = not versions
            versions.append(item)
        latest_plan = plans[0] if plans else None
        latest_validation = next((item for item in validations if latest_plan and item["plan_id"] == latest_plan["id"]), None)
        latest_master = versions[0] if versions else None
        return {
            "project": {"id": project["id"], "title": project["title"]},
            "shot": {"id": shot["id"], "title": shot["title"], "ordinal": shot["ordinal"]},
            "locked_draft_master": master, "master_issue": master_issue,
            "strategies": [_strategy_public(key) for key in STRATEGY_DETAILS],
            "plans": plans, "validations": validations, "jobs": jobs,
            "artifacts": artifacts, "reviews": reviews, "master_versions": versions,
            "current_plan": latest_plan, "current_validation": latest_validation, "current_hd_master": latest_master,
            "summary": {
                "artifact_count": len(artifacts),
                "passed_count": sum(item["decision"] == "pass" for item in reviews),
                "active_job_count": sum(item["state"] in ACTIVE_JOB_STATES for item in jobs),
                "ready_for_assembly": bool(latest_master),
            },
        }


def hd_project_workspace(db_path: Path, output_root: Path) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        shots = [dict(row) for row in db.execute("SELECT id, title, ordinal FROM shots WHERE project_id = ? ORDER BY ordinal", (project["id"],)).fetchall()]
    entries = [hd_shot_workspace(db_path, output_root, shot["id"]) for shot in shots]
    return {
        "project": {"id": project["id"], "title": project["title"], "episode": project.get("episode")},
        "shots": entries,
        "summary": {
            "shot_count": len(entries),
            "locked_master_count": sum(bool(item["locked_draft_master"]) for item in entries),
            "hd_selected_count": sum(bool(item["current_hd_master"]) for item in entries),
            "active_job_count": sum(item["summary"]["active_job_count"] for item in entries),
        },
    }


def selected_hd_source(db: sqlite3.Connection, shot_id: str, output_root: Path) -> dict[str, Any] | None:
    if not _table_exists(db, "hd_master_versions"):
        return None
    row = db.execute(
        """SELECT versions.id AS hd_master_version_id, versions.revision AS hd_master_revision,
        versions.review_id, versions.review_revision, versions.artifact_snapshot,
        artifacts.* FROM hd_master_versions versions JOIN hd_artifacts artifacts ON artifacts.id = versions.artifact_id
        WHERE versions.shot_id = ? ORDER BY versions.revision DESC LIMIT 1""",
        (shot_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    path = _allowed_media(item["output_path"], output_root)
    media = _file_snapshot(path)
    if media["checksum_sha256"] != item["output_sha256"]:
        raise HTTPException(409, "已选高清版本媒体与冻结 SHA-256 不一致")
    return {
        "hd_artifact_id": item["id"], "hd_master_version_id": item["hd_master_version_id"],
        "hd_master_revision": int(item["hd_master_revision"]), "hd_review_id": item["review_id"],
        "hd_review_revision": int(item["review_revision"]), "strategy_type": item["strategy_type"],
        "strategy_kind": item["strategy_kind"], "source_candidate_id": item["source_candidate_id"],
        "source_master_version_id": item["source_master_version_id"], "plan_id": item["plan_id"],
        "plan_hash": item["plan_hash"], "prompt": item["prompt"], "seed": item["seed"],
        "spec": _json(item["spec"], {}), "model_id": item["model_id"], "workflow_id": item["workflow_id"],
        "references": _json(item["reference_snapshot"], []), "media": media,
        "provenance": _json(item["provenance"], {}),
    }


def create_hd_router(db_path: Path, output_root: Path, runner: HDRunner) -> APIRouter:
    router = APIRouter()

    @router.get("/api/hd/workspace")
    def api_hd_workspace() -> dict[str, Any]:
        return hd_project_workspace(db_path, output_root)

    @router.get("/api/hd/shots/{shot_id}")
    def api_hd_shot(shot_id: str) -> dict[str, Any]:
        return hd_shot_workspace(db_path, output_root, shot_id)

    @router.post("/api/hd/shots/{shot_id}/plans")
    def api_create_plan(shot_id: str, payload: HDPlanCreate) -> dict[str, Any]:
        return create_hd_plan(db_path, output_root, shot_id, payload)

    @router.post("/api/hd/validations")
    def api_validate_plan(payload: HDValidationRequest) -> dict[str, Any]:
        return validate_hd_plan(db_path, output_root, payload, runner)

    @router.post("/api/hd/shots/{shot_id}/jobs")
    def api_submit_job(shot_id: str, payload: HDSubmitRequest) -> dict[str, Any]:
        return submit_hd_job(db_path, output_root, shot_id, payload)

    @router.post("/api/hd/jobs/{job_id}/resolve")
    def api_resolve_job(job_id: str, payload: HDUnknownResolution) -> dict[str, Any]:
        return resolve_unknown_job(db_path, job_id, payload)

    @router.post("/api/hd/jobs/{job_id}/retry")
    def api_retry_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return retry_hd_job(db_path, output_root, job_id, str(payload.get("idempotency_key") or ""), bool(payload.get("confirm")))

    @router.post("/api/hd/shots/{shot_id}/reviews")
    def api_save_review(shot_id: str, payload: HDReviewRequest) -> dict[str, Any]:
        return save_hd_review(db_path, output_root, shot_id, payload)

    @router.post("/api/hd/shots/{shot_id}/select")
    def api_select(shot_id: str, payload: HDSelectRequest) -> dict[str, Any]:
        return select_hd_artifact(db_path, output_root, shot_id, payload)

    @router.post("/api/hd/shots/{shot_id}/rollback")
    def api_rollback(shot_id: str, payload: HDRollbackRequest) -> dict[str, Any]:
        return rollback_hd_master(db_path, output_root, shot_id, payload)

    @router.get("/api/hd/artifacts/{artifact_id}/video")
    def api_artifact_video(artifact_id: str) -> FileResponse:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            row = db.execute("SELECT output_path FROM hd_artifacts WHERE id = ? AND project_id = ?", (artifact_id, project["id"])).fetchone()
            if not row:
                raise HTTPException(404, "当前项目中没有这个高清版本")
        path = _allowed_media(row["output_path"], output_root)
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    return router
