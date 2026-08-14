from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend.delivery_plan import (
    DeliveryPlanItemInput,
    DeliveryPlanPatch,
    delivery_workspace,
    init_delivery_schema,
    lock_delivery_plan,
    locked_delivery_plan,
    reopen_delivery_plan,
    save_delivery_plan,
)


class DeliveryPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.db"
        self.video1 = Path(self.temp_dir.name) / "c1.mp4"
        self.video2 = Path(self.temp_dir.name) / "c2.mp4"
        self.video1.write_bytes(b"candidate-one")
        self.video2.write_bytes(b"candidate-two")
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
                  scene_code TEXT NOT NULL, title TEXT NOT NULL, dialogue TEXT NOT NULL, seconds REAL NOT NULL,
                  status TEXT NOT NULL, subtitle_enabled INTEGER NOT NULL DEFAULT 1, subtitle_start_seconds REAL
                );
                CREATE TABLE candidates (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
                  archived INTEGER NOT NULL DEFAULT 0, external_id TEXT, prompt_id TEXT,
                  status TEXT NOT NULL DEFAULT 'completed', output_file TEXT
                );
                CREATE TABLE candidate_master_versions (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, shot_id TEXT NOT NULL,
                  candidate_id TEXT NOT NULL, revision INTEGER NOT NULL, review_id TEXT NOT NULL,
                  review_revision INTEGER NOT NULL, candidate_snapshot TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE creative_storyboard_links (
                  shot_id TEXT PRIMARY KEY, section_id TEXT NOT NULL, last_synced_revision INTEGER NOT NULL
                );
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', 'logline', 60, 'now')")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute("INSERT INTO shots VALUES ('s1', 'p1', 1, 'S01', '开场', '', 5.0, '草稿已选', 0, NULL)")
            db.execute("INSERT INTO shots VALUES ('s2', 'p1', 2, 'S02', '反转', '别出来', 6.0, '草稿已选', 1, 2.0)")
            db.execute("INSERT INTO candidates VALUES ('c1', 's1', 1, 0, 'draft-1', 'prompt-1', 'completed', ?)", (str(self.video1),))
            db.execute("INSERT INTO candidates VALUES ('c2', 's2', 1, 0, 'draft-2', 'prompt-2', 'completed', ?)", (str(self.video2),))
            snapshot1 = json.dumps({"output_file": str(self.video1.resolve()), "size_bytes": self.video1.stat().st_size, "checksum_sha256": hashlib.sha256(self.video1.read_bytes()).hexdigest()})
            snapshot2 = json.dumps({"output_file": str(self.video2.resolve()), "size_bytes": self.video2.stat().st_size, "checksum_sha256": hashlib.sha256(self.video2.read_bytes()).hexdigest()})
            db.execute("INSERT INTO candidate_master_versions VALUES ('m1', 'p1', 's1', 'c1', 1, 'r1', 1, ?, 'now')", (snapshot1,))
            db.execute("INSERT INTO candidate_master_versions VALUES ('m2', 'p1', 's2', 'c2', 1, 'r2', 1, ?, 'now')", (snapshot2,))
            db.execute("INSERT INTO creative_storyboard_links VALUES ('s1', 'section-1', 3)")
            db.execute("INSERT INTO creative_storyboard_links VALUES ('s2', 'section-2', 4)")
            init_delivery_schema(db)
            db.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def items(order: tuple[str, str] = ("s2", "s1")) -> list[DeliveryPlanItemInput]:
        return [
            DeliveryPlanItemInput(
                shot_id=shot_id,
                subtitle_enabled=shot_id == "s2",
                subtitle_start_seconds=1.5 if shot_id == "s2" else None,
            )
            for shot_id in order
        ]

    def test_virtual_draft_can_be_reordered_saved_and_locked(self) -> None:
        virtual = delivery_workspace(self.db_path)
        self.assertEqual(virtual["plan"]["revision"], 0)
        self.assertEqual([item["shot_id"] for item in virtual["plan"]["items"]], ["s1", "s2"])

        saved = save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=self.items()))
        self.assertEqual(saved["plan"]["revision"], 1)
        self.assertEqual([item["shot_id"] for item in saved["plan"]["items"]], ["s2", "s1"])
        self.assertFalse(saved["plan"]["items"][1]["subtitle_enabled"])
        self.assertIsNone(saved["plan"]["items"][1]["subtitle_start_seconds"])
        locked = lock_delivery_plan(self.db_path, 1)
        self.assertEqual(locked["plan"]["status"], "locked")
        self.assertEqual(locked["plan"]["revision"], 2)
        self.assertEqual(len(locked["versions"]), 2)
        self.assertEqual(locked_delivery_plan(self.db_path, "p1")["plan_hash"], locked["plan"]["plan_hash"])

    def test_locked_plan_requires_explicit_new_revision(self) -> None:
        save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=self.items()))
        lock_delivery_plan(self.db_path, 1)
        with self.assertRaises(HTTPException) as locked:
            save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=2, items=self.items(("s1", "s2"))))
        self.assertEqual(locked.exception.status_code, 409)

        reopened = reopen_delivery_plan(self.db_path, 2)
        self.assertEqual(reopened["plan"]["status"], "draft")
        self.assertEqual(reopened["plan"]["revision"], 3)
        changed = save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=3, items=self.items(("s1", "s2"))))
        self.assertEqual(changed["plan"]["revision"], 4)

    def test_stale_revision_and_incomplete_items_fail_closed(self) -> None:
        saved = save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=self.items()))
        with self.assertRaises(HTTPException) as stale:
            save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=self.items()))
        self.assertEqual(stale.exception.status_code, 409)
        with self.assertRaises(HTTPException) as incomplete:
            save_delivery_plan(
                self.db_path,
                DeliveryPlanPatch(base_revision=saved["plan"]["revision"], items=[self.items()[0]]),
            )
        self.assertEqual(incomplete.exception.status_code, 422)

    def test_subtitle_start_must_be_inside_its_shot(self) -> None:
        invalid = self.items()
        invalid[0] = DeliveryPlanItemInput(shot_id="s2", subtitle_enabled=True, subtitle_start_seconds=6.0)
        with self.assertRaises(HTTPException) as outside:
            save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=invalid))
        self.assertEqual(outside.exception.status_code, 422)

    def test_locked_version_freezes_trim_dialogue_and_source_chain(self) -> None:
        items = self.items()
        items[0] = DeliveryPlanItemInput(
            shot_id="s2", subtitle_enabled=True, subtitle_start_seconds=1.0,
            in_point_seconds=0.5, out_point_seconds=5.5, dialogue_mode="mute",
        )
        saved = save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=items))
        source = saved["plan"]["items"][0]
        self.assertEqual(source["section_id"], "section-2")
        self.assertEqual(source["candidate_id"], "c2")
        self.assertEqual(source["master_version_id"], "m2")
        self.assertEqual((source["in_point_seconds"], source["out_point_seconds"]), (0.5, 5.5))
        self.assertEqual(source["dialogue_mode"], "mute")
        locked = lock_delivery_plan(self.db_path, saved["plan"]["revision"])
        with closing(sqlite3.connect(self.db_path)) as db:
            snapshot = db.execute(
                "SELECT snapshot FROM delivery_plan_versions WHERE plan_id = ? AND revision = ?",
                (locked["plan"]["id"], locked["plan"]["revision"]),
            ).fetchone()[0]
        frozen = __import__("json").loads(snapshot)["items"][0]
        self.assertEqual(frozen["source_snapshot"]["candidate_id"], "c2")
        self.assertEqual(frozen["source_snapshot"]["section_id"], "section-2")

    def test_master_change_after_save_blocks_lock_without_partial_write(self) -> None:
        saved = save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=self.items()))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE candidates SET selected = 0 WHERE shot_id = 's2'")
            db.execute("INSERT INTO candidates VALUES ('c2b', 's2', 1, 0, 'draft-2b', 'prompt-2b', 'completed', ?)", (str(self.video2),))
            db.execute("INSERT INTO candidate_master_versions VALUES ('m2b', 'p1', 's2', 'c2b', 2, 'r2b', 1, '{}', 'later')")
            db.commit()
        with self.assertRaises(HTTPException) as changed:
            lock_delivery_plan(self.db_path, saved["plan"]["revision"])
        self.assertEqual(changed.exception.status_code, 409)
        current = delivery_workspace(self.db_path)
        self.assertEqual(current["plan"]["status"], "draft")
        self.assertEqual(current["plan"]["revision"], 1)

    def test_media_replacement_after_save_blocks_lock_without_partial_write(self) -> None:
        saved = save_delivery_plan(self.db_path, DeliveryPlanPatch(base_revision=0, items=self.items()))
        self.video2.write_bytes(b"candidate-two-replaced")
        with self.assertRaises(HTTPException) as changed:
            lock_delivery_plan(self.db_path, saved["plan"]["revision"])
        self.assertEqual(changed.exception.status_code, 409)
        with closing(sqlite3.connect(self.db_path)) as db:
            plan = db.execute("SELECT status, revision FROM delivery_plans").fetchone()
            versions = db.execute("SELECT COUNT(*) FROM delivery_plan_versions").fetchone()[0]
        self.assertEqual(plan, ("draft", 1))
        self.assertEqual(versions, 1)


if __name__ == "__main__":
    unittest.main()
