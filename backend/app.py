from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from .content_planning import create_content_router, init_content_schema
    from .creative_storyboard import create_storyboard_router, init_storyboard_schema
    from .local_agents import create_local_agent_router, init_local_agent_schema, recover_local_agent_runs
    from .delivery_plan import create_delivery_router, init_delivery_schema, locked_delivery_plan
    from .project_archive import create_archive_router, init_archive_schema
    from .production_bible import create_bible_router, init_bible_schema, sync_creative_character_rules
    from .prompt_compiler import (
        approve_plan,
        begin_validation_lease,
        compile_prompt_plan,
        current_plan_input,
        end_validation_lease,
        init_prompt_schema,
        mark_prompt_plans_stale,
        project_prompt_status,
        public_plan,
        record_validation,
    )
    from .production_scheduler import (
        configure_production_scheduler,
        create_production_router,
        init_production_schema,
        start_production_worker,
        stop_production_worker,
    )
    from .review_gate import create_review_router, init_review_schema, require_passed_review
    from .script_workspace import create_script_router, init_script_schema
    from .runtime_control import request_supervisor_action, supervisor_status
except ImportError:  # Support `uvicorn app:app` when backend is the working directory.
    from content_planning import create_content_router, init_content_schema
    from creative_storyboard import create_storyboard_router, init_storyboard_schema
    from local_agents import create_local_agent_router, init_local_agent_schema, recover_local_agent_runs
    from delivery_plan import create_delivery_router, init_delivery_schema, locked_delivery_plan
    from project_archive import create_archive_router, init_archive_schema
    from production_bible import create_bible_router, init_bible_schema, sync_creative_character_rules
    from prompt_compiler import (
        approve_plan,
        begin_validation_lease,
        compile_prompt_plan,
        current_plan_input,
        end_validation_lease,
        init_prompt_schema,
        mark_prompt_plans_stale,
        project_prompt_status,
        public_plan,
        record_validation,
    )
    from production_scheduler import (
        configure_production_scheduler,
        create_production_router,
        init_production_schema,
        start_production_worker,
        stop_production_worker,
    )
    from review_gate import create_review_router, init_review_schema, require_passed_review
    from script_workspace import create_script_router, init_script_schema
    from runtime_control import request_supervisor_action, supervisor_status


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = (ROOT / "runtime").resolve()
API_STARTED_AT = datetime.now(timezone.utc)
API_STARTED_MONOTONIC = time.monotonic()
DB_PATH = Path(os.environ.get("JINGCHANG_DB", ROOT / "backend" / "studio.db"))
COMFY_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_ROOT = Path(
    os.environ.get(
        "COMFYUI_ROOT",
        r"E:\dev\comfyui_workbench\comfyui\ComfyUI-aki-v2\ComfyUI-aki-v2\ComfyUI",
    )
).resolve()
COMFY_OUTPUT_ROOT = (COMFY_ROOT / "output").resolve()
H3_PROJECT_ROOT = COMFY_OUTPUT_ROOT / "h3-draft-refine"


def resolve_h3_pipeline_script() -> Path:
    configured = os.environ.get("H3_PIPELINE_SCRIPT")
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = (
        ROOT / ".agents" / "skills" / "h3-video-draft-refine" / "scripts" / "h3_video_pipeline.py",
        Path.home() / ".codex" / "skills" / "h3-video-draft-refine" / "scripts" / "h3_video_pipeline.py",
        Path.home() / ".agents" / "skills" / "h3-video-draft-refine" / "scripts" / "h3_video_pipeline.py",
    )
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), candidates[0].resolve())


H3_SCRIPT = resolve_h3_pipeline_script()
FRONTEND_DIST = ROOT / "frontend" / "dist"
ASSET_ROOT = (ROOT / "runtime" / "assets").resolve()
EXPORT_ROOT = Path(os.environ.get("JINGCHANG_EXPORT_ROOT", ROOT / "runtime" / "exports")).resolve()
ROUGH_CUT_STEM = "rain-call-ep01-roughcut-v2-1"
EXPORT_SCRIPT = Path(
    os.environ.get("JINGCHANG_EXPORT_SCRIPT", ROOT / "scripts" / "export-roughcut.ps1")
).resolve()
API_BASE = os.environ.get("JINGCHANG_API_BASE", "http://127.0.0.1:8765").rstrip("/")
EXPORT_JOB_ROOT = (ROOT / "runtime" / "export-jobs").resolve()
BACKUP_ROOT = (ROOT / "runtime" / "backups").resolve()
EXPORT_TEST_DELAY_SECONDS = max(0.0, float(os.environ.get("JINGCHANG_EXPORT_TEST_DELAY_SECONDS", "0")))
EXPORT_TERMINAL_STATES = ("已完成", "失败", "已取消")
EXPORT_ACTIVE_STATES = ("排队中", "恢复排队", "导出中", "取消中")

EXPORT_STOP_EVENT = threading.Event()
EXPORT_WAKE_EVENT = threading.Event()
EXPORT_PROCESS_LOCK = threading.Lock()
EXPORT_PROCESSES: dict[str, subprocess.Popen[str]] = {}
EXPORT_WORKER_THREAD: threading.Thread | None = None
EXPORT_WORKER_ID = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"

RECONCILING_JOB_STATES = ("提交中", "已提交待对账", "提交状态未知")
ACTIVE_JOB_STATES = (*RECONCILING_JOB_STATES, "已提交", "排队中", "运行中")
SUBMISSION_BLOCKING_JOB_STATES = (*ACTIVE_JOB_STATES, "待人工对账")
ASSET_LIMITS = {"image": 50 * 1024 * 1024, "video": 2 * 1024 * 1024 * 1024, "audio": 250 * 1024 * 1024}
ASSET_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".webp"},
    "video": {".mp4", ".mov", ".mkv", ".webm"},
    "audio": {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"},
}
REFERENCE_LIMITS = {"image": 9, "video": 3, "audio": 3}
REFERENCE_ROLES = {
    "image": {"identity", "costume", "location", "style", "prop", "generic"},
    "video": {"action", "camera", "performance", "generic"},
    "audio": {"voice", "ambience", "effects", "music", "generic"},
}

