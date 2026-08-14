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
        studio.DB_PATH = self.db_path
        studio.COMFY_OUTPUT_ROOT = self.root.resolve()
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
        self.temp_dir.cleanup()

    @staticmethod
    def probe(_: Path) -> dict:
        return {"width": 608, "height": 352, "duration_seconds": 2.0, "has_audio": 1}

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
