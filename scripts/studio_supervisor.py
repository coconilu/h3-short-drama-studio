from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "runtime"
STATUS_PATH = RUNTIME_ROOT / "supervisor-status.json"
COMMAND_PATH = RUNTIME_ROOT / "supervisor-command.json"
STOP_REQUESTED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def process_alive(pid: int) -> bool:
    if pid <= 0:
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
    except OSError:
        return False
    return True


def resolve_python() -> Path:
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    if not candidate.is_file():
        raise RuntimeError(f"Python 虚拟环境不存在：{candidate}。请先运行 scripts\\setup.ps1")
    return candidate


def resolve_node() -> Path:
    located = shutil.which("node.exe") or shutil.which("node")
    if not located:
        raise RuntimeError("未找到 Node.js。请先运行 scripts\\setup.ps1")
    return Path(located)


@dataclass
class Service:
    name: str
    port: int
    command: list[str]
    cwd: Path
    log_path: Path
    process: subprocess.Popen[str] | None = None
    log_handle: IO[str] | None = None
    managed: bool = False
    external: bool = False
    restarts: int = 0
    state: str = "starting"
    last_exit_code: int | None = None
    next_start_at: float = 0
    started_at: str | None = None

    def public(self) -> dict[str, Any]:
        pid = self.process.pid if self.process and self.process.poll() is None else None
        return {
            "state": self.state,
            "pid": pid,
            "managed": self.managed,
            "external": self.external,
            "port": self.port,
            "restarts": self.restarts,
            "started_at": self.started_at,
            "last_exit_code": self.last_exit_code,
            "log_path": str(self.log_path.resolve()),
        }

    def start(self) -> None:
        if port_open("127.0.0.1", self.port):
            self.external = True
            self.managed = False
            self.state = "external"
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
        self.log_handle.write(f"\n[{utc_now()}] starting {' '.join(self.command)}\n")
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creation_flags,
        )
        self.managed = True
        self.external = False
        self.state = "starting"
        self.started_at = utc_now()

    def stop(self) -> None:
        process = self.process
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
            else:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
        self.process = None
        self.managed = False
        self.external = False
        self.state = "stopped"
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None

    def restart(self) -> None:
        if not self.managed:
            return
        self.stop()
        self.restarts += 1
        self.next_start_at = time.time() + 0.5
        self.state = "restarting"

    def tick(self) -> None:
        if self.external:
            if port_open("127.0.0.1", self.port):
                self.state = "external"
                return
            self.external = False
            self.state = "restarting"
            self.next_start_at = time.time()

        if self.process:
            exit_code = self.process.poll()
            if exit_code is None:
                self.state = "online" if port_open("127.0.0.1", self.port) else "starting"
                return
            self.last_exit_code = exit_code
            self.process = None
            self.managed = False
            if self.log_handle:
                self.log_handle.write(f"[{utc_now()}] exited with code {exit_code}\n")
                self.log_handle.close()
                self.log_handle = None
            self.restarts += 1
            self.next_start_at = time.time() + min(10, max(1, self.restarts * 2))
            self.state = "restarting"

        if self.state in {"starting", "restarting", "stopped"} and time.time() >= self.next_start_at:
            self.start()


def atomic_json(path: Path, payload: dict[str, Any], *, attempts: int = 8) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(attempts):
        try:
            temp.replace(path)
            return True
        except PermissionError:
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def supervisor_already_running() -> bool:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        pid = int(payload.get("supervisor_pid") or 0)
        updated_epoch = float(payload.get("updated_epoch") or 0)
        if pid <= 0 or time.time() - updated_epoch > 8:
            return False
        return process_alive(pid)
    except (OSError, ValueError, TypeError):
        return False


def read_command() -> dict[str, Any] | None:
    try:
        payload = json.loads(COMMAND_PATH.read_text(encoding="utf-8-sig"))
        COMMAND_PATH.unlink()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def install_signal_handlers() -> None:
    def request_stop(_signum: int, _frame: Any) -> None:
        global STOP_REQUESTED
        STOP_REQUESTED = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def run() -> int:
    if supervisor_already_running():
        raise RuntimeError("镜场守护进程已经在运行，请勿重复启动")
    python = resolve_python()
    node = resolve_node()
    vite = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    dist_index = ROOT / "frontend" / "dist" / "index.html"
    if not vite.is_file() or not dist_index.is_file():
        raise RuntimeError("前端依赖或构建产物缺失。请先运行 scripts\\setup.ps1")

    env_api_base = "http://127.0.0.1:8765"
    os.environ["JINGCHANG_API_BASE"] = env_api_base
    services = {
        "api": Service(
            "api",
            8765,
            [str(python), "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8765"],
            ROOT,
            RUNTIME_ROOT / "api.log",
        ),
        "web": Service(
            "web",
            4173,
            [str(node), str(vite), "preview", "--host", "127.0.0.1", "--port", "4173"],
            ROOT / "frontend",
            RUNTIME_ROOT / "web.log",
        ),
    }
    install_signal_handlers()
    started_at = utc_now()
    for service in services.values():
        service.start()

    try:
        while not STOP_REQUESTED:
            command = read_command()
            if command:
                action = command.get("action")
                target = services.get(str(command.get("service")))
                if action == "stop":
                    break
                if action == "restart" and target:
                    target.restart()
            for service in services.values():
                service.tick()
            atomic_json(
                STATUS_PATH,
                {
                    "version": 1,
                    "supervisor_pid": os.getpid(),
                    "started_at": started_at,
                    "updated_at": utc_now(),
                    "updated_epoch": time.time(),
                    "project_root": str(ROOT.resolve()),
                    "services": {name: service.public() for name, service in services.items()},
                },
            )
            time.sleep(1)
    finally:
        for service in reversed(list(services.values())):
            service.stop()
        atomic_json(
            STATUS_PATH,
            {
                "version": 1,
                "supervisor_pid": os.getpid(),
                "started_at": started_at,
                "updated_at": utc_now(),
                "updated_epoch": 0,
                "project_root": str(ROOT.resolve()),
                "services": {name: service.public() for name, service in services.items()},
            },
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="镜场本地服务守护进程")
    parser.add_argument("command", nargs="?", choices=["run"], default="run")
    parser.parse_args()
    try:
        return run()
    except Exception as exc:
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        with (RUNTIME_ROOT / "supervisor-error.log").open("a", encoding="utf-8") as error_log:
            error_log.write(f"[{utc_now()}] {exc}\n")
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
