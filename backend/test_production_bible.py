from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend.production_bible import (
    AssetLinkRequest,
    BibleEntryCreate,
    BibleEntryPatch,
    RevisionRequest,
    ShotLinkRequest,
    create_bible_router,
    init_bible_schema,
    sync_creative_character_rules,
)


class ProductionBibleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.db"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE projects (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, episode TEXT NOT NULL,
                  logline TEXT NOT NULL, target_duration REAL NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE shots (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
                  scene_code TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE assets (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
                  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL DEFAULT '',
                  locked INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, managed_path TEXT,
                  media_type TEXT NOT NULL DEFAULT 'image', archived INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', 'logline', 60, 'now')")
            db.execute("INSERT INTO projects VALUES ('p2', '别的剧', 'EP01', 'logline', 60, 'now')")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute("INSERT INTO shots VALUES ('p1-S01-001', 'p1', 1, 'S01', '开场', '未生成')")
            db.execute("INSERT INTO shots VALUES ('p1-S01-002', 'p1', 2, 'S01', '反转', '未生成')")
            db.execute("INSERT INTO shots VALUES ('p2-S01-001', 'p2', 1, 'S01', '越界', '未生成')")
            db.execute("INSERT INTO assets VALUES ('a1', 'p1', '角色参考', '真实角色图', '', '', 0, 'managed', 'x.png', 'image', 0)")
            db.execute("INSERT INTO assets VALUES ('demo', 'p1', '演示', '演示图', '', '', 0, 'demo', NULL, 'image', 0)")
            init_bible_schema(db)
            db.commit()
        router = create_bible_router(self.db_path)
        self.endpoints = {
            (route.path, next(iter(route.methods))): route.endpoint
            for route in router.routes
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def endpoint(self, path: str, method: str):
        return self.endpoints[(path, method)]

    def create_character(self) -> dict:
        workspace = self.endpoint("/api/bible/entries", "POST")(
            BibleEntryCreate(entry_type="character", name="林夏")
        )
        return workspace["entries"][0]

    def test_lock_requires_compilable_fields_and_tracks_coverage(self) -> None:
        entry = self.create_character()
        with self.assertRaises(HTTPException) as rejected:
            self.endpoint("/api/bible/entries/{entry_id}/lock", "POST")(
                entry["id"], RevisionRequest(base_revision=entry["revision"])
            )
        self.assertEqual(rejected.exception.status_code, 422)

        saved = self.endpoint("/api/bible/entries/{entry_id}", "PATCH")(
            entry["id"],
            BibleEntryPatch(
                base_revision=entry["revision"],
                summary="红色雨衣的年轻女性",
                canonical_description="二十多岁东亚女性，红色雨衣",
                prompt_fragment="young East Asian woman in a red raincoat",
                continuity_rules="雨衣始终保持湿润",
            )
        )["entries"][0]
        linked_asset = self.endpoint("/api/bible/entries/{entry_id}/assets/link", "POST")(
            entry["id"], AssetLinkRequest(base_revision=saved["revision"], asset_id="a1", role="identity")
        )["entries"][0]
        linked_shot_workspace = self.endpoint("/api/bible/entries/{entry_id}/shots/link", "POST")(
            entry["id"],
            ShotLinkRequest(base_revision=linked_asset["revision"], shot_id="p1-S01-001", role="continuity", note="正面"),
        )
        linked_shot = linked_shot_workspace["entries"][0]
        locked_workspace = self.endpoint("/api/bible/entries/{entry_id}/lock", "POST")(
            entry["id"], RevisionRequest(base_revision=linked_shot["revision"])
        )

        locked = locked_workspace["entries"][0]
        first_shot = locked_workspace["shots"][0]
        second_shot = locked_workspace["shots"][1]
        self.assertEqual(locked["status"], "locked")
        self.assertEqual(locked["assets"][0]["id"], "a1")
        self.assertEqual(first_shot["attention"], "连续性输入已锁定")
        self.assertEqual(second_shot["attention"], "未绑定连续性条目")
        self.assertEqual(locked_workspace["summary"]["covered_shot_count"], 1)

    def test_stale_revision_is_rejected(self) -> None:
        entry = self.create_character()
        self.endpoint("/api/bible/entries/{entry_id}", "PATCH")(
            entry["id"], BibleEntryPatch(base_revision=1, summary="版本二")
        )
        with self.assertRaises(HTTPException) as stale:
            self.endpoint("/api/bible/entries/{entry_id}", "PATCH")(
                entry["id"], BibleEntryPatch(base_revision=1, summary="过期页面写入")
            )
        self.assertEqual(stale.exception.status_code, 409)

    def test_demo_asset_and_cross_project_shot_cannot_be_linked(self) -> None:
        entry = self.create_character()
        with self.assertRaises(HTTPException) as demo:
            self.endpoint("/api/bible/entries/{entry_id}/assets/link", "POST")(
                entry["id"], AssetLinkRequest(base_revision=1, asset_id="demo", role="identity")
            )
        with self.assertRaises(HTTPException) as foreign_shot:
            self.endpoint("/api/bible/entries/{entry_id}/shots/link", "POST")(
                entry["id"],
                ShotLinkRequest(base_revision=1, shot_id="p2-S01-001", role="continuity", note=""),
            )
        self.assertEqual(demo.exception.status_code, 400)
        self.assertEqual(foreign_shot.exception.status_code, 404)

    def test_every_mutation_creates_an_immutable_revision(self) -> None:
        entry = self.create_character()
        workspace = self.endpoint("/api/bible/entries/{entry_id}", "PATCH")(
            entry["id"], BibleEntryPatch(base_revision=1, summary="新摘要")
        )
        updated = workspace["entries"][0]
        versions = self.endpoint("/api/bible/entries/{entry_id}/versions", "GET")(entry["id"])
        self.assertEqual([item["revision"] for item in versions], [2, 1])
        self.assertEqual(versions[0]["snapshot"]["summary"], "新摘要")
        self.assertEqual(versions[1]["snapshot"]["summary"], "")

        archived = self.endpoint("/api/bible/entries/{entry_id}/archive", "POST")(
            entry["id"], RevisionRequest(base_revision=updated["revision"])
        )
        self.assertEqual(archived["summary"]["entry_count"], 0)

    def test_creative_character_entries_reject_every_manual_mutation_and_repair_tampering(self) -> None:
        character = {
            "id": "character-derived", "project_id": "p1", "name": "林夏", "identity": "调查员",
            "appearance": "短发、红雨衣", "voice": "低声线、普通话", "status": "approved", "revision": 3,
        }
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            sync_creative_character_rules(db, character)
            db.commit()
        entry_id = "creative-character-derived-character"
        requests = (
            ("/api/bible/entries/{entry_id}", "PATCH", BibleEntryPatch(base_revision=1, summary="污染")),
            ("/api/bible/entries/{entry_id}/lock", "POST", RevisionRequest(base_revision=1)),
            ("/api/bible/entries/{entry_id}/archive", "POST", RevisionRequest(base_revision=1)),
            ("/api/bible/entries/{entry_id}/assets/link", "POST", AssetLinkRequest(base_revision=1, asset_id="a1")),
            ("/api/bible/entries/{entry_id}/assets/unlink", "POST", AssetLinkRequest(base_revision=1, asset_id="a1")),
            ("/api/bible/entries/{entry_id}/shots/link", "POST", ShotLinkRequest(base_revision=1, shot_id="p1-S01-001")),
            ("/api/bible/entries/{entry_id}/shots/unlink", "POST", ShotLinkRequest(base_revision=1, shot_id="p1-S01-001")),
        )
        for path, method, payload in requests:
            with self.subTest(path=path):
                with self.assertRaises(HTTPException) as blocked:
                    self.endpoint(path, method)(entry_id, payload)
                self.assertEqual(blocked.exception.status_code, 409)

        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute(
                "UPDATE production_bible_entries SET status = 'draft', prompt_fragment = '污染' WHERE id = ?",
                (entry_id,),
            )
            db.execute(
                "INSERT INTO production_bible_shots VALUES (?, 'p1-S01-001', 'continuity', '', 'now')", (entry_id,)
            )
            sync_creative_character_rules(db, character)
            db.commit()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            repaired = db.execute("SELECT * FROM production_bible_entries WHERE id = ?", (entry_id,)).fetchone()
            links = db.execute("SELECT COUNT(*) FROM production_bible_shots WHERE entry_id = ?", (entry_id,)).fetchone()[0]
        self.assertEqual(repaired["status"], "locked")
        self.assertEqual(repaired["prompt_fragment"], "短发、红雨衣")
        self.assertEqual(links, 0)

        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            sync_creative_character_rules(db, character)  # restart-time resync is a no-op but remains usable.
            db.commit()
        workspace = self.endpoint("/api/bible", "GET")()
        derived = next(item for item in workspace["entries"] if item["id"] == entry_id)
        self.assertEqual(derived["status"], "locked")
        self.assertTrue(derived["apply_globally"])


if __name__ == "__main__":
    unittest.main()
