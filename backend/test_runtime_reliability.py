from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.runtime_control import request_supervisor_action, supervisor_status
from scripts import studio_supervisor


class RuntimeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_status(self, *, updated_epoch: float, managed: bool = True) -> None:
        (self.runtime_root / "supervisor-status.json").write_text(
            json.dumps(
                {
                    "supervisor_pid": os.getpid(),
                    "updated_epoch": updated_epoch,
                    "services": {"api": {"managed": managed, "state": "online", "port": 8765}},
                }
            ),
            encoding="utf-8",
        )

    def test_missing_status_is_explicitly_not_running(self) -> None:
        status = supervisor_status(self.runtime_root)
        self.assertEqual(status["state"], "not_running")
        self.assertFalse(status["managed"])
        self.assertIn("未由", status["message"])

    def test_fresh_live_status_is_online(self) -> None:
        self.write_status(updated_epoch=time.time())
        status = supervisor_status(self.runtime_root)
        self.assertEqual(status["state"], "online")
        self.assertTrue(status["managed"])

    def test_old_status_is_stale_even_when_pid_exists(self) -> None:
        self.write_status(updated_epoch=time.time() - 30)
        status = supervisor_status(self.runtime_root)
        self.assertEqual(status["state"], "stale")
        self.assertFalse(status["managed"])

    def test_restart_request_is_atomic_and_bounded_to_managed_service(self) -> None:
        self.write_status(updated_epoch=time.time())
        command = request_supervisor_action(self.runtime_root, "api", "restart")
        stored = json.loads((self.runtime_root / "supervisor-command.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, command)
        self.assertEqual(stored["service"], "api")
        self.assertEqual(stored["action"], "restart")

        self.write_status(updated_epoch=time.time(), managed=False)
        with self.assertRaisesRegex(RuntimeError, "不是守护进程"):
            request_supervisor_action(self.runtime_root, "api", "restart")

    def test_supervisor_atomic_status_write_is_valid_json(self) -> None:
        path = self.runtime_root / "status.json"
        self.assertTrue(studio_supervisor.atomic_json(path, {"version": 1, "message": "镜场"}))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["message"], "镜场")

    def test_supervisor_status_write_survives_windows_sharing_violation(self) -> None:
        path = self.runtime_root / "status.json"
        path.write_text('{"version": 1}', encoding="utf-8")
        with patch.object(Path, "replace", side_effect=PermissionError("busy")):
            result = studio_supervisor.atomic_json(path, {"version": 2}, attempts=2)
        self.assertFalse(result)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)


if __name__ == "__main__":
    unittest.main()
