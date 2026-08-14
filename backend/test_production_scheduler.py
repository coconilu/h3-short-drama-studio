from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.production_scheduler import (
    batch_preflight,
    claim_next_item,
    create_batch,
    get_batch,
    init_production_schema,
    mutate_batch,
    poll_running_items,
    recover_batches,
    retry_item,
    submit_claimed_item,
)


class ProductionSchedulerTests(unittest.TestCase):
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
                  title TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE h3_prompt_plans (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL, project_id TEXT NOT NULL,
                  plan_hash TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, shot_id TEXT NOT NULL, kind TEXT NOT NULL,
                  state TEXT NOT NULL, h3_project TEXT, prompt_ids TEXT NOT NULL DEFAULT '[]',
                  candidate_ids TEXT NOT NULL DEFAULT '[]', source_snapshot TEXT NOT NULL DEFAULT '{}',
                  plan_hash TEXT, retry_safe INTEGER NOT NULL DEFAULT 0,
                  reconciliation_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE candidates (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL, external_id TEXT, prompt_id TEXT, seed INTEGER,
                  status TEXT NOT NULL, output_file TEXT, elapsed_seconds REAL, metadata TEXT NOT NULL DEFAULT '{}',
                  archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', 'logline', 60, 'now')")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute("INSERT INTO shots VALUES ('s1', 'p1', 1, '开场', '可生成')")
            db.execute("INSERT INTO shots VALUES ('s2', 'p1', 2, '反转', '可生成')")
            db.execute("INSERT INTO h3_prompt_plans VALUES ('plan-s1', 's1', 'p1', ?, 'approved')", ("a" * 64,))
            db.execute("INSERT INTO h3_prompt_plans VALUES ('plan-s2', 's2', 'p1', ?, 'approved')", ("b" * 64,))
            db.execute(
                "INSERT INTO jobs (shot_id, kind, state, source_snapshot, plan_hash) VALUES ('s1', 'validation', '校验通过', ?, ?)",
                (json.dumps({"arguments": ["draft", "--prompt", "prompt s1", "--count", "2", "--width", "608", "--height", "352", "--seconds", "5.0", "--steps", "20", "--mode", "fl2va"]}), "v" * 64),
            )
            db.execute(
                "INSERT INTO jobs (shot_id, kind, state, source_snapshot, plan_hash) VALUES ('s2', 'validation', '校验通过', ?, ?)",
                (json.dumps({"arguments": ["draft", "--prompt", "prompt s2", "--count", "2", "--width", "608", "--height", "352", "--seconds", "5.0", "--steps", "20", "--mode", "fl2va"]}), "w" * 64),
            )
            init_production_schema(db)
            db.commit()
        self.hashes = {"s1": "a" * 64, "s2": "b" * 64}
        self.candidate_counts = {"s1": 2, "s2": 2}
        self.resolutions = {"s1": "608×352", "s2": "608×352"}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def plan(self, shot_id: str) -> dict:
        return {
            "shot": {"id": shot_id},
            "plan_hash": self.hashes[shot_id],
            "status": "approved",
            "ready": True,
            "mode": "FL2VA",
            "compiled_prompt": f"prompt {shot_id}",
            "spec": {
                "candidate_count": self.candidate_counts[shot_id],
                "resolution": self.resolutions[shot_id],
                "adapter_seconds": 5.0,
                "steps": 20,
            },
        }

    def make_batch(self) -> dict:
        preflight = batch_preflight(self.db_path, self.plan, ["s2", "s1"])
        return create_batch(
            self.db_path, self.plan, ["s2", "s1"], name="EP01 夜间生产", max_attempts=3,
            preflight_hash=preflight["preflight_hash"], idempotency_key="test-batch-0001",
        )

    def test_items_run_in_shot_order_and_only_one_is_active(self) -> None:
        batch = self.make_batch()
        self.assertEqual([item["shot_id"] for item in batch["items"]], ["s1", "s2"])
        first = claim_next_item(self.db_path)
        self.assertEqual(first["shot_id"], "s1")

        submit_claimed_item(
            self.db_path,
            first,
            self.plan,
            lambda item: {
                "state": "排队中", "message": "等待 ComfyUI", "h3_project": f"h3-{item['shot_id']}",
                "prompt_ids": [f"prompt-{item['shot_id']}"],
            },
        )
        self.assertIsNone(claim_next_item(self.db_path), "前一镜头未完成时不得提交下一镜头")

        poll_running_items(
            self.db_path,
            lambda item: {"state": "完成", "message": "2/2 条候选已完成", "prompt_ids": [f"prompt-{item['shot_id']}"]},
        )
        second = claim_next_item(self.db_path)
        self.assertEqual(second["shot_id"], "s2")

        submit_claimed_item(
            self.db_path,
            second,
            self.plan,
            lambda item: {
                "state": "完成", "message": "已有候选已完成", "h3_project": f"h3-{item['shot_id']}",
                "prompt_ids": [f"prompt-{item['shot_id']}"],
            },
        )
        finished = get_batch(self.db_path, batch["id"])
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(finished["completed_count"], 2)
        self.assertEqual(finished["submitted_count"], 2)
        self.assertEqual(finished["items"][0]["attempt_history"][0]["state"], "completed")
        self.assertEqual(finished["items"][0]["attempt_history"][0]["plan_hash"], "a" * 64)

    def test_pause_resume_and_cancel_never_cancel_a_running_gpu_job(self) -> None:
        batch = self.make_batch()
        paused = mutate_batch(self.db_path, batch["id"], "pause")
        self.assertEqual(paused["state"], "paused")
        self.assertIsNone(claim_next_item(self.db_path))
        mutate_batch(self.db_path, batch["id"], "resume")
        first = claim_next_item(self.db_path)
        submit_claimed_item(
            self.db_path, first, self.plan,
            lambda _: {"state": "运行中", "message": "ComfyUI 正在生成", "h3_project": "h3-s1", "prompt_ids": ["p1"]},
        )

        cancelling = mutate_batch(self.db_path, batch["id"], "cancel")
        states = {item["shot_id"]: item["state"] for item in cancelling["items"]}
        self.assertEqual(cancelling["state"], "cancelling")
        self.assertEqual(states, {"s1": "running", "s2": "cancelled"})
        poll_running_items(self.db_path, lambda _: {"state": "完成", "message": "完成", "prompt_ids": ["p1"]})
        stopped = get_batch(self.db_path, batch["id"])
        self.assertEqual(stopped["state"], "cancelled")
        self.assertEqual(stopped["completed_count"], 1)
        self.assertEqual(stopped["cancelled_count"], 1)

    def test_restart_marks_uncertain_submission_failed_instead_of_resubmitting(self) -> None:
        batch = self.make_batch()
        item = claim_next_item(self.db_path)
        self.assertEqual(item["state"], "submitting")
        recovered = recover_batches(self.db_path)
        self.assertEqual(recovered, 1)
        current = get_batch(self.db_path, batch["id"])
        self.assertEqual(current["items"][0]["state"], "failed")
        self.assertEqual(current["items"][0]["error"], "submission_outcome_unknown")
        self.assertEqual(current["state"], "paused")

    def test_changed_plan_fails_closed_and_retry_requires_same_snapshot(self) -> None:
        batch = self.make_batch()
        item = claim_next_item(self.db_path)
        submitted: list[str] = []
        self.hashes["s1"] = "c" * 64
        submit_claimed_item(
            self.db_path, item, self.plan,
            lambda shot_id: submitted.append(shot_id) or {"state": "排队中"},
        )
        current = get_batch(self.db_path, batch["id"])
        self.assertEqual(submitted, [])
        self.assertEqual(current["items"][0]["state"], "failed")
        with self.assertRaises(Exception):
            retry_item(self.db_path, current["items"][0]["id"], self.plan)

    def test_batch_requires_two_low_resolution_candidates_per_shot(self) -> None:
        self.candidate_counts["s1"] = 1
        with self.assertRaises(Exception):
            self.make_batch()

    def test_preflight_is_side_effect_free_and_create_is_idempotent(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            before = (
                db.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM production_shot_leases").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            )
        preflight = batch_preflight(self.db_path, self.plan, ["s1"])
        self.assertTrue(preflight["ok"])
        self.assertFalse(preflight["gpu_submitted"])
        with closing(sqlite3.connect(self.db_path)) as db:
            after = (
                db.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM production_shot_leases").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            )
        self.assertEqual(before, after)
        first = create_batch(
            self.db_path, self.plan, ["s1"], name="idem", max_attempts=3,
            preflight_hash=preflight["preflight_hash"], idempotency_key="idem-request-001",
        )
        replay = create_batch(
            self.db_path, self.plan, ["s1"], name="idem", max_attempts=3,
            preflight_hash=preflight["preflight_hash"], idempotency_key="idem-request-001",
        )
        self.assertEqual(first["id"], replay["id"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM production_batches").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM production_shot_leases").fetchone()[0], 1)

    def test_same_shot_cannot_be_active_in_two_batches(self) -> None:
        self.make_batch()
        blocked = batch_preflight(self.db_path, self.plan, ["s1"])
        self.assertFalse(blocked["ok"])
        self.assertIn("另一个活动生产批次", " ".join(blocked["results"][0]["reasons"]))

    def test_unknown_submission_cannot_retry_or_submit_again(self) -> None:
        batch = self.make_batch()
        item = claim_next_item(self.db_path)
        self.assertEqual(recover_batches(self.db_path), 1)
        failed = get_batch(self.db_path, batch["id"])["items"][0]
        with self.assertRaises(Exception):
            retry_item(self.db_path, failed["id"], self.plan)
        submitted: list[str] = []
        self.assertIsNone(claim_next_item(self.db_path))
        self.assertEqual(submitted, [])
        with closing(sqlite3.connect(self.db_path)) as db:
            lease = db.execute("SELECT state FROM production_shot_leases WHERE item_id = ?", (item["id"],)).fetchone()
            self.assertEqual(lease[0], "unknown")
        self.candidate_counts["s1"] = 2
        self.resolutions["s1"] = "1344×768"
        with self.assertRaises(Exception):
            self.make_batch()

    def test_attempt_persists_prompt_candidate_and_real_media_evidence(self) -> None:
        video = Path(self.temp_dir.name) / "candidate.mp4"
        video.write_bytes(b"controlled-fake-media")
        batch = self.make_batch()
        item = claim_next_item(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as db:
            cursor = db.execute(
                """INSERT INTO jobs
                (shot_id, kind, state, h3_project, prompt_ids, candidate_ids, source_snapshot, plan_hash)
                VALUES (?, 'draft', '完成', ?, ?, ?, ?, ?)""",
                ("s1", "h3-s1", '["prompt-s1"]', '["draft-1"]', '{"prompt":"frozen prompt","seed":42}', "v" * 64),
            )
            draft_job_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, 'completed', ?, 12.5, ?, 0, 'now')",
                ("s1-draft-1", "s1", "draft-1", "prompt-s1", 42, str(video), '{"width":608,"height":352}'),
            )
            db.commit()
        submit_claimed_item(
            self.db_path,
            item,
            self.plan,
            lambda _: {
                "state": "完成", "message": "完成", "h3_project": "h3-s1",
                "prompt_ids": ["prompt-s1"], "candidate_ids": ["draft-1"], "job_id": draft_job_id,
            },
        )
        attempt = get_batch(self.db_path, batch["id"])["items"][0]["attempt_history"][0]
        self.assertEqual(attempt["prompt_ids"], ["prompt-s1"])
        self.assertEqual(attempt["candidate_ids"], ["draft-1"])
        self.assertEqual(attempt["source_snapshot"]["seed"], 42)
        self.assertTrue(attempt["media_evidence"][0]["file_exists"])
        self.assertEqual(attempt["media_evidence"][0]["size_bytes"], len(b"controlled-fake-media"))


if __name__ == "__main__":
    unittest.main()
