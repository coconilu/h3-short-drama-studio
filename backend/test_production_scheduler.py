from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as studio

from backend.production_scheduler import (
    batch_preflight,
    claim_next_item,
    create_batch,
    create_production_router,
    get_batch,
    init_production_schema,
    mutate_batch,
    poll_running_items,
    recover_batches,
    resolve_ownership_conflicts,
    retry_item,
    submit_claimed_item,
    list_ownership_conflicts,
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
                  state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', h3_project TEXT, prompt_ids TEXT NOT NULL DEFAULT '[]',
                  candidate_ids TEXT NOT NULL DEFAULT '[]', source_snapshot TEXT NOT NULL DEFAULT '{}',
                  plan_hash TEXT, retry_safe INTEGER NOT NULL DEFAULT 0,
                  reconciliation_snapshot TEXT NOT NULL DEFAULT '{}',
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

    def make_ownership_conflict(self, *, uncertain: bool = False) -> tuple[str, list[str]]:
        batch = self.make_batch()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            original = db.execute(
                "SELECT * FROM production_batch_items WHERE batch_id = ? AND shot_id = 's1'", (batch["id"],),
            ).fetchone()
            db.execute("DELETE FROM production_shot_leases WHERE shot_id = 's1'")
            first_state = "running" if uncertain else "queued"
            attempts = 1 if uncertain else 0
            db.execute(
                "UPDATE production_batch_items SET state = ?, attempts = ? WHERE id = ?",
                (first_state, attempts, original["id"]),
            )
            db.execute(
                """INSERT INTO production_batches
                (id, project_id, name, state, item_count, config, message, created_at, updated_at)
                VALUES ('conflict-batch', 'p1', '旧批次', 'running', 1, '{}', 'legacy', 'later', 'later')""",
            )
            second_state = "submitting" if uncertain else "queued"
            db.execute(
                """INSERT INTO production_batch_items
                (id, batch_id, shot_id, ordinal, title, state, plan_hash, plan_snapshot,
                 attempts, max_attempts, message, created_at, updated_at)
                VALUES ('conflict-item', 'conflict-batch', 's1', 1, '旧尝试', ?, ?, ?, ?, 3, 'legacy', 'later', 'later')""",
                (second_state, original["plan_hash"], original["plan_snapshot"], attempts),
            )
            item_ids = [str(original["id"]), "conflict-item"]
            if uncertain:
                for index, item_id in enumerate(item_ids, 1):
                    item = db.execute("SELECT * FROM production_batch_items WHERE id = ?", (item_id,)).fetchone()
                    db.execute(
                        """INSERT INTO production_item_attempts
                        (id, item_id, batch_id, shot_id, attempt, state, plan_hash, plan_snapshot,
                         candidate_ids, media_evidence, created_at, updated_at)
                        VALUES (?, ?, ?, 's1', 1, ?, ?, ?, '[]', '[]', ?, ?)""",
                        (
                            f"conflict-attempt-{index}", item_id, item["batch_id"],
                            "running" if index == 1 else "submitting", item["plan_hash"], item["plan_snapshot"],
                            f"later-{index}", f"later-{index}",
                        ),
                    )
            db.commit()
            init_production_schema(db)
            db.commit()
        return batch["id"], item_ids

    def create_draft_job(self, shot_id: str, state: str = "运行中") -> dict:
        prompt_ids = [f"prompt-{shot_id}"]
        candidate_ids = [f"draft-{shot_id}-1", f"draft-{shot_id}-2"]
        with closing(sqlite3.connect(self.db_path)) as db:
            cursor = db.execute(
                """INSERT INTO jobs
                (shot_id, kind, state, message, h3_project, prompt_ids, candidate_ids,
                 source_snapshot, plan_hash, reconciliation_revision)
                VALUES (?, 'draft', ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    shot_id, state, state, f"h3-{shot_id}", json.dumps(prompt_ids), json.dumps(candidate_ids),
                    json.dumps({"prompt": f"prompt {shot_id}"}),
                    "v" * 64 if shot_id == "s1" else "w" * 64,
                ),
            )
            db.commit()
            job_id = int(cursor.lastrowid)
        return {
            "state": state, "message": state, "h3_project": f"h3-{shot_id}",
            "prompt_ids": prompt_ids, "candidate_ids": candidate_ids,
            "job_id": job_id, "job_revision": 0,
        }

    def complete_draft_job(self, item: dict) -> dict:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute(
                """UPDATE jobs SET state = '完成', message = '完成', reconciliation_revision = reconciliation_revision + 1
                WHERE id = ?""",
                (item["draft_job_id"],),
            )
            job = db.execute("SELECT * FROM jobs WHERE id = ?", (item["draft_job_id"],)).fetchone()
            db.commit()
        return {
            "state": job["state"], "message": job["message"], "job_id": int(job["id"]),
            "base_job_revision": int(item.get("draft_job_revision") or 0),
            "job_revision": int(job["reconciliation_revision"] or 0),
            "prompt_ids": json.loads(job["prompt_ids"]), "candidate_ids": json.loads(job["candidate_ids"]),
        }

    def test_items_run_in_shot_order_and_only_one_is_active(self) -> None:
        batch = self.make_batch()
        self.assertEqual([item["shot_id"] for item in batch["items"]], ["s1", "s2"])
        first = claim_next_item(self.db_path)
        self.assertEqual(first["shot_id"], "s1")

        first_result = self.create_draft_job("s1")
        submit_claimed_item(
            self.db_path,
            first,
            self.plan,
            lambda _: first_result,
        )
        self.assertIsNone(claim_next_item(self.db_path), "前一镜头未完成时不得提交下一镜头")

        poll_running_items(self.db_path, self.complete_draft_job)
        second = claim_next_item(self.db_path)
        self.assertEqual(second["shot_id"], "s2")

        second_result = self.create_draft_job("s2", "完成")
        submit_claimed_item(
            self.db_path,
            second,
            self.plan,
            lambda _: second_result,
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
        running_result = self.create_draft_job("s1")
        submit_claimed_item(
            self.db_path, first, self.plan,
            lambda _: running_result,
        )

        cancelling = mutate_batch(self.db_path, batch["id"], "cancel")
        states = {item["shot_id"]: item["state"] for item in cancelling["items"]}
        self.assertEqual(cancelling["state"], "cancelling")
        self.assertEqual(states, {"s1": "running", "s2": "cancelled"})
        poll_running_items(self.db_path, self.complete_draft_job)
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
                "job_revision": 0,
            },
        )
        attempt = get_batch(self.db_path, batch["id"])["items"][0]["attempt_history"][0]
        self.assertEqual(attempt["prompt_ids"], ["prompt-s1"])
        self.assertEqual(attempt["candidate_ids"], ["draft-1"])
        self.assertEqual(attempt["source_snapshot"]["seed"], 42)
        self.assertTrue(attempt["media_evidence"][0]["file_exists"])
        self.assertEqual(attempt["media_evidence"][0]["size_bytes"], len(b"controlled-fake-media"))

    def test_migration_atomically_blocks_running_submitting_and_unknown_conflicts(self) -> None:
        batch = self.make_batch()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            original = db.execute(
                "SELECT * FROM production_batch_items WHERE batch_id = ? AND shot_id = 's1'", (batch["id"],),
            ).fetchone()
            db.execute("DELETE FROM production_shot_leases WHERE shot_id = 's1'")
            db.execute(
                "UPDATE production_batch_items SET state = 'running', attempts = 1 WHERE id = ?", (original["id"],),
            )

            def add_attempt(item_id: str, batch_id: str, state: str, ordinal: int) -> None:
                db.execute(
                    """INSERT INTO production_batches
                    (id, project_id, name, state, item_count, config, message, created_at, updated_at)
                    VALUES (?, 'p1', ?, 'running', 1, '{}', 'legacy', ?, ?)""",
                    (batch_id, batch_id, f"now-{ordinal}", f"now-{ordinal}"),
                )
                item_state = "failed" if state == "submission_unknown" else state
                error = "submission_outcome_unknown" if state == "submission_unknown" else None
                db.execute(
                    """INSERT INTO production_batch_items
                    (id, batch_id, shot_id, ordinal, title, state, plan_hash, plan_snapshot,
                     attempts, max_attempts, message, error, created_at, updated_at)
                    VALUES (?, ?, 's1', ?, 'legacy', ?, ?, ?, 1, 3, 'legacy', ?, ?, ?)""",
                    (item_id, batch_id, ordinal, item_state, original["plan_hash"], original["plan_snapshot"], error, f"now-{ordinal}", f"now-{ordinal}"),
                )
                db.execute(
                    """INSERT INTO production_item_attempts
                    (id, item_id, batch_id, shot_id, attempt, state, plan_hash, plan_snapshot, created_at, updated_at)
                    VALUES (?, ?, ?, 's1', 1, ?, ?, ?, ?, ?)""",
                    (f"attempt-{item_id}", item_id, batch_id, state, original["plan_hash"], original["plan_snapshot"], f"now-{ordinal}", f"now-{ordinal}"),
                )

            db.execute(
                """INSERT INTO production_item_attempts
                (id, item_id, batch_id, shot_id, attempt, state, plan_hash, plan_snapshot, created_at, updated_at)
                VALUES ('attempt-running', ?, ?, 's1', 1, 'running', ?, ?, 'now-0', 'now-0')""",
                (original["id"], batch["id"], original["plan_hash"], original["plan_snapshot"]),
            )
            add_attempt("legacy-submitting", "legacy-batch-submitting", "submitting", 2)
            add_attempt("legacy-unknown", "legacy-batch-unknown", "submission_unknown", 3)
            db.commit()
            init_production_schema(db)
            db.commit()
            states = db.execute(
                "SELECT id, state, error FROM production_batch_items WHERE shot_id = 's1' ORDER BY id",
            ).fetchall()
            self.assertEqual({row["state"] for row in states}, {"failed"})
            self.assertEqual({row["error"] for row in states}, {"migration_shot_ownership_conflict"})
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM production_shot_conflicts WHERE shot_id = 's1' AND state = 'unresolved'",
            ).fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM production_shot_leases WHERE shot_id = 's1'").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM production_item_attempts WHERE shot_id = 's1' AND state = 'submission_unknown'",
            ).fetchone()[0], 3)
            event_count = db.execute(
                "SELECT COUNT(*) FROM production_batch_events WHERE event = 'migration_ownership_conflict'",
            ).fetchone()[0]
            init_production_schema(db)
            db.commit()
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM production_batch_events WHERE event = 'migration_ownership_conflict'",
            ).fetchone()[0], event_count)
        blocked = batch_preflight(self.db_path, self.plan, ["s1"])
        self.assertFalse(blocked["ok"])
        self.assertIn("生产所有权冲突", " ".join(blocked["results"][0]["reasons"]))

    def test_audited_unknown_retry_reuses_own_lease_once_and_can_succeed(self) -> None:
        batch = self.make_batch()
        claimed = claim_next_item(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as db:
            cursor = db.execute(
                """INSERT INTO jobs
                (shot_id, kind, state, message, h3_project, prompt_ids, candidate_ids,
                 source_snapshot, plan_hash, retry_safe, reconciliation_revision)
                VALUES ('s1', 'draft', '待人工对账', 'unknown', 'h3-s1', '[]', '[]', '{}', ?, 0, 1)""",
                ("v" * 64,),
            )
            job_id = int(cursor.lastrowid)
            db.execute(
                "UPDATE production_batch_items SET draft_job_id = ?, draft_job_revision = 1 WHERE id = ?",
                (job_id, claimed["id"]),
            )
            db.commit()
        self.assertEqual(recover_batches(self.db_path), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE jobs SET state = '提交失败', message = '人工确认零提交', retry_safe = 1,
                candidate_ids = '[]', reconciliation_revision = 2 WHERE id = ?""",
                (job_id,),
            )
            db.commit()

        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def retry_once() -> None:
            barrier.wait()
            try:
                retry_item(self.db_path, claimed["id"], self.plan)
                outcomes.append("ok")
            except Exception:
                outcomes.append("conflict")

        workers = [threading.Thread(target=retry_once) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        with closing(sqlite3.connect(self.db_path)) as db:
            lease = db.execute(
                "SELECT item_id, state FROM production_shot_leases WHERE shot_id = 's1'",
            ).fetchone()
            self.assertEqual(lease, (claimed["id"], "queued"))
            self.assertEqual(db.execute(
                "SELECT draft_job_revision FROM production_batch_items WHERE id = ?", (claimed["id"],),
            ).fetchone()[0], 2)

        retry_claim = claim_next_item(self.db_path)
        success = self.create_draft_job("s1", "完成")
        submit_claimed_item(self.db_path, retry_claim, self.plan, lambda _: success)
        finished = get_batch(self.db_path, batch["id"])["items"][0]
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(finished["draft_job_id"], success["job_id"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT candidate_ids FROM jobs WHERE id = ?", (job_id,)).fetchone()[0], "[]")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM production_shot_leases WHERE shot_id = 's1'").fetchone()[0], 0)

    def test_conflict_api_resolves_whole_never_claimed_group_and_persists_audit(self) -> None:
        self.make_ownership_conflict()
        api = FastAPI()
        api.include_router(create_production_router(self.db_path, self.plan))
        client = TestClient(api)
        listed = client.get("/api/production-conflicts?include_resolved=true")
        self.assertEqual(listed.status_code, 200)
        group = listed.json()[0]
        self.assertEqual(group["state"], "unresolved")
        self.assertTrue(group["can_resolve"])
        self.assertEqual({item["proof"]["basis"] for item in group["items"]}, {"queued_never_claimed"})
        missing_confirm = client.post(
            "/api/production-conflicts/s1/resolve",
            json={
                "confirm": False, "expected_revisions": group["expected_revisions"],
                "resolved_by": "测试审片员", "note": "确认旧队列从未领取",
            },
        )
        self.assertEqual(missing_confirm.status_code, 400)
        resolved = client.post(
            "/api/production-conflicts/s1/resolve",
            json={
                "confirm": True, "expected_revisions": group["expected_revisions"],
                "resolved_by": "测试审片员", "note": "确认旧队列从未领取",
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        resolved_group = resolved.json()
        self.assertEqual(resolved_group["state"], "resolved")
        self.assertEqual({item["revision"] for item in resolved_group["items"]}, {1})
        self.assertEqual({item["resolved_by"] for item in resolved_group["items"]}, {"测试审片员"})
        init_result = batch_preflight(self.db_path, self.plan, ["s1"])
        self.assertNotIn("生产所有权冲突", " ".join(init_result["results"][0]["reasons"]))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            init_production_schema(db)
            db.commit()
        restarted = list_ownership_conflicts(self.db_path)[0]
        self.assertEqual(restarted["state"], "resolved")

    def test_uncertain_conflict_requires_every_item_zero_submit_proof(self) -> None:
        _, item_ids = self.make_ownership_conflict(uncertain=True)
        group = list_ownership_conflicts(self.db_path)[0]
        self.assertFalse(group["can_resolve"])
        with self.assertRaises(Exception):
            resolve_ownership_conflicts(
                self.db_path, "s1", expected_revisions=group["expected_revisions"],
                resolved_by="测试审片员", note="证据不足时不得解决",
            )
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM production_shot_conflicts WHERE shot_id = 's1' AND state = 'unresolved'",
            ).fetchone()[0], 2)
            frozen = json.dumps({
                "adapter_returned_success": False,
                "frozen_command": {"arguments": ["draft"]},
                "input_hash": "f" * 64,
                "manifest_before": {"trusted": True},
            })
            for item_id in item_ids:
                cursor = db.execute(
                    """INSERT INTO jobs
                    (shot_id, kind, state, message, candidate_ids, source_snapshot, plan_hash, retry_safe,
                     reconciliation_snapshot, reconciliation_revision)
                    VALUES ('s1', 'draft', '提交失败', '进程未启动', '[]', '{}', ?, 1, ?, 1)""",
                    ("v" * 64, frozen),
                )
                db.execute(
                    "UPDATE production_batch_items SET draft_job_id = ?, draft_job_revision = 1 WHERE id = ?",
                    (int(cursor.lastrowid), item_id),
                )
                db.execute(
                    """UPDATE production_item_attempts SET state = 'submission_unknown', draft_job_id = ?,
                    draft_job_revision = 1 WHERE item_id = ?""",
                    (int(cursor.lastrowid), item_id),
                )
            db.commit()
        provable = list_ownership_conflicts(self.db_path)[0]
        self.assertTrue(provable["can_resolve"])
        resolved = resolve_ownership_conflicts(
            self.db_path, "s1", expected_revisions=provable["expected_revisions"],
            resolved_by="测试审片员", note="两次旧尝试均由冻结证据证明进程未启动",
        )
        self.assertEqual(resolved["state"], "resolved")

    def test_conflict_resolution_is_concurrent_once_and_project_isolated(self) -> None:
        self.make_ownership_conflict()
        group = list_ownership_conflicts(self.db_path)[0]
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def resolve_once() -> None:
            barrier.wait()
            try:
                resolve_ownership_conflicts(
                    self.db_path, "s1", expected_revisions=group["expected_revisions"],
                    resolved_by="并发测试", note="只允许一个事务成功",
                )
                outcomes.append("ok")
            except Exception:
                outcomes.append("conflict")

        workers = [threading.Thread(target=resolve_once) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO projects VALUES ('p2', '另一个项目', 'EP02', 'other', 60, 'now')")
            db.execute("UPDATE workspace_settings SET value = 'p2' WHERE key = 'active_project_id'")
            db.commit()
        self.assertEqual(list_ownership_conflicts(self.db_path), [])
        with self.assertRaises(Exception):
            resolve_ownership_conflicts(
                self.db_path, "s1", expected_revisions=group["expected_revisions"],
                resolved_by="越权测试", note="不能跨项目解决",
            )

    def test_app_syncer_and_scheduler_reject_non_authoritative_job_result(self) -> None:
        batch = self.make_batch()
        claimed = claim_next_item(self.db_path)
        draft = self.create_draft_job("s1")
        submit_claimed_item(self.db_path, claimed, self.plan, lambda _: draft)
        running = get_batch(self.db_path, batch["id"])["items"][0]
        original_db = studio.DB_PATH
        studio.DB_PATH = self.db_path
        try:
            with patch("backend.app.refresh_h3_project", return_value={"state": "运行中", "message": "poll"}) as refresh:
                exact = studio.sync_production_item(running)
            self.assertTrue(exact["authoritative"])
            self.assertEqual(exact["job_id"], draft["job_id"])
            self.assertEqual(refresh.call_args.kwargs["attempt_job"]["id"], draft["job_id"])
        finally:
            studio.DB_PATH = original_db

        forged = {**exact, "job_id": draft["job_id"] + 100, "state": "完成"}
        self.assertEqual(poll_running_items(self.db_path, lambda _: forged), 1)
        failed = get_batch(self.db_path, batch["id"])["items"][0]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "draft_job_reconciliation_conflict")
        self.assertEqual(failed["draft_job_id"], draft["job_id"])
        self.assertEqual(failed["attempt_history"][0]["candidate_ids"], draft["candidate_ids"])
        self.assertEqual(failed["attempt_history"][0]["state"], "submission_unknown")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT state FROM production_shot_leases WHERE shot_id = 's1'").fetchone()[0],
                "unknown",
            )

    def test_external_job_revision_advance_is_cas_consumed_by_exact_attempt(self) -> None:
        batch = self.make_batch()
        claimed = claim_next_item(self.db_path)
        draft = self.create_draft_job("s1")
        submit_claimed_item(self.db_path, claimed, self.plan, lambda _: draft)
        running = get_batch(self.db_path, batch["id"])["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE jobs SET state = '完成', message = '人工对账后完成',
                reconciliation_revision = reconciliation_revision + 1 WHERE id = ?""",
                (draft["job_id"],),
            )
            db.commit()
        original_db = studio.DB_PATH
        studio.DB_PATH = self.db_path
        try:
            self.assertEqual(poll_running_items(self.db_path, studio.sync_production_item), 1)
        finally:
            studio.DB_PATH = original_db
        completed = get_batch(self.db_path, batch["id"])["items"][0]
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["draft_job_revision"], 1)
        self.assertEqual(completed["attempt_history"][0]["draft_job_revision"], 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM production_shot_leases WHERE shot_id = 's1'").fetchone()[0], 0)

    def test_external_revision_with_changed_candidate_set_fails_closed(self) -> None:
        batch = self.make_batch()
        claimed = claim_next_item(self.db_path)
        draft = self.create_draft_job("s1")
        submit_claimed_item(self.db_path, claimed, self.plan, lambda _: draft)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE jobs SET state = '完成', candidate_ids = '["foreign-candidate"]',
                reconciliation_revision = reconciliation_revision + 1 WHERE id = ?""",
                (draft["job_id"],),
            )
            db.commit()
        original_db = studio.DB_PATH
        studio.DB_PATH = self.db_path
        try:
            self.assertEqual(poll_running_items(self.db_path, studio.sync_production_item), 1)
        finally:
            studio.DB_PATH = original_db
        failed = get_batch(self.db_path, batch["id"])["items"][0]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "draft_job_reconciliation_conflict")
        self.assertEqual(failed["attempt_history"][0]["candidate_ids"], draft["candidate_ids"])


if __name__ == "__main__":
    unittest.main()
