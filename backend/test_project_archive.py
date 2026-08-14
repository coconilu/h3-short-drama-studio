from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend import app as studio
from backend.project_archive import create_archive_router, create_project_archive, verify_project_archive


class ProjectArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        studio.DB_PATH = root / "studio.db"
        studio.EXPORT_ROOT = root / "exports"
        studio.EXPORT_JOB_ROOT = root / "jobs"
        self.backup_root = root / "backups"
        studio.EXPORT_ROOT.mkdir()
        studio.EXPORT_JOB_ROOT.mkdir()
        studio.init_db()
        self.project = studio.create_project(
            studio.ProjectCreate(
                title="可归档短剧",
                episode="EP01",
                logline="测试归档的项目。",
                target_duration=8,
                shots=[studio.ShotCreate(title="测试镜头", description="角色走进房间。", prompt="person enters a room")],
            )
        )
        router = create_archive_router(studio.DB_PATH, self.backup_root)
        self.endpoints = {(route.path, next(iter(route.methods))): route.endpoint for route in router.routes}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def endpoint(self, path: str, method: str):
        return self.endpoints[(path, method)]

    def test_archive_package_contains_scoped_data_and_verifies(self) -> None:
        video = studio.EXPORT_ROOT / "delivery.mp4"
        video.write_bytes(b"real-video-bytes")
        now = studio.utc_now()
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO export_runs
                (id, project_id, state, message, output_name, width, height, config, outputs,
                 created_at, updated_at, source_snapshot, is_current)
                VALUES ('delivery', ?, '已完成', 'done', 'delivery', 1344, 768, '{}', ?, ?, ?, '{}', 1)""",
                (self.project["id"], json.dumps({"video": "delivery.mp4"}), now, now),
            )
            db.commit()
        archive = create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"], studio.EXPORT_ROOT)
        package_path = next(self.backup_root.rglob("*.jingchang.zip"))
        self.assertGreater(archive["size_bytes"], 0)
        self.assertEqual(archive["row_counts"]["projects"], 1)
        self.assertEqual(archive["row_counts"]["shots"], 1)
        self.assertEqual(archive["media_count"], 1)
        self.assertEqual(archive["omitted_count"], 0)
        with zipfile.ZipFile(package_path) as package:
            manifest = json.loads(package.read("archive-manifest.json"))
            data = json.loads(package.read("project-data.json"))
        self.assertFalse(manifest["model_weights_included"])
        self.assertIn("media/current-export/", manifest["media"][0]["archive_path"])
        self.assertEqual(data["tables"]["projects"][0]["id"], self.project["id"])
        self.assertTrue(verify_project_archive(studio.DB_PATH, archive["id"])["ok"])

    def test_archiving_active_project_fails_closed_then_restore_is_reversible(self) -> None:
        archive_project = self.endpoint("/api/projects/{project_id}/archive", "POST")
        with self.assertRaises(HTTPException) as active:
            archive_project(self.project["id"])
        self.assertEqual(active.exception.status_code, 409)

        other = studio.create_project(
            studio.ProjectCreate(
                title="当前项目",
                episode="EP01",
                logline="保持激活。",
                target_duration=5,
                shots=[studio.ShotCreate(title="占位镜头", description="静止。", prompt="still frame")],
            )
        )
        result = archive_project(self.project["id"])
        self.assertTrue(result["archived"])
        with self.assertRaises(HTTPException) as blocked:
            studio.set_active_project(self.project["id"])
        self.assertEqual(blocked.exception.status_code, 409)

        restored = self.endpoint("/api/projects/{project_id}/restore", "POST")(self.project["id"])
        self.assertFalse(restored["archived"])
        studio.set_active_project(self.project["id"])
        self.assertEqual(studio.get_project()["id"], self.project["id"])
        self.assertNotEqual(other["id"], self.project["id"])

    def test_corrupted_package_is_marked_invalid(self) -> None:
        archive = create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"])
        package_path = next(self.backup_root.rglob("*.jingchang.zip"))
        with package_path.open("ab") as handle:
            handle.write(b"corrupt")
        result = verify_project_archive(studio.DB_PATH, archive["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "invalid")
        self.assertIn("SHA-256", result["errors"][0])

    def test_archive_contains_candidate_master_and_generation_attempt_history(self) -> None:
        shot_id = self.project["shots"][0]["id"]
        now = studio.utc_now()
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO candidates
                (id, shot_id, label, seed, created_at, thumbnail, selected, scores, note, status, source, archived, metadata)
                VALUES ('c1', ?, 'A', 42, ?, '', 1, '{}', '', 'completed', 'h3', 0, '{}')""",
                (shot_id, now),
            )
            db.execute(
                """INSERT INTO candidate_reviews
                (id, candidate_id, shot_id, project_id, revision, decision, scores, issues, note,
                 watched_seconds, candidate_snapshot, media_probe, created_at)
                VALUES ('r1', 'c1', ?, ?, 1, 'pass', '{}', '[]', '', 5, '{}', '{}', ?)""",
                (shot_id, self.project["id"], now),
            )
            db.execute(
                """INSERT INTO candidate_master_versions
                (id, project_id, shot_id, revision, candidate_id, action, review_id, review_revision,
                 note, candidate_snapshot, created_at)
                VALUES ('m1', ?, ?, 1, 'c1', 'select', 'r1', 1, 'master', '{}', ?)""",
                (self.project["id"], shot_id, now),
            )
            db.execute(
                """INSERT INTO production_batches
                (id, project_id, name, state, item_count, config, message, created_at, updated_at)
                VALUES ('b1', ?, 'batch', 'completed', 1, '{}', 'done', ?, ?)""",
                (self.project["id"], now, now),
            )
            db.execute(
                """INSERT INTO production_batch_items
                (id, batch_id, shot_id, ordinal, title, state, plan_hash, plan_snapshot, message, created_at, updated_at)
                VALUES ('i1', 'b1', ?, 1, 'shot', 'completed', ?, '{}', 'done', ?, ?)""",
                (shot_id, "a" * 64, now, now),
            )
            db.execute(
                """INSERT INTO production_item_attempts
                (id, item_id, batch_id, shot_id, attempt, state, plan_hash, plan_snapshot, created_at, updated_at)
                VALUES ('a1', 'i1', 'b1', ?, 1, 'completed', ?, '{}', ?, ?)""",
                (shot_id, "a" * 64, now, now),
            )
            db.commit()
        archive = create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"])
        self.assertEqual(archive["row_counts"]["candidate_master_versions"], 1)
        self.assertEqual(archive["row_counts"]["production_item_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
