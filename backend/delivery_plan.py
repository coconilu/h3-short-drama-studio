from __future__ import annotations

import hashlib
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
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_delivery_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS delivery_plans (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('draft', 'locked')),
          revision INTEGER NOT NULL,
          plan_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          locked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS delivery_plan_items (
          plan_id TEXT NOT NULL REFERENCES delivery_plans(id) ON DELETE CASCADE,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          subtitle_enabled INTEGER NOT NULL,
          subtitle_start_seconds REAL,
          transition TEXT NOT NULL DEFAULT 'cut' CHECK(transition = 'cut'),
          PRIMARY KEY(plan_id, shot_id),
          UNIQUE(plan_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS delivery_plan_versions (
          id TEXT PRIMARY KEY,
          plan_id TEXT NOT NULL REFERENCES delivery_plans(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL,
          status TEXT NOT NULL,
          plan_hash TEXT NOT NULL,
          source TEXT NOT NULL,
          snapshot TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(plan_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_versions_plan
          ON delivery_plan_versions(plan_id, revision DESC);
        """
    )


class DeliveryPlanItemInput(BaseModel):
    shot_id: str = Field(min_length=1, max_length=200)
    subtitle_enabled: bool = True
    subtitle_start_seconds: float | None = Field(None, ge=0, le=30)
    transition: Literal["cut"] = "cut"


class DeliveryPlanPatch(BaseModel):
    base_revision: int = Field(ge=0)
    items: list[DeliveryPlanItemInput] = Field(min_length=1, max_length=300)


class DeliveryPlanRevision(BaseModel):
    base_revision: int = Field(ge=1)


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    setting = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
    if not setting:
        raise HTTPException(404, "当前没有已激活的项目")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (setting["value"],)).fetchone()
    if not project:
        raise HTTPException(404, "当前项目不存在")
    return dict(project)


def _default_items(db: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    return [
        {
            "shot_id": shot["id"], "ordinal": index,
            "subtitle_enabled": bool(shot["dialogue"] and shot["subtitle_enabled"]),
            "subtitle_start_seconds": shot["subtitle_start_seconds"] if shot["dialogue"] else None, "transition": "cut",
            "title": shot["title"], "scene_code": shot["scene_code"], "dialogue": shot["dialogue"],
            "seconds": shot["seconds"], "shot_status": shot["status"],
        }
        for index, shot in enumerate(
            db.execute("SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal, id", (project_id,)).fetchall(),
            start=1,
        )
    ]


def _plan_items(db: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    return [
        {**dict(row), "subtitle_enabled": bool(row["subtitle_enabled"])}
        for row in db.execute(
            """SELECT items.*, shots.title, shots.scene_code, shots.dialogue, shots.seconds, shots.status AS shot_status
            FROM delivery_plan_items items JOIN shots ON shots.id = items.shot_id
            WHERE items.plan_id = ? ORDER BY items.ordinal""",
            (plan_id,),
        ).fetchall()
    ]


def _hash_items(items: list[dict[str, Any]]) -> str:
    stable = [
        {
            "shot_id": item["shot_id"], "ordinal": item["ordinal"],
            "subtitle_enabled": bool(item["subtitle_enabled"]),
            "subtitle_start_seconds": item.get("subtitle_start_seconds"), "transition": item.get("transition", "cut"),
        }
        for item in items
    ]
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_items(db: sqlite3.Connection, project_id: str, items: list[dict[str, Any]]) -> None:
    current = {
        row["id"]: dict(row)
        for row in db.execute("SELECT * FROM shots WHERE project_id = ?", (project_id,)).fetchall()
    }
    item_ids = [item["shot_id"] for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise HTTPException(422, "装配清单不能重复镜头")
    if set(item_ids) != set(current):
        missing = set(current) - set(item_ids)
        extra = set(item_ids) - set(current)
        detail = []
        if missing:
            detail.append("缺少：" + "、".join(sorted(missing)))
        if extra:
            detail.append("不属于项目：" + "、".join(sorted(extra)))
        raise HTTPException(422, "装配清单必须完整覆盖当前项目镜头；" + "；".join(detail))
    for item in items:
        if not current[item["shot_id"]]["dialogue"]:
            item["subtitle_enabled"] = False
            item["subtitle_start_seconds"] = None
        start = item.get("subtitle_start_seconds")
        if start is not None and float(start) >= float(current[item["shot_id"]]["seconds"]):
            raise HTTPException(422, f"镜头“{current[item['shot_id']]['title']}”字幕起点必须早于镜头时长")


def _snapshot(plan: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": plan["id"], "project_id": plan["project_id"], "status": plan["status"],
        "revision": plan["revision"], "plan_hash": plan["plan_hash"], "items": items,
    }


def _record_version(db: sqlite3.Connection, plan: dict[str, Any], items: list[dict[str, Any]], source: str) -> None:
    snapshot = _snapshot(plan, items)
    db.execute(
        """INSERT INTO delivery_plan_versions
        (id, plan_id, revision, status, plan_hash, source, snapshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"delivery-version-{uuid.uuid4().hex[:12]}", plan["id"], plan["revision"], plan["status"],
            plan["plan_hash"], source, json.dumps(snapshot, ensure_ascii=False), utc_now(),
        ),
    )


def delivery_workspace(db_path: Path) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        row = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        if row:
            plan = dict(row)
            items = _plan_items(db, plan["id"])
            versions = [dict(version) for version in db.execute(
                "SELECT id, revision, status, plan_hash, source, created_at FROM delivery_plan_versions WHERE plan_id = ? ORDER BY revision DESC",
                (plan["id"],),
            ).fetchall()]
        else:
            items = _default_items(db, project["id"])
            plan = {
                "id": None, "project_id": project["id"], "status": "draft", "revision": 0,
                "plan_hash": _hash_items(items), "created_at": None, "updated_at": None, "locked_at": None,
            }
            versions = []
        return {
            "project": {"id": project["id"], "title": project["title"], "episode": project["episode"]},
            "plan": {**plan, "items": items},
            "versions": versions,
            "summary": {
                "item_count": len(items),
                "subtitle_count": sum(bool(item["subtitle_enabled"] and item.get("dialogue")) for item in items),
                "planned_seconds": round(sum(float(item.get("seconds") or 0) for item in items), 3),
            },
        }


def save_delivery_plan(db_path: Path, payload: DeliveryPlanPatch) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        current = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        expected = current["revision"] if current else 0
        if payload.base_revision != expected:
            raise HTTPException(409, f"装配计划已更新，当前修订为 {expected}")
        if current and current["status"] == "locked":
            raise HTTPException(409, "已锁定装配不能直接修改，请先创建新修订")
        raw_items = [item.model_dump() for item in payload.items]
        _validate_items(db, project["id"], raw_items)
        items = [{**item, "ordinal": index} for index, item in enumerate(raw_items, start=1)]
        plan_hash = _hash_items(items)
        now = utc_now()
        if current:
            plan_id = current["id"]
            revision = expected + 1
            db.execute(
                "UPDATE delivery_plans SET revision = ?, plan_hash = ?, updated_at = ? WHERE id = ?",
                (revision, plan_hash, now, plan_id),
            )
            db.execute("DELETE FROM delivery_plan_items WHERE plan_id = ?", (plan_id,))
        else:
            plan_id = f"delivery-plan-{uuid.uuid4().hex[:12]}"
            revision = 1
            db.execute(
                """INSERT INTO delivery_plans
                (id, project_id, status, revision, plan_hash, created_at, updated_at)
                VALUES (?, ?, 'draft', ?, ?, ?, ?)""",
                (plan_id, project["id"], revision, plan_hash, now, now),
            )
        for item in items:
            db.execute(
                """INSERT INTO delivery_plan_items
                (plan_id, shot_id, ordinal, subtitle_enabled, subtitle_start_seconds, transition)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, item["shot_id"], item["ordinal"], int(item["subtitle_enabled"]),
                    item["subtitle_start_seconds"], item["transition"],
                ),
            )
        plan = dict(db.execute("SELECT * FROM delivery_plans WHERE id = ?", (plan_id,)).fetchone())
        _record_version(db, plan, _plan_items(db, plan_id), "save")
        db.commit()
    return delivery_workspace(db_path)


def lock_delivery_plan(db_path: Path, base_revision: int) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        current = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        if not current:
            raise HTTPException(409, "请先保存装配草稿")
        if current["revision"] != base_revision:
            raise HTTPException(409, f"装配计划已更新，当前修订为 {current['revision']}")
        if current["status"] == "locked":
            return delivery_workspace(db_path)
        items = _plan_items(db, current["id"])
        _validate_items(db, project["id"], items)
        now = utc_now()
        revision = current["revision"] + 1
        db.execute(
            "UPDATE delivery_plans SET status = 'locked', revision = ?, updated_at = ?, locked_at = ? WHERE id = ?",
            (revision, now, now, current["id"]),
        )
        plan = dict(db.execute("SELECT * FROM delivery_plans WHERE id = ?", (current["id"],)).fetchone())
        _record_version(db, plan, items, "lock")
        db.commit()
    return delivery_workspace(db_path)


def reopen_delivery_plan(db_path: Path, base_revision: int) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        current = db.execute("SELECT * FROM delivery_plans WHERE project_id = ?", (project["id"],)).fetchone()
        if not current:
            raise HTTPException(404, "装配计划不存在")
        if current["revision"] != base_revision:
            raise HTTPException(409, f"装配计划已更新，当前修订为 {current['revision']}")
        if current["status"] != "locked":
            raise HTTPException(409, "装配计划已经是草稿")
        now = utc_now()
        revision = current["revision"] + 1
        db.execute(
            "UPDATE delivery_plans SET status = 'draft', revision = ?, updated_at = ?, locked_at = NULL WHERE id = ?",
            (revision, now, current["id"]),
        )
        plan = dict(db.execute("SELECT * FROM delivery_plans WHERE id = ?", (current["id"],)).fetchone())
        _record_version(db, plan, _plan_items(db, current["id"]), "reopen")
        db.commit()
    return delivery_workspace(db_path)


def locked_delivery_plan(db_path: Path, project_id: str) -> dict[str, Any] | None:
    with closing(connect(db_path)) as db:
        row = db.execute(
            "SELECT * FROM delivery_plans WHERE project_id = ? AND status = 'locked'",
            (project_id,),
        ).fetchone()
        if not row:
            return None
        plan = dict(row)
        return {**plan, "items": _plan_items(db, plan["id"])}


def create_delivery_router(db_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/delivery-plan")
    def api_delivery_workspace() -> dict[str, Any]:
        return delivery_workspace(db_path)

    @router.put("/api/delivery-plan")
    def api_save_delivery_plan(payload: DeliveryPlanPatch) -> dict[str, Any]:
        return save_delivery_plan(db_path, payload)

    @router.post("/api/delivery-plan/lock")
    def api_lock_delivery_plan(payload: DeliveryPlanRevision) -> dict[str, Any]:
        return lock_delivery_plan(db_path, payload.base_revision)

    @router.post("/api/delivery-plan/reopen")
    def api_reopen_delivery_plan(payload: DeliveryPlanRevision) -> dict[str, Any]:
        return reopen_delivery_plan(db_path, payload.base_revision)

    return router
