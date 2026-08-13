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


BibleType = Literal["character", "location", "prop", "style", "voice"]
BIBLE_TYPES: tuple[str, ...] = ("character", "location", "prop", "style", "voice")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_bible_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS production_bible_entries (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          entry_type TEXT NOT NULL CHECK(entry_type IN ('character', 'location', 'prop', 'style', 'voice')),
          name TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '',
          canonical_description TEXT NOT NULL DEFAULT '',
          prompt_fragment TEXT NOT NULL DEFAULT '',
          negative_prompt TEXT NOT NULL DEFAULT '',
          continuity_rules TEXT NOT NULL DEFAULT '',
          apply_globally INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'locked')),
          revision INTEGER NOT NULL DEFAULT 1,
          archived INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bible_entries_project
          ON production_bible_entries(project_id, archived, entry_type, name);
        CREATE TABLE IF NOT EXISTS production_bible_assets (
          entry_id TEXT NOT NULL REFERENCES production_bible_entries(id) ON DELETE CASCADE,
          asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
          role TEXT NOT NULL DEFAULT 'reference',
          ordinal INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(entry_id, asset_id)
        );
        CREATE INDEX IF NOT EXISTS idx_bible_assets_entry
          ON production_bible_assets(entry_id, ordinal);
        CREATE TABLE IF NOT EXISTS production_bible_shots (
          entry_id TEXT NOT NULL REFERENCES production_bible_entries(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          role TEXT NOT NULL DEFAULT 'continuity',
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          PRIMARY KEY(entry_id, shot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_bible_shots_shot
          ON production_bible_shots(shot_id, entry_id);
        CREATE TABLE IF NOT EXISTS production_bible_versions (
          id TEXT PRIMARY KEY,
          entry_id TEXT NOT NULL REFERENCES production_bible_entries(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          status TEXT NOT NULL,
          source TEXT NOT NULL,
          snapshot TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(entry_id, revision)
        );
        """
    )


class BibleEntryCreate(BaseModel):
    entry_type: BibleType
    name: str = Field(min_length=1, max_length=80)
    summary: str = Field("", max_length=600)
    canonical_description: str = Field("", max_length=4000)
    prompt_fragment: str = Field("", max_length=4000)
    negative_prompt: str = Field("", max_length=2000)
    continuity_rules: str = Field("", max_length=4000)
    apply_globally: bool = False


class BibleEntryPatch(BaseModel):
    base_revision: int = Field(ge=1)
    name: str | None = Field(None, min_length=1, max_length=80)
    summary: str | None = Field(None, max_length=600)
    canonical_description: str | None = Field(None, max_length=4000)
    prompt_fragment: str | None = Field(None, max_length=4000)
    negative_prompt: str | None = Field(None, max_length=2000)
    continuity_rules: str | None = Field(None, max_length=4000)
    apply_globally: bool | None = None


class RevisionRequest(BaseModel):
    base_revision: int = Field(ge=1)


class AssetLinkRequest(RevisionRequest):
    asset_id: str = Field(min_length=1, max_length=120)
    role: str = Field("reference", min_length=1, max_length=60)


class ShotLinkRequest(RevisionRequest):
    shot_id: str = Field(min_length=1, max_length=160)
    role: str = Field("continuity", min_length=1, max_length=60)
    note: str = Field("", max_length=600)


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    setting = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
    if not setting:
        raise HTTPException(404, "当前没有已激活的项目")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (setting["value"],)).fetchone()
    if not project:
        raise HTTPException(404, "当前项目不存在")
    return dict(project)


def _entry_for_project(db: sqlite3.Connection, entry_id: str, project_id: str) -> sqlite3.Row:
    entry = db.execute(
        "SELECT * FROM production_bible_entries WHERE id = ? AND project_id = ? AND archived = 0",
        (entry_id, project_id),
    ).fetchone()
    if not entry:
        raise HTTPException(404, "生产圣经条目不存在")
    return entry


def _entry_snapshot(db: sqlite3.Connection, entry_id: str) -> dict[str, Any]:
    entry = db.execute("SELECT * FROM production_bible_entries WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        raise HTTPException(404, "生产圣经条目不存在")
    payload = dict(entry)
    payload["apply_globally"] = bool(payload["apply_globally"])
    payload["archived"] = bool(payload["archived"])
    payload["assets"] = [
        dict(item)
        for item in db.execute(
            "SELECT asset_id, role, ordinal FROM production_bible_assets WHERE entry_id = ? ORDER BY ordinal",
            (entry_id,),
        ).fetchall()
    ]
    payload["shots"] = [
        dict(item)
        for item in db.execute(
            "SELECT shot_id, role, note FROM production_bible_shots WHERE entry_id = ? ORDER BY shot_id",
            (entry_id,),
        ).fetchall()
    ]
    return payload


def _save_version(db: sqlite3.Connection, entry_id: str, source: str) -> None:
    snapshot = _entry_snapshot(db, entry_id)
    db.execute(
        """INSERT INTO production_bible_versions
        (id, entry_id, revision, status, source, snapshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            f"bible-version-{uuid.uuid4().hex[:12]}",
            entry_id,
            snapshot["revision"],
            snapshot["status"],
            source,
            json.dumps(snapshot, ensure_ascii=False),
            utc_now(),
        ),
    )


def _advance_revision(
    db: sqlite3.Connection,
    entry_id: str,
    project_id: str,
    base_revision: int,
    source: str,
    *,
    status: str = "draft",
) -> None:
    cursor = db.execute(
        """UPDATE production_bible_entries
        SET revision = revision + 1, status = ?, updated_at = ?
        WHERE id = ? AND project_id = ? AND revision = ? AND archived = 0""",
        (status, utc_now(), entry_id, project_id, base_revision),
    )
    if cursor.rowcount != 1:
        raise HTTPException(409, "该条目已在其他操作中更新，请刷新后重试")
    _save_version(db, entry_id, source)


def _asset_public(item: dict[str, Any]) -> dict[str, Any]:
    item["bindable"] = item.get("source") == "managed" and not item.get("archived") and bool(item.get("managed_path"))
    item["preview"] = f"/api/assets/{item['id']}/content" if item["bindable"] else item.get("preview") or ""
    return item


def _entry_public(db: sqlite3.Connection, entry: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = dict(entry)
    payload["apply_globally"] = bool(payload["apply_globally"])
    payload["archived"] = bool(payload["archived"])
    asset_rows = db.execute(
        """SELECT link.role AS bible_role, link.ordinal AS bible_ordinal, assets.*
        FROM production_bible_assets link JOIN assets ON assets.id = link.asset_id
        WHERE link.entry_id = ? ORDER BY link.ordinal""",
        (payload["id"],),
    ).fetchall()
    payload["assets"] = [_asset_public(dict(item)) for item in asset_rows]
    payload["shots"] = [
        dict(item)
        for item in db.execute(
            """SELECT link.shot_id, link.role, link.note, shots.ordinal, shots.title, shots.scene_code
            FROM production_bible_shots link JOIN shots ON shots.id = link.shot_id
            WHERE link.entry_id = ? ORDER BY shots.ordinal""",
            (payload["id"],),
        ).fetchall()
    ]
    version_row = db.execute(
        "SELECT COUNT(*) AS count FROM production_bible_versions WHERE entry_id = ?",
        (payload["id"],),
    ).fetchone()
    payload["version_count"] = int(version_row["count"] or 0)
    return payload


def _workspace(db_path: Path) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        entries = [
            _entry_public(db, item)
            for item in db.execute(
                """SELECT * FROM production_bible_entries
                WHERE project_id = ? AND archived = 0
                ORDER BY CASE entry_type
                  WHEN 'character' THEN 1 WHEN 'location' THEN 2 WHEN 'prop' THEN 3
                  WHEN 'style' THEN 4 ELSE 5 END, name""",
                (project["id"],),
            ).fetchall()
        ]
        shots = [
            dict(item)
            for item in db.execute(
                "SELECT id, ordinal, scene_code, title, status FROM shots WHERE project_id = ? ORDER BY ordinal",
                (project["id"],),
            ).fetchall()
        ]
        for shot in shots:
            effective = [
                entry for entry in entries
                if entry["apply_globally"] or any(link["shot_id"] == shot["id"] for link in entry["shots"])
            ]
            shot["entries"] = [
                {"id": entry["id"], "name": entry["name"], "entry_type": entry["entry_type"], "status": entry["status"]}
                for entry in effective
            ]
            shot["locked_count"] = sum(entry["status"] == "locked" for entry in effective)
            shot["attention"] = (
                "未绑定连续性条目" if not effective
                else f"{len(effective) - shot['locked_count']} 项尚未锁定" if shot["locked_count"] < len(effective)
                else "连续性输入已锁定"
            )
        return {
            "project": {"id": project["id"], "title": project["title"], "episode": project["episode"]},
            "entries": entries,
            "shots": shots,
            "summary": {
                "entry_count": len(entries),
                "locked_count": sum(entry["status"] == "locked" for entry in entries),
                "global_count": sum(entry["apply_globally"] for entry in entries),
                "covered_shot_count": sum(bool(shot["entries"]) for shot in shots),
                "shot_count": len(shots),
            },
        }


def create_bible_router(db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/api/bible", tags=["production-bible"])

    @router.get("")
    def get_bible() -> dict[str, Any]:
        return _workspace(db_path)

    @router.post("/entries", status_code=201)
    def create_entry(payload: BibleEntryCreate) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            entry_id = f"bible-{payload.entry_type}-{uuid.uuid4().hex[:10]}"
            now = utc_now()
            values = payload.model_dump()
            db.execute(
                """INSERT INTO production_bible_entries
                (id, project_id, entry_type, name, summary, canonical_description, prompt_fragment,
                 negative_prompt, continuity_rules, apply_globally, status, revision, archived, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, 0, ?, ?)""",
                (
                    entry_id, project["id"], values["entry_type"], values["name"].strip(), values["summary"].strip(),
                    values["canonical_description"].strip(), values["prompt_fragment"].strip(),
                    values["negative_prompt"].strip(), values["continuity_rules"].strip(),
                    int(values["apply_globally"]), now, now,
                ),
            )
            _save_version(db, entry_id, "human:create")
            db.commit()
        return _workspace(db_path)

    @router.patch("/entries/{entry_id}")
    def update_entry(entry_id: str, payload: BibleEntryPatch) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            _entry_for_project(db, entry_id, project["id"])
            updates = payload.model_dump(exclude_none=True)
            base_revision = updates.pop("base_revision")
            if updates:
                cleaned = {key: value.strip() if isinstance(value, str) else int(value) for key, value in updates.items()}
                columns = ", ".join(f"{key} = ?" for key in cleaned)
                db.execute(f"UPDATE production_bible_entries SET {columns} WHERE id = ?", (*cleaned.values(), entry_id))
                _advance_revision(db, entry_id, project["id"], base_revision, "human:update")
                db.commit()
        return _workspace(db_path)

    @router.post("/entries/{entry_id}/lock")
    def lock_entry(entry_id: str, payload: RevisionRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            entry = _entry_for_project(db, entry_id, project["id"])
            if not entry["canonical_description"].strip() or not entry["prompt_fragment"].strip():
                raise HTTPException(422, "锁定前必须填写标准设定与 H3 提示词片段")
            _advance_revision(db, entry_id, project["id"], payload.base_revision, "human:lock", status="locked")
            db.commit()
        return _workspace(db_path)

    @router.post("/entries/{entry_id}/archive")
    def archive_entry(entry_id: str, payload: RevisionRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            _entry_for_project(db, entry_id, project["id"])
            cursor = db.execute(
                """UPDATE production_bible_entries SET archived = 1, revision = revision + 1,
                status = 'draft', updated_at = ? WHERE id = ? AND project_id = ? AND revision = ?""",
                (utc_now(), entry_id, project["id"], payload.base_revision),
            )
            if cursor.rowcount != 1:
                raise HTTPException(409, "该条目已更新，请刷新后重试")
            _save_version(db, entry_id, "human:archive")
            db.commit()
        return _workspace(db_path)

    @router.post("/entries/{entry_id}/assets/link")
    def link_asset(entry_id: str, payload: AssetLinkRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _entry_for_project(db, entry_id, project["id"])
            asset = db.execute(
                """SELECT * FROM assets WHERE id = ? AND project_id = ? AND archived = 0
                AND source = 'managed' AND managed_path IS NOT NULL""",
                (payload.asset_id, project["id"]),
            ).fetchone()
            if not asset:
                raise HTTPException(400, "只有当前项目的真实受管素材可以绑定到生产圣经")
            ordinal = db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next FROM production_bible_assets WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()["next"]
            try:
                db.execute(
                    "INSERT INTO production_bible_assets VALUES (?, ?, ?, ?, ?)",
                    (entry_id, payload.asset_id, payload.role.strip(), ordinal, utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "该素材已经绑定") from exc
            _advance_revision(db, entry_id, project["id"], payload.base_revision, "human:link-asset")
            db.commit()
        return _workspace(db_path)

    @router.post("/entries/{entry_id}/assets/unlink")
    def unlink_asset(entry_id: str, payload: AssetLinkRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _entry_for_project(db, entry_id, project["id"])
            cursor = db.execute(
                "DELETE FROM production_bible_assets WHERE entry_id = ? AND asset_id = ?",
                (entry_id, payload.asset_id),
            )
            if cursor.rowcount != 1:
                raise HTTPException(404, "素材绑定不存在")
            _advance_revision(db, entry_id, project["id"], payload.base_revision, "human:unlink-asset")
            db.commit()
        return _workspace(db_path)

    @router.post("/entries/{entry_id}/shots/link")
    def link_shot(entry_id: str, payload: ShotLinkRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _entry_for_project(db, entry_id, project["id"])
            shot = db.execute(
                "SELECT id FROM shots WHERE id = ? AND project_id = ?",
                (payload.shot_id, project["id"]),
            ).fetchone()
            if not shot:
                raise HTTPException(404, "当前项目中没有这个镜头")
            try:
                db.execute(
                    "INSERT INTO production_bible_shots VALUES (?, ?, ?, ?, ?)",
                    (entry_id, payload.shot_id, payload.role.strip(), payload.note.strip(), utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "该镜头已经绑定") from exc
            _advance_revision(db, entry_id, project["id"], payload.base_revision, "human:link-shot")
            db.commit()
        return _workspace(db_path)

    @router.post("/entries/{entry_id}/shots/unlink")
    def unlink_shot(entry_id: str, payload: ShotLinkRequest) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            _entry_for_project(db, entry_id, project["id"])
            cursor = db.execute(
                "DELETE FROM production_bible_shots WHERE entry_id = ? AND shot_id = ?",
                (entry_id, payload.shot_id),
            )
            if cursor.rowcount != 1:
                raise HTTPException(404, "镜头绑定不存在")
            _advance_revision(db, entry_id, project["id"], payload.base_revision, "human:unlink-shot")
            db.commit()
        return _workspace(db_path)

    @router.get("/entries/{entry_id}/versions")
    def get_versions(entry_id: str) -> list[dict[str, Any]]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            _entry_for_project(db, entry_id, project["id"])
            return [
                {
                    **dict(item),
                    "snapshot": json.loads(item["snapshot"]),
                }
                for item in db.execute(
                    """SELECT * FROM production_bible_versions WHERE entry_id = ?
                    ORDER BY revision DESC LIMIT 30""",
                    (entry_id,),
                ).fetchall()
            ]

    return router
