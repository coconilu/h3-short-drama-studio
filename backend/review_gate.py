from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import subprocess
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


SCORE_KEYS = ("story_match", "continuity", "action", "visual_quality", "audio_quality")
ISSUE_CODES = {
    "identity_drift", "costume_drift", "location_drift", "action_error", "camera_error",
    "artifact", "pseudo_text", "dialogue_error", "audio_noise", "audio_missing", "timing_error", "other",
}
Probe = Callable[[Path], dict[str, Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone())


def _json_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def init_review_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_reviews (
          id TEXT PRIMARY KEY,
          candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('pass', 'needs_changes', 'reject')),
          scores TEXT NOT NULL,
          issues TEXT NOT NULL,
          note TEXT NOT NULL,
          watched_seconds REAL NOT NULL,
          audio_checks TEXT NOT NULL DEFAULT '{}',
          candidate_snapshot TEXT NOT NULL,
          media_probe TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(candidate_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_reviews_latest
          ON candidate_reviews(candidate_id, revision DESC);
        CREATE TABLE IF NOT EXISTS candidate_master_versions (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE RESTRICT,
          action TEXT NOT NULL CHECK(action IN ('select', 'rollback')),
          rollback_of_revision INTEGER,
          review_id TEXT NOT NULL REFERENCES candidate_reviews(id) ON DELETE RESTRICT,
          review_revision INTEGER NOT NULL,
          note TEXT NOT NULL,
          candidate_snapshot TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(shot_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_master_versions_shot
          ON candidate_master_versions(shot_id, revision DESC);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(candidate_reviews)").fetchall()}
    if "audio_checks" not in columns:
        db.execute("ALTER TABLE candidate_reviews ADD COLUMN audio_checks TEXT NOT NULL DEFAULT '{}'")


def probe_candidate_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, f"无法执行候选媒体 QC：{exc}") from exc
    if completed.returncode != 0:
        raise HTTPException(422, completed.stderr.strip() or "候选视频无法被 ffprobe 识别")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "ffprobe 返回了无效候选信息") from exc
    streams = raw.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    format_info = raw.get("format") or {}
    duration = format_info.get("duration")
    audio_analysis = analyze_candidate_audio(path, float(duration or 0)) if audio else None
    result = {
        "ok": bool(video) and bool(audio) and duration not in (None, "N/A") and float(duration) > 0
        and bool(audio_analysis and audio_analysis.get("ok")),
        "duration_seconds": round(float(duration), 3) if duration not in (None, "N/A") else None,
        "size_bytes": int(format_info.get("size") or path.stat().st_size),
        "bit_rate": int(format_info.get("bit_rate") or 0),
        "video": {
            "codec": video.get("codec_name"), "width": video.get("width"), "height": video.get("height"),
            "frame_rate": video.get("r_frame_rate"),
        },
        "audio": {
            "present": bool(audio), "codec": audio.get("codec_name"), "channels": audio.get("channels"),
            "sample_rate": int(audio.get("sample_rate") or 0),
        },
        "audio_analysis": audio_analysis,
        "issues": [],
    }
    if not video:
        result["issues"].append("没有视频流")
    if not audio:
        result["issues"].append("没有音轨")
    if duration in (None, "N/A") or float(duration or 0) <= 0:
        result["issues"].append("媒体时长无效")
    if audio_analysis and not audio_analysis.get("ok"):
        result["issues"].extend(audio_analysis.get("issues") or [])
    return result


def analyze_candidate_audio(path: Path, duration_seconds: float) -> dict[str, Any]:
    command = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-map", "0:a:0", "-af", "silencedetect=noise=-45dB:d=0.5,volumedetect", "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, f"无法执行候选声音 QC：{exc}") from exc
    if completed.returncode != 0:
        raise HTTPException(422, completed.stderr[-2000:] or "候选音轨无法完成 QC")
    output = completed.stderr

    def db_value(label: str) -> float | None:
        match = re.search(rf"{label}:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", output)
        if not match or match.group(1) == "-inf":
            return None
        return float(match.group(1))

    silence_seconds = round(sum(float(value) for value in re.findall(r"silence_duration:\s*(\d+(?:\.\d+)?)", output)), 3)
    silence_ratio = round(min(1.0, silence_seconds / duration_seconds), 4) if duration_seconds > 0 else 1.0
    mean_volume = db_value("mean_volume")
    max_volume = db_value("max_volume")
    issues: list[str] = []
    if mean_volume is None:
        issues.append("音轨没有可测量信号")
    elif mean_volume < -45:
        issues.append("平均响度过低")
    if silence_ratio >= 0.98:
        issues.append("音轨几乎全程静音")
    if max_volume is not None and max_volume > -0.1:
        issues.append("峰值接近削波")
    return {
        "ok": mean_volume is not None and silence_ratio < 0.98,
        "mean_volume_db": mean_volume,
        "max_volume_db": max_volume,
        "silence_seconds": silence_seconds,
        "silence_ratio": silence_ratio,
        "issues": issues,
        "thresholds": {"silence_db": -45, "silence_min_seconds": 0.5},
    }


class ReviewScores(BaseModel):
    story_match: int = Field(ge=1, le=5)
    continuity: int = Field(ge=1, le=5)
    action: int = Field(ge=1, le=5)
    visual_quality: int = Field(ge=1, le=5)
    audio_quality: int = Field(ge=1, le=5)


class AudioChecks(BaseModel):
    dialogue_match: Literal["pending", "pass", "fail", "not_applicable"] = "pending"
    lip_sync: Literal["pending", "pass", "fail", "not_applicable"] = "pending"
    ambience: Literal["pending", "pass", "fail"] = "pending"


class CandidateReviewRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=240)
    decision: Literal["pass", "needs_changes", "reject"]
    scores: ReviewScores
    audio_checks: AudioChecks = Field(default_factory=AudioChecks)
    issues: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field("", max_length=2000)
    watched_seconds: float = Field(0, ge=0, le=3600)


