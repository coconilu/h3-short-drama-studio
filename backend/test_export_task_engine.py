from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import app as studio


class ExportTaskEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        studio.DB_PATH = root / "studio.db"
        studio.EXPORT_ROOT = root / "exports"
        studio.EXPORT_JOB_ROOT = root / "jobs"
        studio.EXPORT_ROOT.mkdir()
        studio.EXPORT_JOB_ROOT.mkdir()
        studio.init_db()

    def tearDown(self) -> None:
        studio.EXPORT_WAKE_EVENT.clear()
        self.temp_dir.cleanup()

    def insert_run(self, run_id: str, state: str, *, current: int = 0) -> None:
        now = studio.utc_now()
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO export_runs
                (id, project_id, state, message, output_name, width, height, polish_audio,
                 config, outputs, created_at, updated_at, source_snapshot, is_current)
                VALUES (?, 'rain-call-ep01', ?, 'test', ?, 1344, 768, 1, '{}', '{}', ?, ?, ?, ?)""",
                (
                    run_id,
                    state,
                    run_id,
                    now,
                    now,
                    json.dumps({"project_title": "测试", "sources": [{"shot_id": "S1"}]}),
                    current,
                ),
            )
            db.commit()

    def fetch(self, run_id: str) -> dict:
        result = studio.row("SELECT * FROM export_runs WHERE id = ?", (run_id,))
        self.assertIsNotNone(result)
        return result or {}

    def test_recover_orphaned_running_job(self) -> None:
        self.insert_run("recover-me", "导出中")
        with closing(studio.connect()) as db:
            db.execute("UPDATE export_runs SET worker_id = 'dead-worker' WHERE id = 'recover-me'")
            db.commit()

        recovered = studio.recover_export_runs()
        result = self.fetch("recover-me")

        self.assertEqual(recovered, 1)
        self.assertEqual(result["state"], "恢复排队")
        self.assertEqual(result["recovery_count"], 1)
        self.assertIsNone(result["worker_id"])

    def test_cancel_queued_job_then_retry_with_same_snapshot(self) -> None:
        self.insert_run("cancel-me", "排队中")
        cancelled = studio.cancel_export_run("cancel-me")
        retried = studio.retry_export_run("cancel-me")

        self.assertEqual(cancelled["state"], "已取消")
        self.assertTrue(cancelled["can_retry"])
        self.assertEqual(retried["state"], "排队中")
        self.assertEqual(retried["attempt"], 2)
        self.assertEqual(retried["parent_run_id"], "cancel-me")
        self.assertEqual(
            self.fetch(retried["id"])["source_snapshot"],
            self.fetch("cancel-me")["source_snapshot"],
        )

    def test_claim_is_atomic_and_records_worker(self) -> None:
        self.insert_run("claim-me", "排队中")

        claimed = studio.claim_next_export_run()
        second_claim = studio.claim_next_export_run()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], "claim-me")
        self.assertEqual(self.fetch("claim-me")["state"], "导出中")
        self.assertEqual(self.fetch("claim-me")["worker_id"], studio.EXPORT_WORKER_ID)
        self.assertIsNone(second_claim)

    def test_activate_completed_version_is_reversible(self) -> None:
        self.insert_run("version-a", "已完成", current=1)
        self.insert_run("version-b", "已完成")
        (studio.EXPORT_ROOT / "version-a.mp4").write_bytes(b"a")
        (studio.EXPORT_ROOT / "version-b.mp4").write_bytes(b"b")

        activated = studio.activate_export_run("version-b")

        self.assertTrue(activated["is_current"])
        self.assertEqual(studio.current_export_record()[1]["id"], "version-b")
        self.assertEqual(self.fetch("version-a")["is_current"], 0)

    def test_project_creation_and_switching_isolates_workspace_data(self) -> None:
        original = studio.get_project()
        original_asset_ids = {asset["id"] for asset in studio.get_assets()}
        created = studio.create_project(
            studio.ProjectCreate(
                title="电梯十三层",
                episode="EP01",
                logline="深夜电梯停在不存在的十三层。",
                target_duration=18,
                shots=[
                    studio.ShotCreate(
                        title="电梯来电",
                        description="林夏独自进入电梯，楼层灯开始闪烁。",
                        prompt="woman enters an old elevator at midnight, cinematic suspense",
                    ),
                    studio.ShotCreate(
                        title="不存在的楼层",
                        description="显示屏从十二跳到十三，林夏后退。",
                        dialogue="这栋楼没有十三层。",
                        prompt="elevator display changes from 12 to 13, tense close-up",
                    ),
                ],
            )
        )

        self.assertEqual(studio.get_project()["id"], created["id"])
        self.assertEqual(len(studio.get_project()["shots"]), 2)
        self.assertEqual(studio.get_assets(), [])
        self.assertEqual(studio.get_jobs(), [])
        self.assertEqual(studio.get_export_runs(), [])
        self.assertFalse(studio.current_export()["available"])
        preflight = studio.export_preflight()
        self.assertEqual(len(preflight["issues"]), 3)
        self.assertTrue(any(issue["shot_id"] == "delivery-plan" for issue in preflight["issues"]))
        with self.assertRaises(studio.HTTPException) as cross_project:
            studio.get_shot_references(original["shots"][0]["id"])
        self.assertEqual(cross_project.exception.status_code, 404)

        studio.set_active_project(original["id"])
        self.assertEqual(studio.get_project()["id"], original["id"])
        self.assertEqual(len(studio.get_project()["shots"]), 7)
        self.assertEqual({asset["id"] for asset in studio.get_assets()}, original_asset_ids)
        project_rows = studio.get_projects()
        self.assertEqual(len(project_rows), 2)
        self.assertEqual(sum(bool(project["active"]) for project in project_rows), 1)

    def test_batch_dry_run_is_ordered_safe_and_required_before_submit(self) -> None:
        created = studio.create_project(
            studio.ProjectCreate(
                title="批量校验项目",
                episode="EP01",
                logline="验证多镜头安全门。",
                target_duration=12,
                shots=[
                    studio.ShotCreate(title="第一镜", description="人物进入房间。", prompt="person enters a room"),
                    studio.ShotCreate(title="第二镜", description="灯光突然熄灭。", prompt="room lights suddenly go dark"),
                ],
            )
        )
        first, second = created["shots"]
        request = studio.BatchGenerationRequest(shot_ids=[second["id"], first["id"]])
        with patch.object(studio, "run_h3", return_value=SimpleNamespace(stdout="{}")):
            result = studio.batch_dry_run(request)

        self.assertTrue(result["ok"])
        self.assertFalse(result["gpu_submitted"])
        self.assertEqual([item["shot_id"] for item in result["results"]], [first["id"], second["id"]])
        self.assertEqual(len(studio.get_jobs()), 2)

        studio.update_shot(first["id"], studio.ShotPatch(prompt="person enters a darker room"))
        with self.assertRaises(studio.HTTPException) as stale_validation:
            studio.batch_submit(studio.BatchGenerationRequest(shot_ids=[first["id"], second["id"]], confirm=True))
        self.assertEqual(stale_validation.exception.status_code, 409)

        with self.assertRaises(studio.HTTPException) as no_confirmation:
            studio.batch_submit(studio.BatchGenerationRequest(shot_ids=[first["id"]], confirm=False))
        self.assertEqual(no_confirmation.exception.status_code, 400)

    def test_workspace_settings_persist_and_merge_defaults(self) -> None:
        initial = studio.get_settings()
        self.assertEqual(initial["default_landing_page"], "projects")
        self.assertIn("runtime", initial)

        updated = studio.update_settings(
            studio.WorkspaceSettingsPatch(
                sidebar_collapsed=True,
                density="compact",
                default_export_width=1920,
                default_export_height=1080,
                polish_audio=False,
            )
        )

        self.assertTrue(updated["sidebar_collapsed"])
        self.assertEqual(updated["density"], "compact")
        self.assertEqual(updated["default_export_width"], 1920)
        self.assertFalse(studio.get_settings()["polish_audio"])

    def test_global_workbench_uses_real_cross_project_records(self) -> None:
        created = studio.create_project(
            studio.ProjectCreate(
                title="工作台空态项目",
                episode="EP02",
                logline="没有封面的真实筹备项目。",
                target_duration=10,
                shots=[studio.ShotCreate(title="空镜", description="空走廊等待人物出现。", prompt="empty corridor")],
            )
        )
        with closing(studio.connect()) as db:
            cover = db.execute(
                """SELECT candidates.id FROM candidates JOIN shots ON shots.id = candidates.shot_id
                WHERE shots.project_id = 'rain-call-ep01' ORDER BY shots.ordinal, candidates.id LIMIT 1"""
            ).fetchone()
            self.assertIsNotNone(cover)
            db.execute("UPDATE candidates SET selected = 0 WHERE shot_id IN (SELECT id FROM shots WHERE project_id = 'rain-call-ep01')")
            db.execute("UPDATE candidates SET selected = 1, thumbnail_file = 'cover.jpg' WHERE id = ?", (cover["id"],))
            db.commit()

        workbench = studio.get_workbench()
        summary = workbench["summary"]
        created_summary = next(project for project in workbench["projects"] if project["id"] == created["id"])
        original_summary = next(project for project in workbench["projects"] if project["id"] == "rain-call-ep01")

        self.assertEqual(summary["project_count"], 2)
        self.assertEqual(created_summary["phase"], "筹备中")
        self.assertIsNone(created_summary["thumbnail"])
        self.assertTrue(created_summary["active"])
        self.assertEqual(original_summary["thumbnail"], "/api/projects/rain-call-ep01/thumbnail")
        self.assertIn("storage", workbench)

    def test_acceptance_tracks_are_separate_and_reports_are_immutable(self) -> None:
        report = studio.get_production_acceptance()
        self.assertIn("generation", report)
        self.assertIn("delivery", report)
        self.assertEqual({stage["status"] for stage in report["generation"]["stages"]} - {"pass", "warn", "block"}, set())
        self.assertFalse(report["delivery"]["ready"])

        frozen = studio.run_production_acceptance()
        self.assertEqual(len(frozen["latest_run"]["report_hash"]), 64)
        download = studio.download_production_acceptance()
        payload = json.loads(download.body)
        self.assertEqual(payload["id"], frozen["latest_run"]["id"])
        self.assertEqual(payload["report_hash"], frozen["latest_run"]["report_hash"])

    def test_delivery_signoffs_are_append_only_and_require_a_complete_film(self) -> None:
        request = studio.DeliverySignoffRequest(
            category="sound", decision="pass", note="完整听审通过", source="unit-test"
        )
        with patch.object(studio, "current_export", return_value={"available": False}):
            with self.assertRaises(studio.HTTPException) as unavailable:
                studio.create_delivery_signoff(request)
        self.assertEqual(unavailable.exception.status_code, 409)

        fake_export = {
            "available": True, "has_audio": True, "width": 1344, "height": 768,
            "duration_seconds": 10.0, "shot_count": 7, "updated_at": studio.utc_now(),
        }
        with patch.object(studio, "current_export", return_value=fake_export):
            studio.create_delivery_signoff(request)
            studio.create_delivery_signoff(request)
        revisions = studio.rows(
            "SELECT revision FROM delivery_signoffs WHERE category = 'sound' ORDER BY revision", ()
        )
        self.assertEqual([item["revision"] for item in revisions], [1, 2])


if __name__ == "__main__":
    unittest.main()
