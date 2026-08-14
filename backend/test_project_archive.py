from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import app as studio
from backend import project_archive as archive_module
from backend.hd_delivery import (
    HDPlanCreate,
    HDReviewRequest,
    HDScores,
    HDSelectRequest,
    HDSubmitRequest,
    HDValidationRequest,
    create_hd_plan,
    process_next_hd_job,
    save_hd_review,
    select_hd_artifact,
    submit_hd_job,
    validate_hd_plan,
)
from backend.project_archive import (
    _acquire_archive_task,
    _fail_archive_task,
    create_archive_router,
    create_project_archive,
    recover_archive_tasks,
    verify_project_archive,
)


class ProjectArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.root = root
        studio.DB_PATH = root / "studio.db"
        studio.COMFY_OUTPUT_ROOT = root / "comfy-output"
        studio.EXPORT_ROOT = root / "exports"
        studio.EXPORT_JOB_ROOT = root / "jobs"
        self.backup_root = root / "backups"
        studio.EXPORT_ROOT.mkdir()
        studio.EXPORT_JOB_ROOT.mkdir()
        studio.COMFY_OUTPUT_ROOT.mkdir()
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

    def test_active_work_blocks_archive_without_package_or_persistent_lease(self) -> None:
        now = studio.utc_now()
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO production_batches
                (id, project_id, name, state, item_count, config, message, created_at, updated_at)
                VALUES ('active-batch', ?, 'active', 'running', 0, '{}', 'running', ?, ?)""",
                (self.project["id"], now, now),
            )
            db.commit()
        with self.assertRaises(HTTPException) as blocked:
            create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"])
        self.assertEqual(blocked.exception.status_code, 409)
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archives").fetchone()[0], 0)
        self.assertEqual(list(self.backup_root.rglob("*.jingchang.zip")), [])

    def test_media_mutation_during_staging_fails_closed_and_cleans_lease(self) -> None:
        video = studio.EXPORT_ROOT / "mutable-delivery.mp4"
        video.write_bytes(b"trusted-media-before-archive")
        now = studio.utc_now()
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO export_runs
                (id, project_id, state, message, output_name, width, height, config, outputs,
                 created_at, updated_at, source_snapshot, is_current)
                VALUES ('mutable-delivery', ?, ?, 'done', 'delivery', 1344, 768, '{}', ?, ?, ?, '{}', 1)""",
                (self.project["id"], "已完成", json.dumps({"video": "mutable-delivery.mp4"}), now, now),
            )
            db.commit()
        original_copy = archive_module.shutil.copyfileobj
        mutated = False

        def mutate_after_copy(source_handle, target_handle, length=0):
            nonlocal mutated
            original_copy(source_handle, target_handle, length=length)
            if not mutated:
                mutated = True
                video.write_bytes(b"media-mutated-during-staging")

        with patch.object(archive_module.shutil, "copyfileobj", side_effect=mutate_after_copy):
            with self.assertRaises(HTTPException) as blocked:
                create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"], studio.EXPORT_ROOT)
        self.assertEqual(blocked.exception.status_code, 409)
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archives").fetchone()[0], 0)
        self.assertEqual(list(self.backup_root.rglob("*.jingchang.zip")), [])
        self.assertFalse((self.backup_root / ".staging").exists() and any((self.backup_root / ".staging").iterdir()))

    def test_hard_crash_recovery_cleans_private_files_and_releases_freeze_idempotently(self) -> None:
        task_id = "archive-task-hard-crash"
        lease_id = "archive-lease-hard-crash"
        archive_id = "project-archive-hard-crash"
        child_code = "\n".join(
            (
                "import os, sys",
                "from pathlib import Path",
                "from backend.project_archive import _acquire_archive_task, utc_now",
                "result = _acquire_archive_task(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], "
                "mark_archived=False, task_id=sys.argv[4], lease_id=sys.argv[5], archive_id=sys.argv[6], created_at=utc_now())",
                "staging = Path(result['staging_path'])",
                "staging.mkdir(parents=True, exist_ok=False)",
                "(staging / 'orphan.media').write_bytes(b'orphan-staging')",
                "partial = Path(result['partial_path'])",
                "partial.parent.mkdir(parents=True, exist_ok=True)",
                "partial.write_bytes(b'partial-archive')",
                "Path(result['final_path']).write_bytes(b'unregistered-final')",
                "os._exit(23)",
            )
        )
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                child_code,
                str(studio.DB_PATH),
                str(self.backup_root),
                self.project["id"],
                task_id,
                lease_id,
                archive_id,
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )
        self.assertEqual(process.returncode, 23)
        with closing(studio.connect()) as db:
            crashed = dict(db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone())
            self.assertEqual(crashed["state"], "running")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 1)
        crash_paths = [Path(crashed[key]) for key in ("staging_path", "partial_path", "final_path")]
        self.assertTrue(all(path.exists() for path in crash_paths))

        # This is the same ordering used by app.lifespan: idempotent schema
        # initialization first, then archive recovery before any worker starts.
        studio.init_db()
        self.assertEqual(recover_archive_tasks(studio.DB_PATH, self.backup_root), 1)
        with closing(studio.connect()) as db:
            recovered = dict(db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone())
            project = dict(db.execute("SELECT * FROM projects WHERE id = ?", (self.project["id"],)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archives").fetchone()[0], 0)
        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(recovered["stage"], "recovered_failed")
        self.assertIn("owner_process_dead", recovered["audit"])
        self.assertFalse(project["archived"])
        self.assertTrue(all(not path.exists() for path in crash_paths))
        self.assertEqual(recover_archive_tasks(studio.DB_PATH, self.backup_root), 0)
        tasks = self.endpoint("/api/projects/{project_id}/archive-tasks", "GET")(self.project["id"])
        self.assertEqual(tasks[0]["id"], task_id)
        self.assertEqual(tasks[0]["state"], "failed")
        self.assertIn("recovery", tasks[0]["audit"])
        self.assertNotIn("staging_path", tasks[0])
        activity = next(item for item in studio.get_workbench()["activities"] if item["id"] == f"archive-{task_id}")
        self.assertEqual(activity["state"], "failed")
        self.assertIn("异常退出", activity["message"])

        # Releasing only the dead owner restores every downstream archive-lease
        # gate; a fresh snapshot can complete normally without deleting history.
        archive = create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"])
        self.assertEqual(archive["revision"], 1)
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT state FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone()[0],
                "failed",
            )

    def test_recovery_never_clears_live_owner_even_with_stale_heartbeat(self) -> None:
        task_id = "archive-task-live-owner"
        acquired = _acquire_archive_task(
            studio.DB_PATH,
            self.backup_root,
            self.project["id"],
            mark_archived=False,
            task_id=task_id,
            lease_id="archive-lease-live-owner",
            archive_id="project-archive-live-owner",
            created_at=studio.utc_now(),
        )
        with closing(studio.connect()) as db:
            db.execute(
                "UPDATE project_archive_tasks SET heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (task_id,),
            )
            db.execute(
                "UPDATE project_archive_leases SET heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE task_id = ?",
                (task_id,),
            )
            db.commit()
        with patch.object(archive_module, "ARCHIVE_OWNER_INSTANCE", "another-live-server-instance"):
            self.assertEqual(recover_archive_tasks(studio.DB_PATH, self.backup_root), 0)
        with closing(studio.connect()) as db:
            task = dict(db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone())
            self.assertEqual(task["state"], "running")
            self.assertEqual(task["revision"], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 1)
        _fail_archive_task(studio.DB_PATH, task_id, "test cleanup")
        for key in ("staging_path", "partial_path", "final_path"):
            self.assertFalse(Path(acquired[key]).exists())

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

    def test_archive_contains_hd_versions_and_checksum_verified_media(self) -> None:
        shot_id = self.project["shots"][0]["id"]
        source = studio.COMFY_OUTPUT_ROOT / "trusted-draft.mp4"
        source.write_bytes(b"trusted-low-resolution-master")
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        now = studio.utc_now()
        metadata = json.dumps(
            {
                "prompt": "a continuous locked shot",
                "mode": "fl2va",
                "width": 608,
                "height": 352,
                "actual_seconds": 5.167,
            }
        )
        snapshot = json.dumps(
            {
                "output_file": str(source.resolve()),
                "size_bytes": source.stat().st_size,
                "checksum_sha256": source_sha,
                "trace": {"plan_hash": "a" * 64, "plan_snapshot": {"mode": "fl2va"}},
            }
        )
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO candidates
                (id, shot_id, label, seed, created_at, thumbnail, selected, scores, note, status,
                 source, external_id, prompt_id, output_file, archived, metadata)
                VALUES ('archive-low-master', ?, 'A', 42, ?, '', 1, '{}', '', 'completed',
                        'controlled-fake', 'draft-archive', 'prompt-archive', ?, 0, ?)""",
                (shot_id, now, str(source), metadata),
            )
            db.execute(
                """INSERT INTO candidate_reviews
                (id, candidate_id, shot_id, project_id, revision, decision, scores, issues, note,
                 watched_seconds, audio_checks, candidate_snapshot, media_probe, created_at)
                VALUES ('archive-low-review', 'archive-low-master', ?, ?, 1, 'pass', '{}', '[]',
                        'controlled fixture review', 5.167, '{}', ?, '{}', ?)""",
                (shot_id, self.project["id"], snapshot, now),
            )
            db.execute(
                """INSERT INTO candidate_master_versions
                (id, project_id, shot_id, revision, candidate_id, action, review_id, review_revision,
                 note, candidate_snapshot, created_at)
                VALUES ('archive-low-version', ?, ?, 1, 'archive-low-master', 'select',
                        'archive-low-review', 1, 'locked fixture', ?, ?)""",
                (self.project["id"], shot_id, snapshot, now),
            )
            db.commit()

        def runner(request: dict, dry_run: bool) -> dict:
            source = request["source"]
            model_id = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
            workflow_id = "h3-ref2va-regenerate-v2"
            if dry_run:
                return {
                    "gpu_submitted": False,
                    "adapter": "controlled-fake-h3",
                    "model_id": model_id,
                    "workflow_id": workflow_id,
                    "command": request,
                }
            output_root = Path(request["attempt_output_root"])
            output_root.mkdir(parents=True, exist_ok=True)
            output = output_root / f"{request['expected_artifact_id']}.mp4"
            output.write_bytes(b"controlled-fake-hd-archive-media")
            references = {
                "ref_images": [],
                "ref_videos": [source["media"]["path"]],
                "ref_audios": [],
                "first_frame": None,
                "last_frame": None,
            }
            return {
                "gpu_submitted": True,
                "output_file": str(output),
                "model_id": model_id,
                "workflow_id": workflow_id,
                "prompt_id": "fixture-hd-prompt",
                "comfy_task_id": "fixture-comfy-task",
                "adapter_input": {
                    "seed": source["generation_seed"],
                    "prompt_sha256": hashlib.sha256(source["prompt"].encode()).hexdigest(),
                    "model_id": model_id,
                    "workflow_id": workflow_id,
                    "references": references,
                },
                "manifest_record": {
                    "status": "completed",
                    "prompt_id": "fixture-hd-prompt",
                    "seed": source["generation_seed"],
                    "prompt": source["prompt"],
                    "width": request["target_width"],
                    "height": request["target_height"],
                    "references": references,
                },
            }

        def probe(path: Path) -> dict:
            return {
                "ok": path.is_file(),
                "issues": [],
                "duration_seconds": 5.167,
                "video": {"codec": "h264", "width": 1344, "height": 768},
                "audio": {"present": True, "codec": "aac", "channels": 2, "sample_rate": 48000},
            }

        plan = create_hd_plan(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDPlanCreate(strategy_type="ref2va_regenerate"),
        )
        validation = validate_hd_plan(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            HDValidationRequest(plan_id=plan["id"], expected_plan_hash=plan["plan_hash"]),
            runner,
        )
        submit_hd_job(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDSubmitRequest(
                validation_id=validation["id"],
                expected_validation_hash=validation["validation_hash"],
                idempotency_key="archive-hd-fixture-0001",
                confirm=True,
            ),
        )
        self.assertTrue(process_next_hd_job(studio.DB_PATH, studio.COMFY_OUTPUT_ROOT, runner, probe))
        with closing(studio.connect()) as db:
            artifact = dict(db.execute("SELECT * FROM hd_artifacts WHERE shot_id = ?", (shot_id,)).fetchone())
        save_hd_review(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDReviewRequest(
                artifact_id=artifact["id"],
                decision="pass",
                scores=HDScores(
                    story_match=4,
                    identity_continuity=4,
                    temporal_stability=4,
                    visual_detail=4,
                    audio_quality=4,
                ),
                drift_confirmed=True,
                watched_seconds=5.167,
                note="controlled fake was reviewed against the low-resolution master",
            ),
            probe,
        )
        select_hd_artifact(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDSelectRequest(artifact_id=artifact["id"], base_revision=0, note="fixture HD master"),
        )

        archive = create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"])
        for table in (
            "hd_strategy_versions",
            "hd_validations",
            "hd_generation_jobs",
            "hd_artifacts",
            "hd_artifact_reviews",
            "hd_master_versions",
        ):
            self.assertEqual(archive["row_counts"][table], 1, table)
        self.assertEqual(archive["media_count"], 2)
        self.assertEqual(archive["omitted_count"], 0)
        self.assertTrue(verify_project_archive(studio.DB_PATH, archive["id"])["ok"])

        artifact_path = Path(artifact["output_path"])
        artifact_path.chmod(0o600)
        artifact_path.write_bytes(b"tampered-after-record")
        with self.assertRaises(HTTPException) as corrupted:
            create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"])
        self.assertEqual(corrupted.exception.status_code, 409)
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archives").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
