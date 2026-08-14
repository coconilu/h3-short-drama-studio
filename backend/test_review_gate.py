from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend.review_gate import (
    AudioChecks,
    CandidateMasterRollbackRequest,
    CandidateReviewRequest,
    ReviewScores,
    init_review_schema,
    latest_candidate_review,
    record_master_selection,
    require_passed_review,
    review_workspace,
    rollback_master_selection,
    save_candidate_review,
)


class ReviewGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "studio.db"
        self.video_path = self.root / "candidate.mp4"
        self.video_path.write_bytes(b"test-video-v1")
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
                  title TEXT NOT NULL, status TEXT NOT NULL, dialogue TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT 'now'
                );
                CREATE TABLE candidates (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL REFERENCES shots(id), source TEXT NOT NULL,
                  status TEXT NOT NULL, output_file TEXT, external_id TEXT, prompt_id TEXT,
                  scores TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
                  label TEXT NOT NULL DEFAULT 'A', created_at TEXT NOT NULL DEFAULT 'now',
                  selected INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', 'logline', 60, 'now')")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute(
                "INSERT INTO shots (id, project_id, ordinal, title, status, dialogue) VALUES ('s1', 'p1', 1, '镜头一', '待审片', '')"
            )
            db.execute(
                "INSERT INTO candidates VALUES ('c1', 's1', 'h3', 'completed', ?, 'draft-1', 'prompt-1', '{}', '', 0, 'A', 'now', 0)",
                (str(self.video_path),),
            )
            init_review_schema(db)
            db.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def probe(_: Path) -> dict:
        return {
            "ok": True, "duration_seconds": 5.0, "size_bytes": 13,
            "video": {"codec": "h264", "width": 608, "height": 352, "frame_rate": "24/1"},
            "audio": {"present": True, "codec": "aac", "channels": 2, "sample_rate": 48000},
            "issues": [],
        }

    @staticmethod
    def scores(**overrides: int) -> ReviewScores:
        values = {"story_match": 4, "continuity": 4, "action": 4, "visual_quality": 4, "audio_quality": 4}
        values.update(overrides)
        return ReviewScores(**values)

    def request(self, **overrides) -> CandidateReviewRequest:
        values = {
            "candidate_id": "c1", "decision": "pass", "scores": self.scores(),
            "audio_checks": AudioChecks(dialogue_match="not_applicable", lip_sync="not_applicable", ambience="pass"),
            "issues": [], "note": "可进入草稿选择", "watched_seconds": 5.0,
        }
        values.update(overrides)
        return CandidateReviewRequest(**values)

    def test_pass_requires_scores_media_qc_and_sufficient_watch_time(self) -> None:
        with self.assertRaises(HTTPException) as low_score:
            save_candidate_review(
                self.db_path, self.root, "s1", self.request(scores=self.scores(continuity=2)), self.probe,
            )
        self.assertEqual(low_score.exception.status_code, 422)

        with self.assertRaises(HTTPException) as unwatched:
            save_candidate_review(
                self.db_path, self.root, "s1", self.request(watched_seconds=1.0), self.probe,
            )
        self.assertEqual(unwatched.exception.status_code, 422)

        with self.assertRaises(HTTPException) as bad_media:
            save_candidate_review(
                self.db_path, self.root, "s1", self.request(), lambda _: {"ok": False, "duration_seconds": 5.0},
            )
        self.assertEqual(bad_media.exception.status_code, 422)

    def test_review_revisions_drive_selection_gate(self) -> None:
        passed = save_candidate_review(self.db_path, self.root, "s1", self.request(), self.probe)
        self.assertEqual(passed["revision"], 1)
        self.assertTrue(passed["can_select"])
        self.assertEqual(require_passed_review(self.db_path, "c1", self.root)["decision"], "pass")

        changed = save_candidate_review(
            self.db_path, self.root, "s1",
            self.request(decision="needs_changes", issues=["identity_drift"], note="人物身份变化", watched_seconds=2.0),
            self.probe,
        )
        self.assertEqual(changed["revision"], 2)
        self.assertFalse(changed["can_select"])
        with self.assertRaises(HTTPException):
            require_passed_review(self.db_path, "c1", self.root)

    def test_file_change_makes_review_stale(self) -> None:
        save_candidate_review(self.db_path, self.root, "s1", self.request(), self.probe)
        self.video_path.write_bytes(b"test-video-v2-with-different-size")
        latest = latest_candidate_review(self.db_path, "c1", self.root)
        self.assertTrue(latest["stale"])
        self.assertFalse(latest["can_select"])
        workspace = review_workspace(self.db_path, "s1", self.root)
        self.assertEqual(workspace["summary"]["pending_count"], 1)

    def test_non_pass_decision_requires_actionable_note(self) -> None:
        with self.assertRaises(HTTPException) as missing_note:
            save_candidate_review(
                self.db_path, self.root, "s1",
                self.request(decision="reject", issues=["artifact"], note="", watched_seconds=1.0),
                self.probe,
            )
        self.assertEqual(missing_note.exception.status_code, 422)

    def test_dialogue_shot_requires_human_dialogue_and_sync_checks(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE shots SET dialogue = '别出来' WHERE id = 's1'")
            db.commit()
        with self.assertRaises(HTTPException) as unchecked:
            save_candidate_review(
                self.db_path, self.root, "s1",
                self.request(audio_checks=AudioChecks(dialogue_match="pending", lip_sync="pending", ambience="pass")),
                self.probe,
            )
        self.assertEqual(unchecked.exception.status_code, 422)
        passed = save_candidate_review(
            self.db_path, self.root, "s1",
            self.request(audio_checks=AudioChecks(dialogue_match="pass", lip_sync="pass", ambience="pass")),
            self.probe,
        )
        self.assertTrue(passed["can_select"])

    def add_second_candidate(self) -> Path:
        path = self.root / "candidate-2.mp4"
        path.write_bytes(b"test-video-v2")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO candidates VALUES ('c2', 's1', 'h3', 'completed', ?, 'draft-2', 'prompt-2', '{}', '', 0, 'B', 'now', 0)",
                (str(path),),
            )
            db.commit()
        return path

    def test_master_selection_is_append_only_and_can_rollback(self) -> None:
        self.add_second_candidate()
        save_candidate_review(self.db_path, self.root, "s1", self.request(), self.probe)
        save_candidate_review(
            self.db_path, self.root, "s1", self.request(candidate_id="c2", note="候选 B 可用"), self.probe,
        )
        first = record_master_selection(self.db_path, self.root, "s1", "c1", note="先选 A", base_revision=0)
        second = record_master_selection(self.db_path, self.root, "s1", "c2", note="切换 B", base_revision=1)
        rolled = rollback_master_selection(
            self.db_path,
            self.root,
            "s1",
            CandidateMasterRollbackRequest(target_revision=1, base_revision=2, note="回滚到 A", confirm=True),
        )
        self.assertEqual((first["revision"], second["revision"], rolled["revision"]), (1, 2, 3))
        self.assertEqual(rolled["action"], "rollback")
        self.assertEqual(rolled["rollback_of_revision"], 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            selected = db.execute("SELECT id FROM candidates WHERE selected = 1").fetchone()[0]
            candidate_count = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertEqual(selected, "c1")
        self.assertEqual(candidate_count, 2, "母版切换不能覆盖或删除候选")
        workspace = review_workspace(self.db_path, "s1", self.root)
        self.assertEqual([item["revision"] for item in workspace["master_versions"]], [3, 2, 1])
        self.assertTrue(workspace["master_versions"][0]["current"])

    def test_selection_requires_two_real_candidates_and_debt_remains_visible(self) -> None:
        save_candidate_review(self.db_path, self.root, "s1", self.request(), self.probe)
        with self.assertRaises(HTTPException) as insufficient:
            record_master_selection(self.db_path, self.root, "s1", "c1", note="不能只有一个", base_revision=0)
        self.assertEqual(insufficient.exception.status_code, 409)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO candidates VALUES ('legacy', 's1', 'mock', 'completed', NULL, NULL, NULL, '{}', '', 0, '旧', 'now', 0)"
            )
            db.commit()
        workspace = review_workspace(self.db_path, "s1", self.root)
        debt = next(item for item in workspace["comparison"] if item["candidate_id"] == "legacy")
        self.assertEqual(debt["trace"]["evidence_status"], "historical_debt")
        self.assertEqual(workspace["summary"]["evidence_debt_count"], 1)

    def test_review_history_and_media_checksum_are_persisted(self) -> None:
        first = save_candidate_review(self.db_path, self.root, "s1", self.request(), self.probe)
        second = save_candidate_review(
            self.db_path,
            self.root,
            "s1",
            self.request(decision="needs_changes", issues=["other"], note="再次检查", watched_seconds=2),
            self.probe,
        )
        workspace = review_workspace(self.db_path, "s1", self.root)
        review = workspace["reviews"][0]
        self.assertEqual([item["revision"] for item in review["history"]], [2, 1])
        self.assertEqual((first["revision"], second["revision"]), (1, 2))
        with closing(sqlite3.connect(self.db_path)) as db:
            snapshot = db.execute(
                "SELECT candidate_snapshot FROM candidate_reviews WHERE candidate_id = 'c1' AND revision = 1"
            ).fetchone()[0]
        self.assertEqual(len(__import__("json").loads(snapshot)["checksum_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
