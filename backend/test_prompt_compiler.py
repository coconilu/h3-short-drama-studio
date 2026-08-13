from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend.production_bible import init_bible_schema
from backend.prompt_compiler import (
    approve_plan,
    compile_prompt_plan,
    init_prompt_schema,
    record_validation,
)


class PromptCompilerTests(unittest.TestCase):
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
                  scene_code TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, dialogue TEXT NOT NULL,
                  prompt TEXT NOT NULL, status TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                  seconds REAL NOT NULL, candidate_count INTEGER NOT NULL, strategy TEXT NOT NULL,
                  thumbnail TEXT NOT NULL, video TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE assets (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
                  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL DEFAULT '',
                  locked INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, managed_path TEXT,
                  media_type TEXT NOT NULL DEFAULT 'image', duration_seconds REAL, has_audio INTEGER NOT NULL DEFAULT 0,
                  checksum_sha256 TEXT, archived INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE shot_references (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
                  asset_id TEXT NOT NULL REFERENCES assets(id), reference_type TEXT NOT NULL,
                  ordinal INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'generic',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(shot_id, asset_id)
                );
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', 'logline', 60, 'now')")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute(
                """INSERT INTO shots VALUES
                ('p1-S01-001', 'p1', 1, 'S01', '开场', '女孩回头', '谁在那里？',
                 'A woman turns slowly while the camera pushes in.', '未生成', 608, 352, 5.17, 2,
                 'Ref2VA 精修', '', NULL, 'v1')"""
            )
            db.execute(
                """INSERT INTO assets VALUES
                ('image-1', 'p1', '角色参考', '林夏身份图', '', '', 0, 'managed', 'identity.png',
                 'image', NULL, 0, 'sha-image', 0, '{}'),
                ('video-1', 'p1', '动作参考', '回头动作', '', '', 0, 'managed', 'action.mp4',
                 'video', 4.0, 1, 'sha-video', 0, '{}')"""
            )
            db.execute(
                """INSERT INTO shot_references VALUES
                ('shot-ref-video', 'p1-S01-001', 'video-1', 'video', 1, 'action', 'now', 'now')"""
            )
            init_bible_schema(db)
            init_prompt_schema(db)
            db.execute(
                """INSERT INTO production_bible_entries
                (id, project_id, entry_type, name, summary, canonical_description, prompt_fragment,
                 negative_prompt, continuity_rules, apply_globally, status, revision, archived, created_at, updated_at)
                VALUES
                ('char-1', 'p1', 'character', '林夏', '红雨衣女性', '二十多岁东亚女性，红色雨衣',
                 '<Picture 9> young East Asian woman in a wet red raincoat', 'no costume changes',
                 '脸型、发型和雨衣必须跨镜一致', 0, 'locked', 2, 0, 'now', 'now'),
                ('style-draft', 'p1', 'style', '未确认霓虹风格', '', '高饱和霓虹', 'neon', '', '',
                 1, 'draft', 1, 0, 'now', 'now')"""
            )
            db.execute("INSERT INTO production_bible_assets VALUES ('char-1', 'image-1', 'identity', 1, 'now')")
            db.execute("INSERT INTO production_bible_shots VALUES ('char-1', 'p1-S01-001', 'continuity', '', 'now')")
            db.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_compile_uses_locked_bible_and_dynamic_reference_order(self) -> None:
        plan = compile_prompt_plan(self.db_path, "p1-S01-001")

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["mode"], "REF2VA")
        self.assertEqual([item["tag"] for item in plan["references"]], ["<Picture 1>", "<Video 1>"])
        self.assertEqual(plan["references"][0]["source"], "bible")
        self.assertEqual(plan["references"][1]["source"], "shot")
        self.assertIn("<Audio 1>", plan["compiled_prompt"])
        self.assertNotIn("<Picture 9>", plan["compiled_prompt"])
        self.assertIn("谁在那里？", plan["compiled_prompt"])
        self.assertEqual(plan["spec"]["frames"], 124)
        self.assertEqual(plan["spec"]["actual_seconds"], 5.167)
        self.assertTrue(any("未锁定条目" in warning for warning in plan["warnings"]))
        self.assertTrue(any("手写引用标签" in warning for warning in plan["warnings"]))

    def test_validation_and_approval_are_bound_to_immutable_hash(self) -> None:
        original = compile_prompt_plan(self.db_path, "p1-S01-001")
        record_validation(self.db_path, original, {"dry_run": True})
        validated = compile_prompt_plan(self.db_path, "p1-S01-001")
        self.assertEqual(validated["status"], "validated")

        approve_plan(self.db_path, "p1-S01-001", original["plan_hash"])
        self.assertEqual(compile_prompt_plan(self.db_path, "p1-S01-001")["status"], "approved")

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE shots SET status = '可生成', updated_at = 'status-only' WHERE id = 'p1-S01-001'")
            db.commit()
        status_only = compile_prompt_plan(self.db_path, "p1-S01-001")
        self.assertEqual(status_only["plan_hash"], original["plan_hash"])
        self.assertEqual(status_only["status"], "approved")

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE shots SET prompt = 'The woman runs.', updated_at = 'v2' WHERE id = 'p1-S01-001'")
            db.commit()
        changed = compile_prompt_plan(self.db_path, "p1-S01-001")
        self.assertNotEqual(changed["plan_hash"], original["plan_hash"])
        self.assertEqual(changed["status"], "preview")

        with self.assertRaises(HTTPException) as stale:
            approve_plan(self.db_path, "p1-S01-001", changed["plan_hash"])
        self.assertEqual(stale.exception.status_code, 409)

    def test_invalid_video_duration_blocks_dry_run(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE assets SET duration_seconds = 1.0 WHERE id = 'video-1'")
            db.commit()
        plan = compile_prompt_plan(self.db_path, "p1-S01-001")
        self.assertFalse(plan["ready"])
        self.assertTrue(any("2–15 秒" in item for item in plan["blocking"]))

    def test_late_validation_never_revives_superseded_plan(self) -> None:
        original = compile_prompt_plan(self.db_path, "p1-S01-001")
        self.assertTrue(record_validation(self.db_path, original, {"attempt": "initial"}))
        approve_plan(self.db_path, "p1-S01-001", original["plan_hash"])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            from backend.prompt_compiler import mark_prompt_plans_stale
            mark_prompt_plans_stale(db, "p1", "镜头输入在 dry-run 期间变化", shot_ids=["p1-S01-001"])
            db.commit()

        self.assertFalse(record_validation(self.db_path, original, {"attempt": "late"}))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM h3_prompt_plans WHERE plan_hash = ?", (original["plan_hash"],)
            ).fetchone()
        self.assertEqual(row["status"], "superseded")
        self.assertIn("镜头输入在 dry-run 期间变化", row["stale_reasons"])
        self.assertNotIn("late", row["adapter_output"])

    def test_validation_that_returns_after_input_change_is_historical_only(self) -> None:
        original = compile_prompt_plan(self.db_path, "p1-S01-001")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE shots SET prompt = 'changed during dry-run' WHERE id = 'p1-S01-001'")
            db.commit()
        self.assertFalse(record_validation(self.db_path, original, {"attempt": "late"}))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM h3_prompt_plans WHERE plan_hash = ?", (original["plan_hash"],)
            ).fetchone()
        self.assertEqual(row["status"], "superseded")
        self.assertIn("输入已变化", row["stale_reasons"])
        self.assertEqual(compile_prompt_plan(self.db_path, "p1-S01-001")["status"], "preview")


if __name__ == "__main__":
    unittest.main()
