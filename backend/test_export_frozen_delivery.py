from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import app as studio
from backend.delivery_plan import (
    DeliveryPlanItemInput,
    DeliveryPlanPatch,
    init_delivery_schema,
    lock_delivery_plan,
    save_delivery_plan,
)


class FrozenDeliveryExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "studio.db"
        self.video = self.root / "candidate.mp4"
        self.video.write_bytes(b"controlled-frozen-video")
        self.original_db = studio.DB_PATH
        self.original_output = studio.COMFY_OUTPUT_ROOT
        self.original_export_root = studio.EXPORT_ROOT
        self.original_export_job_root = studio.EXPORT_JOB_ROOT
        studio.DB_PATH = self.db_path
        studio.COMFY_OUTPUT_ROOT = self.root.resolve()
        studio.EXPORT_ROOT = (self.root / "exports").resolve()
        studio.EXPORT_JOB_ROOT = (self.root / "export-jobs").resolve()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE projects (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, episode TEXT NOT NULL,
                  logline TEXT NOT NULL, target_duration REAL NOT NULL, created_at TEXT NOT NULL,
                  archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE shots (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
                  scene_code TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, prompt TEXT NOT NULL,
                  dialogue TEXT NOT NULL, seconds REAL NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                  status TEXT NOT NULL, subtitle_enabled INTEGER NOT NULL, subtitle_start_seconds REAL, updated_at TEXT NOT NULL
                );
                CREATE TABLE candidates (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL, selected INTEGER NOT NULL, archived INTEGER NOT NULL,
                  external_id TEXT, prompt_id TEXT, status TEXT NOT NULL, output_file TEXT, source TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE candidate_master_versions (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, shot_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                  revision INTEGER NOT NULL, review_id TEXT NOT NULL, review_revision INTEGER NOT NULL,
                  candidate_snapshot TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE creative_storyboard_links (
                  shot_id TEXT PRIMARY KEY, section_id TEXT NOT NULL, last_synced_revision INTEGER NOT NULL
                );
                CREATE TABLE creative_sections (id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE export_runs (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
                  output_name TEXT NOT NULL UNIQUE, width INTEGER NOT NULL, height INTEGER NOT NULL,
                  polish_audio INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}',
                  outputs TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  completed_at TEXT, error TEXT, attempt INTEGER NOT NULL DEFAULT 1, parent_run_id TEXT,
                  cancel_requested INTEGER NOT NULL DEFAULT 0, started_at TEXT, worker_id TEXT,
                  recovery_count INTEGER NOT NULL DEFAULT 0, source_snapshot TEXT NOT NULL DEFAULT '{}',
                  is_current INTEGER NOT NULL DEFAULT 0, log_file TEXT
                );
                CREATE TABLE export_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES export_runs(id) ON DELETE CASCADE,
                  level TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', 'logline', 60, 'now', 0)")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute(
                "INSERT INTO shots VALUES ('s1', 'p1', 1, 'S01', '开场', '人物走入', 'cinematic prompt', '不要进来', 2, 608, 352, '草稿已选', 1, 0.3, 'now')"
            )
            db.execute(
                "INSERT INTO candidates VALUES ('c1', 's1', 1, 0, 'draft-1', 'prompt-1', 'completed', ?, 'h3', 'now')",
                (str(self.video),),
            )
            media = {
                "output_file": str(self.video.resolve()), "size_bytes": self.video.stat().st_size,
                "checksum_sha256": hashlib.sha256(self.video.read_bytes()).hexdigest(),
            }
            db.execute(
                "INSERT INTO candidate_master_versions VALUES ('m1', 'p1', 's1', 'c1', 1, 'review-1', 2, ?, 'now')",
                (json.dumps({**media, "trace": {"prompt": "cinematic prompt", "seed": 42}}),),
            )
            db.execute("INSERT INTO creative_storyboard_links VALUES ('s1', 'section-1', 3)")
            db.execute("INSERT INTO creative_sections VALUES ('section-1', 7)")
            init_delivery_schema(db)
            db.commit()
        saved = save_delivery_plan(
            self.db_path,
            DeliveryPlanPatch(base_revision=0, items=[DeliveryPlanItemInput(
                shot_id="s1", subtitle_enabled=True, subtitle_start_seconds=0.3,
                in_point_seconds=0.25, out_point_seconds=1.25, dialogue_mode="mute",
            )]),
            self.root,
        )
        lock_delivery_plan(self.db_path, saved["plan"]["revision"], self.root)

    def tearDown(self) -> None:
        studio.DB_PATH = self.original_db
        studio.COMFY_OUTPUT_ROOT = self.original_output
        studio.EXPORT_ROOT = self.original_export_root
        studio.EXPORT_JOB_ROOT = self.original_export_job_root
        self.temp_dir.cleanup()

    @staticmethod
    def probe(_: Path) -> dict:
        return {"width": 608, "height": 352, "duration_seconds": 2.0, "has_audio": 1}

    @staticmethod
    def staged_snapshot(run_id: str) -> dict:
        return json.loads(
            (studio.export_staging_root(run_id) / "request.json").read_text(encoding="utf-8")
        )

    def test_export_uses_frozen_candidate_checksum_and_edit_controls(self) -> None:
        with patch("backend.app.probe_media", side_effect=self.probe):
            result = studio.export_preflight_payload(include_private=True)
        self.assertTrue(result["ready"])
        source = result["sources"][0]
        self.assertEqual(source["source_id"], "c1")
        self.assertEqual((source["in_point_seconds"], source["out_point_seconds"]), (0.25, 1.25))
        self.assertEqual(source["duration_seconds"], 1.0)
        self.assertEqual(source["dialogue_mode"], "mute")
        self.assertEqual(source["dialogue"], "不要进来")
        self.assertTrue(source["subtitle_enabled"])
        self.assertEqual(source["section_revision"], 7)
        self.assertEqual(source["checksum_sha256"], hashlib.sha256(self.video.read_bytes()).hexdigest())
        self.assertEqual(result["delivery_plan"]["plan_hash"], result["delivery_plan"]["calculated_plan_hash"])
        self.assertEqual(len(result["delivery_plan"]["assembly_snapshot_hash"]), 64)

    def test_current_selection_change_does_not_replace_frozen_source(self) -> None:
        other = self.root / "other.mp4"
        other.write_bytes(b"new-current-candidate")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE candidates SET selected = 0 WHERE id = 'c1'")
            db.execute(
                "INSERT INTO candidates VALUES ('c2', 's1', 1, 0, 'draft-2', 'prompt-2', 'completed', ?, 'h3', 'later')",
                (str(other),),
            )
            db.commit()
        with patch("backend.app.probe_media", side_effect=self.probe):
            result = studio.export_preflight_payload(include_private=True)
        self.assertTrue(result["ready"])
        self.assertEqual(result["sources"][0]["source_id"], "c1")

    def test_replaced_frozen_media_fails_closed(self) -> None:
        self.video.write_bytes(b"tampered-frozen-video")
        with patch("backend.app.probe_media", side_effect=self.probe):
            result = studio.export_preflight_payload(include_private=True)
        self.assertFalse(result["ready"])
        self.assertEqual(result["ready_shot_count"], 0)
        self.assertIn("SHA256", result["issues"][0]["message"])

    def test_source_changed_while_copying_to_staging_fails_and_cleans_private_copy(self) -> None:
        with patch("backend.app.probe_media", side_effect=self.probe):
            snapshot = studio.export_preflight_payload(include_private=True)
        original_hash = studio.file_sha256
        source_calls = 0

        def mutate_after_first_source_hash(path: Path) -> str:
            nonlocal source_calls
            result = original_hash(path)
            if path.resolve() == self.video.resolve():
                source_calls += 1
                if source_calls == 1:
                    self.video.write_bytes(b"changed-between-copy-hash-boundaries")
            return result

        with patch("backend.app.file_sha256", side_effect=mutate_after_first_source_hash):
            with self.assertRaisesRegex(RuntimeError, "staging 复制期间发生变化"):
                studio.stage_export_sources("copy-race", snapshot)
        self.assertFalse(studio.export_staging_root("copy-race").exists())

    def test_locked_item_or_plan_hash_tampering_fails_preflight_and_create_atomically(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE delivery_plan_items SET dialogue_mode = 'original'")
            db.commit()
        with patch("backend.app.probe_media", side_effect=self.probe):
            preflight = studio.export_preflight_payload(include_private=True)
        self.assertFalse(preflight["ready"])
        self.assertIn("plan_hash", " ".join(issue["message"] for issue in preflight["issues"]))

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE delivery_plan_items SET dialogue_mode = 'mute'")
            db.commit()
        original_integrity = studio._assembly_integrity
        calls = 0

        def tamper_after_preflight(assembly: dict) -> dict:
            nonlocal calls
            result = original_integrity(assembly)
            calls += 1
            if calls == 1:
                with closing(sqlite3.connect(self.db_path)) as db:
                    db.execute("UPDATE delivery_plan_items SET subtitle_start_seconds = 0.4")
                    db.commit()
            return result

        with patch("backend.app.probe_media", side_effect=self.probe), patch(
            "backend.app._assembly_integrity", side_effect=tamper_after_preflight,
        ):
            with self.assertRaises(Exception):
                studio.create_export(studio.ExportRequest(width=608, height=352, polish_audio=False))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM export_runs").fetchone()[0], 0)

    def test_worker_start_revalidates_locked_assembly_before_ffmpeg(self) -> None:
        with patch("backend.app.probe_media", side_effect=self.probe):
            queued = studio.create_export(studio.ExportRequest(width=608, height=352, polish_audio=False))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE delivery_plan_items SET dialogue_mode = 'original'")
            db.commit()
        claimed = studio.claim_next_export_run()
        self.assertEqual(claimed["id"], queued["id"])
        with patch("backend.app.subprocess.Popen") as popen:
            studio.run_export_job(queued["id"])
        popen.assert_not_called()
        with closing(sqlite3.connect(self.db_path)) as db:
            failed = db.execute("SELECT state, is_current FROM export_runs WHERE id = ?", (queued["id"],)).fetchone()
        self.assertEqual(failed[0], "失败")
        self.assertEqual(failed[1], 0)

    def test_original_media_replaced_after_staging_does_not_change_render_input(self) -> None:
        with patch("backend.app.probe_media", side_effect=self.probe):
            queued = studio.create_export(studio.ExportRequest(width=608, height=352, polish_audio=False))
        claimed = studio.claim_next_export_run()
        self.assertEqual(claimed["id"], queued["id"])
        run = studio.row("SELECT * FROM export_runs WHERE id = ?", (queued["id"],))
        snapshot = json.loads(run["source_snapshot"])
        stem = run["output_name"]

        class SuccessfulRender:
            pid = 4242
            returncode = 0

            def __init__(self, test: "FrozenDeliveryExportTests") -> None:
                self.test = test
                self.finished = False

            def poll(self) -> int:
                if not self.finished:
                    staged = self.test.staged_snapshot(queued["id"])
                    studio.EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
                    for suffix in (".mp4", ".srt", ".vtt"):
                        (studio.EXPORT_ROOT / f"{stem}{suffix}").write_bytes(b"controlled-output")
                    (studio.EXPORT_ROOT / f"{stem}.sources.json").write_text(
                        json.dumps(staged["sources"], ensure_ascii=False), encoding="utf-8",
                    )
                    self.test.video.write_bytes(b"replaced-during-render-with-different-content")
                    self.finished = True
                return 0

        fake = SuccessfulRender(self)
        with patch("backend.app.subprocess.Popen", return_value=fake), patch(
            "backend.app.probe_media", side_effect=self.probe,
        ):
            studio.run_export_job(queued["id"])
        with closing(sqlite3.connect(self.db_path)) as db:
            completed = db.execute("SELECT state, is_current, outputs FROM export_runs WHERE id = ?", (queued["id"],)).fetchone()
        self.assertEqual(completed[0], "已完成")
        self.assertEqual(completed[1], 1)
        manifest = json.loads((studio.EXPORT_ROOT / f"{stem}.production.json").read_text(encoding="utf-8"))
        frozen_sha = snapshot["sources"][0]["checksum_sha256"]
        self.assertEqual(manifest["inputs"][0]["original"]["checksum_sha256"], frozen_sha)
        self.assertEqual(manifest["inputs"][0]["staged"]["checksum_sha256"], frozen_sha)
        self.assertNotEqual(manifest["inputs"][0]["original"]["path"], manifest["inputs"][0]["staged"]["path"])

    def test_original_replaced_after_last_external_check_still_publishes_staged_render(self) -> None:
        with patch("backend.app.probe_media", side_effect=self.probe):
            queued = studio.create_export(studio.ExportRequest(width=608, height=352, polish_audio=False))
        self.assertEqual(studio.claim_next_export_run()["id"], queued["id"])
        run = studio.row("SELECT * FROM export_runs WHERE id = ?", (queued["id"],))
        snapshot = json.loads(run["source_snapshot"])
        stem = run["output_name"]

        class SuccessfulRender:
            pid = 4244
            returncode = 0

            def __init__(self) -> None:
                self.finished = False

            def poll(self) -> int:
                if not self.finished:
                    staged = FrozenDeliveryExportTests.staged_snapshot(queued["id"])
                    studio.EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
                    for suffix in (".mp4", ".srt", ".vtt"):
                        (studio.EXPORT_ROOT / f"{stem}{suffix}").write_bytes(b"controlled-output")
                    (studio.EXPORT_ROOT / f"{stem}.sources.json").write_text(
                        json.dumps(staged["sources"], ensure_ascii=False), encoding="utf-8",
                    )
                    self.finished = True
                return 0

        original_verify = studio._verify_frozen_export_sources
        verify_calls = 0

        def replace_after_third_verify(current_snapshot: dict, rendered_sources=None) -> None:
            nonlocal verify_calls
            verify_calls += 1
            original_verify(current_snapshot, rendered_sources)
            if verify_calls == 3:
                self.video.write_bytes(b"replacement-after-last-external-source-check")

        with patch("backend.app.subprocess.Popen", return_value=SuccessfulRender()), patch(
            "backend.app._verify_frozen_export_sources", side_effect=replace_after_third_verify,
        ), patch("backend.app.probe_media", side_effect=self.probe):
            studio.run_export_job(queued["id"])
        self.assertEqual(verify_calls, 4, "最终发布事务必须再次复核 staged path/SHA")
        with closing(sqlite3.connect(self.db_path)) as db:
            completed = db.execute(
                "SELECT state, is_current, outputs FROM export_runs WHERE id = ?", (queued["id"],),
            ).fetchone()
        self.assertEqual(completed[0], "已完成")
        self.assertEqual(completed[1], 1)
        self.assertTrue((studio.EXPORT_ROOT / f"{stem}.production.json").exists())

    def test_staged_source_tampered_after_last_external_check_fails_without_publish(self) -> None:
        with patch("backend.app.probe_media", side_effect=self.probe):
            queued = studio.create_export(studio.ExportRequest(width=608, height=352, polish_audio=False))
        self.assertEqual(studio.claim_next_export_run()["id"], queued["id"])
        run = studio.row("SELECT * FROM export_runs WHERE id = ?", (queued["id"],))
        stem = run["output_name"]

        class SuccessfulRender:
            pid = 4245
            returncode = 0
            finished = False

            def poll(self) -> int:
                if not self.finished:
                    staged = FrozenDeliveryExportTests.staged_snapshot(queued["id"])
                    studio.EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
                    for suffix in (".mp4", ".srt", ".vtt"):
                        (studio.EXPORT_ROOT / f"{stem}{suffix}").write_bytes(b"controlled-output")
                    (studio.EXPORT_ROOT / f"{stem}.sources.json").write_text(
                        json.dumps(staged["sources"], ensure_ascii=False), encoding="utf-8",
                    )
                    self.finished = True
                return 0

        original_verify = studio._verify_frozen_export_sources
        verify_calls = 0

        def tamper_staged_after_external_verify(current_snapshot: dict, rendered_sources=None) -> None:
            nonlocal verify_calls
            verify_calls += 1
            original_verify(current_snapshot, rendered_sources)
            if verify_calls == 3:
                staged_path = Path(current_snapshot["sources"][0]["path"])
                staged_path.chmod(0o666)
                staged_path.write_bytes(b"tampered-private-staging")

        with patch("backend.app.subprocess.Popen", return_value=SuccessfulRender()), patch(
            "backend.app._verify_frozen_export_sources", side_effect=tamper_staged_after_external_verify,
        ), patch("backend.app.probe_media", side_effect=self.probe):
            studio.run_export_job(queued["id"])
        self.assertEqual(verify_calls, 4)
        with closing(sqlite3.connect(self.db_path)) as db:
            failed = db.execute(
                "SELECT state, is_current, outputs FROM export_runs WHERE id = ?", (queued["id"],),
            ).fetchone()
        self.assertEqual(failed[0], "失败")
        self.assertEqual(failed[1], 0)
        self.assertEqual(json.loads(failed[2]), {})
        self.assertFalse((studio.EXPORT_ROOT / f"{stem}.production.json").exists())
        self.assertFalse(studio.export_staging_root(queued["id"]).exists())

    def _assert_delivery_mutation_during_render_fails(self, sql: str) -> None:
        with patch("backend.app.probe_media", side_effect=self.probe):
            queued = studio.create_export(studio.ExportRequest(width=608, height=352, polish_audio=False))
        self.assertEqual(studio.claim_next_export_run()["id"], queued["id"])
        run = studio.row("SELECT * FROM export_runs WHERE id = ?", (queued["id"],))
        snapshot = json.loads(run["source_snapshot"])
        stem = run["output_name"]

        class SuccessfulRenderWithDeliveryMutation:
            pid = 4343
            returncode = 0

            def __init__(self, test: "FrozenDeliveryExportTests") -> None:
                self.test = test
                self.finished = False

            def poll(self) -> int:
                if not self.finished:
                    staged = self.test.staged_snapshot(queued["id"])
                    studio.EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
                    for suffix in (".mp4", ".srt", ".vtt"):
                        (studio.EXPORT_ROOT / f"{stem}{suffix}").write_bytes(b"controlled-output")
                    (studio.EXPORT_ROOT / f"{stem}.sources.json").write_text(
                        json.dumps(staged["sources"], ensure_ascii=False), encoding="utf-8",
                    )
                    with closing(sqlite3.connect(self.test.db_path)) as db:
                        db.execute(sql)
                        db.commit()
                    self.finished = True
                return 0

        with patch("backend.app.subprocess.Popen", return_value=SuccessfulRenderWithDeliveryMutation(self)):
            studio.run_export_job(queued["id"])
        with closing(sqlite3.connect(self.db_path)) as db:
            failed = db.execute(
                "SELECT state, is_current, outputs FROM export_runs WHERE id = ?", (queued["id"],),
            ).fetchone()
        self.assertEqual(failed[0], "失败")
        self.assertEqual(failed[1], 0)
        self.assertEqual(json.loads(failed[2]), {})
        self.assertFalse((studio.EXPORT_ROOT / f"{stem}.production.json").exists())

    def test_delivery_item_change_after_render_fails_before_publish(self) -> None:
        self._assert_delivery_mutation_during_render_fails(
            "UPDATE delivery_plan_items SET dialogue_mode = 'original'",
        )

    def test_delivery_status_change_after_render_fails_before_publish(self) -> None:
        self._assert_delivery_mutation_during_render_fails(
            "UPDATE delivery_plans SET status = 'draft'",
        )

    def test_delivery_revision_change_after_render_fails_before_publish(self) -> None:
        self._assert_delivery_mutation_during_render_fails(
            "UPDATE delivery_plans SET revision = revision + 1",
        )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("powershell.exe"), "requires FFmpeg and Windows PowerShell")
    def test_export_script_applies_trim_mute_and_frozen_subtitle(self) -> None:
        source = self.root / "real-source.mp4"
        generated = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=320x180:d=2:r=24",
                "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        snapshot = self.root / "export-request.json"
        snapshot.write_text(json.dumps({
            "project_title": "冻结导出测试",
            "sources": [{
                "shot_id": "s1", "ordinal": 1, "title": "trim and mute", "dialogue": "冻结字幕文本",
                "source_type": "candidate", "source_id": "c1", "source_detail": "h3", "path": str(source),
                "checksum_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "in_point_seconds": 0.5, "out_point_seconds": 1.5, "dialogue_mode": "mute",
                "subtitle_enabled": True, "subtitle_start_seconds": 0.1,
            }],
        }, ensure_ascii=False), encoding="utf-8")
        output_root = self.root / "exports"
        completed = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str((Path(__file__).resolve().parents[1] / "scripts" / "export-roughcut.ps1")),
                "-OutputName", "frozen-controls", "-OutputRoot", str(output_root),
                "-SourceSnapshotPath", str(snapshot), "-Width", "320", "-Height", "192", "-RequireSelectedSources",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        output = output_root / "frozen-controls.mp4"
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(output)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        self.assertAlmostEqual(float(probe.stdout.strip()), 1.0, delta=0.08)
        self.assertIn("冻结字幕文本", (output_root / "frozen-controls.srt").read_text(encoding="utf-8-sig"))
        manifest = json.loads((output_root / "frozen-controls.sources.json").read_text(encoding="utf-8"))
        record = manifest[0] if isinstance(manifest, list) else manifest
        self.assertEqual((record["in_point_seconds"], record["out_point_seconds"]), (0.5, 1.5))
        self.assertEqual(record["dialogue_mode"], "mute")
        volume = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(output), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        measured = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", volume.stderr)
        self.assertIsNotNone(measured)
        self.assertLessEqual(float(measured.group(1)), -90.0)


if __name__ == "__main__":
    unittest.main()
