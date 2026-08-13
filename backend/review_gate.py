from __future__ import annotations

import json
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
    return {
        "candidate_id": candidate["id"],
        "shot_id": candidate["shot_id"],
        "external_id": candidate.get("external_id"),
        "prompt_id": candidate.get("prompt_id"),
        "output_file": str(path),
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
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
        stale = any(current.get(key) != snapshot.get(key) for key in ("output_file", "size_bytes", "modified_ns", "prompt_id"))
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
        for candidate in candidates:
            latest = db.execute(
                "SELECT * FROM candidate_reviews WHERE candidate_id = ? ORDER BY revision DESC LIMIT 1",
                (candidate["id"],),
            ).fetchone()
            reviews.append(_review_public(latest, candidate, output_root))
        return {
            "shot_id": shot_id,
            "reviews": reviews,
            "summary": {
                "candidate_count": len(candidates),
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


def create_review_router(db_path: Path, output_root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/shots/{shot_id}/review-workspace")
    def api_review_workspace(shot_id: str) -> dict[str, Any]:
        return review_workspace(db_path, shot_id, output_root)

    @router.post("/api/shots/{shot_id}/candidate-reviews")
    def api_save_review(shot_id: str, payload: CandidateReviewRequest) -> dict[str, Any]:
        return save_candidate_review(db_path, output_root, shot_id, payload)

    return router
