from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as studio
from backend.hd_delivery import HDRunnerError


class HDFFmpegIntegrationTests(unittest.TestCase):
    """Exercise the non-GPU HD path with real, tiny audio/video media."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.previous_output_root = studio.COMFY_OUTPUT_ROOT
        studio.COMFY_OUTPUT_ROOT = self.root / "comfy-output"
        studio.COMFY_OUTPUT_ROOT.mkdir()
        self.source = studio.COMFY_OUTPUT_ROOT / "source-608x352.mp4"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x182033:s=608x352:r=24:d=0.75",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=0.75",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(self.source),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode:
            self.fail(completed.stderr)

    def tearDown(self) -> None:
        studio.COMFY_OUTPUT_ROOT = self.previous_output_root
        self.temp_dir.cleanup()

    def test_real_deterministic_scale_is_landscape_hd_with_audio_and_zero_gpu(self) -> None:
        request = {
            "strategy_type": "deterministic_scale",
            "target_width": 1344,
            "target_height": 768,
            "plan_hash": "d" * 64,
            "expected_artifact_id": "real-scale-fixture",
            "source": {
                "shot_id": "fixture-shot",
                "h3_project": "controlled-fixture",
                "prompt": "not sent to a model",
                "media": {"path": str(self.source)},
            },
        }

        dry_run = studio.run_hd_operation(request, True)
        self.assertFalse(dry_run["gpu_submitted"])
        self.assertEqual(dry_run["adapter"], "ffmpeg")
        self.assertEqual(dry_run["workflow_id"], "deterministic-scale-contain-v1")
        self.assertFalse(Path(dry_run["command"]["output"]).exists())

        attempt_root = studio.COMFY_OUTPUT_ROOT / "hd-delivery" / "attempts" / "real-attempt" / "output"
        result = studio.run_hd_operation({**request, "attempt_output_root": str(attempt_root)}, False)
        self.assertFalse(result["gpu_submitted"])
        output = Path(result["output_file"])
        self.assertTrue(output.is_file())
        probe = studio.probe_media(output)
        self.assertEqual((probe["width"], probe["height"]), (1344, 768))
        self.assertEqual(probe["has_audio"], 1)
        self.assertGreater(probe["duration_seconds"], 0.5)

    def test_project_slug_traversal_is_rejected_before_dry_run_or_process_side_effects(self) -> None:
        request = {
            "strategy_type": "deterministic_scale",
            "target_width": 1344,
            "target_height": 768,
            "plan_hash": "d" * 64,
            "expected_artifact_id": "safe-artifact",
            "source": {
                "shot_id": "fixture-shot",
                "h3_project": "../outside",
                "prompt": "must never reach an adapter",
                "media": {"path": str(self.source)},
            },
        }
        outside = studio.COMFY_OUTPUT_ROOT.parent / "outside"
        with patch.object(studio.subprocess, "run") as process:
            with self.assertRaises(HDRunnerError) as dry_run:
                studio.run_hd_operation(request, True)
            self.assertFalse(dry_run.exception.process_started)
            with self.assertRaises(HDRunnerError) as actual:
                studio.run_hd_operation(
                    {
                        **request,
                        "attempt_output_root": str(
                            studio.COMFY_OUTPUT_ROOT / "hd-delivery" / "attempts" / "safe-attempt" / "output"
                        ),
                    },
                    False,
                )
            self.assertFalse(actual.exception.process_started)
            process.assert_not_called()
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
