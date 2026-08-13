from __future__ import annotations

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
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', 'logline', 60, 'now')")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute("INSERT INTO shots VALUES ('s1', 'p1', 1, 'S01', '开场', '', 5.0, '草稿已选', 0, NULL)")
            db.execute("INSERT INTO shots VALUES ('s2', 'p1', 2, 'S02', '反转', '别出来', 6.0, '草稿已选', 1, 2.0)")
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


if __name__ == "__main__":
    unittest.main()