class CandidateMasterRollbackRequest(BaseModel):
    target_revision: int = Field(ge=1)
    base_revision: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=2000)
    confirm: bool = False


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    setting = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
    if not setting:
        raise HTTPException(404, "当前没有已激活的项目")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (setting["value"],)).fetchone()
    if not project:
        raise HTTPException(404, "当前项目不存在")
    return dict(project)


def _candidate_snapshot(candidate: dict[str, Any], path: Path) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "candidate_id": candidate["id"],
        "shot_id": candidate["shot_id"],
        "external_id": candidate.get("external_id"),
        "prompt_id": candidate.get("prompt_id"),
        "output_file": str(path),
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "checksum_sha256": digest.hexdigest(),
    }


def _allowed_candidate_path(candidate: dict[str, Any], output_root: Path) -> Path:
    if candidate.get("source") != "h3" or not candidate.get("output_file"):
        raise HTTPException(409, "只有真实且已完成的 H3 候选可以进入生产审片")
    path = Path(candidate["output_file"]).resolve()
    try:
        path.relative_to(output_root.resolve())
    except ValueError as exc:
        raise HTTPException(403, "候选视频不在允许的 ComfyUI 输出目录") from exc
    if not path.is_file():
        raise HTTPException(404, "候选视频文件不存在")
    return path


def _review_public(review: sqlite3.Row | dict[str, Any] | None, candidate: dict[str, Any], output_root: Path) -> dict[str, Any]:
    if not review:
        return {
            "candidate_id": candidate["id"], "status": "pending", "revision": 0,
            "decision": None, "scores": {}, "issues": [], "note": "", "watched_seconds": 0,
            "audio_checks": {}, "media_probe": None, "stale": False, "can_select": False,
        }
    item = dict(review)
    item["scores"] = json.loads(item.get("scores") or "{}")
    item["issues"] = json.loads(item.get("issues") or "[]")
    item["audio_checks"] = json.loads(item.get("audio_checks") or "{}")
    item["media_probe"] = json.loads(item.get("media_probe") or "{}")
    snapshot = json.loads(item.get("candidate_snapshot") or "{}")
    stale = True
    try:
        path = _allowed_candidate_path(candidate, output_root)
        current = _candidate_snapshot(candidate, path)
        stale = any(
            current.get(key) != snapshot.get(key)
            for key in ("output_file", "size_bytes", "modified_ns", "prompt_id", "checksum_sha256")
        )
    except HTTPException:
        stale = True
    item["status"] = "stale" if stale else "reviewed"
    item["stale"] = stale
    item["can_select"] = item["decision"] == "pass" and not stale and bool(item["media_probe"].get("ok"))
    item.pop("candidate_snapshot", None)
    item.pop("project_id", None)
    return item


