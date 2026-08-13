from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    from .prompt_compiler import mark_prompt_plans_stale
except ImportError:  # Support `uvicorn app:app` from the backend directory.
    from prompt_compiler import mark_prompt_plans_stale


SYNC_FIELDS = (
    ("title", "标题"),
    ("description", "画面"),
    ("dialogue", "对白"),
    ("sound", "声音"),
    ("seconds", "预计时长"),
    ("ordinal", "排序"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _ensure_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_storyboard_schema(db: sqlite3.Connection) -> None:
    _ensure_column(db, "shots", "sound", "TEXT NOT NULL DEFAULT ''")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS creative_storyboard_links (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          section_id TEXT NOT NULL REFERENCES creative_sections(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          created_from_revision INTEGER NOT NULL,
          last_synced_revision INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(project_id, section_id),
          UNIQUE(project_id, shot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_creative_storyboard_links_project
          ON creative_storyboard_links(project_id, section_id);
        CREATE TABLE IF NOT EXISTS creative_storyboard_syncs (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          plan_hash TEXT NOT NULL,
          section_revisions TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('applied')),
          preview TEXT NOT NULL,
          applied_snapshot TEXT NOT NULL,
          confirmed_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          applied_at TEXT NOT NULL,
          UNIQUE(project_id, plan_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_creative_storyboard_syncs_project
          ON creative_storyboard_syncs(project_id, applied_at DESC);
        """
    )


class StoryboardPreviewRequest(BaseModel):
    pass


class StoryboardApplyRequest(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)
    confirm: bool = False
    confirmed_by: str = Field("human:ui", min_length=2, max_length=120)


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute(
        """SELECT projects.* FROM projects
        JOIN workspace_settings ON workspace_settings.key = 'active_project_id'
          AND workspace_settings.value = projects.id
        WHERE COALESCE(projects.archived, 0) = 0"""
    ).fetchone()
    if not row:
        raise HTTPException(404, "当前没有已激活的项目")
    return dict(row)


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _proposed_shot(section: sqlite3.Row, ordinal: int) -> dict[str, Any]:
    scene = str(section["scene"] or section["summary"] or "").strip()
    action = str(section["action"] or section["content"] or "").strip()
    visual = str(section["visual"] or section["summary"] or "").strip()
    sound = str(section["sound"] or "").strip()
    prompt_parts = [
        f"Scene: {scene}" if scene else "",
        f"Action: {action}" if action else "",
        f"Visual intent: {visual}" if visual else "",
        f"Sound cue: {sound}" if sound else "",
    ]
    return {
        "ordinal": ordinal,
        "scene_code": f"C{int(section['chapter_ordinal']):02d}",
        "title": str(section["title"]).strip(),
        "description": "\n".join(
            part for part in (
                f"场景：{scene}" if scene else "",
                f"动作：{action}" if action else "",
                f"视觉：{visual}" if visual else "",
            ) if part
        ),
        "dialogue": str(section["dialogue"] or "").strip(),
        "sound": sound,
        "prompt": "\n".join(part for part in prompt_parts if part),
        "seconds": float(section["planned_seconds"]),
    }


def _shot_fields(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    return {
        key: item.get(key)
        for key in ("ordinal", "scene_code", "title", "description", "dialogue", "sound", "prompt", "seconds")
    }


def _field_diffs(
    current: dict[str, Any] | None,
    proposed: dict[str, Any] | None,
    *,
    decision: str,
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for field, label in SYNC_FIELDS:
        before = current.get(field) if current else None
        after = proposed.get(field) if proposed else None
        if field == "seconds":
            before = round(float(before), 3) if before is not None else None
            after = round(float(after), 3) if after is not None else None
        changed = before != after
        diffs.append(
            {
                "field": field,
                "label": label,
                "before": before,
                "after": after,
                "changed": changed,
                "decision": decision if changed else "keep",
            }
        )
    return diffs


def _evidence_reasons(db: sqlite3.Connection, shot_id: str) -> list[str]:
    labels = {
        "candidates": "候选片段",
        "promotions": "成片版本",
        "jobs": "生成记录",
        "shot_references": "镜头参考素材",
        "delivery_plan_items": "交付清单",
        "production_batch_items": "生产批次",
        "h3_prompt_plans": "H3 计划历史",
        "production_bible_shots": "生产圣经镜头绑定",
        "script_storyboard_links": "历史剧本分镜映射",
        "candidate_reviews": "候选审片证据",
    }
    # Audit every declared FK to shots instead of maintaining a partial hand-written list.
    # A future production table therefore fails closed until its relation is handled explicitly.
    checks: list[tuple[str, str, str]] = []
    for table_row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
        table = str(table_row["name"])
        if table == "creative_storyboard_links":
            continue
        for foreign_key in db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
            if str(foreign_key["table"]) != "shots":
                continue
            column = str(foreign_key["from"])
            checks.append(
                (
                    table,
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?',
                    labels.get(table, f"生产关系 {table}"),
                )
            )
    reasons: list[str] = []
    for table, query, label in checks:
        if _table_exists(db, table) and int(db.execute(query, (shot_id,)).fetchone()[0] or 0) > 0:
            reasons.append(label)
    return reasons


def _sync_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("section_revisions", "preview", "applied_snapshot"):
        if isinstance(item.get(key), str):
            item[key] = json.loads(item[key])
    return item


def _build_preview(
    db: sqlite3.Connection,
    project: dict[str, Any],
) -> dict[str, Any]:
    sections = db.execute(
        """SELECT sections.*, chapters.ordinal AS chapter_ordinal, chapters.title AS chapter_title
        FROM creative_sections sections
        JOIN creative_chapters chapters ON chapters.id = sections.chapter_id
        WHERE sections.project_id = ? AND sections.status <> 'archived'
          AND chapters.status <> 'archived'
        ORDER BY chapters.ordinal, sections.ordinal, sections.id""",
        (project["id"],),
    ).fetchall()
    links = {
        row["section_id"]: row
        for row in db.execute(
            "SELECT * FROM creative_storyboard_links WHERE project_id = ?", (project["id"],)
        ).fetchall()
    }
    shots = {
        row["id"]: row
        for row in db.execute("SELECT * FROM shots WHERE project_id = ?", (project["id"],)).fetchall()
    }
    if not sections and not links:
        raise HTTPException(422, "当前项目没有可同步的小节或受管分镜映射")
    linked_shot_ids = {str(link["shot_id"]) for link in links.values()}
    managed_slots = sorted(
        int(shots[shot_id]["ordinal"])
        for shot_id in linked_shot_ids
        if shot_id in shots
    )
    next_ordinal = max((int(shot["ordinal"]) for shot in shots.values()), default=0) + 1
    while len(managed_slots) < len(sections):
        managed_slots.append(next_ordinal)
        next_ordinal += 1

    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    selected_ids = {str(item["id"]) for item in sections}
    for index, section in enumerate(sections):
        if section["status"] != "approved":
            blockers.append(f"小节“{section['title']}”尚未批准")
        link = links.get(section["id"])
        current_row = shots.get(link["shot_id"]) if link else None
        proposed = _proposed_shot(section, managed_slots[index])
        current = _shot_fields(current_row)
        diffs = _field_diffs(current, proposed, decision="create" if current is None else "update")
        changed_fields = [item["field"] for item in diffs if item["changed"]]
        if current is None:
            action = "create"
        elif not changed_fields:
            action = "unchanged"
        elif changed_fields == ["ordinal"]:
            action = "reorder"
        else:
            action = "update"
        rows.append(
            {
                "action": action,
                "section": {
                    "id": section["id"],
                    "chapter_id": section["chapter_id"],
                    "chapter_title": section["chapter_title"],
                    "title": section["title"],
                    "status": section["status"],
                    "revision": section["revision"],
                },
                "shot_id": current_row["id"] if current_row else None,
                "mapping_id": link["id"] if link else None,
                "current": current,
                "proposed": proposed,
                "field_diffs": diffs,
                "protected_reasons": [],
            }
        )

    for section_id, link in links.items():
        if section_id in selected_ids:
            continue
        shot = shots.get(link["shot_id"])
        if not shot:
            continue
        reasons = _evidence_reasons(db, shot["id"])
        action = "protected" if reasons else "delete"
        if reasons:
            blockers.append(f"镜头“{shot['title']}”已有{'、'.join(reasons)}，不能删除")
        rows.append(
            {
                "action": action,
                "section": {"id": section_id, "status": "archived"},
                "shot_id": shot["id"],
                "mapping_id": link["id"],
                "current": _shot_fields(shot),
                "proposed": None,
                "field_diffs": _field_diffs(_shot_fields(shot), None, decision="protected" if reasons else "delete"),
                "protected_reasons": reasons,
            }
        )

    for shot in sorted(shots.values(), key=lambda item: (item["ordinal"], item["id"])):
        if shot["id"] in linked_shot_ids:
            continue
        rows.append(
            {
                "action": "preserve",
                "section": None,
                "shot_id": shot["id"],
                "mapping_id": None,
                "current": _shot_fields(shot),
                "proposed": _shot_fields(shot),
                "field_diffs": _field_diffs(_shot_fields(shot), _shot_fields(shot), decision="keep"),
                "protected_reasons": ["历史手工分镜，无小节来源映射"],
            }
        )

    summary = {
        action: sum(row["action"] == action for row in rows)
        for action in ("create", "update", "delete", "reorder", "unchanged", "protected", "preserve")
    }
    stable = {
        "project_id": project["id"],
        "rows": rows,
        "blockers": blockers,
    }
    changes = sum(summary[action] for action in ("create", "update", "delete", "reorder"))
    return {
        **stable,
        "plan_hash": _json_hash(stable),
        "summary": summary,
        "can_apply": not blockers and changes > 0,
        "section_revisions": {str(section["id"]): int(section["revision"]) for section in sections},
        "safety": {
            "preview_has_side_effects": False,
            "explicit_confirmation_required": True,
            "historical_manual_shots_preserved": True,
        },
    }


def invalidate_sections(
    db: sqlite3.Connection,
    project_id: str,
    reason: str,
    section_ids: list[str] | None = None,
) -> int:
    if not _table_exists(db, "creative_storyboard_links"):
        return 0
    query = "SELECT shot_id FROM creative_storyboard_links WHERE project_id = ?"
    params: list[Any] = [project_id]
    if section_ids is not None:
        if not section_ids:
            return 0
        query += f" AND section_id IN ({','.join('?' for _ in section_ids)})"
        params.extend(section_ids)
    shot_ids = [str(row["shot_id"]) for row in db.execute(query, params).fetchall()]
    return mark_prompt_plans_stale(db, project_id, reason, shot_ids=shot_ids)


def _apply_preview(
    db: sqlite3.Connection,
    project: dict[str, Any],
    preview: dict[str, Any],
    confirmed_by: str,
) -> dict[str, Any]:
    now = utc_now()
    applied: list[dict[str, Any]] = []
    for row in preview["rows"]:
        action = row["action"]
        if action in ("unchanged", "preserve"):
            applied.append({"action": action, "shot_id": row["shot_id"], "section_id": row["section"]["id"] if row["section"] else None})
            continue
        if action in ("protected",):
            raise HTTPException(409, "同步包含受保护镜头，必须先处理候选或交付关系")
        if action == "delete":
            invalidate_sections(db, project["id"], f"来源小节已归档，镜头 {row['shot_id']} 将删除", [row["section"]["id"]])
            db.execute("DELETE FROM shots WHERE id = ? AND project_id = ?", (row["shot_id"], project["id"]))
            applied.append({"action": action, "shot_id": row["shot_id"], "section_id": row["section"]["id"]})
            continue

        proposed = row["proposed"]
        section = row["section"]
        if action == "create":
            shot_id = f"storyboard-shot-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO shots
                (id, project_id, ordinal, scene_code, title, description, dialogue, sound, prompt, status,
                 width, height, seconds, candidate_count, strategy, thumbnail, video,
                 subtitle_enabled, subtitle_start_seconds, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '未生成', 608, 352, ?, 2, 'Ref2VA 精修', '', NULL, 1, NULL, ?)""",
                (
                    shot_id, project["id"], proposed["ordinal"], proposed["scene_code"], proposed["title"],
                    proposed["description"], proposed["dialogue"], proposed["sound"], proposed["prompt"],
                    proposed["seconds"], now,
                ),
            )
            link_id = f"storyboard-link-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO creative_storyboard_links
                (id, project_id, section_id, shot_id, created_from_revision, last_synced_revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    link_id, project["id"], section["id"], shot_id, section["revision"],
                    section["revision"], now, now,
                ),
            )
        else:
            shot_id = row["shot_id"]
            db.execute(
                """UPDATE shots SET ordinal = ?, scene_code = ?, title = ?, description = ?, dialogue = ?,
                sound = ?, prompt = ?, seconds = ?, updated_at = ? WHERE id = ? AND project_id = ?""",
                (
                    proposed["ordinal"], proposed["scene_code"], proposed["title"], proposed["description"],
                    proposed["dialogue"], proposed["sound"], proposed["prompt"], proposed["seconds"], now,
                    shot_id, project["id"],
                ),
            )
            db.execute(
                """UPDATE creative_storyboard_links SET last_synced_revision = ?, updated_at = ?
                WHERE project_id = ? AND section_id = ? AND shot_id = ?""",
                (section["revision"], now, project["id"], section["id"], shot_id),
            )
            mark_prompt_plans_stale(
                db,
                project["id"],
                f"小节“{section['title']}”R{section['revision']} 已同步到分镜",
                shot_ids=[shot_id],
            )
        applied.append({"action": action, "shot_id": shot_id, "section_id": section["id"]})

    sync_id = f"creative-storyboard-sync-{uuid.uuid4().hex[:12]}"
    db.execute(
        """INSERT INTO creative_storyboard_syncs
        (id, project_id, plan_hash, section_revisions, state, preview, applied_snapshot,
         confirmed_by, created_at, applied_at)
        VALUES (?, ?, ?, ?, 'applied', ?, ?, ?, ?, ?)""",
        (
            sync_id,
            project["id"],
            preview["plan_hash"],
            json.dumps(preview["section_revisions"], ensure_ascii=False),
            json.dumps(preview, ensure_ascii=False),
            json.dumps(applied, ensure_ascii=False),
            confirmed_by.strip(),
            now,
            now,
        ),
    )
    row = db.execute("SELECT * FROM creative_storyboard_syncs WHERE id = ?", (sync_id,)).fetchone()
    return _sync_public(row)


def _status(db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    latest = db.execute(
        "SELECT * FROM creative_storyboard_syncs WHERE project_id = ? ORDER BY applied_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    mapped = int(db.execute(
        "SELECT COUNT(*) FROM creative_storyboard_links WHERE project_id = ?", (project_id,)
    ).fetchone()[0])
    manual = int(db.execute(
        """SELECT COUNT(*) FROM shots WHERE project_id = ? AND id NOT IN
        (SELECT shot_id FROM creative_storyboard_links WHERE project_id = ?)""",
        (project_id, project_id),
    ).fetchone()[0])
    return {
        "project_id": project_id,
        "mapped_shot_count": mapped,
        "historical_manual_shot_count": manual,
        "latest_sync": _sync_public(latest) if latest else None,
    }


def create_storyboard_router(db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/api/creative-planning/storyboard-sync", tags=["creative-storyboard"])

    @router.get("")
    def status() -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            return _status(db, project["id"])

    @router.post("/preview")
    def preview(payload: StoryboardPreviewRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            return _build_preview(db, project)

    @router.post("/apply")
    def apply(payload: StoryboardApplyRequest) -> dict[str, Any]:
        if not payload.confirm:
            raise HTTPException(422, "应用同步前必须明确确认")
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            existing = db.execute(
                "SELECT * FROM creative_storyboard_syncs WHERE project_id = ? AND plan_hash = ?",
                (project["id"], payload.plan_hash),
            ).fetchone()
            if existing:
                db.commit()
                return {"sync": _sync_public(existing), "status": _status(db, project["id"]), "idempotent": True}
            current = _build_preview(db, project)
            if current["plan_hash"] != payload.plan_hash:
                raise HTTPException(409, "小节、分镜或生产证据已变化，请重新检查逐镜差异")
            if not current["can_apply"]:
                detail = "；".join(current["blockers"]) or "当前同步没有可应用的变化"
                raise HTTPException(409, detail)
            result = _apply_preview(db, project, current, payload.confirmed_by)
            db.commit()
            return {"sync": result, "status": _status(db, project["id"]), "idempotent": False}

    return router
