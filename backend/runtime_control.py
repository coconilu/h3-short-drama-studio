from __future__ import annotations

import json
import os
import time
import uuid
import ctypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUS_MAX_AGE_SECONDS = 8.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def supervisor_status(runtime_root: Path) -> dict[str, Any]:
    status_path = runtime_root / "supervisor-status.json"
    payload = _read_json(status_path)
    if not payload:
        return {
            "state": "not_running",
            "managed": False,
            "status_path": str(status_path.resolve()),
            "message": "当前 API 未由镜场守护进程托管",
            "services": {},
        }

    try:
        updated_epoch = float(payload.get("updated_epoch") or 0)
    except (TypeError, ValueError):
        updated_epoch = 0
    supervisor_pid = payload.get("supervisor_pid")
    fresh = time.time() - updated_epoch <= STATUS_MAX_AGE_SECONDS
    alive = process_alive(supervisor_pid if isinstance(supervisor_pid, int) else None)
    online = fresh and alive
    return {
        **payload,
        "state": "online" if online else "stale",
        "managed": online,
        "status_path": str(status_path.resolve()),
        "message": "守护进程在线" if online else "守护状态已过期，请重新启动镜场",
    }


def request_supervisor_action(runtime_root: Path, service: str, action: str) -> dict[str, Any]:
    status = supervisor_status(runtime_root)
    if status["state"] != "online":
        raise RuntimeError(status["message"])
    service_status = (status.get("services") or {}).get(service) or {}
    if not service_status.get("managed"):
        raise RuntimeError(f"{service} 当前不是守护进程启动的服务，不能从工作台重启")

    runtime_root.mkdir(parents=True, exist_ok=True)
    command_path = runtime_root / "supervisor-command.json"
    temp_path = runtime_root / f"supervisor-command-{uuid.uuid4().hex}.tmp"
    command = {
        "id": uuid.uuid4().hex,
        "action": action,
        "service": service,
        "requested_at": utc_now(),
    }
    temp_path.write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(command_path)
    return command