def latest_candidate_review(db_path: Path, candidate_id: str, output_root: Path) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        candidate_row = db.execute("SELECT * FROM candidates WHERE id = ? AND archived = 0", (candidate_id,)).fetchone()
        if not candidate_row:
            raise HTTPException(404, "候选不存在")
        candidate = dict(candidate_row)
        review = db.execute(
            "SELECT * FROM candidate_reviews WHERE candidate_id = ? ORDER BY revision DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        return _review_public(review, candidate, output_root)


def _review_history(
    db: sqlite3.Connection, candidate: dict[str, Any], output_root: Path,
) -> list[dict[str, Any]]:
    return [
        _review_public(review, candidate, output_root)
        for review in db.execute(
            "SELECT * FROM candidate_reviews WHERE candidate_id = ? ORDER BY revision DESC", (candidate["id"],),
        ).fetchall()
    ]


def _candidate_trace(
    db: sqlite3.Connection, candidate: dict[str, Any], output_root: Path,
) -> dict[str, Any]:
    media_path: Path | None = None
    media_reason: str | None = None
    try:
        media_path = _allowed_candidate_path(candidate, output_root)
    except HTTPException as exc:
        media_reason = str(exc.detail)
    metadata = {}
    try:
        metadata = json.loads(candidate.get("metadata") or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    external_id = str(candidate.get("external_id") or "")
    prompt_id = str(candidate.get("prompt_id") or "")
    associations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if _table_exists(db, "production_item_attempts") and _table_exists(db, "jobs"):
        for attempt_row in db.execute(
            """SELECT * FROM production_item_attempts
            WHERE shot_id = ? AND state = 'completed' AND draft_job_id IS NOT NULL ORDER BY rowid""",
            (candidate["shot_id"],),
        ).fetchall():
            attempt = dict(attempt_row)
            attempt_candidate_ids = _json_list(attempt.get("candidate_ids"))
            if not external_id or external_id not in attempt_candidate_ids:
                continue
            job_row = db.execute("SELECT * FROM jobs WHERE id = ? AND kind = 'draft'", (attempt["draft_job_id"],)).fetchone()
            if not job_row:
                continue
            job = dict(job_row)
            if (
                job.get("shot_id") == candidate["shot_id"]
                and external_id in _json_list(job.get("candidate_ids"))
                and prompt_id in _json_list(job.get("prompt_ids"))
            ):
                associations.append((attempt, job))
    attempt, job = associations[0] if len(associations) == 1 else ({}, {})
    source_snapshot = {}
    if job:
        try:
            source_snapshot = json.loads(job.get("source_snapshot") or "{}")
        except (TypeError, json.JSONDecodeError):
            pass
    arguments = source_snapshot.get("arguments") if isinstance(source_snapshot, dict) else None
    arguments = arguments if isinstance(arguments, list) else []
    def argument_value(flag: str) -> Any:
        try:
            return arguments[arguments.index(flag) + 1]
        except (ValueError, IndexError):
            return None
    prompt = metadata.get("prompt") or argument_value("--prompt")
    width = metadata.get("width") or argument_value("--width")
    height = metadata.get("height") or argument_value("--height")
    seconds = metadata.get("actual_seconds") or argument_value("--seconds")
    snapshot = _candidate_snapshot(candidate, media_path) if media_path else {}
    attempt_media = []
    try:
        attempt_media = json.loads(attempt.get("media_evidence") or "[]") if attempt else []
    except (TypeError, json.JSONDecodeError):
        attempt_media = []
    owned_media = [item for item in attempt_media if str(item.get("candidate_id") or "") == external_id]
    media_association_ok = bool(
        len(owned_media) == 1
        and owned_media[0].get("checksum_sha256") == snapshot.get("checksum_sha256")
        and owned_media[0].get("output_file") == snapshot.get("output_file")
        and owned_media[0].get("seed") == candidate.get("seed")
    )
    complete = bool(
        media_path and candidate.get("status") == "completed" and len(associations) == 1
        and attempt.get("plan_hash") and attempt.get("validation_hash")
        and job.get("plan_hash") == attempt.get("validation_hash")
        and job.get("state") == "完成"
        and prompt_id in _json_list(attempt.get("prompt_ids"))
        and media_association_ok
        and prompt and prompt_id and external_id and candidate.get("seed") is not None
        and width and height and seconds
    )
    debt_reasons: list[str] = []
    if media_reason:
        debt_reasons.append(media_reason)
    if len(associations) != 1:
        debt_reasons.append("missing_or_ambiguous_attempt_association")
    if not (attempt.get("plan_hash") and attempt.get("validation_hash")):
        debt_reasons.append("missing_plan_hash")
    if job and job.get("state") != "完成":
        debt_reasons.append("draft_job_not_completed")
    if attempt and prompt_id not in _json_list(attempt.get("prompt_ids")):
        debt_reasons.append("prompt_not_owned_by_attempt")
    if not media_association_ok:
        debt_reasons.append("missing_or_stale_attempt_media_evidence")
    if not prompt:
        debt_reasons.append("missing_prompt")
    if not (prompt_id and external_id):
        debt_reasons.append("missing_prompt_or_candidate_id")
    if candidate.get("seed") is None:
        debt_reasons.append("missing_seed")
    if not (width and height and seconds):
        debt_reasons.append("missing_spec")
    return {
        "evidence_status": "verified" if complete else "historical_debt",
        "debt_reason": ",".join(dict.fromkeys(debt_reasons)) or None,
        "prompt": prompt,
        "seed": candidate.get("seed"),
        "spec": {
            "width": int(width) if width else None,
            "height": int(height) if height else None,
            "actual_seconds": float(seconds) if seconds else None,
        },
        "plan_hash": attempt.get("plan_hash"),
        "validation_hash": attempt.get("validation_hash"),
        "h3_project": job.get("h3_project"),
        "draft_job_id": job.get("id"),
        "production_attempt_id": attempt.get("id"),
        "prompt_id": prompt_id or None,
        "candidate_id": external_id or None,
        "media": {
            "output_file": str(media_path) if media_path else candidate.get("output_file"),
            "file_exists": bool(media_path),
            "size_bytes": media_path.stat().st_size if media_path else None,
            "checksum_sha256": snapshot.get("checksum_sha256"),
        },
    }


def _master_versions(db: sqlite3.Connection, shot_id: str) -> list[dict[str, Any]]:
    versions = []
    for row in db.execute(
        """SELECT versions.*, candidates.label, candidates.status AS candidate_status,
        candidates.selected AS candidate_selected
        FROM candidate_master_versions versions JOIN candidates ON candidates.id = versions.candidate_id
        WHERE versions.shot_id = ? ORDER BY versions.revision DESC""",
        (shot_id,),
    ).fetchall():
        item = dict(row)
        item["candidate_snapshot"] = json.loads(item.get("candidate_snapshot") or "{}")
        item["current"] = False
        versions.append(item)
    if versions and versions[0].get("candidate_selected"):
        versions[0]["current"] = True
    return versions


def review_workspace(db_path: Path, shot_id: str, output_root: Path) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        shot = db.execute("SELECT * FROM shots WHERE id = ? AND project_id = ?", (shot_id, project["id"])).fetchone()
        if not shot:
            raise HTTPException(404, "当前项目中没有这个镜头")
        candidates = [
            dict(candidate)
            for candidate in db.execute(
                "SELECT * FROM candidates WHERE shot_id = ? AND archived = 0 ORDER BY created_at, label",
                (shot_id,),
            ).fetchall()
        ]
        reviews: list[dict[str, Any]] = []
        comparison: list[dict[str, Any]] = []
        for candidate in candidates:
            history = _review_history(db, candidate, output_root)
            # Keep the latest public review independent from the history list.
            # Reusing history[0] here makes latest["history"] point back to the
            # list that already contains latest, which FastAPI cannot serialize.
            latest = dict(history[0]) if history else _review_public(None, candidate, output_root)
            latest["history"] = history
            reviews.append(latest)
            comparison.append({
                "candidate_id": candidate["id"],
                "label": candidate.get("label"),
                "selected": bool(candidate.get("selected")),
                "status": candidate.get("status"),
                "trace": _candidate_trace(db, candidate, output_root),
            })
        masters = _master_versions(db, shot_id)
        legacy_selected = next((candidate for candidate in candidates if candidate.get("selected")), None)
        selected_debt = bool(legacy_selected and not masters)
        return {
            "shot_id": shot_id,
            "reviews": reviews,
            "comparison": comparison,
            "master_versions": masters,
            "selected_debt": {
                "active": selected_debt,
                "candidate_id": legacy_selected["id"] if selected_debt else None,
                "message": "历史母版没有 append-only 选择凭证；保留可用，但下一次切换必须通过结构化审片。" if selected_debt else None,
            },
            "summary": {
                "candidate_count": len(candidates),
                "comparable_count": sum(item["trace"]["evidence_status"] == "verified" for item in comparison),
                "evidence_debt_count": sum(item["trace"]["evidence_status"] == "historical_debt" for item in comparison),
                "passed_count": sum(item["can_select"] for item in reviews),
                "needs_changes_count": sum(item.get("decision") == "needs_changes" and not item["stale"] for item in reviews),
                "rejected_count": sum(item.get("decision") == "reject" and not item["stale"] for item in reviews),
                "pending_count": sum(item["status"] == "pending" or item["stale"] for item in reviews),
            },
        }


def save_candidate_review(
    db_path: Path,
    output_root: Path,
    shot_id: str,
    payload: CandidateReviewRequest,
    probe: Probe = probe_candidate_media,
) -> dict[str, Any]:
    unknown_issues = set(payload.issues) - ISSUE_CODES
    if unknown_issues:
        raise HTTPException(422, "不支持的审片问题类型：" + "、".join(sorted(unknown_issues)))
    if payload.decision != "pass" and not payload.note.strip():
        raise HTTPException(422, "保留修改或拒绝时必须写明问题，供下一轮生成使用")
    scores = payload.scores.model_dump()
    audio_checks = payload.audio_checks.model_dump()
    if payload.decision == "pass" and min(scores.values()) < 3:
        raise HTTPException(422, "存在低于 3 分的维度，不能标记通过")

    with closing(connect(db_path)) as db:
        project = _active_project(db)
        candidate_row = db.execute(
            """SELECT candidates.*, shots.project_id, shots.dialogue FROM candidates JOIN shots ON shots.id = candidates.shot_id
            WHERE candidates.id = ? AND candidates.shot_id = ? AND candidates.archived = 0""",
            (payload.candidate_id, shot_id),
        ).fetchone()
        if not candidate_row or candidate_row["project_id"] != project["id"]:
            raise HTTPException(404, "当前镜头中没有这个候选")
        candidate = dict(candidate_row)
        if candidate.get("status") != "completed":
            raise HTTPException(409, "候选尚未生成完成")
        path = _allowed_candidate_path(candidate, output_root)
        media_probe = probe(path)
        if payload.decision == "pass" and not media_probe.get("ok"):
            raise HTTPException(422, "媒体技术 QC 未通过，不能标记通过")
        if payload.decision == "pass":
            required_audio_checks = {"ambience": "pass"}
            if candidate.get("dialogue", "").strip():
                required_audio_checks.update({"dialogue_match": "pass", "lip_sync": "pass"})
            elif audio_checks["dialogue_match"] not in ("pass", "not_applicable") or audio_checks["lip_sync"] not in ("pass", "not_applicable"):
                raise HTTPException(422, "无对白镜头请将对白与口型检查设为不适用或通过")
            failed_checks = [name for name, expected in required_audio_checks.items() if audio_checks.get(name) != expected]
            if failed_checks:
                raise HTTPException(422, "声音人工检查尚未通过：" + "、".join(failed_checks))
        duration = float(media_probe.get("duration_seconds") or 0)
        watched_required = max(0.0, min(duration * 0.8, duration - 0.35))
        if payload.decision == "pass" and payload.watched_seconds + 0.01 < watched_required:
            raise HTTPException(422, f"通过前至少需观看 {watched_required:.2f} 秒，当前记录 {payload.watched_seconds:.2f} 秒")
        latest = db.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM candidate_reviews WHERE candidate_id = ?",
            (candidate["id"],),
        ).fetchone()["revision"]
        now = utc_now()
        review_id = f"candidate-review-{uuid.uuid4().hex[:12]}"
        db.execute(
            """INSERT INTO candidate_reviews
            (id, candidate_id, shot_id, project_id, revision, decision, scores, issues, note,
             watched_seconds, audio_checks, candidate_snapshot, media_probe, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                review_id, candidate["id"], shot_id, project["id"], latest + 1, payload.decision,
                json.dumps(scores, ensure_ascii=False), json.dumps(payload.issues, ensure_ascii=False),
                payload.note.strip(), payload.watched_seconds, json.dumps(audio_checks, ensure_ascii=False),
                json.dumps(_candidate_snapshot(candidate, path), ensure_ascii=False),
                json.dumps(media_probe, ensure_ascii=False), now,
            ),
        )
        db.execute(
            "UPDATE candidates SET scores = ?, note = ? WHERE id = ?",
            (json.dumps(scores, ensure_ascii=False), payload.note.strip(), candidate["id"]),
        )
        db.commit()
        review = db.execute("SELECT * FROM candidate_reviews WHERE id = ?", (review_id,)).fetchone()
        return _review_public(review, candidate, output_root)


def require_passed_review(db_path: Path, candidate_id: str, output_root: Path) -> dict[str, Any]:
    review = latest_candidate_review(db_path, candidate_id, output_root)
    if not review.get("can_select"):
        if review.get("stale"):
            raise HTTPException(409, "候选文件或生成来源已变化，请重新完成审片")
        raise HTTPException(409, "候选尚未通过结构化审片门禁")
    return review


def record_master_selection(
    db_path: Path,
    output_root: Path,
    shot_id: str,
    candidate_id: str,
    *,
    note: str,
    base_revision: int,
    action: Literal["select", "rollback"] = "select",
    rollback_of_revision: int | None = None,
) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        candidate_row = db.execute(
            """SELECT candidates.* FROM candidates JOIN shots ON shots.id = candidates.shot_id
            WHERE candidates.id = ? AND candidates.shot_id = ? AND shots.project_id = ?
            AND candidates.archived = 0""",
            (candidate_id, shot_id, project["id"]),
        ).fetchone()
        if not candidate_row:
            raise HTTPException(404, "当前镜头中没有这个候选")
        candidate = dict(candidate_row)
        current_revision = int(db.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM candidate_master_versions WHERE shot_id = ?", (shot_id,),
        ).fetchone()[0])
        if base_revision != current_revision:
            raise HTTPException(409, f"草稿母版已变化，当前修订为 {current_revision}")

        comparable = 0
        for row in db.execute(
            "SELECT * FROM candidates WHERE shot_id = ? AND archived = 0 AND status = 'completed'", (shot_id,),
        ).fetchall():
            trace = _candidate_trace(db, dict(row), output_root)
            if trace["evidence_status"] == "verified":
                comparable += 1
        if comparable < 2:
            raise HTTPException(409, "至少需要 2 条有完整且唯一生成证据的完成候选，才能选择草稿母版")

        latest_review = db.execute(
            "SELECT * FROM candidate_reviews WHERE candidate_id = ? ORDER BY revision DESC LIMIT 1", (candidate_id,),
        ).fetchone()
        if not latest_review or latest_review["decision"] != "pass":
            raise HTTPException(409, "候选尚未通过最新一版结构化审片")
        try:
            media_probe = json.loads(latest_review["media_probe"] or "{}")
            reviewed_snapshot = json.loads(latest_review["candidate_snapshot"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(409, "最新审片凭证损坏，请重新审片") from exc
        if not media_probe.get("ok"):
            raise HTTPException(409, "候选媒体技术检查未通过")
        path = _allowed_candidate_path(candidate, output_root)
        snapshot = _candidate_snapshot(candidate, path)
        if any(
            reviewed_snapshot.get(key) != snapshot.get(key)
            for key in ("candidate_id", "shot_id", "external_id", "prompt_id", "output_file", "size_bytes", "modified_ns", "checksum_sha256")
        ):
            raise HTTPException(409, "审片后的候选文件或来源已变化，请重新审片")
        trace = _candidate_trace(db, candidate, output_root)
        if trace["evidence_status"] != "verified":
            raise HTTPException(409, f"候选生成证据不完整：{trace.get('debt_reason') or 'unknown'}")
        # Re-read the file immediately before the first write. This catches a
        # same-path replacement between review validation and commit.
        if _candidate_snapshot(candidate, path) != snapshot:
            raise HTTPException(409, "选择母版期间候选文件发生变化，未写入任何状态")
        master_snapshot = {**snapshot, "trace": trace, "review_revision": int(latest_review["revision"])}
        now = utc_now()
        next_revision = current_revision + 1
        version_id = f"candidate-master-{uuid.uuid4().hex[:12]}"
        db.execute("UPDATE candidates SET selected = 0 WHERE shot_id = ?", (shot_id,))
        db.execute("UPDATE candidates SET selected = 1, note = ? WHERE id = ?", (note.strip(), candidate_id))
        db.execute(
            "UPDATE shots SET status = '草稿已选', updated_at = ? WHERE id = ? AND project_id = ?",
            (now, shot_id, project["id"]),
        )
        db.execute(
            """INSERT INTO candidate_master_versions
            (id, project_id, shot_id, revision, candidate_id, action, rollback_of_revision,
             review_id, review_revision, note, candidate_snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id, project["id"], shot_id, next_revision, candidate_id, action, rollback_of_revision,
                latest_review["id"], latest_review["revision"], note.strip(),
                json.dumps(master_snapshot, ensure_ascii=False, sort_keys=True), now,
            ),
        )
        db.commit()
        result = dict(db.execute(
            "SELECT * FROM candidate_master_versions WHERE id = ?", (version_id,),
        ).fetchone())
        result["candidate_snapshot"] = json.loads(result["candidate_snapshot"])
        result["current"] = True
        return result


def rollback_master_selection(
    db_path: Path, output_root: Path, shot_id: str, payload: CandidateMasterRollbackRequest,
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(400, "回滚草稿母版前必须显式确认")
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        target = db.execute(
            """SELECT * FROM candidate_master_versions
            WHERE shot_id = ? AND project_id = ? AND revision = ?""",
            (shot_id, project["id"], payload.target_revision),
        ).fetchone()
        if not target:
            raise HTTPException(404, "目标草稿母版版本不存在")
        target_candidate = target["candidate_id"]
    return record_master_selection(
        db_path,
        output_root,
        shot_id,
        target_candidate,
        note=payload.note,
        base_revision=payload.base_revision,
        action="rollback",
        rollback_of_revision=payload.target_revision,
    )


def create_review_router(db_path: Path, output_root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/shots/{shot_id}/review-workspace")
    def api_review_workspace(shot_id: str) -> dict[str, Any]:
        return review_workspace(db_path, shot_id, output_root)

    @router.post("/api/shots/{shot_id}/candidate-reviews")
    def api_save_review(shot_id: str, payload: CandidateReviewRequest) -> dict[str, Any]:
        return save_candidate_review(db_path, output_root, shot_id, payload)

    @router.post("/api/shots/{shot_id}/candidate-master/rollback")
    def api_rollback_master(shot_id: str, payload: CandidateMasterRollbackRequest) -> dict[str, Any]:
        return rollback_master_selection(db_path, output_root, shot_id, payload)

    return router
