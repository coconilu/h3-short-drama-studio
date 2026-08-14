from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
    _claim_job,
)
from backend.production_scheduler import claim_next_item
from backend.prompt_compiler import begin_validation_lease, compile_prompt_plan, end_validation_lease
from backend.project_archive import (
    ArchiveReconciliationResolve,
    _acquire_archive_task,
    _fail_archive_task,
    create_archive_router,
    create_project_archive,
    list_archive_reconciliations,
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

    def assert_downstream_claims_resume(self) -> None:
        """Exercise the four real worker predicates after a freeze is released."""
        shot_id = self.project["shots"][0]["id"]
        studio.activate_project(self.project["id"])
        h3_lease = begin_validation_lease(studio.DB_PATH, compile_prompt_plan(studio.DB_PATH, shot_id))
        end_validation_lease(studio.DB_PATH, h3_lease)
        now = studio.utc_now()
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO production_batches
                (id, project_id, name, state, item_count, config, message, created_at, updated_at)
                VALUES ('post-reconcile-batch', ?, 'resume', 'running', 1, '{}', 'ready', ?, ?)""",
                (self.project["id"], now, now),
            )
            db.execute(
                """INSERT INTO production_batch_items
                (id, batch_id, shot_id, ordinal, title, state, plan_hash, plan_snapshot,
                 message, created_at, updated_at)
                VALUES ('post-reconcile-item', 'post-reconcile-batch', ?, 1, 'resume', 'queued', ?, '{}',
                        'ready', ?, ?)""",
                (shot_id, "1" * 64, now, now),
            )
            db.execute(
                """INSERT INTO production_shot_leases
                (shot_id, batch_id, item_id, state, created_at, updated_at)
                VALUES (?, 'post-reconcile-batch', 'post-reconcile-item', 'queued', ?, ?)""",
                (shot_id, now, now),
            )
            db.execute(
                """INSERT INTO export_runs
                (id, project_id, state, message, output_name, width, height, config, outputs,
                 created_at, updated_at, source_snapshot)
                VALUES ('post-reconcile-export', ?, '排队中', 'ready', 'post-reconcile-export',
                        1344, 768, '{}', '{}', ?, ?, '{}')""",
                (self.project["id"], now, now),
            )
            db.execute(
                """INSERT INTO candidates
                (id, shot_id, label, seed, created_at, thumbnail, selected, scores, note, status,
                 source, archived, metadata)
                VALUES ('post-reconcile-candidate', ?, 'A', 1, ?, '', 1, '{}', '', 'completed',
                        'controlled-fixture', 0, '{}')""",
                (shot_id, now),
            )
            db.execute(
                """INSERT INTO candidate_reviews
                (id, candidate_id, shot_id, project_id, revision, decision, scores, issues, note,
                 watched_seconds, candidate_snapshot, media_probe, created_at)
                VALUES ('post-reconcile-review', 'post-reconcile-candidate', ?, ?, 1, 'pass', '{}',
                        '[]', 'fixture', 1, '{}', '{}', ?)""",
                (shot_id, self.project["id"], now),
            )
            db.execute(
                """INSERT INTO candidate_master_versions
                (id, project_id, shot_id, revision, candidate_id, action, review_id, review_revision,
                 note, candidate_snapshot, created_at)
                VALUES ('post-reconcile-master', ?, ?, 1, 'post-reconcile-candidate', 'select',
                        'post-reconcile-review', 1, 'fixture', '{}', ?)""",
                (self.project["id"], shot_id, now),
            )
            db.execute(
                """INSERT INTO hd_strategy_versions
                (id, project_id, shot_id, revision, strategy_type, target_width, target_height,
                 source_master_version_id, source_candidate_id, source_snapshot, plan_hash,
                 estimated_operation, created_at)
                VALUES ('post-reconcile-plan', ?, ?, 1, 'deterministic_scale', 1344, 768,
                        'post-reconcile-master', 'post-reconcile-candidate', '{}', ?, 'fixture', ?)""",
                (self.project["id"], shot_id, "2" * 64, now),
            )
            db.execute(
                """INSERT INTO hd_validations
                (id, project_id, shot_id, plan_id, plan_hash, validation_hash, adapter, model_id,
                 workflow_id, command_snapshot, evidence, created_at)
                VALUES ('post-reconcile-validation', ?, ?, 'post-reconcile-plan', ?, ?, 'fixture',
                        'ffmpeg-lanczos', 'deterministic-scale-contain-v1', '{}', '{}', ?)""",
                (self.project["id"], shot_id, "2" * 64, "3" * 64, now),
            )
            db.execute(
                """INSERT INTO hd_generation_jobs
                (id, project_id, shot_id, plan_id, validation_id, attempt, idempotency_key, state,
                 plan_hash, validation_hash, command_snapshot, expected_artifact_id, message,
                 created_at, updated_at)
                VALUES ('post-reconcile-hd', ?, ?, 'post-reconcile-plan', 'post-reconcile-validation',
                        1, 'post-reconcile-hd', 'queued', ?, ?, '{}', 'post-reconcile-artifact',
                        'ready', ?, ?)""",
                (self.project["id"], shot_id, "2" * 64, "3" * 64, now, now),
            )
            db.commit()

        self.assertEqual(claim_next_item(studio.DB_PATH)["id"], "post-reconcile-item")
        self.assertEqual(_claim_job(studio.DB_PATH)["id"], "post-reconcile-hd")
        self.assertEqual(studio.claim_next_export_run()["id"], "post-reconcile-export")

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

    def test_legacy_taskless_lease_migrates_to_auditable_manual_reconciliation(self) -> None:
        with closing(studio.connect()) as db:
            db.execute("DROP TABLE project_archive_reconciliations")
            db.execute("DROP TABLE project_archive_leases")
            db.execute("DROP TABLE project_archive_tasks")
            db.execute(
                """CREATE TABLE project_archive_leases (
                project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                lease_id TEXT NOT NULL UNIQUE,
                operation TEXT NOT NULL,
                created_at TEXT NOT NULL)"""
            )
            db.execute(
                "INSERT INTO project_archive_leases VALUES (?, 'legacy-taskless-freeze', 'archive', ?)",
                (self.project["id"], studio.utc_now()),
            )
            db.commit()

        studio.init_db()
        studio.init_db()
        self.assertEqual(recover_archive_tasks(studio.DB_PATH, self.backup_root), 0)
        self.assertEqual(recover_archive_tasks(studio.DB_PATH, self.backup_root), 0)
        with closing(studio.connect()) as db:
            lease_columns = {row[1] for row in db.execute("PRAGMA table_info(project_archive_leases)")}
            reconciliation = dict(db.execute("SELECT * FROM project_archive_reconciliations").fetchone())
            self.assertIn("task_id", lease_columns)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_reconciliations").fetchone()[0], 1)
        self.assertEqual(reconciliation["reason"], "taskless_lease")
        self.assertEqual(reconciliation["state"], "unresolved")
        self.assertEqual(reconciliation["revision"], 0)

        list_endpoint = self.endpoint("/api/projects/{project_id}/archive-reconciliations", "GET")
        resolve_endpoint = self.endpoint(
            "/api/projects/{project_id}/archive-reconciliations/{reconciliation_id}/resolve", "POST",
        )
        visible = list_endpoint(self.project["id"], include_resolved=True)
        self.assertEqual(visible[0]["lease_id"], "legacy-taskless-freeze")
        workbench_conflicts = studio.get_workbench()["archive_reconciliations"]
        self.assertEqual([item["id"] for item in workbench_conflicts], [reconciliation["id"]])
        self.assertEqual(workbench_conflicts[0]["project_title"], self.project["title"])
        other = studio.create_project(
            studio.ProjectCreate(
                title="隔离项目", episode="EP02", logline="不能解除其他项目冻结", target_duration=5,
                shots=[studio.ShotCreate(title="隔离镜头", description="隔离", prompt="isolated")],
            )
        )
        self.assertEqual(list_endpoint(other["id"], include_resolved=True), [])
        with self.assertRaises(HTTPException) as cross_project:
            resolve_endpoint(
                other["id"], reconciliation["id"],
                ArchiveReconciliationResolve(
                    expected_revision=0,
                    confirmed_by="本机操作员",
                    note="已检查本机归档进程列表并确认没有存活任务",
                    confirm_no_live_archive_process=True,
                ),
            )
        self.assertEqual(cross_project.exception.status_code, 404)
        with self.assertRaises(HTTPException) as unconfirmed:
            resolve_endpoint(
                self.project["id"], reconciliation["id"],
                ArchiveReconciliationResolve(
                    expected_revision=0,
                    confirmed_by="本机操作员",
                    note="尚未完成存活归档进程的人工确认",
                    confirm_no_live_archive_process=False,
                ),
            )
        self.assertEqual(unconfirmed.exception.status_code, 400)
        with self.assertRaises(HTTPException) as stale:
            resolve_endpoint(
                self.project["id"], reconciliation["id"],
                ArchiveReconciliationResolve(
                    expected_revision=99,
                    confirmed_by="本机操作员",
                    note="已检查本机归档进程列表并确认没有存活任务",
                    confirm_no_live_archive_process=True,
                ),
            )
        self.assertEqual(stale.exception.status_code, 409)
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT state FROM project_archive_reconciliations").fetchone()[0], "unresolved")

        payload = ArchiveReconciliationResolve(
            expected_revision=0,
            confirmed_by="本机操作员",
            note="已检查任务管理器与归档日志，确认没有存活归档进程",
            confirm_no_live_archive_process=True,
        )

        def resolve_once() -> tuple[str, int | None]:
            try:
                result = resolve_endpoint(self.project["id"], reconciliation["id"], payload)
                return result["state"], None
            except HTTPException as exc:
                return "error", exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: resolve_once(), range(2)))
        self.assertEqual(sum(1 for state, _ in results if state == "resolved"), 1)
        self.assertEqual(sum(1 for _, status in results if status == 409), 1)
        with closing(studio.connect()) as db:
            resolved = dict(db.execute("SELECT * FROM project_archive_reconciliations").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases").fetchone()[0], 0)
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["revision"], 1)
        self.assertTrue(resolved["confirmed_no_live_process"])
        self.assertEqual(resolved["resolved_by"], "本机操作员")

        archive = create_project_archive(studio.DB_PATH, self.backup_root, self.project["id"])
        self.assertEqual(archive["revision"], 1)
        self.assert_downstream_claims_resume()

    def test_terminal_and_owner_mismatch_leases_remain_frozen_until_exact_manual_resolution(self) -> None:
        terminal = _acquire_archive_task(
            studio.DB_PATH, self.backup_root, self.project["id"], mark_archived=False,
            task_id="archive-task-terminal", lease_id="archive-lease-terminal",
            archive_id="project-archive-terminal", created_at=studio.utc_now(),
        )
        del terminal
        with closing(studio.connect()) as db:
            db.execute(
                """UPDATE project_archive_tasks
                SET state = 'completed', stage = 'completed', completed_at = ?
                WHERE id = 'archive-task-terminal'""",
                (studio.utc_now(),),
            )
            db.commit()
        self.assertEqual(recover_archive_tasks(studio.DB_PATH, self.backup_root), 0)
        terminal_conflict = list_archive_reconciliations(studio.DB_PATH, self.project["id"], include_resolved=False)[0]
        self.assertEqual(terminal_conflict["reason"], "terminal_task_lease")
        self.assertEqual(
            self.endpoint("/api/projects/{project_id}/archive-reconciliations/{reconciliation_id}/resolve", "POST")(
                self.project["id"], terminal_conflict["id"],
                ArchiveReconciliationResolve(
                    expected_revision=terminal_conflict["revision"], confirmed_by="本机操作员",
                    note="已确认终态任务没有对应的存活归档进程",
                    confirm_no_live_archive_process=True,
                ),
            )["state"],
            "resolved",
        )
        with closing(studio.connect()) as db:
            terminal_task = dict(db.execute("SELECT * FROM project_archive_tasks WHERE id = 'archive-task-terminal'").fetchone())
            self.assertEqual(terminal_task["state"], "completed")
            self.assertIn("manual_reconciliation", terminal_task["audit"])

        mismatch_project = studio.create_project(
            studio.ProjectCreate(
                title="owner mismatch", episode="EP03", logline="归档 owner 不一致", target_duration=5,
                shots=[studio.ShotCreate(title="owner mismatch", description="冲突", prompt="mismatch")],
            )
        )
        _acquire_archive_task(
            studio.DB_PATH, self.backup_root, mismatch_project["id"], mark_archived=False,
            task_id="archive-task-mismatch", lease_id="archive-lease-mismatch",
            archive_id="project-archive-mismatch", created_at=studio.utc_now(),
        )
        with closing(studio.connect()) as db:
            db.execute(
                """UPDATE project_archive_tasks
                SET owner_instance = 'dead-task-owner', owner_pid = 2147483000,
                    owner_process_identity = 'win-filetime:111'
                WHERE id = 'archive-task-mismatch'"""
            )
            db.execute(
                "UPDATE project_archive_leases SET owner_instance = 'different-lease-owner' WHERE lease_id = 'archive-lease-mismatch'"
            )
            db.commit()
        with patch.object(archive_module, "_process_identity", return_value=(False, None)):
            self.assertEqual(recover_archive_tasks(studio.DB_PATH, self.backup_root), 0)
        mismatch_conflict = list_archive_reconciliations(
            studio.DB_PATH, mismatch_project["id"], include_resolved=False,
        )[0]
        self.assertEqual(mismatch_conflict["reason"], "task_lease_owner_mismatch")
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT state FROM project_archive_tasks WHERE id = 'archive-task-mismatch'").fetchone()[0], "running")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases WHERE project_id = ?", (mismatch_project["id"],)).fetchone()[0], 1)
        resolved = self.endpoint(
            "/api/projects/{project_id}/archive-reconciliations/{reconciliation_id}/resolve", "POST",
        )(
            mismatch_project["id"], mismatch_conflict["id"],
            ArchiveReconciliationResolve(
                expected_revision=mismatch_conflict["revision"], confirmed_by="本机操作员",
                note="已核对两个 owner 记录并确认没有存活归档进程",
                confirm_no_live_archive_process=True,
            ),
        )
        self.assertEqual(resolved["state"], "resolved")
        with closing(studio.connect()) as db:
            task = dict(db.execute("SELECT * FROM project_archive_tasks WHERE id = 'archive-task-mismatch'").fetchone())
            self.assertEqual(task["state"], "failed")
            self.assertEqual(task["stage"], "reconciled_failed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_archive_leases WHERE project_id = ?", (mismatch_project["id"],)).fetchone()[0], 0)

    def test_owner_identity_requires_same_verified_os_identity_kind_before_pid_reuse_recovery(self) -> None:
        cases = (
            ("fallback-to-os", "foreign-instance", "runtime:123:legacy", (True, "win-filetime:222"), False),
            ("os-to-unreadable", "foreign-instance", "win-filetime:111", (True, None), False),
            ("verified-same", "foreign-instance", "win-filetime:111", (True, "win-filetime:111"), False),
            ("current-owner", archive_module.ARCHIVE_OWNER_INSTANCE, "win-filetime:111", (True, "win-filetime:222"), False),
            ("verified-different", "foreign-instance", "win-filetime:111", (True, "win-filetime:222"), True),
        )
        for index, (label, owner_instance, stored_identity, current_result, should_recover) in enumerate(cases, 1):
            with self.subTest(label=label):
                project = studio.create_project(
                    studio.ProjectCreate(
                        title=f"identity {label}", episode=f"EP{index + 10}", logline="owner identity matrix",
                        target_duration=5,
                        shots=[studio.ShotCreate(title=label, description="identity", prompt="identity")],
                    )
                )
                task_id = f"archive-task-identity-{index}"
                lease_id = f"archive-lease-identity-{index}"
                _acquire_archive_task(
                    studio.DB_PATH, self.backup_root, project["id"], mark_archived=False,
                    task_id=task_id, lease_id=lease_id,
                    archive_id=f"project-archive-identity-{index}", created_at=studio.utc_now(),
                )
                with closing(studio.connect()) as db:
                    for table, key, value in (
                        ("project_archive_tasks", "id", task_id),
                        ("project_archive_leases", "lease_id", lease_id),
                    ):
                        db.execute(
                            f"""UPDATE {table}
                            SET owner_instance = ?, owner_host = ?, owner_pid = 424242,
                                owner_process_identity = ? WHERE {key} = ?""",
                            (owner_instance, archive_module.ARCHIVE_OWNER_HOST, stored_identity, value),
                        )
                    db.commit()
                    before_task = dict(db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone())
                    before_lease = dict(db.execute("SELECT * FROM project_archive_leases WHERE lease_id = ?", (lease_id,)).fetchone())
                with patch.object(archive_module, "_process_identity", return_value=current_result):
                    recovered = recover_archive_tasks(studio.DB_PATH, self.backup_root)
                with closing(studio.connect()) as db:
                    after_task = dict(db.execute("SELECT * FROM project_archive_tasks WHERE id = ?", (task_id,)).fetchone())
                    after_lease_row = db.execute("SELECT * FROM project_archive_leases WHERE lease_id = ?", (lease_id,)).fetchone()
                    reconciliation_count = db.execute(
                        "SELECT COUNT(*) FROM project_archive_reconciliations WHERE project_id = ?", (project["id"],),
                    ).fetchone()[0]
                if should_recover:
                    self.assertEqual(recovered, 1)
                    self.assertEqual(after_task["state"], "failed")
                    self.assertEqual(after_task["stage"], "recovered_failed")
                    self.assertIsNone(after_lease_row)
                else:
                    self.assertEqual(recovered, 0)
                    self.assertEqual(after_task, before_task)
                    self.assertEqual(dict(after_lease_row), before_lease)
                    self.assertEqual(reconciliation_count, 0)
                    _fail_archive_task(studio.DB_PATH, task_id, "identity matrix cleanup")

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