DEFAULT_WORKSPACE_PREFERENCES: dict[str, Any] = {
    "sidebar_collapsed": False,
    "default_landing_page": "projects",
    "density": "comfortable",
    "comfyui_url": COMFY_URL,
    "default_export_width": 1344,
    "default_export_height": 768,
    "polish_audio": True,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def ensure_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {column["name"] for column in db.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate_db(db: sqlite3.Connection) -> None:
    for name, definition in (
        ("subtitle_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("subtitle_start_seconds", "REAL"),
        ("sound", "TEXT NOT NULL DEFAULT ''"),
    ):
        ensure_column(db, "shots", name, definition)
    for name, definition in (
        ("source", "TEXT NOT NULL DEFAULT 'mock'"),
        ("source_path", "TEXT"),
        ("managed_path", "TEXT"),
        ("mime_type", "TEXT"),
        ("media_type", "TEXT"),
        ("size_bytes", "INTEGER"),
        ("checksum_sha256", "TEXT"),
        ("width", "INTEGER"),
        ("height", "INTEGER"),
        ("duration_seconds", "REAL"),
        ("has_audio", "INTEGER NOT NULL DEFAULT 0"),
        ("created_at", "TEXT"),
        ("archived", "INTEGER NOT NULL DEFAULT 0"),
        ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        ensure_column(db, "assets", name, definition)
    for name, definition in (
        ("status", "TEXT NOT NULL DEFAULT 'completed'"),
        ("source", "TEXT NOT NULL DEFAULT 'mock'"),
        ("external_id", "TEXT"),
        ("prompt_id", "TEXT"),
        ("output_file", "TEXT"),
        ("thumbnail_file", "TEXT"),
        ("elapsed_seconds", "REAL"),
        ("archived", "INTEGER NOT NULL DEFAULT 0"),
        ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        ensure_column(db, "candidates", name, definition)
    for name, definition in (
        ("h3_project", "TEXT"),
        ("prompt_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ("updated_at", "TEXT"),
        ("completed_at", "TEXT"),
        ("plan_hash", "TEXT"),
        ("source_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
        ("reconciliation_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
        ("retry_safe", "INTEGER NOT NULL DEFAULT 0"),
    ):
        ensure_column(db, "jobs", name, definition)
    for name, definition in (
        ("attempt", "INTEGER NOT NULL DEFAULT 1"),
        ("parent_run_id", "TEXT"),
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("started_at", "TEXT"),
        ("worker_id", "TEXT"),
        ("recovery_count", "INTEGER NOT NULL DEFAULT 0"),
        ("source_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
        ("is_current", "INTEGER NOT NULL DEFAULT 0"),
        ("log_file", "TEXT"),
    ):
        ensure_column(db, "export_runs", name, definition)
    db.execute("UPDATE candidates SET source = 'mock' WHERE source IS NULL OR source = ''")
    db.execute("UPDATE candidates SET status = 'completed' WHERE status IS NULL OR status = ''")
    db.execute("UPDATE jobs SET updated_at = created_at WHERE updated_at IS NULL")
    db.execute("UPDATE assets SET source = 'mock' WHERE source IS NULL OR source = ''")
    db.execute("UPDATE assets SET created_at = ? WHERE created_at IS NULL", (utc_now(),))
    completed_projects = db.execute(
        "SELECT DISTINCT project_id FROM export_runs WHERE state = '已完成'"
    ).fetchall()
    for completed_project in completed_projects:
        current_export = db.execute(
            """SELECT id FROM export_runs
            WHERE project_id = ? AND state = '已完成' AND is_current = 1 LIMIT 1""",
            (completed_project["project_id"],),
        ).fetchone()
        if not current_export:
            latest_export = db.execute(
                """SELECT id FROM export_runs WHERE project_id = ? AND state = '已完成'
                ORDER BY completed_at DESC LIMIT 1""",
                (completed_project["project_id"],),
            ).fetchone()
            if latest_export:
                db.execute("UPDATE export_runs SET is_current = 1 WHERE id = ?", (latest_export["id"],))
    # The seeded acceptance project already passed a subtitle timing review.
    # Persist those decisions on shots so every platform export can reproduce V2.1.
    db.execute("UPDATE shots SET subtitle_start_seconds = 3.2 WHERE id = 'EP01-S01-02' AND subtitle_start_seconds IS NULL")
    db.execute("UPDATE shots SET subtitle_start_seconds = 3.5 WHERE id IN ('EP01-S01-04', 'EP01-S01-06') AND subtitle_start_seconds IS NULL")
    db.execute("UPDATE shots SET subtitle_enabled = 0 WHERE id = 'EP01-S01-07'")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              episode TEXT NOT NULL,
              logline TEXT NOT NULL,
              target_duration REAL NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shots (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES projects(id),
              ordinal INTEGER NOT NULL,
              scene_code TEXT NOT NULL,
              title TEXT NOT NULL,
              description TEXT NOT NULL,
              dialogue TEXT NOT NULL,
              prompt TEXT NOT NULL,
              status TEXT NOT NULL,
              width INTEGER NOT NULL,
              height INTEGER NOT NULL,
              seconds REAL NOT NULL,
              candidate_count INTEGER NOT NULL,
              strategy TEXT NOT NULL,
              thumbnail TEXT NOT NULL,
              video TEXT,
              subtitle_enabled INTEGER NOT NULL DEFAULT 1,
              subtitle_start_seconds REAL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES projects(id),
              kind TEXT NOT NULL,
              name TEXT NOT NULL,
              description TEXT NOT NULL,
              preview TEXT NOT NULL,
              locked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS candidates (
              id TEXT PRIMARY KEY,
              shot_id TEXT NOT NULL REFERENCES shots(id),
              label TEXT NOT NULL,
              seed INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              thumbnail TEXT NOT NULL,
              video TEXT,
              selected INTEGER NOT NULL DEFAULT 0,
              scores TEXT NOT NULL,
              note TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'completed',
              source TEXT NOT NULL DEFAULT 'mock',
              external_id TEXT,
              prompt_id TEXT,
              output_file TEXT,
              thumbnail_file TEXT,
              elapsed_seconds REAL,
              archived INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_id TEXT NOT NULL REFERENCES shots(id),
              kind TEXT NOT NULL,
              state TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at TEXT NOT NULL,
              h3_project TEXT,
              prompt_ids TEXT NOT NULL DEFAULT '[]',
              updated_at TEXT,
              completed_at TEXT,
              plan_hash TEXT,
              source_snapshot TEXT NOT NULL DEFAULT '{}',
              reconciliation_snapshot TEXT NOT NULL DEFAULT '{}',
              retry_safe INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS promotions (
              id TEXT PRIMARY KEY,
              shot_id TEXT NOT NULL REFERENCES shots(id),
              h3_project TEXT NOT NULL,
              external_id TEXT NOT NULL,
              source_candidate TEXT,
              strategy TEXT NOT NULL,
              status TEXT NOT NULL,
              width INTEGER NOT NULL,
              height INTEGER NOT NULL,
              actual_seconds REAL,
              elapsed_seconds REAL,
              prompt_id TEXT,
              output_file TEXT,
              selected INTEGER NOT NULL DEFAULT 0,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              selected_at TEXT,
              metadata TEXT NOT NULL DEFAULT '{}',
              UNIQUE(shot_id, external_id)
            );
            CREATE TABLE IF NOT EXISTS shot_references (
              id TEXT PRIMARY KEY,
              shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
              asset_id TEXT NOT NULL REFERENCES assets(id),
              reference_type TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              role TEXT NOT NULL DEFAULT 'generic',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(shot_id, asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_shot_references_order
              ON shot_references(shot_id, reference_type, ordinal);
            CREATE TABLE IF NOT EXISTS export_runs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES projects(id),
              state TEXT NOT NULL,
              message TEXT NOT NULL,
              output_name TEXT NOT NULL UNIQUE,
              width INTEGER NOT NULL,
              height INTEGER NOT NULL,
              polish_audio INTEGER NOT NULL DEFAULT 1,
              config TEXT NOT NULL DEFAULT '{}',
              outputs TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_export_runs_created
              ON export_runs(created_at DESC);
            CREATE TABLE IF NOT EXISTS export_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL REFERENCES export_runs(id) ON DELETE CASCADE,
              level TEXT NOT NULL,
              event TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_export_events_run
              ON export_events(run_id, id);
            CREATE TABLE IF NOT EXISTS delivery_signoffs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              category TEXT NOT NULL CHECK(category IN ('picture_continuity', 'sound')),
              revision INTEGER NOT NULL,
              decision TEXT NOT NULL CHECK(decision IN ('pass', 'reject')),
              note TEXT NOT NULL,
              source TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(project_id, category, revision)
            );
            CREATE TABLE IF NOT EXISTS production_acceptance_runs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              status TEXT NOT NULL,
              report_hash TEXT NOT NULL,
              report TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_acceptance_runs_project
              ON production_acceptance_runs(project_id, created_at DESC);
            """
        )
        init_content_schema(db)
        init_local_agent_schema(db)
        init_script_schema(db)
        init_bible_schema(db)
        init_prompt_schema(db)
        init_storyboard_schema(db)
        init_production_schema(db)
        init_review_schema(db)
        init_delivery_schema(db)
        init_archive_schema(db)
        migrate_db(db)
        for character in db.execute(
            "SELECT * FROM creative_characters WHERE status = 'approved'"
        ).fetchall():
            sync_creative_character_rules(db, character)
        existing = db.execute("SELECT COUNT(*) AS count FROM projects").fetchone()["count"]
        if existing:
            active_project = db.execute(
                "SELECT value FROM workspace_settings WHERE key = 'active_project_id'"
            ).fetchone()
            if not active_project or not db.execute(
                "SELECT id FROM projects WHERE id = ? AND archived = 0", (active_project["value"] if active_project else "",)
            ).fetchone():
                first_project = db.execute("SELECT id FROM projects WHERE archived = 0 ORDER BY created_at LIMIT 1").fetchone()
                if first_project:
                    db.execute(
                        """INSERT INTO workspace_settings (key, value, updated_at)
                        VALUES ('active_project_id', ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                        (first_project["id"], utc_now()),
                    )
            db.commit()
            return

        db.execute(
            "INSERT INTO projects (id, title, episode, logline, target_duration, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "rain-call-ep01",
                "雨夜来电",
                "EP01",
                "一个失联三年的号码，在暴雨夜打进林夏的手机；电话那头的人，正坐在她身后的车里。",
                38.0,
                utc_now(),
            ),
        )
        db.execute(
            "INSERT INTO workspace_settings (key, value, updated_at) VALUES ('active_project_id', 'rain-call-ep01', ?)",
            (utc_now(),),
        )
        shots = [
            ("EP01-S01-01", 1, "S01", "雨幕中的便利店", "林夏冲出便利店，雨声盖住手机震动。", "", "cinematic rainy night, East Asian woman in a red raincoat exits a convenience store, wet street reflections, restrained handheld camera, tense urban suspense", "未生成", 608, 352, 5.17, 3, "保真放大", "/media/shot-01.jpg"),
            ("EP01-S01-02", 2, "S01", "陌生来电", "屏幕亮起：哥哥。林夏停住脚步。", "林夏：不可能……", "close-up of a wet smartphone in a woman's hand, caller name 哥哥, rain drops, amber and blue practical light, shallow depth of field, psychological suspense", "未生成", 608, 352, 5.17, 3, "Ref2VA 精修", "/media/shot-02.jpg"),
            ("EP01-S01-03", 3, "S01", "便利店门口的迟疑", "她没有接听，后退一步贴近黑色越野车。", "", "East Asian woman in red raincoat hesitates beside a dark off-road vehicle outside a convenience store, heavy rain, anxious glance over shoulder, slow push-in, cinematic realism", "可生成", 608, 352, 5.17, 2, "Ref2VA 精修", "/media/shot-03.jpg"),
            ("EP01-S01-04", 4, "S01", "后排的呼吸", "电话接通，车内传来同频的呼吸声。", "电话：别回头。", "rainy car window, female silhouette reflected beside an indistinct figure in the rear seat, synchronized breath fog, rack focus from reflection to darkness", "未生成", 608, 352, 5.17, 2, "保真放大", "/media/shot-04.jpg"),
            ("EP01-S01-05", 5, "S01", "镜面里的乘客", "林夏从后视镜看见一双眼睛。", "", "extreme close-up rear-view mirror revealing a pair of eyes in a dark back seat, woman's shocked reflection, lightning flash, high tension, no gore", "未生成", 608, 352, 5.17, 2, "Ref2VA 精修", "/media/shot-05.jpg"),
            ("EP01-S01-06", 6, "S01", "车门落锁", "四扇车门同时落锁，手机从手中滑落。", "林夏：你是谁？", "four car door locks snap down in sequence, phone falls in slow motion onto wet pavement, sharp sound cue, low angle tracking shot, cinematic thriller", "未生成", 608, 352, 5.17, 2, "保真放大", "/media/shot-06.jpg"),
            ("EP01-S01-07", 7, "S01", "来电者", "后排传来哥哥的声音，画面切黑。", "哥哥：三年了，你还在躲我。", "woman frozen beside car in storm, warm voice comes from pitch-black rear seat, slow dolly toward open window, cut to black at final word", "未生成", 608, 352, 5.17, 2, "Ref2VA 精修", "/media/shot-07.jpg"),
        ]
        for shot in shots:
            db.execute(
                """INSERT INTO shots
                (id, project_id, ordinal, scene_code, title, description, dialogue, prompt, status,
                 width, height, seconds, candidate_count, strategy, thumbnail, video, updated_at)
                VALUES (?, 'rain-call-ep01', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*shot, "/media/h3-sample.mp4", utc_now()),
            )
        assets = [
            ("role-linxia", "角色", "林夏 · 主角", "28 岁，红色雨衣；克制、警觉，人物一致性已锁定。", "/media/shot-03.jpg", 1),
            ("role-brother", "角色", "林峥 · 来电者", "失踪三年的哥哥；当前仅建立声音与轮廓设定。", "/media/shot-07.jpg", 0),
            ("location-store", "场景", "24 小时便利店", "雨夜、冷白室内灯与暖黄路灯形成色温反差。", "/media/shot-01.jpg", 1),
            ("vehicle-suv", "道具", "黑色越野车", "右侧车窗有规律水痕；全场关键叙事道具。", "/media/shot-05.jpg", 1),
        ]
        for asset in assets:
            db.execute(
                """INSERT INTO assets
                (id, project_id, kind, name, description, preview, locked)
                VALUES (?, 'rain-call-ep01', ?, ?, ?, ?, ?)""",
                asset,
            )
        scores = json.dumps(
            {"人物一致性": 4.5, "动作可信度": 4.0, "镜头语言": 4.7, "画面瑕疵": 3.8, "剧情可用性": 4.6},
            ensure_ascii=False,
        )
        for index, (label, seed, selected) in enumerate(
            (("A", 3271456821, 0), ("B", 9817234450, 1), ("C", 1654327789, 0)), start=1
        ):
            db.execute(
                """INSERT INTO candidates
                (id, shot_id, label, seed, created_at, thumbnail, video, selected, scores, note,
                 status, source, archived, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'mock', 0, '{}')""",
                (
                    f"EP01-S01-03-{label}",
                    "EP01-S01-03",
                    label,
                    seed,
                    utc_now(),
                    f"/media/candidate-{index}.jpg",
                    "/media/h3-sample.mp4",
                    selected,
                    scores,
                    "演示评分：该候选来自既有 H3 视频，并非本镜头真实生成。",
                ),
            )
        db.execute(
            """INSERT INTO jobs
            (shot_id, kind, state, message, created_at, updated_at)
            VALUES ('EP01-S01-03', 'validation', '就绪', '工作流参数已校验', ?, ?)""",
            (utc_now(), utc_now()),
        )
        db.commit()


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [dict(result) for result in db.execute(query, params).fetchall()]


def row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    result = rows(query, params)
    return result[0] if result else None


def workspace_preferences() -> dict[str, Any]:
    stored = row("SELECT value FROM workspace_settings WHERE key = 'app_preferences'")
    parsed: dict[str, Any] = {}
    if stored:
        try:
            candidate = json.loads(stored["value"])
            if isinstance(candidate, dict):
                parsed = candidate
        except (json.JSONDecodeError, TypeError):
            parsed = {}
    return {**DEFAULT_WORKSPACE_PREFERENCES, **parsed}


def save_workspace_preferences(preferences: dict[str, Any]) -> None:
    with closing(connect()) as db:
        db.execute(
            """INSERT INTO workspace_settings (key, value, updated_at)
            VALUES ('app_preferences', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (json.dumps(preferences, ensure_ascii=False), utc_now()),
        )
        db.commit()


def effective_comfy_url() -> str:
    return str(workspace_preferences().get("comfyui_url") or COMFY_URL).rstrip("/")


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def storage_payload() -> dict[str, Any]:
    runtime_root = (ROOT / "runtime").resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(runtime_root)
    return {
        "runtime_root": str(runtime_root),
        "asset_root": str(ASSET_ROOT),
        "export_root": str(EXPORT_ROOT),
        "managed_bytes": directory_size(runtime_root),
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
    }


def active_project() -> dict[str, Any] | None:
    selected = row(
        """SELECT projects.* FROM projects
        JOIN workspace_settings ON workspace_settings.key = 'active_project_id'
        AND workspace_settings.value = projects.id
        WHERE projects.archived = 0
        LIMIT 1"""
    )
    return selected or row("SELECT * FROM projects WHERE archived = 0 ORDER BY created_at LIMIT 1")


def require_active_shot(shot_id: str) -> dict[str, Any]:
    project = active_project()
    shot = row("SELECT * FROM shots WHERE id = ?", (shot_id,))
    if not project or not shot or shot["project_id"] != project["id"]:
        raise HTTPException(404, "当前项目中不存在该镜头")
    return shot


def require_active_asset(asset_id: str) -> dict[str, Any]:
    project = active_project()
    asset = row("SELECT * FROM assets WHERE id = ? AND archived = 0", (asset_id,))
    if not project or not asset or asset["project_id"] != project["id"]:
        raise HTTPException(404, "当前项目中不存在该素材")
    return asset


def require_active_candidate(candidate_id: str) -> dict[str, Any]:
    project = active_project()
    candidate = row(
        """SELECT candidates.*, shots.project_id FROM candidates
        JOIN shots ON shots.id = candidates.shot_id
        WHERE candidates.id = ? AND candidates.archived = 0""",
        (candidate_id,),
    )
    if not project or not candidate or candidate["project_id"] != project["id"]:
        raise HTTPException(404, "当前项目中不存在该候选")
    return candidate


def require_active_promotion(promotion_id: str) -> dict[str, Any]:
    project = active_project()
    promotion = row(
        """SELECT promotions.*, shots.project_id FROM promotions
        JOIN shots ON shots.id = promotions.shot_id
        WHERE promotions.id = ?""",
        (promotion_id,),
    )
    if not project or not promotion or promotion["project_id"] != project["id"]:
        raise HTTPException(404, "当前项目中不存在该成片版本")
    return promotion


def require_active_export_run(run_id: str) -> dict[str, Any]:
    project = active_project()
    export_run = row("SELECT * FROM export_runs WHERE id = ?", (run_id,))
    if not project or not export_run or export_run["project_id"] != project["id"]:
        raise HTTPException(404, "当前项目中不存在该导出任务")
    return export_run


def set_active_project(project_id: str) -> None:
    project = row("SELECT id, archived FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, "项目不存在")
    if project["archived"]:
        raise HTTPException(409, "归档项目不能直接打开，请先恢复到工作台")
    with closing(connect()) as db:
        db.execute(
            """INSERT INTO workspace_settings (key, value, updated_at)
            VALUES ('active_project_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (project_id, utc_now()),
        )
        db.commit()


def media_type_for_suffix(suffix: str) -> str:
    normalized = suffix.lower()
    for media_type, extensions in ASSET_EXTENSIONS.items():
        if normalized in extensions:
            return media_type
    raise HTTPException(415, f"不支持的素材格式：{normalized or '无扩展名'}")


def allowed_asset_file(file_path: str | None) -> Path:
    if not file_path:
        raise HTTPException(404, "素材没有受管文件")
    path = Path(file_path).resolve()
    try:
        path.relative_to(ASSET_ROOT)
    except ValueError as exc:
        raise HTTPException(403, "素材文件不在工作台受管目录") from exc
    if not path.is_file():
        raise HTTPException(404, "素材文件不存在")
    return path


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height,duration", "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, f"无法探测素材媒体信息：{exc}") from exc
    if completed.returncode != 0:
        raise HTTPException(415, completed.stderr.strip() or "素材文件无法被 ffprobe 识别")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(415, "ffprobe 返回了无效结果") from exc
    streams = payload.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    duration = (payload.get("format") or {}).get("duration") or video_stream.get("duration")
    return {
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "duration_seconds": round(float(duration), 3) if duration not in (None, "N/A") else None,
        "has_audio": 1 if any(stream.get("codec_type") == "audio" for stream in streams) else 0,
    }


def asset_public(item: dict[str, Any]) -> dict[str, Any]:
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
    item["bindable"] = item.get("source") == "managed" and not item.get("archived") and bool(item.get("managed_path"))
    if item["bindable"]:
        item["preview"] = f"/api/assets/{item['id']}/content"
        item["content_url"] = item["preview"]
    else:
        item["content_url"] = None
    return item


def reference_rows(shot_id: str) -> list[dict[str, Any]]:
    references = rows(
        """SELECT * FROM shot_references WHERE shot_id = ?
        ORDER BY CASE reference_type WHEN 'image' THEN 1 WHEN 'video' THEN 2 ELSE 3 END, ordinal""",
        (shot_id,),
    )
    assets_by_id = {
        asset["id"]: asset_public(asset)
        for asset in rows(
            "SELECT * FROM assets WHERE id IN (SELECT asset_id FROM shot_references WHERE shot_id = ?)",
            (shot_id,),
        )
    }
    embedded_audio_index: dict[str, int] = {}
    next_audio = 1
    for reference in references:
        if reference["reference_type"] == "video" and assets_by_id[reference["asset_id"]].get("has_audio"):
            embedded_audio_index[reference["id"]] = next_audio
            next_audio += 1
    standalone_audio_index = next_audio
    for reference in references:
        media_type = reference["reference_type"]
        if media_type == "image":
            tag = f"<Picture {reference['ordinal']}>"
        elif media_type == "video":
            tag = f"<Video {reference['ordinal']}>"
        else:
            tag = f"<Audio {standalone_audio_index}>"
            standalone_audio_index += 1
        reference["tag"] = tag
        reference["audio_tag"] = (
            f"<Audio {embedded_audio_index[reference['id']]}>" if reference["id"] in embedded_audio_index else None
        )
        reference["asset"] = assets_by_id[reference["asset_id"]]
    return references


def normalize_reference_ordinals(db: sqlite3.Connection, shot_id: str, media_type: str) -> None:
    ids = [
        record["id"]
        for record in db.execute(
            "SELECT id FROM shot_references WHERE shot_id = ? AND reference_type = ? ORDER BY ordinal, created_at",
            (shot_id, media_type),
        )
    ]
    for ordinal, reference_id in enumerate(ids, start=1):
        db.execute("UPDATE shot_references SET ordinal = ?, updated_at = ? WHERE id = ?", (ordinal, utc_now(), reference_id))


def compile_reference_prompt(creative_prompt: str, references: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    templates = {
        "identity": "Use {tag} as the exact character identity and visible costume reference; preserve facial features, age, hair, body proportions, clothing design, and clothing color.",
        "costume": "Use {tag} as the costume reference; preserve clothing design, color, material, and accessories.",
        "location": "Use {tag} as the location continuity reference; preserve the setting, layout, weather, vehicle, and practical lighting.",
        "style": "Use {tag} as the visual style reference; preserve palette, contrast, lens character, and cinematic texture.",
        "prop": "Use {tag} as the exact prop reference; preserve its shape, color, and distinctive details.",
        "action": "Use {tag} as the action reference; follow the motion rhythm and body mechanics while preserving the story subject.",
        "camera": "Use {tag} as the camera reference; follow its framing, movement, and timing.",
        "performance": "Use {tag} as the performance reference; follow its expression and gesture timing.",
        "voice": "Use {tag} as the voice reference; preserve speaker identity, pacing, and emotional tone.",
        "ambience": "Use {tag} as the ambience reference; preserve its acoustic environment and intensity.",
        "effects": "Use {tag} as the sound-effects reference; follow its timing and texture.",
        "music": "Use {tag} as the music reference; preserve mood and rhythm without changing the shot action.",
        "generic": "Use {tag} as a continuity reference and retain its relevant visual or audio characteristics.",
    }
    mapping: list[dict[str, Any]] = []
    instructions: list[str] = []
    for reference in references:
        role = reference.get("role") or "generic"
        instruction = templates.get(role, templates["generic"]).format(tag=reference["tag"])
        instructions.append(instruction)
        mapping.append({
            "reference_id": reference["id"],
            "asset_id": reference["asset_id"],
            "asset_name": reference["asset"]["name"],
            "media_type": reference["reference_type"],
            "role": role,
            "tag": reference["tag"],
            "audio_tag": reference.get("audio_tag"),
            "source": reference.get("source", "shot"),
            "source_label": reference.get("source_label", "镜头手工引用"),
            "bible_entry_id": reference.get("bible_entry_id"),
        })
    if not instructions:
        return creative_prompt, mapping
    return "\n".join([*instructions, "Create the shot described below:", creative_prompt]), mapping


def h3_project_for_shot(shot_id: str) -> str:
    clean = re.sub(r"[^a-z0-9-]+", "-", shot_id.lower()).strip("-")
    return f"jingchang-rain-call-{clean}"


def h3_manifest_path(project: str) -> Path:
    return H3_PROJECT_ROOT / project / "manifest.json"


def read_manifest(project: str) -> dict[str, Any]:
    path = h3_manifest_path(project)
    if not path.is_file():
        raise HTTPException(404, f"H3 manifest 尚未创建：{project}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"H3 manifest 无法读取：{exc}") from exc


def manifest_evidence(project: str) -> dict[str, Any]:
    manifest = read_manifest(project)
    return {
        "project": manifest.get("project"),
        "updated_at": manifest.get("updated_at"),
        "candidate_ids": [str(record.get("id")) for record in manifest.get("candidates") or [] if record.get("id")],
        "prompt_ids": [str(record.get("prompt_id")) for record in manifest.get("candidates") or [] if record.get("prompt_id")],
    }


def comfy_queue_evidence(prompt_ids: list[str]) -> dict[str, Any]:
    expected = {str(prompt_id) for prompt_id in prompt_ids if prompt_id}
    evidence: dict[str, Any] = {
        "available": False,
        "checked_at": utc_now(),
        "expected_prompt_ids": sorted(expected),
        "running_prompt_ids": [],
        "pending_prompt_ids": [],
    }
    try:
        with urllib.request.urlopen(f"{effective_comfy_url()}/queue", timeout=2.0) as response:
            queue = json.load(response)
        running = {
            str(item[1]) for item in queue.get("queue_running") or []
            if isinstance(item, (list, tuple)) and len(item) > 1 and item[1]
        }
        pending = {
            str(item[1]) for item in queue.get("queue_pending") or []
            if isinstance(item, (list, tuple)) and len(item) > 1 and item[1]
        }
        evidence.update({
            "available": True,
            "running_prompt_ids": sorted(expected & running),
            "pending_prompt_ids": sorted(expected & pending),
            "running_count": len(running),
            "pending_count": len(pending),
        })
    except (OSError, urllib.error.URLError, ValueError, TypeError, IndexError) as exc:
        evidence["error"] = str(exc)
    return evidence


def update_reconciliation_job(
    job_id: int,
    state: str,
    message: str,
    evidence: dict[str, Any],
    *,
    retry_safe: bool = False,
    completed: bool = False,
) -> None:
    now = utc_now()
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """UPDATE jobs SET state = ?, message = ?, reconciliation_snapshot = ?, retry_safe = ?,
            updated_at = ?, completed_at = ? WHERE id = ?""",
            (
                state,
                message,
                json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                int(retry_safe),
                now,
                now if completed else None,
                job_id,
            ),
        )
        db.commit()


def reconcile_generation_job(job: dict[str, Any]) -> dict[str, Any]:
    project = str(job.get("h3_project") or "")
    if not project:
        update_reconciliation_job(
            int(job["id"]), "待人工对账", "任务缺少 H3 项目标识，禁止自动重试", {}, completed=True,
        )
        return row("SELECT * FROM jobs WHERE id = ?", (job["id"],)) or job
    try:
        frozen = json.loads(job.get("reconciliation_snapshot") or "{}")
    except (TypeError, ValueError):
        frozen = {}
    if (
        not isinstance(frozen, dict)
        or frozen.get("h3_project") != project
        or "frozen_command" not in frozen
        or "baseline_candidate_ids" not in frozen
    ):
        update_reconciliation_job(
            int(job["id"]),
            "待人工对账",
            "提交任务缺少可信的冻结命令或 manifest 基线；禁止推断提交结果和自动重试",
            frozen if isinstance(frozen, dict) else {},
            completed=True,
        )
        return row("SELECT * FROM jobs WHERE id = ?", (job["id"],)) or job
    baseline_ids = set(str(value) for value in frozen.get("baseline_candidate_ids") or [])
    try:
        manifest = read_manifest(project)
    except HTTPException as exc:
        evidence = {**frozen, "manifest_error": str(exc.detail), "last_checked_at": utc_now()}
        update_reconciliation_job(
            int(job["id"]), "待人工对账", "H3 manifest 缺失或损坏，提交副作用未知；禁止自动重试",
            evidence, completed=True,
        )
        return row("SELECT * FROM jobs WHERE id = ?", (job["id"],)) or job
    current_records = manifest.get("candidates") or []
    submitted_records = [
        record for record in current_records
        if record.get("id") and str(record.get("id")) not in baseline_ids
    ]
    submitted_ids = [str(record.get("id")) for record in submitted_records]
    submitted_prompt_ids = [str(record.get("prompt_id")) for record in submitted_records if record.get("prompt_id")]
    evidence = {
        **frozen,
        "manifest_after": {
            "updated_at": manifest.get("updated_at"),
            "candidate_ids": [str(record.get("id")) for record in current_records if record.get("id")],
            "prompt_ids": [str(record.get("prompt_id")) for record in current_records if record.get("prompt_id")],
        },
        "submitted_candidate_ids": submitted_ids,
        "submitted_prompt_ids": submitted_prompt_ids,
        "last_checked_at": utc_now(),
    }
    evidence["comfyui_queue"] = comfy_queue_evidence(submitted_prompt_ids)
    if not submitted_ids:
        message = (
            "H3 适配器已返回成功，但 manifest 未出现本次候选；禁止自动重试"
            if frozen.get("adapter_returned_success")
            else "服务中断后未发现可证明本次提交结果的 manifest 候选；转人工对账，禁止自动重试"
        )
        update_reconciliation_job(int(job["id"]), "待人工对账", message, evidence, completed=True)
        return row("SELECT * FROM jobs WHERE id = ?", (job["id"],)) or job
    try:
        state = sync_manifest_to_db(job["shot_id"], project, manifest, job_id=int(job["id"]), record_ids=submitted_ids)
    except Exception as exc:
        update_reconciliation_job(
            int(job["id"]), "待人工对账", f"H3 候选已存在，但平台同步失败：{exc}；禁止重复提交",
            {**evidence, "sync_error": str(exc)}, completed=True,
        )
        return row("SELECT * FROM jobs WHERE id = ?", (job["id"],)) or job
    update_reconciliation_job(int(job["id"]), state["state"], state["message"], evidence, completed=state["state"] in {"完成", "失败"})
    return row("SELECT * FROM jobs WHERE id = ?", (job["id"],)) or job


def run_h3(arguments: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    if not H3_SCRIPT.is_file():
        raise HTTPException(503, f"H3 适配脚本不存在：{H3_SCRIPT}")
    command = [sys.executable, "-X", "utf8", str(H3_SCRIPT), *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "H3 适配器响应超时") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "H3 适配器执行失败"
        raise HTTPException(502, detail)
    return completed


def candidate_public(item: dict[str, Any]) -> dict[str, Any]:
    item["seed"] = str(item.get("seed") or "0")
    try:
        item["scores"] = json.loads(item.get("scores") or "{}")
    except json.JSONDecodeError:
        item["scores"] = {}
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
    if item.get("source") == "h3":
        item["video"] = f"/api/candidates/{item['id']}/video" if item.get("output_file") else None
        if item.get("thumbnail_file"):
            item["thumbnail"] = f"/api/candidates/{item['id']}/thumbnail"
    return item


def promotion_public(item: dict[str, Any]) -> dict[str, Any]:
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
    item["video"] = f"/api/promotions/{item['id']}/video" if item.get("output_file") else None
    return item


def sync_manifest_to_db(
    shot_id: str,
    project: str,
    manifest: dict[str, Any],
    *,
    job_id: int | None = None,
    record_ids: list[str] | None = None,
) -> dict[str, Any]:
    records = manifest.get("candidates") or []
    tracked_ids = set(record_ids) if record_ids is not None else None
    tracked_records = [record for record in records if tracked_ids is None or str(record.get("id")) in tracked_ids]
    selected_id = manifest.get("selected_id")
    status_order = {"error": 4, "running": 3, "queued": 2, "queueing": 1, "completed": 0}
    completed_count = sum(record.get("status") == "completed" for record in tracked_records)
    total_count = len(tracked_records)
    worst_status = max(
        (record.get("status", "queued") for record in tracked_records),
        key=lambda value: status_order.get(value, 2),
        default="queued",
    )

    with closing(connect()) as db:
        shot = db.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
        if not shot:
            raise HTTPException(404, "镜头不存在")
        for index, record in enumerate(records):
            external_id = str(record.get("id") or f"draft-{index + 1:03d}")
            candidate_id = f"{shot_id}-{external_id}"
            label = chr(ord("A") + index) if index < 26 else str(index + 1)
            status = str(record.get("status") or "queued")
            output_file = record.get("output_file")
            thumbnail_file = record.get("contact_sheet")
            note = "等待生成完成" if status != "completed" else "真实 H3 候选，等待审片。"
            if record.get("review_notes"):
                note = str(record["review_notes"])
            if status == "error":
                note = str(record.get("error") or "生成失败")
            existing_candidate = db.execute(
                "SELECT selected, note FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if existing_candidate and existing_candidate["selected"] and existing_candidate["note"]:
                note = existing_candidate["note"]
            scores = (
                json.dumps({"综合评分": float(record["review_score"])}, ensure_ascii=False)
                if record.get("review_score") is not None else "{}"
            )
            metadata = json.dumps(record, ensure_ascii=False)
            db.execute(
                """INSERT INTO candidates
                (id, shot_id, label, seed, created_at, thumbnail, video, selected, scores, note,
                 status, source, external_id, prompt_id, output_file, thumbnail_file,
                 elapsed_seconds, archived, metadata)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'h3', ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                  label = excluded.label,
                  seed = excluded.seed,
                  selected = excluded.selected,
                  scores = excluded.scores,
                  note = excluded.note,
                  status = excluded.status,
                  prompt_id = excluded.prompt_id,
                  output_file = excluded.output_file,
                  thumbnail_file = excluded.thumbnail_file,
                  elapsed_seconds = excluded.elapsed_seconds,
                  archived = 0,
                  metadata = excluded.metadata""",
                (
                    candidate_id,
                    shot_id,
                    label,
                    int(record.get("seed") or 0),
                    record.get("queued_at") or utc_now(),
                    shot["thumbnail"],
                    1 if external_id == selected_id else 0,
                    scores,
                    note,
                    status,
                    external_id,
                    record.get("prompt_id"),
                    output_file,
                    thumbnail_file,
                    record.get("elapsed_seconds"),
                    metadata,
                ),
            )

        for record in manifest.get("promotions") or []:
            external_id = str(record.get("id") or "")
            if not external_id:
                continue
            promotion_id = f"{shot_id}-{external_id}"
            db.execute(
                """INSERT INTO promotions
                (id, shot_id, h3_project, external_id, source_candidate, strategy, status,
                 width, height, actual_seconds, elapsed_seconds, prompt_id, output_file,
                 selected, note, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  source_candidate = excluded.source_candidate,
                  strategy = excluded.strategy,
                  status = excluded.status,
                  width = excluded.width,
                  height = excluded.height,
                  actual_seconds = excluded.actual_seconds,
                  elapsed_seconds = excluded.elapsed_seconds,
                  prompt_id = excluded.prompt_id,
                  output_file = excluded.output_file,
                  metadata = excluded.metadata""",
                (
                    promotion_id,
                    shot_id,
                    project,
                    external_id,
                    record.get("source_candidate"),
                    record.get("strategy") or "unknown",
                    record.get("status") or "queued",
                    int(record.get("width") or shot["width"]),
                    int(record.get("height") or shot["height"]),
                    record.get("actual_seconds"),
                    record.get("elapsed_seconds"),
                    record.get("prompt_id"),
                    record.get("output_file"),
                    record.get("completed_at") or record.get("queued_at") or utc_now(),
                    json.dumps(record, ensure_ascii=False),
                ),
            )

        if total_count and completed_count == total_count:
            job_state = "完成"
            has_final = db.execute(
                "SELECT 1 FROM promotions WHERE shot_id = ? AND selected = 1 LIMIT 1", (shot_id,)
            ).fetchone()
            shot_state = "已定稿" if has_final else "草稿已选" if selected_id else "待审片"
            message = f"{completed_count}/{total_count} 条真实候选已完成"
            completed_at = utc_now()
        elif worst_status == "error":
            job_state = "失败"
            shot_state = "生成失败"
            message = f"{completed_count}/{total_count} 条完成，至少一条失败"
            completed_at = utc_now()
        elif worst_status == "running":
            job_state = "运行中"
            shot_state = "生成中"
            message = f"{completed_count}/{total_count} 条完成，ComfyUI 正在生成"
            completed_at = None
        else:
            job_state = "排队中"
            shot_state = "生成中"
            message = f"{completed_count}/{total_count} 条完成，等待 ComfyUI"
            completed_at = None

        prompt_ids = [record.get("prompt_id") for record in tracked_records if record.get("prompt_id")]
        latest_job = (
            db.execute("SELECT id FROM jobs WHERE id = ? AND shot_id = ?", (job_id, shot_id)).fetchone()
            if job_id is not None
            else db.execute(
                "SELECT id FROM jobs WHERE shot_id = ? AND kind = 'draft' AND h3_project = ? ORDER BY id DESC LIMIT 1",
                (shot_id, project),
            ).fetchone()
        )
        if latest_job:
            db.execute(
                """UPDATE jobs SET state = ?, message = ?, prompt_ids = ?, updated_at = ?, completed_at = ?
                WHERE id = ?""",
                (job_state, message, json.dumps(prompt_ids), utc_now(), completed_at, latest_job["id"]),
            )
        if shot_state == "草稿已选" and selected_id:
            selected_candidate_id = f"{shot_id}-{selected_id}"
            selected_candidate = db.execute(
                "SELECT output_file, thumbnail_file FROM candidates WHERE id = ?", (selected_candidate_id,)
            ).fetchone()
            if selected_candidate:
                selected_video = (
                    f"/api/candidates/{selected_candidate_id}/video" if selected_candidate["output_file"] else shot["video"]
                )
                selected_thumbnail = (
                    f"/api/candidates/{selected_candidate_id}/thumbnail"
                    if selected_candidate["thumbnail_file"] else shot["thumbnail"]
                )
                db.execute(
                    "UPDATE shots SET status = ?, thumbnail = ?, video = ?, updated_at = ? WHERE id = ?",
                    (shot_state, selected_thumbnail, selected_video, utc_now(), shot_id),
                )
            else:
                db.execute("UPDATE shots SET status = ?, updated_at = ? WHERE id = ?", (shot_state, utc_now(), shot_id))
        else:
            db.execute("UPDATE shots SET status = ?, updated_at = ? WHERE id = ?", (shot_state, utc_now(), shot_id))
        db.commit()

    return {
        "project": project,
        "state": job_state,
        "message": message,
        "completed": completed_count,
        "total": total_count,
        "prompt_ids": prompt_ids,
        "external_ids": [str(record.get("id")) for record in tracked_records],
    }


def refresh_h3_project(shot_id: str, project: str) -> dict[str, Any]:
    run_h3(["status", "--project", project], timeout=30)
    manifest = read_manifest(project)
    try:
        with urllib.request.urlopen(f"{effective_comfy_url()}/queue", timeout=2.0) as response:
            queue = json.load(response)
        running_ids = {item[1] for item in queue.get("queue_running") or [] if len(item) > 1}
        pending_ids = {item[1] for item in queue.get("queue_pending") or [] if len(item) > 1}
        for record in manifest.get("candidates") or []:
            prompt_id = record.get("prompt_id")
            if prompt_id in running_ids:
                record["status"] = "running"
            elif prompt_id in pending_ids and record.get("status") != "completed":
                record["status"] = "queued"
    except (OSError, urllib.error.URLError, ValueError, IndexError):
        pass
    needs_sheet = any(
        record.get("status") == "completed"
        and record.get("output_file")
        and not record.get("contact_sheet")
        for record in manifest.get("candidates") or []
    )
    if needs_sheet:
        run_h3(["sheet", "--project", project], timeout=120)
        manifest = read_manifest(project)
    return sync_manifest_to_db(shot_id, project, manifest)


class ShotPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    dialogue: str | None = None
    sound: str | None = None
    prompt: str | None = None
    status: str | None = None
    width: int | None = Field(None, ge=256, le=1344)
    height: int | None = Field(None, ge=256, le=768)
    seconds: float | None = Field(None, ge=1.0, le=15.1)
    candidate_count: int | None = Field(None, ge=1, le=4)
    subtitle_enabled: bool | None = None
    subtitle_start_seconds: float | None = Field(None, ge=0, le=30)
    strategy: Literal["保真放大", "Ref2VA 精修"] | None = None


class ShotCreate(BaseModel):
    title: str = Field(min_length=2, max_length=60)
    description: str = Field(min_length=2, max_length=300)
    prompt: str = Field(min_length=3, max_length=2000)
    dialogue: str = ""


class ProjectCreate(BaseModel):
    title: str = Field(min_length=2, max_length=80)
    episode: str = Field("EP01", min_length=1, max_length=20)
    logline: str = Field("", max_length=500)
    target_duration: float = Field(60.0, ge=5.0, le=3600.0)
    shots: list[ShotCreate] = Field(default_factory=list, max_length=200)


class GenerateRequest(BaseModel):
    confirm: bool = False
    dry_run: bool = True
    expected_validation_hash: str | None = Field(None, min_length=64, max_length=64)


class BatchGenerationRequest(BaseModel):
    shot_ids: list[str] = Field(min_length=1, max_length=200)
    confirm: bool = False


class ReferenceCreate(BaseModel):
    asset_id: str
    role: str = "generic"


class ReferencePatch(BaseModel):
    role: str


class ReferenceOrder(BaseModel):
    reference_type: Literal["image", "video", "audio"]
    reference_ids: list[str]


class AssetPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    kind: str | None = Field(None, min_length=1, max_length=40)
    description: str | None = Field(None, max_length=500)


class FrameExtractRequest(BaseModel):
    seconds: float = Field(ge=0)
    name: str | None = Field(None, min_length=2, max_length=80)
    kind: str = Field("角色参考", min_length=1, max_length=40)
    description: str = Field("", max_length=500)


class ReviewRequest(BaseModel):
    candidate_id: str
    note: str = ""


class FinalizePromotionRequest(BaseModel):
    promotion_id: str
    note: str = ""


class ExportRequest(BaseModel):
    width: int = Field(1344, ge=320, le=3840)
    height: int = Field(768, ge=180, le=2160)
    polish_audio: bool = True


class WorkspaceSettingsPatch(BaseModel):
    sidebar_collapsed: bool | None = None
    default_landing_page: Literal["projects", "activity", "global-queue"] | None = None
    density: Literal["comfortable", "compact"] | None = None
    comfyui_url: str | None = Field(None, min_length=8, max_length=240)
    default_export_width: int | None = Field(None, ge=320, le=3840)
    default_export_height: int | None = Field(None, ge=180, le=2160)
    polish_audio: bool | None = None


class ConnectionTestRequest(BaseModel):
    comfyui_url: str | None = Field(None, min_length=8, max_length=240)


class PromptPlanRequest(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)


class DeliverySignoffRequest(BaseModel):
    category: Literal["picture_continuity", "sound"]
    decision: Literal["pass", "reject"]
    note: str = Field(min_length=2, max_length=500)
    source: str = Field(default="human-review", min_length=2, max_length=80)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    EXPORT_JOB_ROOT.mkdir(parents=True, exist_ok=True)
    init_db()
    recover_local_agent_runs(DB_PATH)
    start_export_worker()
    configure_production_scheduler(
        DB_PATH,
        lambda shot_id: compile_prompt_plan(DB_PATH, shot_id),
        lambda shot_id: generate(shot_id, GenerateRequest(confirm=True, dry_run=False)),
        lambda shot_id: refresh_h3_project(shot_id, h3_project_for_shot(shot_id)),
    )
    start_production_worker()
    try:
        yield
    finally:
        stop_production_worker()
        stop_export_worker()


app = FastAPI(title="镜场 H3 Studio API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:4173", "http://localhost:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(create_content_router(DB_PATH))
app.include_router(create_storyboard_router(DB_PATH))
app.include_router(create_local_agent_router(DB_PATH))
app.include_router(create_script_router(DB_PATH, ROOT))
app.include_router(create_bible_router(DB_PATH))
app.include_router(create_production_router(DB_PATH, lambda shot_id: compile_prompt_plan(DB_PATH, shot_id)))
app.include_router(create_review_router(DB_PATH, COMFY_OUTPUT_ROOT))
app.include_router(create_delivery_router(DB_PATH))
app.include_router(create_archive_router(DB_PATH, BACKUP_ROOT, EXPORT_ROOT))


@app.get("/api/health")
def health() -> dict[str, Any]:
    comfy_online = False
    gpu = None
    error = None
    comfy_url = effective_comfy_url()
    try:
        with urllib.request.urlopen(f"{comfy_url}/system_stats", timeout=2.0) as response:
            payload = json.load(response)
            comfy_online = True
            devices = payload.get("devices") or []
            if devices:
                gpu = devices[0].get("name")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        error = str(exc)
    export_queue = row(
        """SELECT
        SUM(CASE WHEN state IN ('排队中', '恢复排队') THEN 1 ELSE 0 END) AS queued,
        SUM(CASE WHEN state IN ('导出中', '取消中') THEN 1 ELSE 0 END) AS running
        FROM export_runs"""
    ) or {}
    return {
        "api": "online",
        "api_pid": os.getpid(),
        "api_started_at": API_STARTED_AT.isoformat(),
        "api_uptime_seconds": round(time.monotonic() - API_STARTED_MONOTONIC, 1),
        "database_path": str(DB_PATH.resolve()),
        "comfyui": "online" if comfy_online else "offline",
        "comfyui_url": comfy_url,
        "gpu": gpu,
        "h3_adapter": H3_SCRIPT.is_file(),
        "h3_project_root": str(H3_PROJECT_ROOT),
        "export_worker": "online" if EXPORT_WORKER_THREAD and EXPORT_WORKER_THREAD.is_alive() else "offline",
        "export_worker_id": EXPORT_WORKER_ID,
        "export_queue": {"queued": export_queue.get("queued") or 0, "running": export_queue.get("running") or 0},
        "supervisor": supervisor_status(RUNTIME_ROOT),
        "error": error,
    }


@app.get("/api/runtime")
def get_runtime_status() -> dict[str, Any]:
    return {
        "api": {
            "state": "online",
            "pid": os.getpid(),
            "started_at": API_STARTED_AT.isoformat(),
            "uptime_seconds": round(time.monotonic() - API_STARTED_MONOTONIC, 1),
            "database_path": str(DB_PATH.resolve()),
        },
        "supervisor": supervisor_status(RUNTIME_ROOT),
    }


@app.post("/api/runtime/restart-api", status_code=202)
def restart_api() -> dict[str, Any]:
    try:
        command = request_supervisor_action(RUNTIME_ROOT, "api", "restart")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "message": "API 重启请求已提交，工作台会自动重连", "command_id": command["id"]}


def settings_public() -> dict[str, Any]:
    return {
        **workspace_preferences(),
        "runtime": {
            **storage_payload(),
            "comfyui_root": str(COMFY_ROOT),
            "h3_project_root": str(H3_PROJECT_ROOT),
            "h3_script": str(H3_SCRIPT),
            "h3_adapter": H3_SCRIPT.is_file(),
        },
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return settings_public()


@app.patch("/api/settings")
def update_settings(payload: WorkspaceSettingsPatch) -> dict[str, Any]:
    updates = payload.model_dump(exclude_none=True)
    if "comfyui_url" in updates:
        comfyui_url = updates["comfyui_url"].strip().rstrip("/")
        if not comfyui_url.startswith(("http://", "https://")) or any(char.isspace() for char in comfyui_url):
            raise HTTPException(422, "ComfyUI 地址必须是有效的 http(s) URL")
        updates["comfyui_url"] = comfyui_url
    preferences = {**workspace_preferences(), **updates}
    save_workspace_preferences(preferences)
    return settings_public()


@app.post("/api/settings/test-connection")
def test_settings_connection(payload: ConnectionTestRequest) -> dict[str, Any]:
    comfy_url = (payload.comfyui_url or effective_comfy_url()).strip().rstrip("/")
    if not comfy_url.startswith(("http://", "https://")) or any(char.isspace() for char in comfy_url):
        raise HTTPException(422, "ComfyUI 地址必须是有效的 http(s) URL")
    try:
        with urllib.request.urlopen(f"{comfy_url}/system_stats", timeout=3.0) as response:
            payload = json.load(response)
        devices = payload.get("devices") or []
        return {
            "ok": True,
            "message": "ComfyUI 连接正常",
            "comfyui_url": comfy_url,
            "gpu": devices[0].get("name") if devices else None,
        }
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise HTTPException(503, f"无法连接 ComfyUI：{exc}") from exc


@app.get("/api/workbench")
def get_workbench() -> dict[str, Any]:
    current = active_project()
    project_rows = rows(
        f"""SELECT projects.*,
        (SELECT COUNT(*) FROM shots WHERE shots.project_id = projects.id) AS shot_count,
        (SELECT COUNT(*) FROM shots WHERE shots.project_id = projects.id AND shots.status = '已定稿') AS final_count,
        (SELECT COALESCE(ROUND(SUM(seconds), 2), 0) FROM shots WHERE shots.project_id = projects.id) AS planned_seconds,
        (SELECT COUNT(*) FROM jobs JOIN shots job_shot ON job_shot.id = jobs.shot_id
          WHERE job_shot.project_id = projects.id) AS job_count,
        (SELECT COUNT(*) FROM jobs JOIN shots active_shot ON active_shot.id = jobs.shot_id
          WHERE active_shot.project_id = projects.id
          AND jobs.state IN ({','.join('?' for _ in ACTIVE_JOB_STATES)})) AS active_job_count,
        (SELECT COUNT(*) FROM candidates JOIN shots candidate_shot ON candidate_shot.id = candidates.shot_id
          WHERE candidate_shot.project_id = projects.id AND candidates.status = 'completed' AND candidates.archived = 0) AS candidate_count,
        (SELECT '/api/projects/' || projects.id || '/thumbnail'
          FROM candidates cover_candidate JOIN shots cover_shot ON cover_shot.id = cover_candidate.shot_id
          WHERE cover_shot.project_id = projects.id AND cover_candidate.selected = 1
          AND cover_candidate.archived = 0 AND cover_candidate.thumbnail_file IS NOT NULL
          AND cover_candidate.thumbnail_file <> '' ORDER BY cover_shot.ordinal LIMIT 1) AS thumbnail,
        MAX(
          projects.created_at,
          COALESCE((SELECT MAX(updated_at) FROM shots WHERE shots.project_id = projects.id), projects.created_at),
          COALESCE((SELECT MAX(updated_at) FROM creative_briefs WHERE creative_briefs.project_id = projects.id), projects.created_at),
          COALESCE((SELECT MAX(updated_at) FROM creative_proposals WHERE creative_proposals.project_id = projects.id), projects.created_at),
          COALESCE((SELECT MAX(updated_at) FROM creative_characters WHERE creative_characters.project_id = projects.id), projects.created_at),
          COALESCE((SELECT MAX(updated_at) FROM creative_chapters WHERE creative_chapters.project_id = projects.id), projects.created_at),
          COALESCE((SELECT MAX(updated_at) FROM creative_sections WHERE creative_sections.project_id = projects.id), projects.created_at),
          COALESCE((SELECT MAX(jobs.updated_at) FROM jobs JOIN shots job_shot ON job_shot.id = jobs.shot_id
                    WHERE job_shot.project_id = projects.id), projects.created_at),
          COALESCE((SELECT MAX(updated_at) FROM export_runs WHERE export_runs.project_id = projects.id), projects.created_at)
        ) AS updated_at
        FROM projects ORDER BY updated_at DESC""",
        ACTIVE_JOB_STATES,
    )
    projects: list[dict[str, Any]] = []
    for project in project_rows:
        shot_count = int(project.get("shot_count") or 0)
        final_count = int(project.get("final_count") or 0)
        if project.get("archived"):
            phase = "已归档"
            category = "archived"
        elif shot_count and final_count == shot_count:
            phase = "已完成"
            category = "completed"
        elif int(project.get("active_job_count") or 0):
            phase = "生成中"
            category = "production"
        elif int(project.get("job_count") or 0) or int(project.get("candidate_count") or 0) or final_count:
            phase = "制作中"
            category = "production"
        else:
            phase = "筹备中"
            category = "planning"
        projects.append({
            **project,
            "active": bool(current and project["id"] == current["id"]),
            "phase": phase,
            "category": category,
            "progress": round(final_count / shot_count * 100) if shot_count else 0,
        })

    generation_activity = rows(
        """SELECT 'job-' || jobs.id AS id, 'generation' AS item_type, jobs.kind,
        jobs.state, jobs.message, jobs.created_at, COALESCE(jobs.updated_at, jobs.created_at) AS updated_at,
        shots.id AS shot_id, shots.title, projects.id AS project_id, projects.title AS project_title
        FROM jobs JOIN shots ON shots.id = jobs.shot_id
        JOIN projects ON projects.id = shots.project_id
        ORDER BY jobs.id DESC LIMIT 40"""
    )
    export_activity = rows(
        """SELECT 'export-' || export_runs.id AS id, 'export' AS item_type, 'export' AS kind,
        export_runs.state, export_runs.message, export_runs.created_at, export_runs.updated_at,
        NULL AS shot_id, export_runs.output_name AS title, projects.id AS project_id, projects.title AS project_title
        FROM export_runs JOIN projects ON projects.id = export_runs.project_id
        ORDER BY export_runs.updated_at DESC LIMIT 20"""
    )
    activities = sorted(
        [*generation_activity, *export_activity], key=lambda item: item.get("updated_at") or item["created_at"], reverse=True
    )[:30]
    queue_items = [
        item for item in activities
        if item["state"] in {*ACTIVE_JOB_STATES, *EXPORT_ACTIVE_STATES}
    ]
    pending_generation = row(
        "SELECT COUNT(*) AS count FROM shots WHERE status IN ('未生成', '可生成')"
    ) or {"count": 0}
    pending_review = row(
        """SELECT COUNT(DISTINCT candidates.shot_id) AS count FROM candidates
        JOIN shots ON shots.id = candidates.shot_id
        WHERE candidates.status = 'completed' AND candidates.archived = 0
        AND candidates.selected = 0 AND shots.status <> '已定稿'"""
    ) or {"count": 0}
    return {
        "summary": {
            "project_count": len(projects),
            "production_count": sum(1 for project in projects if project["category"] == "production"),
            "archived_count": sum(1 for project in projects if project["category"] == "archived"),
            "pending_generation": pending_generation["count"],
            "pending_review": pending_review["count"],
        },
        "projects": projects,
        "activities": activities,
        "queue": queue_items,
        "storage": storage_payload(),
    }


def project_detail(project: dict[str, Any]) -> dict[str, Any]:
    project = dict(project)
    project["shots"] = rows("SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal", (project["id"],))
    source_mappings = {
        item["shot_id"]: item
        for item in rows(
            """SELECT links.shot_id, links.section_id, links.last_synced_revision,
            sections.title AS section_title, sections.revision AS current_section_revision
            FROM creative_storyboard_links links
            JOIN creative_sections sections ON sections.id = links.section_id
            WHERE links.project_id = ?""",
            (project["id"],),
        )
    }
    for shot in project["shots"]:
        shot["source_mapping"] = source_mappings.get(shot["id"])
        final_output = row(
            "SELECT * FROM promotions WHERE shot_id = ? AND selected = 1 ORDER BY selected_at DESC LIMIT 1",
            (shot["id"],),
        )
        shot["final_output"] = promotion_public(final_output) if final_output else None
    return project


@app.get("/api/projects")
def get_projects() -> list[dict[str, Any]]:
    current = active_project()
    return [
        {**project, "active": bool(current and project["id"] == current["id"])}
        for project in rows(
            """SELECT projects.*,
            (SELECT COUNT(*) FROM shots WHERE shots.project_id = projects.id) AS shot_count,
            (SELECT COUNT(*) FROM shots WHERE shots.project_id = projects.id AND shots.status = '已定稿') AS final_count
            FROM projects ORDER BY created_at DESC"""
        )
    ]


@app.get("/api/projects/{project_id}/thumbnail")
def project_thumbnail(project_id: str) -> FileResponse:
    if not row("SELECT id FROM projects WHERE id = ?", (project_id,)):
        raise HTTPException(404, "项目不存在")
    candidate = row(
        """SELECT candidates.thumbnail_file FROM candidates
        JOIN shots ON shots.id = candidates.shot_id
        WHERE shots.project_id = ? AND candidates.selected = 1 AND candidates.archived = 0
        AND candidates.thumbnail_file IS NOT NULL AND candidates.thumbnail_file <> ''
        ORDER BY shots.ordinal LIMIT 1""",
        (project_id,),
    )
    if not candidate:
        raise HTTPException(404, "项目还没有可用封面")
    return FileResponse(allowed_output_file(candidate["thumbnail_file"]), media_type="image/jpeg")


@app.post("/api/projects")
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    project_id = f"project-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    now = utc_now()
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO projects (id, title, episode, logline, target_duration, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, payload.title.strip(), payload.episode.strip(), payload.logline.strip(), payload.target_duration, now),
        )
        for ordinal, shot in enumerate(payload.shots, start=1):
            shot_id = f"{project_id}-S01-{ordinal:03d}"
            db.execute(
                """INSERT INTO shots
                (id, project_id, ordinal, scene_code, title, description, dialogue, prompt, status,
                 width, height, seconds, candidate_count, strategy, thumbnail, video, updated_at)
                VALUES (?, ?, ?, 'S01', ?, ?, ?, ?, '未生成', 608, 352, 5.17, 2,
                        'Ref2VA 精修', '', NULL, ?)""",
                (
                    shot_id, project_id, ordinal, shot.title.strip(), shot.description.strip(),
                    shot.dialogue.strip(), shot.prompt.strip(), now,
                ),
            )
        db.execute(
            """INSERT INTO workspace_settings (key, value, updated_at)
            VALUES ('active_project_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (project_id, now),
        )
        db.commit()
    return project_detail(row("SELECT * FROM projects WHERE id = ?", (project_id,)) or {})


@app.post("/api/projects/{project_id}/activate")
def activate_project(project_id: str) -> dict[str, Any]:
    set_active_project(project_id)
    return project_detail(row("SELECT * FROM projects WHERE id = ?", (project_id,)) or {})


@app.get("/api/project")
def get_project() -> dict[str, Any]:
    project = active_project()
    if not project:
        raise HTTPException(404, "项目不存在")
    return project_detail(project)


@app.get("/api/assets")
def get_assets() -> list[dict[str, Any]]:
    project = active_project()
    if not project:
        return []
    return [
        asset_public(item)
        for item in rows(
            "SELECT * FROM assets WHERE project_id = ? AND archived = 0 ORDER BY created_at DESC, kind, name",
            (project["id"],),
        )
    ]


@app.post("/api/assets")
async def create_asset(
    file: UploadFile = File(...),
    name: str = Form(...),
    kind: str = Form("参考素材"),
    description: str = Form(""),
    project_id: str = Form(""),
) -> dict[str, Any]:
    if not project_id:
        current = active_project()
        project_id = current["id"] if current else ""
    if not row("SELECT id FROM projects WHERE id = ?", (project_id,)):
        raise HTTPException(404, "项目不存在")
    clean_name = name.strip()
    if len(clean_name) < 2 or len(clean_name) > 80:
        raise HTTPException(400, "素材名称需为 2–80 个字符")
    source_name = Path(file.filename or "").name
    suffix = Path(source_name).suffix.lower()
    media_type = media_type_for_suffix(suffix)
    asset_id = f"asset-{uuid.uuid4().hex[:12]}"
    media_dir = (ASSET_ROOT / project_id / media_type).resolve()
    try:
        media_dir.relative_to(ASSET_ROOT)
    except ValueError as exc:
        raise HTTPException(400, "项目标识无法映射到受管素材目录") from exc
    media_dir.mkdir(parents=True, exist_ok=True)
    temp_path = media_dir / f".{asset_id}.upload"
    final_path = media_dir / f"{asset_id}{suffix}"
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with temp_path.open("xb") as target:
            while chunk := await file.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > ASSET_LIMITS[media_type]:
                    raise HTTPException(413, f"{media_type} 素材超过工作台大小限制")
                digest.update(chunk)
                target.write(chunk)
        if size_bytes == 0:
            raise HTTPException(400, "不能导入空文件")
        temp_path.replace(final_path)
        probe = probe_media(final_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    mime_type = mimetypes.guess_type(source_name)[0] or "application/octet-stream"
    now = utc_now()
    with closing(connect()) as db:
        db.execute(
            """INSERT INTO assets
            (id, project_id, kind, name, description, preview, locked, source, source_path,
             managed_path, mime_type, media_type, size_bytes, checksum_sha256, width, height,
             duration_seconds, has_audio, created_at, archived, metadata)
            VALUES (?, ?, ?, ?, ?, '', 0, 'managed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}')""",
            (
                asset_id, project_id, kind.strip() or "参考素材", clean_name, description.strip(), source_name,
                str(final_path), mime_type, media_type, size_bytes, digest.hexdigest(), probe["width"], probe["height"],
                probe["duration_seconds"], probe["has_audio"], now,
            ),
        )
        db.commit()
    return asset_public(row("SELECT * FROM assets WHERE id = ?", (asset_id,)) or {})


@app.get("/api/assets/{asset_id}/content")
def asset_content(asset_id: str) -> FileResponse:
    asset = require_active_asset(asset_id)
    if asset.get("source") != "managed":
        raise HTTPException(404, "受管素材不存在")
    return FileResponse(allowed_asset_file(asset.get("managed_path")), media_type=asset.get("mime_type"))


@app.patch("/api/assets/{asset_id}")
def update_asset(asset_id: str, payload: AssetPatch) -> dict[str, Any]:
    require_active_asset(asset_id)
    values = payload.model_dump(exclude_none=True)
    if not values:
        asset = row("SELECT * FROM assets WHERE id = ? AND archived = 0", (asset_id,))
        if not asset:
            raise HTTPException(404, "素材不存在")
        return asset_public(asset)
    values = {key: value.strip() for key, value in values.items()}
    assignments = ", ".join(f"{key} = ?" for key in values)
    with closing(connect()) as db:
        cursor = db.execute(f"UPDATE assets SET {assignments} WHERE id = ? AND archived = 0", (*values.values(), asset_id))
        if cursor.rowcount == 0:
            raise HTTPException(404, "素材不存在")
        db.commit()
    return asset_public(row("SELECT * FROM assets WHERE id = ?", (asset_id,)) or {})


@app.get("/api/shots/{shot_id}/references")
def get_shot_references(shot_id: str) -> list[dict[str, Any]]:
    require_active_shot(shot_id)
    return reference_rows(shot_id)


@app.post("/api/shots/{shot_id}/references")
def bind_shot_reference(shot_id: str, payload: ReferenceCreate) -> list[dict[str, Any]]:
    shot = require_active_shot(shot_id)
    asset = require_active_asset(payload.asset_id)
    if asset.get("project_id") != shot["project_id"]:
        raise HTTPException(400, "素材与镜头不属于同一项目")
    if asset.get("source") != "managed" or not asset.get("managed_path"):
        raise HTTPException(400, "演示素材没有本地受管文件，不能绑定到 Ref2VA")
    allowed_asset_file(asset["managed_path"])
    media_type = asset.get("media_type")
    if media_type not in REFERENCE_LIMITS:
        raise HTTPException(400, "素材媒体类型无效")
    if payload.role not in REFERENCE_ROLES[media_type]:
        raise HTTPException(400, f"{media_type} 不支持角色：{payload.role}")
    with closing(connect()) as db:
        count = db.execute(
            "SELECT COUNT(*) AS count FROM shot_references WHERE shot_id = ? AND reference_type = ?",
            (shot_id, media_type),
        ).fetchone()["count"]
        if count >= REFERENCE_LIMITS[media_type]:
            raise HTTPException(400, f"{media_type} 参考最多 {REFERENCE_LIMITS[media_type]} 个")
        reference_id = f"ref-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        try:
            db.execute(
                """INSERT INTO shot_references
                (id, shot_id, asset_id, reference_type, ordinal, role, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (reference_id, shot_id, payload.asset_id, media_type, count + 1, payload.role, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该素材已经绑定到这个镜头") from exc
        mark_prompt_plans_stale(db, shot["project_id"], "镜头参考素材已绑定", shot_ids=[shot_id])
        db.commit()
    return reference_rows(shot_id)


@app.patch("/api/shots/{shot_id}/references/{reference_id}")
def update_shot_reference(shot_id: str, reference_id: str, payload: ReferencePatch) -> list[dict[str, Any]]:
    require_active_shot(shot_id)
    reference = row("SELECT * FROM shot_references WHERE id = ? AND shot_id = ?", (reference_id, shot_id))
    if not reference:
        raise HTTPException(404, "镜头引用不存在")
    if payload.role not in REFERENCE_ROLES[reference["reference_type"]]:
        raise HTTPException(400, f"{reference['reference_type']} 不支持角色：{payload.role}")
    with closing(connect()) as db:
        db.execute("UPDATE shot_references SET role = ?, updated_at = ? WHERE id = ?", (payload.role, utc_now(), reference_id))
        shot = db.execute("SELECT project_id FROM shots WHERE id = ?", (shot_id,)).fetchone()
        mark_prompt_plans_stale(db, shot["project_id"], "镜头参考素材角色已变化", shot_ids=[shot_id])
        db.commit()
    return reference_rows(shot_id)


@app.put("/api/shots/{shot_id}/references/order")
def reorder_shot_references(shot_id: str, payload: ReferenceOrder) -> list[dict[str, Any]]:
    require_active_shot(shot_id)
    existing = rows(
        "SELECT id FROM shot_references WHERE shot_id = ? AND reference_type = ? ORDER BY ordinal",
        (shot_id, payload.reference_type),
    )
    existing_ids = [item["id"] for item in existing]
    if len(payload.reference_ids) != len(set(payload.reference_ids)) or set(payload.reference_ids) != set(existing_ids):
        raise HTTPException(400, "排序必须包含该媒体类型的全部引用，且不能重复")
    with closing(connect()) as db:
        for ordinal, reference_id in enumerate(payload.reference_ids, start=1):
            db.execute(
                "UPDATE shot_references SET ordinal = ?, updated_at = ? WHERE id = ? AND shot_id = ?",
                (ordinal, utc_now(), reference_id, shot_id),
            )
        shot = db.execute("SELECT project_id FROM shots WHERE id = ?", (shot_id,)).fetchone()
        mark_prompt_plans_stale(db, shot["project_id"], "镜头参考素材顺序已变化", shot_ids=[shot_id])
        db.commit()
    return reference_rows(shot_id)


@app.delete("/api/shots/{shot_id}/references/{reference_id}")
def unbind_shot_reference(shot_id: str, reference_id: str) -> list[dict[str, Any]]:
    require_active_shot(shot_id)
    reference = row("SELECT * FROM shot_references WHERE id = ? AND shot_id = ?", (reference_id, shot_id))
    if not reference:
        raise HTTPException(404, "镜头引用不存在")
    with closing(connect()) as db:
        db.execute("DELETE FROM shot_references WHERE id = ? AND shot_id = ?", (reference_id, shot_id))
        normalize_reference_ordinals(db, shot_id, reference["reference_type"])
        shot = db.execute("SELECT project_id FROM shots WHERE id = ?", (shot_id,)).fetchone()
        mark_prompt_plans_stale(db, shot["project_id"], "镜头参考素材已解绑", shot_ids=[shot_id])
        db.commit()
    return reference_rows(shot_id)


@app.get("/api/jobs")
def get_jobs() -> list[dict[str, Any]]:
    project = active_project()
    if not project:
        return []
    result = rows(
        """SELECT jobs.*, shots.title FROM jobs
        JOIN shots ON shots.id = jobs.shot_id
        WHERE shots.project_id = ? ORDER BY jobs.id DESC""",
        (project["id"],),
    )
    for item in result:
        try:
            item["prompt_ids"] = json.loads(item.get("prompt_ids") or "[]")
        except json.JSONDecodeError:
            item["prompt_ids"] = []
    return result


@app.get("/api/shots/{shot_id}/candidates")
def get_candidates(shot_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
    require_active_shot(shot_id)
    query = "SELECT * FROM candidates WHERE shot_id = ?"
    if not include_archived:
        query += " AND archived = 0"
    query += " ORDER BY created_at, label"
    return [candidate_public(item) for item in rows(query, (shot_id,))]


@app.get("/api/shots/{shot_id}/promotions")
def get_promotions(shot_id: str) -> list[dict[str, Any]]:
    require_active_shot(shot_id)
    return [
        promotion_public(item)
        for item in rows("SELECT * FROM promotions WHERE shot_id = ? ORDER BY created_at, external_id", (shot_id,))
    ]


def allowed_output_file(file_path: str | None) -> Path:
    if not file_path:
        raise HTTPException(404, "候选产物尚未生成")
    path = Path(file_path).resolve()
    try:
        path.relative_to(COMFY_OUTPUT_ROOT)
    except ValueError as exc:
        raise HTTPException(403, "候选产物不在允许的 ComfyUI 输出目录") from exc
    if not path.is_file():
        raise HTTPException(404, "候选产物文件不存在")
    return path


def export_run_public(item: dict[str, Any], include_events: bool = False) -> dict[str, Any]:
    item = dict(item)
    item.pop("source_snapshot", None)
    for field in ("config", "outputs"):
        try:
            item[field] = json.loads(item.get(field) or "{}")
        except json.JSONDecodeError:
            item[field] = {}
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    item["is_current"] = bool(item.get("is_current"))
    item["can_cancel"] = item.get("state") in EXPORT_ACTIVE_STATES and not item["cancel_requested"]
    item["can_retry"] = item.get("state") in ("失败", "已取消")
    item["can_activate"] = item.get("state") == "已完成" and not item["is_current"]
    item["log_available"] = bool(item.get("log_file") and Path(item["log_file"]).is_file())
    if include_events:
        item["events"] = rows(
            "SELECT id, level, event, message, created_at FROM export_events WHERE run_id = ? ORDER BY id DESC LIMIT 12",
            (item["id"],),
        )[::-1]
    return item


def export_preflight_payload(include_private: bool = False) -> dict[str, Any]:
    project = active_project()
    if not project:
        raise HTTPException(404, "项目不存在")
    current_shots = rows("SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal", (project["id"],))
    issues: list[dict[str, str]] = []
    assembly = locked_delivery_plan(DB_PATH, project["id"])
    if not assembly:
        issues.append({"shot_id": "delivery-plan", "title": "成片装配", "message": "装配计划尚未锁定"})
        shots = current_shots
    else:
        shots_by_id = {shot["id"]: shot for shot in current_shots}
        assembly_ids = [item["shot_id"] for item in assembly["items"]]
        if set(assembly_ids) != set(shots_by_id):
            issues.append({"shot_id": "delivery-plan", "title": "成片装配", "message": "锁定装配与当前项目镜头不一致，请创建新修订"})
        shots = []
        for item in assembly["items"]:
            if item["shot_id"] not in shots_by_id:
                continue
            shots.append({
                **shots_by_id[item["shot_id"]],
                "ordinal": item["ordinal"],
                "subtitle_enabled": item["subtitle_enabled"],
                "subtitle_start_seconds": item["subtitle_start_seconds"],
            })
    sources: list[dict[str, Any]] = []
    total_seconds = 0.0
    for shot in shots:
        selected = row(
            """SELECT id, 'promotion' AS source_type, status, output_file, strategy AS source_detail
            FROM promotions WHERE shot_id = ? AND selected = 1
            ORDER BY selected_at DESC LIMIT 1""",
            (shot["id"],),
        )
        if not selected:
            selected = row(
                """SELECT id, 'candidate' AS source_type, status, output_file, source AS source_detail
                FROM candidates WHERE shot_id = ? AND selected = 1 AND archived = 0
                ORDER BY created_at DESC LIMIT 1""",
                (shot["id"],),
            )
        issue = None
        if not selected:
            issue = "尚未选择草稿母版或最终成片"
        elif selected.get("status") != "completed":
            issue = f"所选版本状态为 {selected.get('status')}"
        elif selected.get("source_type") == "candidate" and selected.get("source_detail") != "h3":
            issue = "所选版本仍是演示数据，不允许进入生产导出"
        else:
            try:
                source_path = allowed_output_file(selected.get("output_file"))
                probe = probe_media(source_path)
                if not probe.get("has_audio"):
                    issue = "所选视频没有音轨"
                else:
                    duration = float(probe.get("duration_seconds") or 0)
                    total_seconds += duration
                    source = {
                        "shot_id": shot["id"],
                        "ordinal": shot["ordinal"],
                        "title": shot["title"],
                        "source_type": selected["source_type"],
                        "source_id": selected["id"],
                        "source_detail": selected.get("source_detail"),
                        "duration_seconds": duration,
                        "width": probe.get("width"),
                        "height": probe.get("height"),
                        "has_audio": bool(probe.get("has_audio")),
                    }
                    if include_private:
                        source.update(
                            {
                                "path": str(source_path),
                                "dialogue": shot.get("dialogue") or "",
                                "subtitle_enabled": bool(shot.get("subtitle_enabled", 1)),
                                "subtitle_start_seconds": shot.get("subtitle_start_seconds"),
                            }
                        )
                    sources.append(source)
            except HTTPException as exc:
                issue = str(exc.detail)
        if issue:
            issues.append({"shot_id": shot["id"], "title": shot["title"], "message": issue})
    if not shots:
        issues.append({"shot_id": "project", "title": project["title"], "message": "项目还没有镜头"})
    return {
        "ready": not issues and len(sources) == len(shots),
        "project_id": project["id"],
        "project_title": project["title"],
        "shot_count": len(shots),
        "ready_shot_count": len(sources),
        "duration_seconds": round(total_seconds, 3),
        "source_policy": "selected_only",
        "delivery_plan": None if not assembly else {
            "id": assembly["id"], "revision": assembly["revision"], "plan_hash": assembly["plan_hash"], "status": assembly["status"],
        },
        "sources": sources,
        "issues": issues,
    }


def allowed_export_file(stem: str, suffix: str) -> Path:
    path = (EXPORT_ROOT / f"{stem}{suffix}").resolve()
    try:
        path.relative_to(EXPORT_ROOT)
    except ValueError as exc:
        raise HTTPException(403, "导出产物不在允许目录") from exc
    if not path.is_file():
        raise HTTPException(404, "导出产物不存在")
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def record_export_event(run_id: str, event: str, message: str, level: str = "info") -> None:
    with closing(connect()) as db:
        db.execute(
            "INSERT INTO export_events (run_id, level, event, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, level, event, message[:5000], utc_now()),
        )
        db.commit()


def cleanup_export_outputs(stem: str) -> None:
    for suffix in (".mp4", ".srt", ".vtt", ".sources.json", ".production.json", ".production.tmp.json"):
        path = (EXPORT_ROOT / f"{stem}{suffix}").resolve()
        try:
            path.relative_to(EXPORT_ROOT)
        except ValueError:
            continue
        path.unlink(missing_ok=True)


class ExportCancelled(RuntimeError):
    pass


class ExportInterrupted(RuntimeError):
    pass


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        capture_output=True,
        check=False,
        timeout=15,
    )


def export_cancel_requested(run_id: str) -> bool:
    current = row("SELECT cancel_requested FROM export_runs WHERE id = ?", (run_id,))
    return bool(current and current.get("cancel_requested"))


def claim_next_export_run() -> dict[str, Any] | None:
    now = utc_now()
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute(
            """SELECT * FROM export_runs
            WHERE state IN ('排队中', '恢复排队') AND cancel_requested = 0
            ORDER BY created_at, attempt LIMIT 1"""
        ).fetchone()
        if not pending:
            db.commit()
            return None
        changed = db.execute(
            """UPDATE export_runs SET state = '导出中', message = '工作进程已认领，正在准备输入快照',
            worker_id = ?, started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE id = ? AND state IN ('排队中', '恢复排队') AND cancel_requested = 0""",
            (EXPORT_WORKER_ID, now, now, pending["id"]),
        )
        if changed.rowcount != 1:
            db.rollback()
            return None
        db.execute(
            "INSERT INTO export_events (run_id, level, event, message, created_at) VALUES (?, 'info', 'claimed', ?, ?)",
            (pending["id"], f"任务由 {EXPORT_WORKER_ID} 认领", now),
        )
        claimed = db.execute("SELECT * FROM export_runs WHERE id = ?", (pending["id"],)).fetchone()
        db.commit()
        return dict(claimed) if claimed else None


def recover_export_runs() -> int:
    now = utc_now()
    recovered = 0
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        stranded = db.execute(
            "SELECT id, state, cancel_requested FROM export_runs WHERE state IN ('导出中', '取消中')"
        ).fetchall()
        for item in stranded:
            if item["cancel_requested"]:
                db.execute(
                    """UPDATE export_runs SET state = '已取消', message = '服务重启时完成取消', worker_id = NULL,
                    updated_at = ?, completed_at = ? WHERE id = ?""",
                    (now, now, item["id"]),
                )
                event, message = "cancelled", "服务重启时检测到取消请求，任务已终止"
            else:
                db.execute(
                    """UPDATE export_runs SET state = '恢复排队', message = '服务重启后等待恢复', worker_id = NULL,
                    recovery_count = recovery_count + 1, updated_at = ? WHERE id = ?""",
                    (now, item["id"]),
                )
                event, message = "recovered", "检测到未完成任务，已重新放入持久化队列"
                recovered += 1
            db.execute(
                "INSERT INTO export_events (run_id, level, event, message, created_at) VALUES (?, 'warning', ?, ?, ?)",
                (item["id"], event, message, now),
            )
        db.commit()
    return recovered


def wait_before_export_for_test(run_id: str) -> None:
    if EXPORT_TEST_DELAY_SECONDS <= 0:
        return
    deadline = time.monotonic() + EXPORT_TEST_DELAY_SECONDS
    record_export_event(run_id, "test_delay", f"测试延迟 {EXPORT_TEST_DELAY_SECONDS:.1f} 秒", "warning")
    while time.monotonic() < deadline:
        if export_cancel_requested(run_id):
            raise ExportCancelled("用户在导出开始前取消任务")
        if EXPORT_STOP_EVENT.is_set():
            raise ExportInterrupted("服务停止，任务等待下次启动恢复")
        time.sleep(0.1)


def run_export_job(run_id: str) -> None:
    export_run = row("SELECT * FROM export_runs WHERE id = ?", (run_id,))
    if not export_run:
        return
    try:
        wait_before_export_for_test(run_id)
        if export_cancel_requested(run_id):
            raise ExportCancelled("用户取消了导出任务")
        if not EXPORT_SCRIPT.is_file():
            raise RuntimeError(f"导出脚本不存在：{EXPORT_SCRIPT}")
        EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
        EXPORT_JOB_ROOT.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(export_run.get("source_snapshot") or "{}")
        if not snapshot.get("sources"):
            raise RuntimeError("导出任务缺少冻结的输入快照")
        snapshot_path = EXPORT_JOB_ROOT / f"{run_id}.request.json"
        snapshot_temp = EXPORT_JOB_ROOT / f"{run_id}.request.tmp.json"
        snapshot_temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot_temp.replace(snapshot_path)
        log_path = EXPORT_JOB_ROOT / f"{run_id}.log"
        with closing(connect()) as db:
            db.execute(
                "UPDATE export_runs SET message = '正在执行 FFmpeg 横屏合成', log_file = ?, updated_at = ? WHERE id = ?",
                (str(log_path), utc_now(), run_id),
            )
            db.commit()
        record_export_event(run_id, "snapshot_ready", f"已冻结 {len(snapshot['sources'])} 个镜头输入")
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(EXPORT_SCRIPT),
            "-ApiBase", API_BASE,
            "-OutputName", export_run["output_name"],
            "-OutputRoot", str(EXPORT_ROOT),
            "-SourceSnapshotPath", str(snapshot_path),
            "-Width", str(export_run["width"]),
            "-Height", str(export_run["height"]),
            "-RequireSelectedSources",
        ]
        if export_run["polish_audio"]:
            command.append("-PolishAudio")
        record_export_event(run_id, "ffmpeg_started", "FFmpeg 合成进程已启动")
        started = time.monotonic()
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
            with EXPORT_PROCESS_LOCK:
                EXPORT_PROCESSES[run_id] = process
            try:
                while process.poll() is None:
                    if export_cancel_requested(run_id):
                        terminate_process_tree(process)
                        raise ExportCancelled("用户取消了正在执行的导出任务")
                    if EXPORT_STOP_EVENT.is_set():
                        terminate_process_tree(process)
                        raise ExportInterrupted("服务停止，任务等待下次启动恢复")
                    if time.monotonic() - started > 3600:
                        terminate_process_tree(process)
                        raise RuntimeError("导出超过 60 分钟，已终止")
                    time.sleep(0.25)
            finally:
                with EXPORT_PROCESS_LOCK:
                    EXPORT_PROCESSES.pop(run_id, None)
        if process.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-5000:] if log_path.is_file() else ""
            raise RuntimeError(tail.strip() or f"FFmpeg 导出失败，退出码 {process.returncode}")
        record_export_event(run_id, "ffmpeg_completed", "FFmpeg 合成完成，开始验证产物")
        stem = export_run["output_name"]
        video_path = allowed_export_file(stem, ".mp4")
        subtitles_path = allowed_export_file(stem, ".srt")
        captions_path = allowed_export_file(stem, ".vtt")
        sources_path = allowed_export_file(stem, ".sources.json")
        sources = json.loads(sources_path.read_text(encoding="utf-8-sig"))
        verified_sources = []
        for source in sources:
            source_path = allowed_output_file(source.get("path"))
            verified_sources.append({**source, "sha256": file_sha256(source_path)})
        config = json.loads(export_run.get("config") or "{}")
        video_probe = probe_media(video_path)
        production_manifest_path = EXPORT_ROOT / f"{stem}.production.json"
        production_manifest_temp = EXPORT_ROOT / f"{stem}.production.tmp.json"
        production_manifest = {
            "schema_version": 2,
            "export_run_id": run_id,
            "project_id": export_run["project_id"],
            "attempt": export_run.get("attempt", 1),
            "parent_run_id": export_run.get("parent_run_id"),
            "recovery_count": export_run.get("recovery_count", 0),
            "created_at": export_run["created_at"],
            "completed_at": utc_now(),
            "config": config,
            "inputs": verified_sources,
            "outputs": {
                "video": {"file": video_path.name, "sha256": file_sha256(video_path), "probe": video_probe},
                "subtitles": {"file": subtitles_path.name, "sha256": file_sha256(subtitles_path)},
                "captions": {"file": captions_path.name, "sha256": file_sha256(captions_path)},
                "sources": {"file": sources_path.name, "sha256": file_sha256(sources_path)},
            },
        }
        production_manifest_temp.write_text(
            json.dumps(production_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        production_manifest_temp.replace(production_manifest_path)
        outputs = {
            "video": video_path.name,
            "subtitles": subtitles_path.name,
            "captions": captions_path.name,
            "sources": sources_path.name,
            "manifest": production_manifest_path.name,
            "duration_seconds": video_probe.get("duration_seconds"),
            "size_bytes": video_path.stat().st_size,
        }
        completed_at = utc_now()
        with closing(connect()) as db:
            db.execute("UPDATE export_runs SET is_current = 0 WHERE project_id = ?", (export_run["project_id"],))
            db.execute(
                """UPDATE export_runs SET state = '已完成', message = '横屏成片与生产清单已生成',
                outputs = ?, updated_at = ?, completed_at = ?, error = NULL, worker_id = NULL,
                cancel_requested = 0, is_current = 1 WHERE id = ?""",
                (json.dumps(outputs, ensure_ascii=False), completed_at, completed_at, run_id),
            )
            db.commit()
        record_export_event(run_id, "completed", "媒体校验与生产清单写入完成，已设为当前版本")
    except ExportCancelled as exc:
        cleanup_export_outputs(export_run["output_name"])
        cancelled_at = utc_now()
        with closing(connect()) as db:
            db.execute(
                """UPDATE export_runs SET state = '已取消', message = '导出已取消，未完成产物已清理',
                error = NULL, worker_id = NULL, updated_at = ?, completed_at = ? WHERE id = ?""",
                (cancelled_at, cancelled_at, run_id),
            )
            db.commit()
        record_export_event(run_id, "cancelled", str(exc), "warning")
    except ExportInterrupted as exc:
        cleanup_export_outputs(export_run["output_name"])
        with closing(connect()) as db:
            db.execute(
                """UPDATE export_runs SET state = '恢复排队', message = '服务停止，等待下次启动恢复',
                worker_id = NULL, recovery_count = recovery_count + 1, updated_at = ? WHERE id = ?""",
                (utc_now(), run_id),
            )
            db.commit()
        record_export_event(run_id, "interrupted", str(exc), "warning")
    except Exception as exc:
        cleanup_export_outputs(export_run["output_name"])
        failed_at = utc_now()
        with closing(connect()) as db:
            db.execute(
                """UPDATE export_runs SET state = '失败', message = '导出失败', error = ?,
                worker_id = NULL, updated_at = ?, completed_at = ? WHERE id = ?""",
                (str(exc)[-5000:], failed_at, failed_at, run_id),
            )
            db.commit()
        record_export_event(run_id, "failed", str(exc)[-5000:], "error")


def export_worker_loop() -> None:
    while not EXPORT_STOP_EVENT.is_set():
        claimed = claim_next_export_run()
        if claimed:
            run_export_job(claimed["id"])
            continue
        EXPORT_WAKE_EVENT.wait(1.0)
        EXPORT_WAKE_EVENT.clear()


def start_export_worker() -> int:
    global EXPORT_WORKER_THREAD
    EXPORT_STOP_EVENT.clear()
    EXPORT_WAKE_EVENT.clear()
    recovered = recover_export_runs()
    EXPORT_WORKER_THREAD = threading.Thread(target=export_worker_loop, name="jingchang-export-worker", daemon=True)
    EXPORT_WORKER_THREAD.start()
    EXPORT_WAKE_EVENT.set()
    return recovered


def stop_export_worker() -> None:
    EXPORT_STOP_EVENT.set()
    EXPORT_WAKE_EVENT.set()
    if EXPORT_WORKER_THREAD and EXPORT_WORKER_THREAD.is_alive():
        EXPORT_WORKER_THREAD.join(timeout=20)


@app.get("/api/candidates/{candidate_id}/video")
def candidate_video(candidate_id: str) -> FileResponse:
    candidate = require_active_candidate(candidate_id)
    return FileResponse(allowed_output_file(candidate.get("output_file")), media_type="video/mp4")


@app.get("/api/candidates/{candidate_id}/thumbnail")
def candidate_thumbnail(candidate_id: str) -> FileResponse:
    candidate = require_active_candidate(candidate_id)
    return FileResponse(allowed_output_file(candidate.get("thumbnail_file")), media_type="image/jpeg")


@app.post("/api/candidates/{candidate_id}/extract-frame")
def extract_candidate_frame(candidate_id: str, payload: FrameExtractRequest) -> dict[str, Any]:
    require_active_candidate(candidate_id)
    candidate = row(
        """SELECT candidates.*, shots.project_id, shots.title AS shot_title
        FROM candidates JOIN shots ON shots.id = candidates.shot_id
        WHERE candidates.id = ? AND candidates.archived = 0""",
        (candidate_id,),
    )
    if not candidate:
        raise HTTPException(404, "候选不存在")
    if candidate.get("status") != "completed" or not candidate.get("output_file"):
        raise HTTPException(409, "候选视频尚未生成完成")
    video_path = allowed_output_file(candidate["output_file"])
    video_probe = probe_media(video_path)
    duration = float(video_probe.get("duration_seconds") or 0)
    seconds = round(float(payload.seconds), 3)
    if duration <= 0 or seconds > duration:
        raise HTTPException(400, f"提取时间点必须位于 0–{duration:.3f} 秒")

    source_locator = f"candidate:{candidate_id}@{seconds:.3f}s"
    existing = row(
        "SELECT * FROM assets WHERE source = 'managed' AND source_path = ? AND archived = 0",
        (source_locator,),
    )
    if existing:
        return {**asset_public(existing), "created": False}

    asset_id = f"asset-{uuid.uuid4().hex[:12]}"
    media_dir = (ASSET_ROOT / candidate["project_id"] / "image").resolve()
    try:
        media_dir.relative_to(ASSET_ROOT)
    except ValueError as exc:
        raise HTTPException(400, "项目标识无法映射到受管素材目录") from exc
    media_dir.mkdir(parents=True, exist_ok=True)
    temp_path = media_dir / f".{asset_id}.tmp.png"
    final_path = media_dir / f"{asset_id}.png"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seconds:.3f}",
        "-i", str(video_path), "-frames:v", "1", str(temp_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
        )
        if completed.returncode != 0 or not temp_path.is_file():
            raise HTTPException(502, completed.stderr.strip() or "候选帧提取失败")
        temp_path.replace(final_path)
        image_probe = probe_media(final_path)
        digest = hashlib.sha256()
        with final_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except Exception:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise

    asset_name = payload.name.strip() if payload.name else f"{candidate['shot_title']} · {seconds:.2f} 秒人物帧"
    description = payload.description.strip() or (
        f"从 {candidate['shot_id']} 候选 {candidate.get('label') or candidate_id} 的 {seconds:.2f} 秒提取；"
        "用于后续镜头的人物与服装连续性。"
    )
    metadata = json.dumps(
        {
            "provenance": {
                "type": "candidate_frame",
                "candidate_id": candidate_id,
                "shot_id": candidate["shot_id"],
                "source_file": str(video_path),
                "seconds": seconds,
            }
        },
        ensure_ascii=False,
    )
    now = utc_now()
    try:
        with closing(connect()) as db:
            db.execute(
                """INSERT INTO assets
                (id, project_id, kind, name, description, preview, locked, source, source_path,
                 managed_path, mime_type, media_type, size_bytes, checksum_sha256, width, height,
                 duration_seconds, has_audio, created_at, archived, metadata)
                VALUES (?, ?, ?, ?, ?, '', 0, 'managed', ?, ?, 'image/png', 'image', ?, ?, ?, ?, NULL, 0, ?, 0, ?)""",
                (
                    asset_id, candidate["project_id"], payload.kind.strip(), asset_name, description, source_locator,
                    str(final_path), final_path.stat().st_size, digest.hexdigest(), image_probe["width"],
                    image_probe["height"], now, metadata,
                ),
            )
            db.commit()
    except Exception:
        final_path.unlink(missing_ok=True)
        raise
    created = row("SELECT * FROM assets WHERE id = ?", (asset_id,))
    return {**asset_public(created or {}), "created": True}


@app.get("/api/promotions/{promotion_id}/video")
def promotion_video(promotion_id: str) -> FileResponse:
    promotion = require_active_promotion(promotion_id)
    return FileResponse(allowed_output_file(promotion.get("output_file")), media_type="video/mp4")


@app.get("/api/exports/preflight")
def export_preflight() -> dict[str, Any]:
    return export_preflight_payload()


@app.get("/api/exports")
def get_export_runs() -> list[dict[str, Any]]:
    project = active_project()
    if not project:
        return []
    return [
        export_run_public(item, include_events=True)
        for item in rows(
            "SELECT * FROM export_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 20",
            (project["id"],),
        )
    ]


@app.post("/api/exports")
def create_export(payload: ExportRequest) -> dict[str, Any]:
    if payload.width % 32 or payload.height % 32:
        raise HTTPException(400, "导出宽高必须能被 32 整除")
    preflight = export_preflight_payload(include_private=True)
    if not preflight["ready"]:
        raise HTTPException(409, {"message": "导出前检查未通过", "issues": preflight["issues"]})
    run_id = f"export-{uuid.uuid4().hex[:12]}"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    safe_project = re.sub(r"[^a-z0-9-]+", "-", preflight["project_id"].lower()).strip("-")
    output_name = f"{safe_project}-platform-{timestamp}"
    now = utc_now()
    private_keys = {"path", "dialogue", "subtitle_enabled", "subtitle_start_seconds"}
    public_sources = [
        {key: value for key, value in source.items() if key not in private_keys}
        for source in preflight["sources"]
    ]
    source_snapshot = {
        "schema_version": 2,
        "captured_at": now,
        "project_id": preflight["project_id"],
        "project_title": preflight["project_title"],
        "delivery_plan": preflight["delivery_plan"],
        "sources": preflight["sources"],
    }
    snapshot_json = json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True)
    config = {
        "profile": "horizontal-production-v2",
        "width": payload.width,
        "height": payload.height,
        "fps": 24,
        "polish_audio": payload.polish_audio,
        "source_policy": "selected_only",
        "shot_count": preflight["shot_count"],
        "preflight_sources": public_sources,
        "source_snapshot_sha256": hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
        "delivery_plan_hash": preflight["delivery_plan"]["plan_hash"],
        "delivery_plan_revision": preflight["delivery_plan"]["revision"],
    }
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT id FROM export_runs WHERE state IN ('排队中', '恢复排队', '导出中', '取消中') LIMIT 1"
        ).fetchone()
        if active:
            db.rollback()
            raise HTTPException(409, "已有导出任务正在运行")
        db.execute(
            """INSERT INTO export_runs
            (id, project_id, state, message, output_name, width, height, polish_audio,
             config, outputs, created_at, updated_at, source_snapshot, attempt)
            VALUES (?, ?, '排队中', '等待持久化工作进程认领', ?, ?, ?, ?, ?, '{}', ?, ?, ?, 1)""",
            (
                run_id, preflight["project_id"], output_name, payload.width, payload.height,
                int(payload.polish_audio), json.dumps(config, ensure_ascii=False), now, now, snapshot_json,
            ),
        )
        db.execute(
            "INSERT INTO export_events (run_id, level, event, message, created_at) VALUES (?, 'info', 'queued', ?, ?)",
            (run_id, f"已冻结 {len(preflight['sources'])} 个镜头来源并进入队列", now),
        )
        db.commit()
    EXPORT_WAKE_EVENT.set()
    return export_run_public(row("SELECT * FROM export_runs WHERE id = ?", (run_id,)) or {}, include_events=True)


@app.get("/api/export-runs/{run_id}")
def get_export_run(run_id: str) -> dict[str, Any]:
    export_run = require_active_export_run(run_id)
    return export_run_public(export_run, include_events=True)


@app.post("/api/export-runs/{run_id}/cancel")
def cancel_export_run(run_id: str) -> dict[str, Any]:
    require_active_export_run(run_id)
    now = utc_now()
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        export_run = db.execute("SELECT * FROM export_runs WHERE id = ?", (run_id,)).fetchone()
        if not export_run:
            db.rollback()
            raise HTTPException(404, "导出任务不存在")
        if export_run["state"] in EXPORT_TERMINAL_STATES:
            db.commit()
            return export_run_public(dict(export_run), include_events=True)
        if export_run["state"] in ("排队中", "恢复排队"):
            db.execute(
                """UPDATE export_runs SET state = '已取消', message = '排队任务已取消', cancel_requested = 1,
                worker_id = NULL, updated_at = ?, completed_at = ? WHERE id = ?""",
                (now, now, run_id),
            )
            message = "任务在执行前被取消"
        else:
            db.execute(
                """UPDATE export_runs SET state = '取消中', message = '正在终止 FFmpeg 进程',
                cancel_requested = 1, updated_at = ? WHERE id = ?""",
                (now, run_id),
            )
            message = "已记录取消请求，工作进程将终止当前任务"
        db.execute(
            "INSERT INTO export_events (run_id, level, event, message, created_at) VALUES (?, 'warning', 'cancel_requested', ?, ?)",
            (run_id, message, now),
        )
        db.commit()
    EXPORT_WAKE_EVENT.set()
    return export_run_public(row("SELECT * FROM export_runs WHERE id = ?", (run_id,)) or {}, include_events=True)


@app.post("/api/export-runs/{run_id}/retry")
def retry_export_run(run_id: str) -> dict[str, Any]:
    original = require_active_export_run(run_id)
    if original["state"] not in ("失败", "已取消"):
        raise HTTPException(409, "只有失败或已取消的任务可以重试")
    retry_id = f"export-{uuid.uuid4().hex[:12]}"
    attempt = int(original.get("attempt") or 1) + 1
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    safe_project = re.sub(r"[^a-z0-9-]+", "-", original["project_id"].lower()).strip("-")
    output_name = f"{safe_project}-platform-{timestamp}-retry{attempt}"
    now = utc_now()
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT id FROM export_runs WHERE state IN ('排队中', '恢复排队', '导出中', '取消中') LIMIT 1"
        ).fetchone()
        if active:
            db.rollback()
            raise HTTPException(409, "已有导出任务正在运行")
        db.execute(
            """INSERT INTO export_runs
            (id, project_id, state, message, output_name, width, height, polish_audio, config, outputs,
             created_at, updated_at, attempt, parent_run_id, source_snapshot)
            VALUES (?, ?, '排队中', '使用原输入快照重试', ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)""",
            (
                retry_id, original["project_id"], output_name, original["width"], original["height"],
                original["polish_audio"], original["config"], now, now, attempt, run_id,
                original.get("source_snapshot") or "{}",
            ),
        )
        db.execute(
            "INSERT INTO export_events (run_id, level, event, message, created_at) VALUES (?, 'info', 'retry_queued', ?, ?)",
            (retry_id, f"从 {run_id} 创建第 {attempt} 次尝试，沿用冻结输入", now),
        )
        db.commit()
    EXPORT_WAKE_EVENT.set()
    return export_run_public(row("SELECT * FROM export_runs WHERE id = ?", (retry_id,)) or {}, include_events=True)


@app.post("/api/export-runs/{run_id}/activate")
def activate_export_run(run_id: str) -> dict[str, Any]:
    export_run = require_active_export_run(run_id)
    if export_run["state"] != "已完成":
        raise HTTPException(409, "只有已完成版本可以设为当前版本")
    allowed_export_file(export_run["output_name"], ".mp4")
    now = utc_now()
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE export_runs SET is_current = 0 WHERE project_id = ?", (export_run["project_id"],))
        db.execute("UPDATE export_runs SET is_current = 1, updated_at = ? WHERE id = ?", (now, run_id))
        db.execute(
            "INSERT INTO export_events (run_id, level, event, message, created_at) VALUES (?, 'info', 'activated', '已人工切换为当前可交付版本', ?)",
            (run_id, now),
        )
        db.commit()
    return export_run_public(row("SELECT * FROM export_runs WHERE id = ?", (run_id,)) or {}, include_events=True)


@app.get("/api/export-runs/{run_id}/logs")
def get_export_logs(run_id: str) -> dict[str, Any]:
    export_run = require_active_export_run(run_id)
    log_text = ""
    if export_run.get("log_file"):
        log_path = Path(export_run["log_file"]).resolve()
        try:
            log_path.relative_to(EXPORT_JOB_ROOT)
        except ValueError as exc:
            raise HTTPException(403, "日志文件不在允许目录") from exc
        if log_path.is_file():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
    return {
        "run_id": run_id,
        "events": rows(
            "SELECT id, level, event, message, created_at FROM export_events WHERE run_id = ? ORDER BY id",
            (run_id,),
        ),
        "process_log_tail": log_text,
    }


@app.get("/api/export-runs/{run_id}/files/{artifact}")
def export_run_file(run_id: str, artifact: Literal["video", "subtitles", "captions", "sources", "manifest"]) -> FileResponse:
    export_run = require_active_export_run(run_id)
    if export_run["state"] != "已完成":
        raise HTTPException(409, "导出任务尚未完成")
    suffixes = {
        "video": (".mp4", "video/mp4"),
        "subtitles": (".srt", "application/x-subrip"),
        "captions": (".vtt", "text/vtt"),
        "sources": (".sources.json", "application/json"),
        "manifest": (".production.json", "application/json"),
    }
    suffix, media_type = suffixes[artifact]
    path = allowed_export_file(export_run["output_name"], suffix)
    return FileResponse(path, media_type=media_type, filename=path.name)


def current_export_record() -> tuple[str, dict[str, Any] | None]:
    project = active_project()
    if not project:
        return "", None
    latest = row(
        """SELECT * FROM export_runs WHERE project_id = ? AND state = '已完成'
        ORDER BY is_current DESC, completed_at DESC LIMIT 1""",
        (project["id"],),
    )
    if latest:
        return latest["output_name"], export_run_public(latest)
    if project["id"] == "rain-call-ep01":
        return ROUGH_CUT_STEM, None
    return "", None


@app.get("/api/exports/current")
def current_export() -> dict[str, Any]:
    stem, export_run = current_export_record()
    video_path = EXPORT_ROOT / f"{stem}.mp4"
    subtitle_path = EXPORT_ROOT / f"{stem}.srt"
    webvtt_path = EXPORT_ROOT / f"{stem}.vtt"
    sources_path = EXPORT_ROOT / f"{stem}.sources.json"
    manifest_path = EXPORT_ROOT / f"{stem}.production.json"
    if not video_path.is_file():
        return {"available": False}
    probe = probe_media(video_path)
    sources: list[dict[str, Any]] = []
    if sources_path.is_file():
        try:
            sources = json.loads(sources_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            sources = []
    return {
        "available": True,
        "name": "平台横屏生产导出" if export_run else "EP01 后期粗剪 v2.1",
        "video": "/api/exports/current/video",
        "subtitles": "/api/exports/current/subtitles" if subtitle_path.is_file() else None,
        "captions": "/api/exports/current/captions" if webvtt_path.is_file() else None,
        "sources": "/api/exports/current/sources" if sources_path.is_file() else None,
        "manifest": "/api/exports/current/manifest" if manifest_path.is_file() else None,
        "run": export_run,
        "width": probe.get("width"),
        "height": probe.get("height"),
        "duration_seconds": probe.get("duration_seconds"),
        "has_audio": bool(probe.get("has_audio")),
        "shot_count": len(sources),
        "size_bytes": video_path.stat().st_size,
        "updated_at": datetime.fromtimestamp(video_path.stat().st_mtime, timezone.utc).isoformat(),
        "quality_note": (
            "平台后台按已选版本合成；导出参数、输入来源、哈希和产物均写入生产清单。低清草稿仅做确定性放大。"
            if export_run else
            "画面锁定自 V2；已统一对白与环境响度、加入连续雨声底并校准字幕时点。低清草稿仍为确定性放大，不代表新增生成细节。"
        ),
    }


@app.get("/api/exports/current/video")
def current_export_video() -> FileResponse:
    stem, _ = current_export_record()
    video_path = allowed_export_file(stem, ".mp4")
    return FileResponse(video_path, media_type="video/mp4", filename=video_path.name)


@app.get("/api/exports/current/subtitles")
def current_export_subtitles() -> FileResponse:
    stem, _ = current_export_record()
    subtitle_path = allowed_export_file(stem, ".srt")
    return FileResponse(subtitle_path, media_type="application/x-subrip", filename=subtitle_path.name)


@app.get("/api/exports/current/captions")
def current_export_captions() -> FileResponse:
    stem, _ = current_export_record()
    webvtt_path = allowed_export_file(stem, ".vtt")
    return FileResponse(webvtt_path, media_type="text/vtt; charset=utf-8")


@app.get("/api/exports/current/sources")
def current_export_sources() -> FileResponse:
    stem, _ = current_export_record()
    sources_path = allowed_export_file(stem, ".sources.json")
    return FileResponse(sources_path, media_type="application/json", filename=sources_path.name)


@app.get("/api/exports/current/manifest")
def current_export_manifest() -> FileResponse:
    stem, export_run = current_export_record()
    if not export_run:
        raise HTTPException(404, "旧版粗剪没有生产清单")
    manifest_path = allowed_export_file(stem, ".production.json")
    return FileResponse(manifest_path, media_type="application/json", filename=manifest_path.name)


def acceptance_stage(stage_id: str, label: str, status: str, evidence: str, action: str = "") -> dict[str, str]:
    return {"id": stage_id, "label": label, "status": status, "evidence": evidence, "action": action}


def latest_delivery_signoffs(project_id: str) -> dict[str, dict[str, Any]]:
    records = rows(
        """SELECT signoffs.* FROM delivery_signoffs signoffs
        JOIN (
          SELECT category, MAX(revision) AS revision FROM delivery_signoffs
          WHERE project_id = ? GROUP BY category
        ) latest ON latest.category = signoffs.category AND latest.revision = signoffs.revision
        WHERE signoffs.project_id = ?""",
        (project_id, project_id),
    )
    return {record["category"]: record for record in records}


def production_acceptance_payload() -> dict[str, Any]:
    project = active_project()
    if not project:
        raise HTTPException(404, "项目不存在")
    project_id = project["id"]
    shots = rows("SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal", (project_id,))
    shot_count = len(shots)

    document = row("SELECT * FROM script_documents WHERE project_id = ?", (project_id,))
    scene_count = 0
    if document:
        scene_count = int((row(
            "SELECT COUNT(*) AS count FROM script_sections WHERE document_id = ? AND section_type = 'scene'",
            (document["id"],),
        ) or {"count": 0})["count"])
    script_pass = bool(document and document["status"] == "approved" and scene_count >= shot_count and shot_count)

    locked_coverage = 0
    for shot in shots:
        coverage = row(
            """SELECT COUNT(DISTINCT entries.id) AS count FROM production_bible_entries entries
            LEFT JOIN production_bible_shots links ON links.entry_id = entries.id AND links.shot_id = ?
            WHERE entries.project_id = ? AND entries.archived = 0 AND entries.status = 'locked'
            AND (entries.apply_globally = 1 OR links.shot_id IS NOT NULL)""",
            (shot["id"], project_id),
        ) or {"count": 0}
        if int(coverage["count"]):
            locked_coverage += 1

    prompt_plans = project_prompt_status(DB_PATH)
    approved_plans = sum(plan.get("ready") and plan.get("status") == "approved" for plan in prompt_plans)

    generation_stages = [
        acceptance_stage(
            "script", "批准剧本结构", "pass" if script_pass else "block",
            f"剧本 {document['status'] if document else '缺失'} · {scene_count}/{shot_count} 个镜头场景",
            "在剧本开发中批准当前版本并完整覆盖分镜" if not script_pass else "",
        ),
        acceptance_stage(
            "bible", "锁定连续性圣经", "pass" if locked_coverage == shot_count and shot_count else "block",
            f"{locked_coverage}/{shot_count} 个镜头有锁定规则覆盖",
            "补齐角色、场景或风格规则并锁定" if locked_coverage != shot_count else "",
        ),
        acceptance_stage(
            "prompt", "批准当前 H3 计划", "pass" if approved_plans == shot_count and shot_count else "block",
            f"{approved_plans}/{shot_count} 个当前计划已 dry-run 并批准",
            "在生成计划中重新编译、dry-run 并批准" if approved_plans != shot_count else "",
        ),
    ]
    generation_ready = all(stage["status"] == "pass" for stage in generation_stages)

    preflight = export_preflight_payload()
    selected_sources = int(preflight["ready_shot_count"])
    assembly_locked = bool(preflight.get("delivery_plan"))
    selected_candidate_ids = [
        item["id"] for item in rows(
            """SELECT candidates.id FROM candidates JOIN shots ON shots.id = candidates.shot_id
            WHERE shots.project_id = ? AND candidates.selected = 1 AND candidates.archived = 0""",
            (project_id,),
        )
    ]
    review_pass_count = 0
    for candidate_id in selected_candidate_ids:
        latest_review = row(
            "SELECT decision FROM candidate_reviews WHERE candidate_id = ? ORDER BY revision DESC LIMIT 1",
            (candidate_id,),
        )
        if latest_review and latest_review["decision"] == "pass":
            review_pass_count += 1

    deliverable = current_export()
    export_pass = bool(
        deliverable.get("available") and deliverable.get("has_audio")
        and int(deliverable.get("width") or 0) > int(deliverable.get("height") or 0)
        and int(deliverable.get("shot_count") or 0) == shot_count
    )
    signoffs = latest_delivery_signoffs(project_id)
    picture_pass = signoffs.get("picture_continuity", {}).get("decision") == "pass"
    sound_pass = signoffs.get("sound", {}).get("decision") == "pass"

    archive_record = row(
        """SELECT * FROM project_archives WHERE project_id = ? AND state = 'ready'
        ORDER BY revision DESC LIMIT 1""",
        (project_id,),
    )
    archive_pass = False
    archive_evidence = "尚无可校验归档"
    if archive_record:
        try:
            archive_manifest = json.loads(archive_record.get("manifest") or "{}")
        except json.JSONDecodeError:
            archive_manifest = {}
        omitted = len(archive_manifest.get("omitted_media", []))
        latest_signoff_at = max((record.get("created_at") or "" for record in signoffs.values()), default="")
        required_after = max(deliverable.get("updated_at") or "", latest_signoff_at)
        after_evidence = not required_after or archive_record["created_at"] >= required_after
        archive_pass = bool(archive_record.get("verified_at") and omitted == 0 and after_evidence)
        archive_evidence = f"R{archive_record['revision']} · 遗漏 {omitted} · {'晚于成片与人工确认' if after_evidence else '早于当前成片或人工确认'}"

    review_evidence = f"{review_pass_count}/{len(selected_candidate_ids)} 个已选候选有结构化通过记录"
    if selected_sources == shot_count and review_pass_count < len(selected_candidate_ids):
        review_evidence += "；旧项目由整片人工确认补充，历史逐候选证据仍不伪造"
    delivery_stages = [
        acceptance_stage(
            "sources", "真实镜头来源", "pass" if selected_sources == shot_count and shot_count else "block",
            f"{selected_sources}/{shot_count} 个镜头有真实已选本地版本",
            "完成生成、结构化审片并选择母版" if selected_sources != shot_count else "",
        ),
        acceptance_stage(
            "assembly", "锁定交付装配", "pass" if assembly_locked else "block",
            f"装配 R{preflight['delivery_plan']['revision']}" if assembly_locked else "装配计划尚未锁定",
            "在成片交付中核对顺序与字幕后锁定" if not assembly_locked else "",
        ),
        acceptance_stage(
            "candidate_review", "逐候选审片证据", "pass" if review_pass_count == len(selected_candidate_ids) and selected_candidate_ids else "warn",
            review_evidence,
            "新项目应逐候选完成结构化审片" if review_pass_count < len(selected_candidate_ids) else "",
        ),
        acceptance_stage(
            "export", "横屏成片技术检查", "pass" if export_pass else "block",
            (
                f"{deliverable.get('width')}×{deliverable.get('height')} · {deliverable.get('duration_seconds', 0):.3f} 秒 · "
                f"{deliverable.get('shot_count')} 镜头 · {'有音轨' if deliverable.get('has_audio') else '无音轨'}"
                if deliverable.get("available") else "尚无当前成片"
            ),
            "锁定装配并完成一次平台横屏导出" if not export_pass else "",
        ),
        acceptance_stage(
            "picture_signoff", "整片画面与连贯性确认", "pass" if picture_pass else "block",
            signoffs.get("picture_continuity", {}).get("note", "尚未人工确认"),
            "完整观看成片后在验收面板确认" if not picture_pass else "",
        ),
        acceptance_stage(
            "sound_signoff", "整片声音确认", "pass" if sound_pass else "block",
            signoffs.get("sound", {}).get("note", "尚未人工确认"),
            "完整听审对白、环境声和音量后确认" if not sound_pass else "",
        ),
        acceptance_stage(
            "archive", "交付后完整归档", "pass" if archive_pass else "block", archive_evidence,
            "在所有项目中创建并校验最新归档包" if not archive_pass else "",
        ),
    ]
    delivery_ready = all(stage["status"] != "block" for stage in delivery_stages)
    status = "deliverable" if delivery_ready else "production_ready" if generation_ready else "blocked"
    latest_run = row(
        "SELECT id, status, report_hash, created_at FROM production_acceptance_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    return {
        "project": {"id": project_id, "title": project["title"], "episode": project["episode"]},
        "status": status,
        "generation": {"ready": generation_ready, "stages": generation_stages},
        "delivery": {"ready": delivery_ready, "stages": delivery_stages, "signoffs": signoffs},
        "latest_run": latest_run,
        "generated_at": utc_now(),
    }


@app.get("/api/acceptance")
def get_production_acceptance() -> dict[str, Any]:
    return production_acceptance_payload()


@app.post("/api/acceptance/signoffs")
def create_delivery_signoff(payload: DeliverySignoffRequest) -> dict[str, Any]:
    project = active_project()
    if not project:
        raise HTTPException(404, "项目不存在")
    if not current_export().get("available"):
        raise HTTPException(409, "当前项目尚无完整成片，不能写入整片人工确认")
    with closing(connect()) as db:
        revision = int(db.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM delivery_signoffs WHERE project_id = ? AND category = ?",
            (project["id"], payload.category),
        ).fetchone()[0])
        record_id = f"delivery-signoff-{uuid.uuid4().hex[:12]}"
        db.execute(
            """INSERT INTO delivery_signoffs
            (id, project_id, category, revision, decision, note, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (record_id, project["id"], payload.category, revision, payload.decision, payload.note.strip(), payload.source.strip(), utc_now()),
        )
        db.commit()
    return production_acceptance_payload()


@app.post("/api/acceptance/run")
def run_production_acceptance() -> dict[str, Any]:
    report = production_acceptance_payload()
    stable = {key: value for key, value in report.items() if key not in ("generated_at", "latest_run")}
    report_hash = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    run_id = f"acceptance-{uuid.uuid4().hex[:12]}"
    created_at = utc_now()
    with closing(connect()) as db:
        db.execute(
            """INSERT INTO production_acceptance_runs
            (id, project_id, status, report_hash, report, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, report["project"]["id"], report["status"], report_hash, json.dumps(report, ensure_ascii=False), created_at),
        )
        db.commit()
    return {**report, "latest_run": {"id": run_id, "status": report["status"], "report_hash": report_hash, "created_at": created_at}}


@app.get("/api/acceptance/report")
def download_production_acceptance() -> Response:
    project = active_project()
    if not project:
        raise HTTPException(404, "项目不存在")
    latest = row(
        "SELECT * FROM production_acceptance_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (project["id"],),
    )
    if not latest:
        raise HTTPException(404, "尚未冻结生产验收报告")
    payload = {
        "schema_version": 1, "id": latest["id"], "project_id": project["id"],
        "status": latest["status"], "report_hash": latest["report_hash"],
        "created_at": latest["created_at"], "report": json.loads(latest["report"]),
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{project["id"]}-acceptance.json"'},
    )


@app.patch("/api/shots/{shot_id}")
def update_shot(shot_id: str, patch: ShotPatch) -> dict[str, Any]:
    shot = require_active_shot(shot_id)
    values = patch.model_dump(exclude_none=True)
    if values.get("width") and values["width"] % 32:
        raise HTTPException(400, "宽度必须能被 32 整除")
    if values.get("height") and values["height"] % 32:
        raise HTTPException(400, "高度必须能被 32 整除")
    if not values:
        found = row("SELECT * FROM shots WHERE id = ?", (shot_id,))
        if not found:
            raise HTTPException(404, "镜头不存在")
        return found
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with closing(connect()) as db:
        cursor = db.execute(f"UPDATE shots SET {assignments} WHERE id = ?", (*values.values(), shot_id))
        if cursor.rowcount == 0:
            raise HTTPException(404, "镜头不存在")
        if set(values) - {"status", "updated_at"}:
            mark_prompt_plans_stale(db, shot["project_id"], f"镜头“{shot['title']}”字段已手工修改", shot_ids=[shot_id])
        db.commit()
    return row("SELECT * FROM shots WHERE id = ?", (shot_id,))


@app.post("/api/shots")
def create_shot(payload: ShotCreate) -> dict[str, Any]:
    project = active_project()
    if not project:
        raise HTTPException(404, "项目不存在")
    with closing(connect()) as db:
        next_ordinal = db.execute(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM shots WHERE project_id = ?",
            (project["id"],),
        ).fetchone()["n"]
        shot_id = f"{project['id']}-S01-{next_ordinal:03d}"
        db.execute(
            """INSERT INTO shots
            (id, project_id, ordinal, scene_code, title, description, dialogue, prompt, status,
             width, height, seconds, candidate_count, strategy, thumbnail, video, updated_at)
            VALUES (?, ?, ?, 'S01', ?, ?, ?, ?, '未生成', 608, 352, 5.17, 2,
                    'Ref2VA 精修', '', NULL, ?)""",
            (
                shot_id, project["id"], next_ordinal, payload.title, payload.description,
                payload.dialogue, payload.prompt, utc_now(),
            ),
        )
        db.commit()
    return row("SELECT * FROM shots WHERE id = ?", (shot_id,))


def build_h3_arguments(
    shot: dict[str, Any],
    dry_run: bool,
    *,
    compiled_prompt_override: str | None = None,
    references_override: list[dict[str, Any]] | None = None,
    prompt_plan_hash: str | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    project = h3_project_for_shot(shot["id"])
    references = references_override if references_override is not None else reference_rows(shot["id"])
    counts = {
        media_type: sum(reference["reference_type"] == media_type for reference in references)
        for media_type in ("image", "video", "audio")
    }
    for media_type, count in counts.items():
        if count > REFERENCE_LIMITS[media_type]:
            raise HTTPException(400, f"{media_type} 参考超过 H3 上限 {REFERENCE_LIMITS[media_type]}")
    for reference in references:
        asset = reference["asset"]
        allowed_asset_file(asset.get("managed_path"))
        if reference["reference_type"] == "video":
            duration = asset.get("duration_seconds")
            if duration is None or not 2.0 <= float(duration) <= 15.0:
                raise HTTPException(400, f"参考视频“{asset['name']}”需为 2–15 秒")

    if compiled_prompt_override is None:
        compiled_prompt, mapping = compile_reference_prompt(shot["prompt"], references)
    else:
        compiled_prompt = compiled_prompt_override
        _, mapping = compile_reference_prompt("", references)
    mode = "ref2va" if references else "fl2va"
    requested_seconds = float(shot["seconds"])
    # H3 snaps upward to 17k+5 frames. Asking for 5.17 seconds would snap to
    # 141 frames; ask for 5.0 to obtain the intended 124-frame/5.167-second tier.
    adapter_seconds = 5.0 if abs(requested_seconds - 5.17) < 0.05 else requested_seconds
    arguments = [
        "draft",
        "--project",
        project,
        "--prompt",
        compiled_prompt,
        "--count",
        str(shot["candidate_count"]),
        "--width",
        str(shot["width"]),
        "--height",
        str(shot["height"]),
        "--seconds",
        str(adapter_seconds),
        "--steps",
        "20",
        "--mode",
        mode,
    ]
    for reference in references:
        argument = {
            "image": "--ref-image",
            "video": "--ref-video",
            "audio": "--ref-audio",
        }[reference["reference_type"]]
        arguments.extend([argument, str(allowed_asset_file(reference["asset"].get("managed_path")))])
    if counts["image"]:
        arguments.extend(["--ref-image-size", "match"])
    if dry_run:
        arguments.append("--dry-run")
    plan = {
        "mode": mode.upper(),
        "reference_counts": counts,
        "references": mapping,
        "compiled_prompt": compiled_prompt,
        "requested_seconds": requested_seconds,
        "adapter_seconds": adapter_seconds,
        "resolution": f"{shot['width']}×{shot['height']}",
        "candidate_count": shot["candidate_count"],
        "gpu_submitted": False if dry_run else None,
        "prompt_plan_hash": prompt_plan_hash,
    }
    return project, arguments, plan


def normalized_h3_input(arguments: list[str]) -> dict[str, Any]:
    """Freeze the exact adapter contract shared by dry-run and GPU submission."""
    normalized_arguments = list(arguments)
    if normalized_arguments and normalized_arguments[-1] == "--dry-run":
        normalized_arguments.pop()
    return {
        "adapter": "h3-video-draft-refine",
        "entrypoint": str(H3_SCRIPT),
        "arguments": normalized_arguments,
    }


def h3_input_hash(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@app.get("/api/prompt-plans")
def list_prompt_plans() -> list[dict[str, Any]]:
    return project_prompt_status(DB_PATH)


@app.get("/api/shots/{shot_id}/prompt-plan")
def preview_prompt_plan(shot_id: str) -> dict[str, Any]:
    return public_plan(compile_prompt_plan(DB_PATH, shot_id))


@app.post("/api/shots/{shot_id}/prompt-plan/dry-run")
def dry_run_prompt_plan(shot_id: str, request: PromptPlanRequest) -> dict[str, Any]:
    compiled = compile_prompt_plan(DB_PATH, shot_id)
    if compiled["plan_hash"] != request.plan_hash:
        raise HTTPException(409, "镜头、生产圣经或引用素材已变化，请刷新编译预览后重试")
    if not compiled["ready"]:
        raise HTTPException(400, {"message": "编译计划存在阻断项", "blocking": compiled["blocking"]})
    lease_id = begin_validation_lease(DB_PATH, compiled)
    try:
        shot = require_active_shot(shot_id)
        project, arguments, adapter_plan = build_h3_arguments(
            shot,
            True,
            compiled_prompt_override=compiled["compiled_prompt"],
            references_override=compiled["_references"],
            prompt_plan_hash=compiled["plan_hash"],
        )
        completed = run_h3(arguments, timeout=90)
        try:
            adapter_output: Any = json.loads(completed.stdout.strip())
        except json.JSONDecodeError:
            adapter_output = completed.stdout.strip()
        if not record_validation(DB_PATH, compiled, adapter_output):
            raise HTTPException(409, "H3 dry-run 返回时输入或计划状态已变化，请重新检查")
    finally:
        end_validation_lease(DB_PATH, lease_id)
    current = public_plan(compile_prompt_plan(DB_PATH, shot_id))
    return {
        **current,
        "ok": True,
        "message": "H3 节点图已构建，未提交 GPU",
        "h3_project": project,
        "adapter_plan": adapter_plan,
        "adapter_output": adapter_output,
        "gpu_submitted": False,
    }


@app.post("/api/shots/{shot_id}/prompt-plan/approve")
def approve_prompt_plan(shot_id: str, request: PromptPlanRequest) -> dict[str, Any]:
    compiled = compile_prompt_plan(DB_PATH, shot_id)
    if compiled["plan_hash"] != request.plan_hash:
        raise HTTPException(409, "镜头、生产圣经或引用素材已变化，请刷新编译预览后重试")
    approve_plan(DB_PATH, shot_id, request.plan_hash)
    return {
        **public_plan(compile_prompt_plan(DB_PATH, shot_id)),
        "message": "生成计划已批准；后续生产任务将使用这份不可变快照",
    }


def ordered_active_shots(shot_ids: list[str]) -> list[dict[str, Any]]:
    if len(shot_ids) != len(set(shot_ids)):
        raise HTTPException(400, "批量镜头不能重复")
    shots = [require_active_shot(shot_id) for shot_id in shot_ids]
    return sorted(shots, key=lambda shot: (shot["ordinal"], shot["id"]))


def batch_error_message(error: HTTPException) -> str:
    if isinstance(error.detail, str):
        return error.detail
    return json.dumps(error.detail, ensure_ascii=False)


@app.post("/api/shots/{shot_id}/generate")
def generate(shot_id: str, request: GenerateRequest) -> dict[str, Any]:
    shot = require_active_shot(shot_id)
    if not request.dry_run and not request.confirm:
        raise HTTPException(400, "实际提交生成前必须显式确认")

    if not request.dry_run:
        active = row(
            """SELECT * FROM jobs WHERE shot_id = ? AND kind = 'draft'
            AND state IN (?, ?, ?, ?, ?, ?, ?) ORDER BY id DESC LIMIT 1""",
            (shot_id, *SUBMISSION_BLOCKING_JOB_STATES),
        )
        if active:
            raise HTTPException(409, f"该镜头已有活动任务：{active['state']}")

    compiled = compile_prompt_plan(DB_PATH, shot_id)
    project, arguments, plan = build_h3_arguments(
        shot,
        request.dry_run,
        compiled_prompt_override=compiled["compiled_prompt"],
        references_override=compiled["_references"],
        prompt_plan_hash=compiled["plan_hash"],
    )
    input_snapshot = normalized_h3_input(arguments)
    validation_hash = h3_input_hash(input_snapshot)
    plan["validation_input_hash"] = validation_hash
    if request.dry_run:
        lease_id = begin_validation_lease(DB_PATH, compiled)
        try:
            completed = run_h3(arguments, timeout=90)
            with closing(connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                current_input = current_plan_input(db, shot_id, shot["project_id"])
                if current_input is None:
                    raise HTTPException(409, "H3 dry-run 返回时镜头已删除，未保存校验结果")
                if current_input["plan_hash"] != compiled["plan_hash"]:
                    raise HTTPException(409, "H3 dry-run 返回时镜头、引用素材或生产圣经已变化，未保存过期校验")
                now = utc_now()
                db.execute(
                    """INSERT INTO jobs
                    (shot_id, kind, state, message, created_at, updated_at, h3_project, plan_hash, source_snapshot)
                    VALUES (?, 'validation', '校验通过', '节点图已构建，未占用 GPU', ?, ?, ?, ?, ?)""",
                    (
                        shot_id, now, now, project, validation_hash,
                        json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ),
                )
                db.execute(
                    """UPDATE shots SET
                    status = CASE WHEN status IN ('生成中', '待审片', '已定稿') THEN status ELSE '可生成' END,
                    updated_at = ? WHERE id = ?""",
                    (now, shot_id),
                )
                db.commit()
            try:
                adapter_output: Any = json.loads(completed.stdout.strip())
            except json.JSONDecodeError:
                adapter_output = completed.stdout.strip()
            return {
                "ok": True,
                "state": "校验通过",
                "message": "节点图已构建，未占用 GPU",
                "h3_project": project,
                **plan,
                "adapter_output": adapter_output,
            }
        finally:
            end_validation_lease(DB_PATH, lease_id)

    generation_lease_id = f"h3-generation-{uuid.uuid4().hex[:12]}"
    submitting_job_id: int | None = None
    try:
        baseline_evidence = manifest_evidence(project)
    except HTTPException as exc:
        baseline_evidence = {"manifest_error": str(exc.detail), "candidate_ids": [], "prompt_ids": []}
    baseline_queue_evidence = (
        comfy_queue_evidence(baseline_evidence.get("prompt_ids") or [])
        if baseline_evidence.get("prompt_ids")
        else {"available": None, "reason": "manifest 基线没有 prompt_id"}
    )
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        current_input = current_plan_input(db, shot_id, shot["project_id"])
        if current_input is None or current_input["plan_hash"] != compiled["plan_hash"]:
            raise HTTPException(409, "镜头、引用素材或生产圣经已变化，请重新 dry-run")
        current_shot = db.execute(
            "SELECT * FROM shots WHERE id = ? AND project_id = ?", (shot_id, shot["project_id"])
        ).fetchone()
        project, arguments, plan = build_h3_arguments(
            dict(current_shot),
            False,
            compiled_prompt_override=compiled["compiled_prompt"],
            references_override=compiled["_references"],
            prompt_plan_hash=compiled["plan_hash"],
        )
        input_snapshot = normalized_h3_input(arguments)
        validation_hash = h3_input_hash(input_snapshot)
        credential = db.execute(
            """SELECT state, plan_hash FROM jobs
            WHERE shot_id = ? AND kind = 'validation' ORDER BY id DESC LIMIT 1""",
            (shot_id,),
        ).fetchone()
        expected_hash = request.expected_validation_hash or (credential["plan_hash"] if credential else None)
        if (
            not credential
            or credential["state"] != "校验通过"
            or credential["plan_hash"] != expected_hash
            or validation_hash != expected_hash
        ):
            raise HTTPException(409, "当前 H3 适配器输入未通过最近一次 dry-run，禁止提交 GPU")
        now = utc_now()
        try:
            db.execute(
                """INSERT INTO h3_generation_leases
                (id, shot_id, project_id, validation_hash, input_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    generation_lease_id,
                    shot_id,
                    shot["project_id"],
                    validation_hash,
                    json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该镜头已有进行中的 H3 GPU 提交") from exc
        cursor = db.execute(
            """INSERT INTO jobs
            (shot_id, kind, state, message, created_at, h3_project, updated_at, plan_hash,
             source_snapshot, reconciliation_snapshot, retry_safe)
            VALUES (?, 'draft', '提交中', '已冻结通过 dry-run 的 H3 命令，正在提交', ?, ?, ?, ?, ?, ?, 0)""",
            (
                shot_id,
                now,
                project,
                now,
                validation_hash,
                json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(
                    {
                        "h3_project": project,
                        "input_hash": validation_hash,
                        "frozen_command": input_snapshot,
                        "baseline_candidate_ids": baseline_evidence.get("candidate_ids") or [],
                        "manifest_before": baseline_evidence,
                        "comfyui_before": baseline_queue_evidence,
                        "adapter_returned_success": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        submitting_job_id = int(cursor.lastrowid)
        db.commit()

    try:
        completed = run_h3(arguments, timeout=90)
    except Exception as exc:
        with closing(connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            now = utc_now()
            unknown_state = "提交状态未知"
            db.execute(
                """UPDATE jobs SET state = ?, message = ?, updated_at = ?, completed_at = NULL, retry_safe = 0
                WHERE id = ? AND state = '提交中'""",
                (unknown_state, f"H3 适配器返回异常，外部提交副作用未知：{exc}", now, submitting_job_id),
            )
            db.execute("DELETE FROM h3_generation_leases WHERE id = ?", (generation_lease_id,))
            db.commit()
        raise

    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        reconciliation = db.execute("SELECT reconciliation_snapshot FROM jobs WHERE id = ?", (submitting_job_id,)).fetchone()
        try:
            evidence = json.loads(reconciliation["reconciliation_snapshot"] or "{}") if reconciliation else {}
        except (TypeError, ValueError):
            evidence = {}
        evidence["adapter_returned_success"] = True
        evidence["adapter_completed_at"] = utc_now()
        db.execute(
            """UPDATE jobs SET state = '已提交待对账',
            message = 'H3 适配器已返回，正在核对 manifest 与 ComfyUI 证据',
            reconciliation_snapshot = ?, updated_at = ? WHERE id = ? AND state = '提交中'""",
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")), utc_now(), submitting_job_id),
        )
        db.execute("DELETE FROM h3_generation_leases WHERE id = ?", (generation_lease_id,))
        db.commit()

    reconciled = reconcile_generation_job(row("SELECT * FROM jobs WHERE id = ?", (submitting_job_id,)) or {})
    prompt_ids = json.loads(reconciled.get("prompt_ids") or "[]")
    return {
        "ok": True,
        "state": reconciled["state"],
        "message": reconciled["message"],
        "h3_project": project,
        "prompt_ids": prompt_ids,
        "mode": plan["mode"],
        "references": plan["references"],
    }


@app.post("/api/generation/batch/dry-run")
def batch_dry_run(request: BatchGenerationRequest) -> dict[str, Any]:
    shots = ordered_active_shots(request.shot_ids)
    results: list[dict[str, Any]] = []
    for shot in shots:
        try:
            result = generate(shot["id"], GenerateRequest(confirm=False, dry_run=True))
            results.append({
                "shot_id": shot["id"],
                "title": shot["title"],
                "ok": True,
                "mode": result["mode"],
                "resolution": result["resolution"],
                "candidate_count": result["candidate_count"],
                "message": result["message"],
            })
        except HTTPException as error:
            results.append({
                "shot_id": shot["id"],
                "title": shot["title"],
                "ok": False,
                "message": batch_error_message(error),
            })
    passed = sum(bool(result["ok"]) for result in results)
    return {
        "ok": passed == len(results),
        "requested_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "gpu_submitted": False,
        "results": results,
    }


@app.post("/api/generation/batch/submit")
def batch_submit(request: BatchGenerationRequest) -> dict[str, Any]:
    if not request.confirm:
        raise HTTPException(400, "批量提交 GPU 前必须显式确认")
    shots = ordered_active_shots(request.shot_ids)
    validation_issues: list[dict[str, str]] = []
    validation_credentials: dict[str, str] = {}
    for shot in shots:
        current = compile_prompt_plan(DB_PATH, shot["id"])
        _, arguments, _ = build_h3_arguments(
            shot,
            True,
            compiled_prompt_override=current["compiled_prompt"],
            references_override=current["_references"],
            prompt_plan_hash=current["plan_hash"],
        )
        current_validation_hash = h3_input_hash(normalized_h3_input(arguments))
        validation = row(
            """SELECT state, plan_hash, source_snapshot FROM jobs
            WHERE shot_id = ? AND kind = 'validation'
            ORDER BY id DESC LIMIT 1""",
            (shot["id"],),
        )
        if (
            not validation
            or validation["state"] != "校验通过"
            or not validation.get("plan_hash")
            or validation["plan_hash"] != current_validation_hash
        ):
            validation_issues.append({
                "shot_id": shot["id"],
                "message": "镜头、引用素材或生产圣经与最近成功 dry-run 不一致",
            })
        else:
            validation_credentials[shot["id"]] = validation["plan_hash"]
    if validation_issues:
        raise HTTPException(409, {"message": "批量提交前检查未通过", "issues": validation_issues})

    results: list[dict[str, Any]] = []
    for shot in shots:
        try:
            result = generate(
                shot["id"],
                GenerateRequest(
                    confirm=True,
                    dry_run=False,
                    expected_validation_hash=validation_credentials[shot["id"]],
                ),
            )
            results.append({
                "shot_id": shot["id"],
                "title": shot["title"],
                "ok": True,
                "state": result["state"],
                "mode": result["mode"],
                "prompt_ids": result["prompt_ids"],
                "message": result["message"],
            })
        except HTTPException as error:
            results.append({
                "shot_id": shot["id"],
                "title": shot["title"],
                "ok": False,
                "message": batch_error_message(error),
            })
    submitted = sum(bool(result["ok"]) for result in results)
    return {
        "ok": submitted == len(results),
        "requested_count": len(results),
        "submitted_count": submitted,
        "failed_count": len(results) - submitted,
        "results": results,
    }


@app.post("/api/shots/{shot_id}/sync")
def sync_shot(shot_id: str) -> dict[str, Any]:
    require_active_shot(shot_id)
    project = h3_project_for_shot(shot_id)
    reconciliation = row(
        """SELECT * FROM jobs WHERE shot_id = ? AND kind = 'draft'
        AND state IN (?, ?, ?) ORDER BY id DESC LIMIT 1""",
        (shot_id, *RECONCILING_JOB_STATES),
    )
    if reconciliation:
        reconciled = reconcile_generation_job(reconciliation)
        return {
            "ok": reconciled["state"] not in {"待人工对账", "提交状态未知"},
            "state": reconciled["state"],
            "message": reconciled["message"],
            "prompt_ids": json.loads(reconciled.get("prompt_ids") or "[]"),
        }
    return {"ok": True, **refresh_h3_project(shot_id, project)}


@app.post("/api/shots/{shot_id}/review")
def select_candidate(shot_id: str, request: ReviewRequest) -> dict[str, Any]:
    require_active_shot(shot_id)
    candidate = row(
        "SELECT * FROM candidates WHERE id = ? AND shot_id = ? AND archived = 0",
        (request.candidate_id, shot_id),
    )
    if not candidate:
        raise HTTPException(404, "候选版本不存在")
    if candidate.get("status") != "completed":
        raise HTTPException(409, "候选尚未生成完成")
    if not candidate.get("selected"):
        require_passed_review(DB_PATH, request.candidate_id, COMFY_OUTPUT_ROOT)
    if candidate.get("source") == "h3" and candidate.get("external_id"):
        project = h3_project_for_shot(shot_id)
        if request.note.strip():
            run_h3(
                ["review", "--project", project, "--candidate", candidate["external_id"], "--notes", request.note.strip()],
                timeout=30,
            )
        run_h3(["select", "--project", project, "--candidate", candidate["external_id"]], timeout=30)

    with closing(connect()) as db:
        db.execute("UPDATE candidates SET selected = 0 WHERE shot_id = ?", (shot_id,))
        db.execute("UPDATE candidates SET selected = 1, note = ? WHERE id = ?", (request.note, request.candidate_id))
        selected_thumbnail = (
            f"/api/candidates/{request.candidate_id}/thumbnail"
            if candidate.get("thumbnail_file") else candidate.get("thumbnail")
        )
        selected_video = (
            f"/api/candidates/{request.candidate_id}/video"
            if candidate.get("output_file") else candidate.get("video")
        )
        db.execute(
            "UPDATE shots SET status = '草稿已选', thumbnail = ?, video = ?, updated_at = ? WHERE id = ?",
            (selected_thumbnail, selected_video, utc_now(), shot_id),
        )
        db.execute(
            """INSERT INTO jobs
            (shot_id, kind, state, message, created_at, updated_at)
            VALUES (?, 'review', '完成', ?, ?, ?)""",
            (shot_id, f"候选 {candidate['label']} 已选为定稿", utc_now(), utc_now()),
        )
        db.commit()
    return {"ok": True, "selected": request.candidate_id}


@app.post("/api/shots/{shot_id}/finalize-promotion")
def finalize_promotion(shot_id: str, request: FinalizePromotionRequest) -> dict[str, Any]:
    require_active_shot(shot_id)
    project = h3_project_for_shot(shot_id)
    refresh_h3_project(shot_id, project)
    promotion = row(
        "SELECT * FROM promotions WHERE id = ? AND shot_id = ?",
        (request.promotion_id, shot_id),
    )
    if not promotion:
        raise HTTPException(404, "成片版本不存在")
    if promotion.get("status") != "completed" or not promotion.get("output_file"):
        raise HTTPException(409, "成片版本尚未生成完成")
    strategy = "Ref2VA 精修" if promotion.get("strategy") == "ref2va" else "保真放大"
    selected_at = utc_now()
    with closing(connect()) as db:
        db.execute("UPDATE promotions SET selected = 0 WHERE shot_id = ?", (shot_id,))
        db.execute(
            "UPDATE promotions SET selected = 1, note = ?, selected_at = ? WHERE id = ?",
            (request.note, selected_at, request.promotion_id),
        )
        db.execute(
            """UPDATE shots SET status = '已定稿', width = ?, height = ?, seconds = ?,
            strategy = ?, video = ?, updated_at = ? WHERE id = ?""",
            (
                promotion["width"],
                promotion["height"],
                promotion.get("actual_seconds") or 0,
                strategy,
                f"/api/promotions/{request.promotion_id}/video",
                selected_at,
                shot_id,
            ),
        )
        db.execute(
            """INSERT INTO jobs
            (shot_id, kind, state, message, created_at, h3_project, prompt_ids, updated_at, completed_at)
            VALUES (?, 'delivery', '完成', ?, ?, ?, ?, ?, ?)""",
            (
                shot_id,
                f"{promotion['external_id']} · {strategy} 已选为最终交付",
                selected_at,
                project,
                json.dumps([promotion["prompt_id"]] if promotion.get("prompt_id") else []),
                selected_at,
                selected_at,
            ),
        )
        db.commit()
    selected = row("SELECT * FROM promotions WHERE id = ?", (request.promotion_id,))
    return {"ok": True, "final_output": promotion_public(selected)}


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    project = active_project()
    if not project:
        raise HTTPException(404, "项目不存在")
    status_counts = rows(
        "SELECT status, COUNT(*) AS count FROM shots WHERE project_id = ? GROUP BY status",
        (project["id"],),
    )
    assets = row("SELECT COUNT(*) AS count FROM assets WHERE project_id = ? AND archived = 0", (project["id"],))
    duration = row("SELECT ROUND(SUM(seconds), 2) AS seconds FROM shots WHERE project_id = ?", (project["id"],))
    return {
        "status_counts": status_counts,
        "asset_count": assets["count"],
        "planned_seconds": duration["seconds"],
    }


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")
    app.mount("/media", StaticFiles(directory=FRONTEND_DIST / "media"), name="media")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")
