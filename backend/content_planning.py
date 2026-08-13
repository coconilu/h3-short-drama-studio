from __future__ import annotations

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
    db = sqlite3.connect(db_path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_content_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS creative_briefs (
          project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
          theme TEXT NOT NULL DEFAULT '',
          genre TEXT NOT NULL DEFAULT '',
          tone TEXT NOT NULL DEFAULT '',
          audience TEXT NOT NULL DEFAULT '',
          target_duration REAL NOT NULL DEFAULT 0,
          constraints TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'approved')),
          revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS creative_proposals (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          title TEXT NOT NULL,
          synopsis TEXT NOT NULL DEFAULT '',
          core_conflict TEXT NOT NULL DEFAULT '',
          ending TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'finalized', 'archived')),
          revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_creative_proposal_final
          ON creative_proposals(project_id) WHERE status = 'finalized';
        CREATE INDEX IF NOT EXISTS idx_creative_proposals_project
          ON creative_proposals(project_id, status, ordinal);
        CREATE TABLE IF NOT EXISTS creative_characters (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          name TEXT NOT NULL,
          identity TEXT NOT NULL DEFAULT '',
          goal TEXT NOT NULL DEFAULT '',
          obstacle TEXT NOT NULL DEFAULT '',
          personality TEXT NOT NULL DEFAULT '',
          appearance TEXT NOT NULL DEFAULT '',
          voice TEXT NOT NULL DEFAULT '',
          relationships TEXT NOT NULL DEFAULT '',
          reference_notes TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'approved', 'archived')),
          revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_creative_characters_project
          ON creative_characters(project_id, status, ordinal);
        CREATE TABLE IF NOT EXISTS creative_chapters (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          title TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '',
          pacing_goal TEXT NOT NULL DEFAULT '',
          planned_seconds REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'approved', 'archived')),
          revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_creative_chapters_project
          ON creative_chapters(project_id, status, ordinal);
        CREATE TABLE IF NOT EXISTS creative_sections (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          chapter_id TEXT NOT NULL REFERENCES creative_chapters(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          title TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          pacing_goal TEXT NOT NULL DEFAULT '',
          planned_seconds REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'approved', 'archived')),
          revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_creative_sections_chapter
          ON creative_sections(chapter_id, status, ordinal);
        CREATE INDEX IF NOT EXISTS idx_creative_sections_project
          ON creative_sections(project_id, status);
        CREATE TABLE IF NOT EXISTS creative_revisions (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          entity_type TEXT NOT NULL CHECK(entity_type IN ('brief', 'proposal', 'character', 'chapter', 'section')),
          entity_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          source TEXT NOT NULL,
          snapshot TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(entity_type, entity_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_creative_revisions_entity
          ON creative_revisions(project_id, entity_type, entity_id, revision DESC);
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(creative_sections)").fetchall()}
    if "content" not in columns:
        db.execute("ALTER TABLE creative_sections ADD COLUMN content TEXT NOT NULL DEFAULT ''")


ContentStatus = Literal["draft", "approved"]


class RevisionBase(BaseModel):
    base_revision: int = Field(ge=1)
    source: str = Field("human:manual", min_length=2, max_length=120)


class BriefUpdate(RevisionBase):
    theme: str = Field(max_length=1200)
    genre: str = Field(max_length=120)
    tone: str = Field(max_length=240)
    audience: str = Field(max_length=240)
    target_duration: float = Field(ge=0, le=36000)
    constraints: str = Field("", max_length=4000)
    status: ContentStatus = "draft"


class ProposalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    synopsis: str = Field("", max_length=6000)
    core_conflict: str = Field("", max_length=2400)
    ending: str = Field("", max_length=2400)
    source: str = Field("human:create", min_length=2, max_length=120)


class ProposalUpdate(RevisionBase):
    title: str | None = Field(None, min_length=1, max_length=160)
    synopsis: str | None = Field(None, max_length=6000)
    core_conflict: str | None = Field(None, max_length=2400)
    ending: str | None = Field(None, max_length=2400)


class CharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    identity: str = Field("", max_length=1600)
    goal: str = Field("", max_length=1600)
    obstacle: str = Field("", max_length=1600)
    personality: str = Field("", max_length=1600)
    appearance: str = Field("", max_length=3000)
    voice: str = Field("", max_length=3000)
    relationships: str = Field("", max_length=4000)
    reference_notes: str = Field("", max_length=3000)
    source: str = Field("human:create", min_length=2, max_length=120)


class CharacterUpdate(RevisionBase):
    name: str | None = Field(None, min_length=1, max_length=120)
    identity: str | None = Field(None, max_length=1600)
    goal: str | None = Field(None, max_length=1600)
    obstacle: str | None = Field(None, max_length=1600)
    personality: str | None = Field(None, max_length=1600)
    appearance: str | None = Field(None, max_length=3000)
    voice: str | None = Field(None, max_length=3000)
    relationships: str | None = Field(None, max_length=4000)
    reference_notes: str | None = Field(None, max_length=3000)
    status: ContentStatus | None = None


class ChapterCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field("", max_length=6000)
    pacing_goal: str = Field("", max_length=1200)
    planned_seconds: float = Field(0, ge=0, le=36000)
    source: str = Field("human:create", min_length=2, max_length=120)


class ChapterUpdate(RevisionBase):
    title: str | None = Field(None, min_length=1, max_length=160)
    summary: str | None = Field(None, max_length=6000)
    pacing_goal: str | None = Field(None, max_length=1200)
    planned_seconds: float | None = Field(None, ge=0, le=36000)
    status: ContentStatus | None = None


class SectionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field("", max_length=6000)
    content: str = Field("", max_length=24000)
    pacing_goal: str = Field("", max_length=1200)
    planned_seconds: float = Field(0, ge=0, le=36000)
    source: str = Field("human:create", min_length=2, max_length=120)


class SectionUpdate(RevisionBase):
    title: str | None = Field(None, min_length=1, max_length=160)
    summary: str | None = Field(None, max_length=6000)
    content: str | None = Field(None, max_length=24000)
    pacing_goal: str | None = Field(None, max_length=1200)
    planned_seconds: float | None = Field(None, ge=0, le=36000)
    status: ContentStatus | None = None


class OrderUpdate(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    base_revisions: dict[str, int] = Field(min_length=1, max_length=500)
    parent_base_revision: int | None = Field(None, ge=1)
    source: str = Field("human:reorder", min_length=2, max_length=120)


class ChapterSplit(BaseModel):
    section_id: str
    new_title: str = Field(min_length=1, max_length=160)
    base_revisions: dict[str, int] = Field(min_length=1, max_length=500)
    child_base_revisions: dict[str, int] = Field(min_length=2, max_length=500)
    source: str = Field("human:split-chapter", min_length=2, max_length=120)


class SectionSplit(BaseModel):
    new_title: str = Field(min_length=1, max_length=160)
    summary_before: str = Field(max_length=6000)
    summary_after: str = Field(max_length=6000)
    base_revisions: dict[str, int] = Field(min_length=1, max_length=500)
    parent_base_revision: int = Field(ge=1)
    source: str = Field("human:split-section", min_length=2, max_length=120)


class MergeRequest(BaseModel):
    target_id: str
    base_revisions: dict[str, int] = Field(min_length=2, max_length=500)
    child_base_revisions: dict[str, int] = Field(default_factory=dict, max_length=500)
    parent_base_revision: int | None = Field(None, ge=1)
    source: str = Field("human:merge", min_length=2, max_length=120)


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


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value.strip() if isinstance(value, str) else value for key, value in values.items()}


def _row_for_project(
    db: sqlite3.Connection,
    table: str,
    entity_id: str,
    project_id: str,
    *,
    include_archived: bool = False,
) -> sqlite3.Row:
    where = "id = ? AND project_id = ?"
    if not include_archived:
        where += " AND status <> 'archived'"
    row = db.execute(f"SELECT * FROM {table} WHERE {where}", (entity_id, project_id)).fetchone()
    if not row:
        raise HTTPException(404, "创作内容不存在或不属于当前项目")
    return row


def _snapshot(db: sqlite3.Connection, entity_type: str, entity_id: str) -> dict[str, Any]:
    table = {
        "brief": "creative_briefs",
        "proposal": "creative_proposals",
        "character": "creative_characters",
        "chapter": "creative_chapters",
        "section": "creative_sections",
    }[entity_type]
    key = "project_id" if entity_type == "brief" else "id"
    row = db.execute(f"SELECT * FROM {table} WHERE {key} = ?", (entity_id,)).fetchone()
    if not row:
        raise HTTPException(404, "无法为不存在的内容创建修订")
    result = dict(row)
    if entity_type == "chapter":
        result["sections"] = [
            dict(item)
            for item in db.execute(
                "SELECT * FROM creative_sections WHERE chapter_id = ? ORDER BY status = 'archived', ordinal",
                (entity_id,),
            ).fetchall()
        ]
    return result


def _save_revision(db: sqlite3.Connection, project_id: str, entity_type: str, entity_id: str, source: str) -> None:
    snapshot = _snapshot(db, entity_type, entity_id)
    db.execute(
        """INSERT INTO creative_revisions
        (id, project_id, entity_type, entity_id, revision, source, snapshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"creative-revision-{uuid.uuid4().hex[:12]}",
            project_id,
            entity_type,
            entity_id,
            snapshot["revision"],
            source.strip(),
            json.dumps(snapshot, ensure_ascii=False),
            utc_now(),
        ),
    )


def _advance(
    db: sqlite3.Connection,
    table: str,
    entity_type: str,
    entity_id: str,
    project_id: str,
    base_revision: int,
    source: str,
    values: dict[str, Any],
) -> None:
    values = _clean(values)
    assignments = [f"{key} = ?" for key in values]
    assignments.extend(["revision = revision + 1", "updated_at = ?"])
    cursor = db.execute(
        f"UPDATE {table} SET {', '.join(assignments)} "
        "WHERE id = ? AND project_id = ? AND revision = ? AND status <> 'archived'",
        (*values.values(), utc_now(), entity_id, project_id, base_revision),
    )
    if cursor.rowcount != 1:
        raise HTTPException(409, "内容已被其他操作更新，请刷新后重试")
    _save_revision(db, project_id, entity_type, entity_id, source)


def _bootstrap_project(db: sqlite3.Connection, project: dict[str, Any]) -> None:
    project_id = project["id"]
    now = utc_now()
    if not db.execute("SELECT 1 FROM creative_briefs WHERE project_id = ?", (project_id,)).fetchone():
        db.execute(
            """INSERT INTO creative_briefs
            (project_id, theme, genre, tone, audience, target_duration, constraints, status,
             revision, created_at, updated_at)
            VALUES (?, ?, '', '', '', ?, '', 'draft', 1, ?, ?)""",
            (project_id, project.get("logline") or "", float(project.get("target_duration") or 0), now, now),
        )
        _save_revision(db, project_id, "brief", project_id, "migration:project")
    if not db.execute("SELECT 1 FROM creative_proposals WHERE project_id = ?", (project_id,)).fetchone():
        proposal_id = f"proposal-{uuid.uuid4().hex[:12]}"
        db.execute(
            """INSERT INTO creative_proposals
            (id, project_id, ordinal, title, synopsis, core_conflict, ending, status,
             revision, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?, '', '', 'finalized', 1, ?, ?)""",
            (proposal_id, project_id, project["title"], project.get("logline") or "", now, now),
        )
        _save_revision(db, project_id, "proposal", proposal_id, "migration:project")
    if db.execute("SELECT 1 FROM creative_chapters WHERE project_id = ?", (project_id,)).fetchone():
        return

    document = db.execute("SELECT id FROM script_documents WHERE project_id = ?", (project_id,)).fetchone()
    if document:
        acts = db.execute(
            """SELECT * FROM script_sections WHERE document_id = ? AND section_type = 'act'
            ORDER BY ordinal""",
            (document["id"],),
        ).fetchall()
        for chapter_ordinal, act in enumerate(acts, start=1):
            chapter_id = f"chapter-{uuid.uuid4().hex[:12]}"
            db.execute(
                """INSERT INTO creative_chapters
                (id, project_id, ordinal, title, summary, pacing_goal, planned_seconds, status,
                 revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                (
                    chapter_id,
                    project_id,
                    chapter_ordinal,
                    act["title"],
                    act["summary"],
                    act["goal"],
                    float(act["planned_seconds"] or 0),
                    now,
                    now,
                ),
            )
            scenes = db.execute(
                """SELECT * FROM script_sections WHERE parent_id = ? AND section_type = 'scene'
                ORDER BY ordinal""",
                (act["id"],),
            ).fetchall()
            for section_ordinal, scene in enumerate(scenes, start=1):
                section_id = f"section-{uuid.uuid4().hex[:12]}"
                db.execute(
                    """INSERT INTO creative_sections
                    (id, project_id, chapter_id, ordinal, title, summary, pacing_goal, planned_seconds,
                     status, revision, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                    (
                        section_id,
                        project_id,
                        chapter_id,
                        section_ordinal,
                        scene["title"],
                        scene["summary"],
                        scene["goal"],
                        float(scene["planned_seconds"] or 0),
                        now,
                        now,
                    ),
                )
                _save_revision(db, project_id, "section", section_id, "migration:script")
            _save_revision(db, project_id, "chapter", chapter_id, "migration:script")
        return

    shots = db.execute("SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal", (project_id,)).fetchall()
    if not shots:
        return
    chapter_id = f"chapter-{uuid.uuid4().hex[:12]}"
    db.execute(
        """INSERT INTO creative_chapters
        (id, project_id, ordinal, title, summary, pacing_goal, planned_seconds, status,
         revision, created_at, updated_at)
        VALUES (?, ?, 1, '第一章', ?, '承接现有分镜', ?, 'draft', 1, ?, ?)""",
        (
            chapter_id,
            project_id,
            project.get("logline") or "",
            sum(float(shot["seconds"] or 0) for shot in shots),
            now,
            now,
        ),
    )
    for ordinal, shot in enumerate(shots, start=1):
        section_id = f"section-{uuid.uuid4().hex[:12]}"
        db.execute(
            """INSERT INTO creative_sections
            (id, project_id, chapter_id, ordinal, title, summary, pacing_goal, planned_seconds,
             status, revision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '承接现有镜头', ?, 'draft', 1, ?, ?)""",
            (
                section_id,
                project_id,
                chapter_id,
                ordinal,
                shot["title"],
                shot["description"],
                float(shot["seconds"] or 0),
                now,
                now,
            ),
        )
        _save_revision(db, project_id, "section", section_id, "migration:shots")
    _save_revision(db, project_id, "chapter", chapter_id, "migration:shots")


def _entity_public(db: sqlite3.Connection, entity_type: str, item: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    entity_id = result["project_id"] if entity_type == "brief" else result["id"]
    result["version_count"] = int(
        db.execute(
            """SELECT COUNT(*) AS count FROM creative_revisions
            WHERE entity_type = ? AND entity_id = ?""",
            (entity_type, entity_id),
        ).fetchone()["count"]
    )
    return result


def _workspace(db_path: Path) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        _bootstrap_project(db, project)
        db.commit()
        brief = _entity_public(
            db,
            "brief",
            db.execute("SELECT * FROM creative_briefs WHERE project_id = ?", (project["id"],)).fetchone(),
        )
        proposals = [
            _entity_public(db, "proposal", item)
            for item in db.execute(
                """SELECT * FROM creative_proposals
                WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (project["id"],),
            ).fetchall()
        ]
        characters = [
            _entity_public(db, "character", item)
            for item in db.execute(
                """SELECT * FROM creative_characters
                WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (project["id"],),
            ).fetchall()
        ]
        chapters: list[dict[str, Any]] = []
        for item in db.execute(
            """SELECT * FROM creative_chapters
            WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal""",
            (project["id"],),
        ).fetchall():
            chapter = _entity_public(db, "chapter", item)
            chapter["sections"] = [
                _entity_public(db, "section", section)
                for section in db.execute(
                    """SELECT * FROM creative_sections
                    WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal""",
                    (chapter["id"],),
                ).fetchall()
            ]
            chapters.append(chapter)
        finalized = next((proposal for proposal in proposals if proposal["status"] == "finalized"), None)
        section_count = sum(len(chapter["sections"]) for chapter in chapters)
        next_actions = [
            {
                "id": "brief",
                "label": "完善并批准创作简报",
                "complete": brief["status"] == "approved",
                "action": "填写主题、类型、基调、受众和约束",
            },
            {
                "id": "proposal",
                "label": "对比至少两个剧情提案并定案",
                "complete": len(proposals) >= 2 and finalized is not None,
                "action": f"当前 {len(proposals)} 个提案，{'已有定案' if finalized else '尚未定案'}",
            },
            {
                "id": "characters",
                "label": "建立角色连续性档案",
                "complete": bool(characters) and all(item["status"] == "approved" for item in characters),
                "action": "补齐关系、外观和声音，并逐个批准",
            },
            {
                "id": "structure",
                "label": "完成章节与小节结构",
                "complete": bool(chapters) and section_count > 0,
                "action": "为定案剧情拆出可排序的章节和小节",
            },
        ]
        return {
            "project": {"id": project["id"], "title": project["title"], "episode": project["episode"]},
            "brief": brief,
            "proposals": proposals,
            "characters": characters,
            "chapters": chapters,
            "summary": {
                "proposal_count": len(proposals),
                "character_count": len(characters),
                "chapter_count": len(chapters),
                "section_count": section_count,
                "finalized_proposal_id": finalized["id"] if finalized else None,
                "ready": all(item["complete"] for item in next_actions),
            },
            "next_actions": next_actions,
        }


def _archive_entity(
    db: sqlite3.Connection,
    table: str,
    entity_type: str,
    entity_id: str,
    project_id: str,
    payload: RevisionBase,
) -> None:
    _advance(
        db,
        table,
        entity_type,
        entity_id,
        project_id,
        payload.base_revision,
        payload.source,
        {"status": "archived"},
    )


def _normalize_order(
    db: sqlite3.Connection,
    table: str,
    entity_type: str,
    project_id: str,
    ids: list[str],
    source: str,
    *,
    chapter_id: str | None = None,
) -> None:
    where = "project_id = ? AND status <> 'archived'"
    params: tuple[Any, ...] = (project_id,)
    if chapter_id:
        where += " AND chapter_id = ?"
        params += (chapter_id,)
    current = db.execute(f"SELECT id, ordinal FROM {table} WHERE {where} ORDER BY ordinal", params).fetchall()
    if set(ids) != {item["id"] for item in current} or len(ids) != len(current):
        raise HTTPException(422, "排序必须包含当前层级的全部有效条目，不能跨项目或遗漏")
    current_order = {item["id"]: int(item["ordinal"]) for item in current}
    for ordinal, entity_id in enumerate(ids, start=1):
        if current_order[entity_id] == ordinal:
            continue
        db.execute(
            f"UPDATE {table} SET ordinal = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
            (ordinal, utc_now(), entity_id),
        )
        _save_revision(db, project_id, entity_type, entity_id, source)


def _verify_revision_map(records: list[sqlite3.Row], expected: dict[str, int], label: str) -> None:
    current = {str(record["id"]): int(record["revision"]) for record in records}
    normalized = {str(entity_id): int(revision) for entity_id, revision in expected.items()}
    if current != normalized:
        raise HTTPException(409, f"{label}已在其他操作中更新，请刷新后重试")


def _verify_parent_revision(record: sqlite3.Row, expected: int | None, label: str) -> None:
    if expected is None:
        raise HTTPException(422, f"{label}操作缺少父级修订前置条件")
    if int(record["revision"]) != int(expected):
        raise HTTPException(409, f"{label}父级已在其他操作中更新，请刷新后重试")


def _shift_ordinals(
    db: sqlite3.Connection,
    table: str,
    entity_type: str,
    project_id: str,
    source: str,
    *,
    ordinal_after: int,
    chapter_id: str | None = None,
) -> None:
    where = "project_id = ? AND status <> 'archived' AND ordinal > ?"
    params: tuple[Any, ...] = (project_id, ordinal_after)
    if chapter_id:
        where += " AND chapter_id = ?"
        params += (chapter_id,)
    records = db.execute(f"SELECT id FROM {table} WHERE {where} ORDER BY ordinal DESC", params).fetchall()
    for record in records:
        db.execute(
            f"UPDATE {table} SET ordinal = ordinal + 1, revision = revision + 1, updated_at = ? WHERE id = ?",
            (utc_now(), record["id"]),
        )
        _save_revision(db, project_id, entity_type, record["id"], source)


def create_content_router(db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/api/creative-planning", tags=["creative-planning"])

    @router.get("")
    def get_workspace() -> dict[str, Any]:
        return _workspace(db_path)

    @router.get("/archive")
    def get_archive() -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            specs = (
                ("proposal", "creative_proposals", "title"),
                ("character", "creative_characters", "name"),
                ("chapter", "creative_chapters", "title"),
                ("section", "creative_sections", "title"),
            )
            entries: list[dict[str, Any]] = []
            for entity_type, table, title_column in specs:
                archived = db.execute(
                    f"""SELECT id, {title_column} AS title, status, revision, updated_at
                    FROM {table} WHERE project_id = ? AND status = 'archived' ORDER BY updated_at DESC""",
                    (project["id"],),
                ).fetchall()
                for record in archived:
                    latest = db.execute(
                        """SELECT source, created_at FROM creative_revisions
                        WHERE project_id = ? AND entity_type = ? AND entity_id = ? AND revision = ?""",
                        (project["id"], entity_type, record["id"], record["revision"]),
                    ).fetchone()
                    entries.append({
                        **dict(record),
                        "entity_type": entity_type,
                        "source": latest["source"] if latest else "unknown",
                        "archived_at": latest["created_at"] if latest else record["updated_at"],
                        "history_url": f"/api/creative-planning/history/{entity_type}/{record['id']}",
                    })
            entries.sort(key=lambda item: item["archived_at"], reverse=True)
            return {
                "project": {"id": project["id"], "title": project["title"], "episode": project["episode"]},
                "entries": entries,
                "summary": {
                    "total": len(entries),
                    "by_type": {
                        entity_type: sum(item["entity_type"] == entity_type for item in entries)
                        for entity_type in ("proposal", "character", "chapter", "section")
                    },
                },
            }

    @router.put("/brief")
    def update_brief(payload: BriefUpdate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _bootstrap_project(db, project)
            values = _clean(payload.model_dump(exclude={"base_revision", "source"}))
            cursor = db.execute(
                """UPDATE creative_briefs SET theme = ?, genre = ?, tone = ?, audience = ?,
                target_duration = ?, constraints = ?, status = ?, revision = revision + 1, updated_at = ?
                WHERE project_id = ? AND revision = ?""",
                (
                    values["theme"], values["genre"], values["tone"], values["audience"],
                    values["target_duration"], values["constraints"], values["status"], utc_now(),
                    project["id"], payload.base_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise HTTPException(409, "创作简报已更新，请刷新后重试")
            _save_revision(db, project["id"], "brief", project["id"], payload.source)
            db.commit()
        return _workspace(db_path)

    @router.post("/proposals", status_code=201)
    def create_proposal(payload: ProposalCreate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _bootstrap_project(db, project)
            proposal_id = f"proposal-{uuid.uuid4().hex[:12]}"
            ordinal = int(db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM creative_proposals WHERE project_id = ? AND status <> 'archived'",
                (project["id"],),
            ).fetchone()[0])
            now = utc_now()
            values = _clean(payload.model_dump(exclude={"source"}))
            db.execute(
                """INSERT INTO creative_proposals
                (id, project_id, ordinal, title, synopsis, core_conflict, ending, status,
                 revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                (proposal_id, project["id"], ordinal, values["title"], values["synopsis"],
                 values["core_conflict"], values["ending"], now, now),
            )
            _save_revision(db, project["id"], "proposal", proposal_id, payload.source)
            db.commit()
        return _workspace(db_path)

    @router.patch("/proposals/{proposal_id}")
    def update_proposal(proposal_id: str, payload: ProposalUpdate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _row_for_project(db, "creative_proposals", proposal_id, project["id"])
            values = payload.model_dump(exclude_none=True, exclude={"base_revision", "source"})
            if values:
                _advance(db, "creative_proposals", "proposal", proposal_id, project["id"], payload.base_revision, payload.source, values)
            db.commit()
        return _workspace(db_path)

    @router.post("/proposals/{proposal_id}/finalize")
    def finalize_proposal(proposal_id: str, payload: RevisionBase) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _row_for_project(db, "creative_proposals", proposal_id, project["id"])
            previous = db.execute(
                """SELECT id, revision FROM creative_proposals
                WHERE project_id = ? AND status = 'finalized' AND id <> ?""",
                (project["id"], proposal_id),
            ).fetchone()
            if previous:
                _advance(
                    db, "creative_proposals", "proposal", previous["id"], project["id"],
                    previous["revision"], f"{payload.source}:superseded", {"status": "draft"},
                )
            _advance(
                db, "creative_proposals", "proposal", proposal_id, project["id"],
                payload.base_revision, payload.source, {"status": "finalized"},
            )
            db.commit()
        return _workspace(db_path)

    @router.post("/proposals/{proposal_id}/archive")
    def archive_proposal(proposal_id: str, payload: RevisionBase) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            proposal = _row_for_project(db, "creative_proposals", proposal_id, project["id"])
            if proposal["status"] == "finalized":
                raise HTTPException(409, "定案提案不能直接归档，请先定案另一个提案")
            _archive_entity(db, "creative_proposals", "proposal", proposal_id, project["id"], payload)
            db.commit()
        return _workspace(db_path)

    @router.post("/characters", status_code=201)
    def create_character(payload: CharacterCreate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            character_id = f"character-{uuid.uuid4().hex[:12]}"
            ordinal = int(db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM creative_characters WHERE project_id = ? AND status <> 'archived'",
                (project["id"],),
            ).fetchone()[0])
            now = utc_now()
            values = _clean(payload.model_dump(exclude={"source"}))
            db.execute(
                """INSERT INTO creative_characters
                (id, project_id, ordinal, name, identity, goal, obstacle, personality, appearance,
                 voice, relationships, reference_notes, status, revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                (character_id, project["id"], ordinal, *values.values(), now, now),
            )
            _save_revision(db, project["id"], "character", character_id, payload.source)
            db.commit()
        return _workspace(db_path)

    @router.patch("/characters/{character_id}")
    def update_character(character_id: str, payload: CharacterUpdate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _row_for_project(db, "creative_characters", character_id, project["id"])
            values = payload.model_dump(exclude_none=True, exclude={"base_revision", "source"})
            if values:
                _advance(db, "creative_characters", "character", character_id, project["id"], payload.base_revision, payload.source, values)
            db.commit()
        return _workspace(db_path)

    @router.post("/characters/{character_id}/archive")
    def archive_character(character_id: str, payload: RevisionBase) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _row_for_project(db, "creative_characters", character_id, project["id"])
            _archive_entity(db, "creative_characters", "character", character_id, project["id"], payload)
            db.commit()
        return _workspace(db_path)

    @router.post("/chapters", status_code=201)
    def create_chapter(payload: ChapterCreate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            chapter_id = f"chapter-{uuid.uuid4().hex[:12]}"
            ordinal = int(db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM creative_chapters WHERE project_id = ? AND status <> 'archived'",
                (project["id"],),
            ).fetchone()[0])
            now = utc_now()
            values = _clean(payload.model_dump(exclude={"source"}))
            db.execute(
                """INSERT INTO creative_chapters
                (id, project_id, ordinal, title, summary, pacing_goal, planned_seconds, status,
                 revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                (chapter_id, project["id"], ordinal, *values.values(), now, now),
            )
            _save_revision(db, project["id"], "chapter", chapter_id, payload.source)
            db.commit()
        return _workspace(db_path)

    @router.patch("/chapters/{chapter_id}")
    def update_chapter(chapter_id: str, payload: ChapterUpdate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _row_for_project(db, "creative_chapters", chapter_id, project["id"])
            values = payload.model_dump(exclude_none=True, exclude={"base_revision", "source"})
            if values:
                _advance(db, "creative_chapters", "chapter", chapter_id, project["id"], payload.base_revision, payload.source, values)
            db.commit()
        return _workspace(db_path)

    @router.post("/chapters/{chapter_id}/archive")
    def archive_chapter(chapter_id: str, payload: RevisionBase) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _row_for_project(db, "creative_chapters", chapter_id, project["id"])
            section_rows = db.execute(
                "SELECT id, revision FROM creative_sections WHERE chapter_id = ? AND status <> 'archived'",
                (chapter_id,),
            ).fetchall()
            for section in section_rows:
                _advance(
                    db, "creative_sections", "section", section["id"], project["id"], section["revision"],
                    f"{payload.source}:chapter", {"status": "archived"},
                )
            _archive_entity(db, "creative_chapters", "chapter", chapter_id, project["id"], payload)
            db.commit()
        return _workspace(db_path)

    @router.put("/chapters/order")
    def order_chapters(payload: OrderUpdate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            chapters = db.execute(
                """SELECT id, revision FROM creative_chapters
                WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (project["id"],),
            ).fetchall()
            _verify_revision_map(chapters, payload.base_revisions, "章节排序")
            _normalize_order(db, "creative_chapters", "chapter", project["id"], payload.ids, payload.source)
            db.commit()
        return _workspace(db_path)

    @router.post("/chapters/{chapter_id}/sections", status_code=201)
    def create_section(chapter_id: str, payload: SectionCreate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            chapter = _row_for_project(db, "creative_chapters", chapter_id, project["id"])
            section_id = f"section-{uuid.uuid4().hex[:12]}"
            ordinal = int(db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM creative_sections WHERE chapter_id = ? AND status <> 'archived'",
                (chapter_id,),
            ).fetchone()[0])
            now = utc_now()
            values = _clean(payload.model_dump(exclude={"source"}))
            db.execute(
                """INSERT INTO creative_sections
                (id, project_id, chapter_id, ordinal, title, summary, content, pacing_goal, planned_seconds,
                 status, revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                (section_id, project["id"], chapter_id, ordinal, *values.values(), now, now),
            )
            _save_revision(db, project["id"], "section", section_id, payload.source)
            db.execute(
                "UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (utc_now(), chapter_id),
            )
            _save_revision(db, project["id"], "chapter", chapter_id, f"{payload.source}:section")
            db.commit()
        return _workspace(db_path)

    @router.patch("/sections/{section_id}")
    def update_section(section_id: str, payload: SectionUpdate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _row_for_project(db, "creative_sections", section_id, project["id"])
            values = payload.model_dump(exclude_none=True, exclude={"base_revision", "source"})
            if values:
                _advance(db, "creative_sections", "section", section_id, project["id"], payload.base_revision, payload.source, values)
            db.commit()
        return _workspace(db_path)

    @router.post("/sections/{section_id}/archive")
    def archive_section(section_id: str, payload: RevisionBase) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            section = _row_for_project(db, "creative_sections", section_id, project["id"])
            _archive_entity(db, "creative_sections", "section", section_id, project["id"], payload)
            db.execute(
                "UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (utc_now(), section["chapter_id"]),
            )
            _save_revision(db, project["id"], "chapter", section["chapter_id"], f"{payload.source}:section")
            db.commit()
        return _workspace(db_path)

    @router.put("/chapters/{chapter_id}/sections/order")
    def order_sections(chapter_id: str, payload: OrderUpdate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            chapter = _row_for_project(db, "creative_chapters", chapter_id, project["id"])
            sections = db.execute(
                """SELECT id, revision FROM creative_sections
                WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (chapter_id,),
            ).fetchall()
            _verify_parent_revision(chapter, payload.parent_base_revision, "小节排序")
            _verify_revision_map(sections, payload.base_revisions, "小节排序")
            _normalize_order(
                db, "creative_sections", "section", project["id"], payload.ids, payload.source, chapter_id=chapter_id,
            )
            db.execute(
                "UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (utc_now(), chapter_id),
            )
            _save_revision(db, project["id"], "chapter", chapter_id, f"{payload.source}:sections")
            db.commit()
        return _workspace(db_path)

    @router.post("/chapters/{chapter_id}/split")
    def split_chapter(chapter_id: str, payload: ChapterSplit) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            chapters = db.execute(
                """SELECT * FROM creative_chapters
                WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (project["id"],),
            ).fetchall()
            _verify_revision_map(chapters, payload.base_revisions, "章节拆分")
            chapter = next((record for record in chapters if record["id"] == chapter_id), None)
            if not chapter:
                raise HTTPException(404, "创作内容不存在或不属于当前项目")
            sections = db.execute(
                "SELECT * FROM creative_sections WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal",
                (chapter_id,),
            ).fetchall()
            _verify_revision_map(sections, payload.child_base_revisions, "章节拆分涉及的小节")
            split_index = next((index for index, section in enumerate(sections) if section["id"] == payload.section_id), -1)
            if split_index <= 0:
                raise HTTPException(422, "请选择当前章节第二个或更后面的小节作为拆分起点")
            _shift_ordinals(
                db, "creative_chapters", "chapter", project["id"], f"{payload.source}:reorder",
                ordinal_after=int(chapter["ordinal"]),
            )
            new_id = f"chapter-{uuid.uuid4().hex[:12]}"
            now = utc_now()
            db.execute(
                """INSERT INTO creative_chapters
                (id, project_id, ordinal, title, summary, pacing_goal, planned_seconds, status,
                 revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', ?, 0, 'draft', 1, ?, ?)""",
                (new_id, project["id"], chapter["ordinal"] + 1, payload.new_title.strip(), chapter["pacing_goal"], now, now),
            )
            moved = sections[split_index:]
            for ordinal, section in enumerate(moved, start=1):
                db.execute(
                    """UPDATE creative_sections SET chapter_id = ?, ordinal = ?, revision = revision + 1,
                    updated_at = ? WHERE id = ?""",
                    (new_id, ordinal, utc_now(), section["id"]),
                )
                _save_revision(db, project["id"], "section", section["id"], payload.source)
            db.execute(
                "UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (utc_now(), chapter_id),
            )
            _save_revision(db, project["id"], "chapter", chapter_id, payload.source)
            _save_revision(db, project["id"], "chapter", new_id, payload.source)
            db.commit()
        return _workspace(db_path)

    @router.post("/chapters/{chapter_id}/merge")
    def merge_chapter(chapter_id: str, payload: MergeRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            chapters = db.execute(
                """SELECT * FROM creative_chapters
                WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (project["id"],),
            ).fetchall()
            _verify_revision_map(chapters, payload.base_revisions, "章节合并")
            source = next((record for record in chapters if record["id"] == chapter_id), None)
            target = next((record for record in chapters if record["id"] == payload.target_id), None)
            if not source or not target:
                raise HTTPException(404, "创作内容不存在或不属于当前项目")
            if source["id"] == target["id"]:
                raise HTTPException(422, "章节不能合并到自身")
            start = int(db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) FROM creative_sections WHERE chapter_id = ? AND status <> 'archived'",
                (target["id"],),
            ).fetchone()[0])
            source_sections = db.execute(
                "SELECT * FROM creative_sections WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal",
                (source["id"],),
            ).fetchall()
            _verify_revision_map(source_sections, payload.child_base_revisions, "章节合并涉及的小节")
            for offset, section in enumerate(source_sections, start=1):
                db.execute(
                    """UPDATE creative_sections SET chapter_id = ?, ordinal = ?, revision = revision + 1,
                    updated_at = ? WHERE id = ?""",
                    (target["id"], start + offset, utc_now(), section["id"]),
                )
                _save_revision(db, project["id"], "section", section["id"], payload.source)
            combined_summary = "\n\n".join(part for part in (target["summary"], source["summary"]) if part.strip())
            _advance(
                db, "creative_chapters", "chapter", target["id"], project["id"], target["revision"], payload.source,
                {"summary": combined_summary, "planned_seconds": float(target["planned_seconds"]) + float(source["planned_seconds"])},
            )
            _advance(
                db, "creative_chapters", "chapter", source["id"], project["id"], source["revision"], payload.source,
                {"status": "archived"},
            )
            remaining = [
                item["id"]
                for item in db.execute(
                    "SELECT id FROM creative_chapters WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal",
                    (project["id"],),
                ).fetchall()
            ]
            _normalize_order(db, "creative_chapters", "chapter", project["id"], remaining, f"{payload.source}:reorder")
            db.commit()
        return _workspace(db_path)

    @router.post("/sections/{section_id}/split")
    def split_section(section_id: str, payload: SectionSplit) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            section_row = _row_for_project(db, "creative_sections", section_id, project["id"])
            chapter = _row_for_project(db, "creative_chapters", section_row["chapter_id"], project["id"])
            sections = db.execute(
                """SELECT * FROM creative_sections
                WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (chapter["id"],),
            ).fetchall()
            _verify_parent_revision(chapter, payload.parent_base_revision, "小节拆分")
            _verify_revision_map(sections, payload.base_revisions, "小节拆分")
            section = next(record for record in sections if record["id"] == section_id)
            _shift_ordinals(
                db, "creative_sections", "section", project["id"], f"{payload.source}:reorder",
                ordinal_after=int(section["ordinal"]), chapter_id=section["chapter_id"],
            )
            half_seconds = round(float(section["planned_seconds"]) / 2, 3)
            _advance(
                db, "creative_sections", "section", section_id, project["id"], section["revision"], payload.source,
                {"summary": payload.summary_before, "planned_seconds": half_seconds},
            )
            new_id = f"section-{uuid.uuid4().hex[:12]}"
            now = utc_now()
            db.execute(
                """INSERT INTO creative_sections
                (id, project_id, chapter_id, ordinal, title, summary, pacing_goal, planned_seconds,
                 status, revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                (
                    new_id, project["id"], section["chapter_id"], section["ordinal"] + 1,
                    payload.new_title.strip(), payload.summary_after.strip(), section["pacing_goal"],
                    float(section["planned_seconds"]) - half_seconds, now, now,
                ),
            )
            _save_revision(db, project["id"], "section", new_id, payload.source)
            db.execute(
                "UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (utc_now(), section["chapter_id"]),
            )
            _save_revision(db, project["id"], "chapter", section["chapter_id"], payload.source)
            db.commit()
        return _workspace(db_path)

    @router.post("/sections/{section_id}/merge")
    def merge_section(section_id: str, payload: MergeRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            source_row = _row_for_project(db, "creative_sections", section_id, project["id"])
            chapter = _row_for_project(db, "creative_chapters", source_row["chapter_id"], project["id"])
            sections = db.execute(
                """SELECT * FROM creative_sections
                WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal""",
                (chapter["id"],),
            ).fetchall()
            _verify_parent_revision(chapter, payload.parent_base_revision, "小节合并")
            _verify_revision_map(sections, payload.base_revisions, "小节合并")
            source = next((record for record in sections if record["id"] == section_id), None)
            target = next((record for record in sections if record["id"] == payload.target_id), None)
            if not source or not target:
                raise HTTPException(404, "创作内容不存在或不属于当前项目")
            if source["id"] == target["id"]:
                raise HTTPException(422, "小节不能合并到自身")
            if source["chapter_id"] != target["chapter_id"]:
                raise HTTPException(422, "只能合并同一章节内的小节")
            combined = "\n\n".join(part for part in (target["summary"], source["summary"]) if part.strip())
            combined_content = "\n\n".join(part for part in (target["content"], source["content"]) if part.strip())
            _advance(
                db, "creative_sections", "section", target["id"], project["id"], target["revision"], payload.source,
                {
                    "summary": combined,
                    "content": combined_content,
                    "planned_seconds": float(target["planned_seconds"]) + float(source["planned_seconds"]),
                },
            )
            _advance(
                db, "creative_sections", "section", source["id"], project["id"], source["revision"], payload.source,
                {"status": "archived"},
            )
            remaining = [
                item["id"]
                for item in db.execute(
                    """SELECT id FROM creative_sections WHERE chapter_id = ? AND status <> 'archived'
                    ORDER BY ordinal""",
                    (source["chapter_id"],),
                ).fetchall()
            ]
            _normalize_order(
                db, "creative_sections", "section", project["id"], remaining,
                f"{payload.source}:reorder", chapter_id=source["chapter_id"],
            )
            db.execute(
                "UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (utc_now(), source["chapter_id"]),
            )
            _save_revision(db, project["id"], "chapter", source["chapter_id"], payload.source)
            db.commit()
        return _workspace(db_path)

    @router.get("/history/{entity_type}/{entity_id}")
    def history(entity_type: Literal["brief", "proposal", "character", "chapter", "section"], entity_id: str) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            rows = db.execute(
                """SELECT * FROM creative_revisions
                WHERE project_id = ? AND entity_type = ? AND entity_id = ? ORDER BY revision DESC""",
                (project["id"], entity_type, entity_id),
            ).fetchall()
            if not rows:
                raise HTTPException(404, "当前项目没有该内容的修订历史")
            revisions = []
            for row in rows:
                item = dict(row)
                item["snapshot"] = json.loads(item["snapshot"])
                revisions.append(item)
            return {"entity_type": entity_type, "entity_id": entity_id, "revisions": revisions}

    return router
