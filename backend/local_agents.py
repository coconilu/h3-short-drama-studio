from __future__ import annotations

import difflib
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    from .content_planning import _active_project, _advance, _save_revision, connect
except ImportError:  # Support `uvicorn app:app` from backend/.
    from content_planning import _active_project, _advance, _save_revision, connect


ProviderAdapter = Literal["codex", "kimi"]
AgentScope = Literal["plot", "outline", "chapter", "section", "body"]
AgentOperation = Literal["generate", "expand", "compress", "rewrite", "proofread"]

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("completed", "failed", "cancelled", "applied", "rejected")
PROCESS_LOCK = threading.Lock()
PROCESSES: dict[str, subprocess.Popen[str]] = {}
THREADS: dict[str, threading.Thread] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_local_agent_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS local_agent_providers (
          id TEXT PRIMARY KEY,
          adapter TEXT NOT NULL CHECK(adapter IN ('codex', 'kimi')),
          label TEXT NOT NULL,
          executable_path TEXT,
          enabled INTEGER NOT NULL DEFAULT 1,
          timeout_seconds INTEGER NOT NULL DEFAULT 300,
          model TEXT NOT NULL DEFAULT '',
          capabilities TEXT NOT NULL DEFAULT '[]',
          probe_state TEXT NOT NULL DEFAULT 'unknown',
          installed INTEGER NOT NULL DEFAULT 0,
          callable INTEGER NOT NULL DEFAULT 0,
          auth_state TEXT NOT NULL DEFAULT 'unknown',
          model_state TEXT NOT NULL DEFAULT 'unknown',
          callable_state TEXT NOT NULL DEFAULT 'unknown',
          version TEXT,
          last_error TEXT,
          last_probe_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS creative_agent_runs (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          provider_id TEXT NOT NULL REFERENCES local_agent_providers(id),
          scope TEXT NOT NULL CHECK(scope IN ('plot', 'outline', 'chapter', 'section', 'body')),
          operation TEXT NOT NULL CHECK(operation IN ('generate', 'expand', 'compress', 'rewrite', 'proofread')),
          target_id TEXT,
          parent_id TEXT,
          instruction TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'applied', 'rejected')),
          message TEXT NOT NULL,
          input_summary TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          base_payload TEXT NOT NULL,
          base_revisions TEXT NOT NULL,
          proposed_payload TEXT NOT NULL DEFAULT '{}',
          diff_payload TEXT NOT NULL DEFAULT '[]',
          raw_output TEXT NOT NULL DEFAULT '',
          log TEXT NOT NULL DEFAULT '',
          error TEXT,
          command_info TEXT NOT NULL DEFAULT '{}',
          provider_version TEXT,
          provider_adapter TEXT NOT NULL DEFAULT '',
          executable_path TEXT NOT NULL DEFAULT '',
          executable_fingerprint TEXT NOT NULL DEFAULT '',
          timeout_seconds INTEGER NOT NULL DEFAULT 300,
          model TEXT NOT NULL DEFAULT '',
          retry_of TEXT REFERENCES creative_agent_runs(id),
          attempt INTEGER NOT NULL DEFAULT 1,
          cancel_requested INTEGER NOT NULL DEFAULT 0,
          recoverable INTEGER NOT NULL DEFAULT 1,
          confirmed_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT,
          applied_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_creative_agent_runs_project
          ON creative_agent_runs(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_creative_agent_runs_state
          ON creative_agent_runs(state, created_at);
        """
    )
    provider_columns = {row[1] for row in db.execute("PRAGMA table_info(local_agent_providers)").fetchall()}
    for name, definition in (
        ("auth_state", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("model_state", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("callable_state", "TEXT NOT NULL DEFAULT 'unknown'"),
    ):
        if name not in provider_columns:
            db.execute(f"ALTER TABLE local_agent_providers ADD COLUMN {name} {definition}")
    run_columns = {row[1] for row in db.execute("PRAGMA table_info(creative_agent_runs)").fetchall()}
    for name, definition in (
        ("provider_adapter", "TEXT NOT NULL DEFAULT ''"),
        ("executable_path", "TEXT NOT NULL DEFAULT ''"),
        ("executable_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("timeout_seconds", "INTEGER NOT NULL DEFAULT 300"),
    ):
        if name not in run_columns:
            db.execute(f"ALTER TABLE creative_agent_runs ADD COLUMN {name} {definition}")
    now = utc_now()
    defaults = (
        ("codex", "codex", "Codex CLI"),
        ("kimi", "kimi", "Kimi Code CLI"),
    )
    for provider_id, adapter, label in defaults:
        db.execute(
            """INSERT OR IGNORE INTO local_agent_providers
            (id, adapter, label, enabled, timeout_seconds, capabilities, probe_state,
             installed, callable, created_at, updated_at)
            VALUES (?, ?, ?, 1, 300, ?, 'unknown', 0, 0, ?, ?)""",
            (
                provider_id,
                adapter,
                label,
                json.dumps(_adapter_capabilities(adapter), ensure_ascii=False),
                now,
                now,
            ),
        )


class ProviderRegistration(BaseModel):
    adapter: ProviderAdapter
    label: str | None = Field(None, min_length=1, max_length=80)
    executable_path: str | None = Field(None, max_length=500)
    enabled: bool = True
    timeout_seconds: int = Field(300, ge=1, le=3600)
    model: str = Field("", max_length=120)


class ProviderUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=80)
    executable_path: str | None = Field(None, max_length=500)
    enabled: bool | None = None
    timeout_seconds: int | None = Field(None, ge=1, le=3600)
    model: str | None = Field(None, max_length=120)


class AgentRunCreate(BaseModel):
    provider_id: str = Field(min_length=1, max_length=80)
    scope: AgentScope
    operation: AgentOperation
    target_id: str | None = Field(None, max_length=160)
    parent_id: str | None = Field(None, max_length=160)
    instruction: str = Field(min_length=2, max_length=4000)


class AgentApply(BaseModel):
    confirmed_by: str = Field("human:ui", min_length=2, max_length=120)


def _adapter_capabilities(adapter: str) -> list[dict[str, Any]]:
    return [
        {"scope": scope, "operations": ["generate", "expand", "compress", "rewrite", "proofread"]}
        for scope in ("plot", "outline", "chapter", "section", "body")
    ]


def _json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _provider_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    result["installed"] = bool(result["installed"])
    result["callable"] = bool(result["callable"])
    result["capabilities"] = _json(result.pop("capabilities"), [])
    if result["callable_state"] == "verified":
        result["action_hint"] = "安装、登录和模型调用条件均已验证。"
    elif result["callable"]:
        result["action_hint"] = result.get("last_error") or "配置已就绪；为避免消耗额度，实际模型调用仍标记为未验证。"
    elif not result["enabled"]:
        result["action_hint"] = "提供方已停用；在系统设置中重新启用后再探测。"
    elif result["installed"]:
        result["action_hint"] = result.get("last_error") or "CLI 已找到，但登录或模型配置不可用。"
    else:
        command = "codex" if result["adapter"] == "codex" else "kimi"
        result["action_hint"] = f"未找到 {command}；请先安装 CLI，或填写可执行文件路径。"
    return result


def _run_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, fallback in (
        ("base_payload", {}),
        ("base_revisions", []),
        ("proposed_payload", {}),
        ("diff_payload", []),
        ("command_info", {}),
    ):
        result[key] = _json(result[key], fallback)
    result["cancel_requested"] = bool(result["cancel_requested"])
    result["recoverable"] = bool(result["recoverable"])
    result["diff"] = result.pop("diff_payload")
    return result


def _resolve_executable(row: sqlite3.Row | dict[str, Any]) -> str | None:
    configured = (row["executable_path"] or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())
        return shutil.which(configured)
    return shutil.which("codex" if row["adapter"] == "codex" else "kimi")


def _executable_fingerprint(executable: str) -> str:
    path = Path(executable).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _isolated_environment(adapter: str) -> dict[str, str]:
    """Pass only runtime/auth/proxy variables required by the selected CLI."""
    common = {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOME",
        "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PATH", "PATHEXT",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    adapter_specific = {
        "codex": {"CODEX_HOME", "OPENAI_API_KEY"},
        "kimi": {"KIMI_CODE_HOME", "KIMI_API_KEY", "MOONSHOT_API_KEY"},
    }[adapter]
    result = {name: value for name, value in os.environ.items() if name in common | adapter_specific}
    result.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return result


def _command_prefix(executable: str) -> list[str]:
    return [sys.executable, executable] if Path(executable).suffix.lower() == ".py" else [executable]


def _probe_command(executable: str, args: list[str], adapter: str, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_command_prefix(executable), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=min(10, timeout_seconds),
        check=False,
        env=_isolated_environment(adapter),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _structural_ids(value: Any, *, fields: tuple[str, ...]) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, dict):
        identifiers.update(str(key).strip() for key in value if str(key).strip())
        records = value.values()
    elif isinstance(value, list):
        records = value
    else:
        return identifiers
    for record in records:
        if not isinstance(record, dict):
            continue
        for field in fields:
            candidate = record.get(field)
            if isinstance(candidate, str) and candidate.strip():
                identifiers.add(candidate.strip())
            elif isinstance(candidate, list):
                identifiers.update(str(item).strip() for item in candidate if isinstance(item, str) and item.strip())
    return identifiers


def _parse_kimi_provider_config(raw: str, configured_model: str) -> tuple[bool, str, str | None]:
    """Return whether Kimi may be attempted, model state, and an actionable error."""
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False, "unavailable", "Kimi provider list --json 返回了无效 JSON"
    if not isinstance(payload, dict):
        return False, "unavailable", "Kimi provider 配置必须是 JSON 对象"
    provider_ids = _structural_ids(payload.get("providers"), fields=("id", "alias", "aliases"))
    if not provider_ids:
        return False, "unavailable", "Kimi provider 配置中没有可用提供方"
    models = payload.get("models")
    model_ids = _structural_ids(models, fields=("id", "alias", "aliases", "model"))
    if not model_ids:
        return False, "unavailable", "Kimi provider 配置中没有可用模型"

    requested = configured_model.strip()
    if requested:
        if requested not in model_ids:
            return False, "unavailable", f"Kimi 配置中未找到精确模型别名或 ID：{requested}"
        return True, "verified", None

    default_model = payload.get("defaultModel") or payload.get("default_model")
    if isinstance(default_model, dict):
        default_model = default_model.get("id") or default_model.get("alias") or default_model.get("model")
    if isinstance(default_model, str) and default_model.strip() in model_ids:
        return True, "verified", None
    if default_model:
        return False, "unavailable", "Kimi 默认模型不是已配置的精确模型别名或 ID"
    return True, "unverified", "Kimi JSON 配置未声明默认模型；实际模型选择未验证"


def probe_provider(db_path: Path, provider_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM local_agent_providers WHERE id = ?", (provider_id,)).fetchone()
        if not row:
            raise HTTPException(404, "本地 Agent 提供方不存在")
        executable = _resolve_executable(row)
        installed = bool(executable)
        callable_state = False
        auth_state = "unknown"
        model_state = "unknown"
        callable_detail = "unknown"
        version: str | None = None
        error: str | None = None
        if not row["enabled"]:
            error = "提供方已停用"
            auth_state = model_state = callable_detail = "unavailable"
        elif not executable:
            error = "未找到可执行文件"
            auth_state = model_state = callable_detail = "unavailable"
        else:
            try:
                result = _probe_command(executable, ["--version"], row["adapter"], int(row["timeout_seconds"]))
                version = (result.stdout or result.stderr or "已检测").strip().splitlines()[0][:160]
                if result.returncode != 0:
                    error = f"版本探测退出码 {result.returncode}"
                    auth_state = model_state = callable_detail = "unavailable"
                elif row["adapter"] == "codex":
                    auth = _probe_command(executable, ["login", "status"], "codex", int(row["timeout_seconds"]))
                    auth_output = (auth.stdout or auth.stderr or "").strip()
                    if auth.returncode == 0 and "logged in" in auth_output.lower():
                        auth_state = "verified"
                        model_state = "unverified"
                        callable_detail = "unverified"
                        callable_state = True
                    else:
                        auth_state = "unavailable" if auth.returncode != 0 else "unverified"
                        model_state = callable_detail = "unverified"
                        error = "Codex 登录状态未确认；请运行 codex login status"
                else:
                    configured = _probe_command(
                        executable, ["provider", "list", "--json"], "kimi", int(row["timeout_seconds"])
                    )
                    config_output = (configured.stdout or configured.stderr or "").strip()
                    model = (row["model"] or "").strip()
                    if configured.returncode != 0:
                        config_ok, model_state, config_error = False, "unavailable", None
                    else:
                        config_ok, model_state, config_error = _parse_kimi_provider_config(config_output, model)
                    auth_state = "unverified" if config_ok else "unavailable"
                    callable_detail = "unverified" if config_ok else "unavailable"
                    callable_state = config_ok
                    if configured.returncode != 0:
                        error = "Kimi 提供方配置不可读取；请运行 kimi provider list"
                    else:
                        error = config_error
            except subprocess.TimeoutExpired:
                error = "版本探测超时"
                auth_state = model_state = callable_detail = "unavailable"
            except OSError as exc:
                error = str(exc)[:500]
                auth_state = model_state = callable_detail = "unavailable"
        now = utc_now()
        db.execute(
            """UPDATE local_agent_providers
            SET executable_path = COALESCE(executable_path, ?), probe_state = ?, installed = ?, callable = ?,
                auth_state = ?, model_state = ?, callable_state = ?, version = ?, last_error = ?,
                last_probe_at = ?, updated_at = ? WHERE id = ?""",
            (
                executable,
                callable_detail,
                int(installed),
                int(callable_state),
                auth_state,
                model_state,
                callable_detail,
                version,
                error,
                now,
                now,
                provider_id,
            ),
        )
        db.commit()
        return _provider_public(db.execute("SELECT * FROM local_agent_providers WHERE id = ?", (provider_id,)).fetchone())


def provider_statuses(db_path: Path, *, probe: bool = False) -> list[dict[str, Any]]:
    if probe:
        with closing(connect(db_path)) as db:
            ids = [row["id"] for row in db.execute("SELECT id FROM local_agent_providers ORDER BY id").fetchall()]
        for provider_id in ids:
            probe_provider(db_path, provider_id)
    with closing(connect(db_path)) as db:
        return [
            _provider_public(row)
            for row in db.execute("SELECT * FROM local_agent_providers ORDER BY adapter, id").fetchall()
        ]


def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是文本")
    result = value.strip()[:limit]
    if required and not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _seconds(value: Any) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("planned_seconds 必须是数字") from exc
    if result < 0 or result > 36000:
        raise ValueError("planned_seconds 超出范围")
    return result


def _section_contract(value: Any, *, include_content: bool = True) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("section 必须是对象")
    result = {
        "title": _text(value.get("title"), "title", 160, required=True),
        "summary": _text(value.get("summary"), "summary", 6000),
        "pacing_goal": _text(value.get("pacing_goal"), "pacing_goal", 1200),
        "planned_seconds": _seconds(value.get("planned_seconds")),
    }
    if include_content:
        result["content"] = _text(value.get("content"), "content", 24000)
    return result


def validate_proposal(scope: str, parsed: dict[str, Any]) -> dict[str, Any]:
    value = parsed.get("proposal")
    if not isinstance(value, dict):
        raise ValueError("输出必须包含 proposal 对象")
    if scope == "plot":
        return {"proposal": {
            "title": _text(value.get("title"), "title", 160, required=True),
            "synopsis": _text(value.get("synopsis"), "synopsis", 12000, required=True),
            "core_conflict": _text(value.get("core_conflict"), "core_conflict", 6000),
            "ending": _text(value.get("ending"), "ending", 6000),
        }}
    if scope == "outline":
        chapters = value.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            raise ValueError("outline proposal.chapters 必须是非空数组")
        normalized = []
        for chapter in chapters[:100]:
            if not isinstance(chapter, dict):
                raise ValueError("chapters 中每一项都必须是对象")
            sections = chapter.get("sections")
            if not isinstance(sections, list):
                raise ValueError("每个 chapter 都必须包含 sections 数组")
            normalized.append({
                "title": _text(chapter.get("title"), "title", 160, required=True),
                "summary": _text(chapter.get("summary"), "summary", 6000),
                "pacing_goal": _text(chapter.get("pacing_goal"), "pacing_goal", 1200),
                "planned_seconds": _seconds(chapter.get("planned_seconds")),
                "sections": [_section_contract(section) for section in sections[:300]],
            })
        return {"proposal": {"chapters": normalized}}
    if scope == "chapter":
        result = _section_contract(value, include_content=False)
        sections = value.get("sections", [])
        if not isinstance(sections, list):
            raise ValueError("chapter proposal.sections 必须是数组")
        result["sections"] = [_section_contract(section) for section in sections[:300]]
        return {"proposal": result}
    if scope == "section":
        return {"proposal": _section_contract(value)}
    if scope == "body":
        return {"proposal": {"content": _text(value.get("content"), "content", 24000, required=True)}}
    raise ValueError("不支持的 Agent 作用域")


def extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Agent 输出必须是 JSON 对象")
    return parsed


def provider_result_text(adapter: str, raw: str) -> str:
    return raw


class ForbiddenToolError(RuntimeError):
    pass


def _write_kimi_profile(sandbox_root: Path) -> tuple[Path, Path]:
    skills_dir = sandbox_root / "isolated-skills"
    skills_dir.mkdir()
    profile = sandbox_root / "structured-writer.md"
    profile.write_text(
        """---
name: jingchang-structured-writer
description: Isolated structured writing adapter
tools: []
subagents: []
---
Only return the requested structured JSON. Tool use, filesystem access, subprocesses, and subagents are disabled.
""",
        encoding="utf-8",
    )
    return skills_dir, profile


def _acp_update_kind(message: dict[str, Any]) -> str:
    if message.get("method") != "session/update":
        return ""
    update = message.get("params", {}).get("update", {})
    if not isinstance(update, dict):
        return ""
    return str(update.get("sessionUpdate") or update.get("type") or "").lower()


def _acp_message_text(message: dict[str, Any]) -> str:
    if _acp_update_kind(message) not in {"agent_message_chunk", "agentmessagechunk"}:
        return ""
    content = message.get("params", {}).get("update", {}).get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text") or "")
    if isinstance(content, str):
        return content
    return ""


def _run_kimi_acp(
    process: subprocess.Popen[str], prompt: str, timeout_seconds: int, sandbox_root: Path
) -> tuple[str, str]:
    """Drive Kimi through ACP stdin and reject every tool/permission request at the adapter boundary."""
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("Kimi ACP 管道不可用")
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_stream(name: str, stream: Any) -> None:
        for line in iter(stream.readline, ""):
            output_queue.put((name, line))
        output_queue.put((name, None))

    stdout_thread = threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + timeout_seconds
    next_id = 1
    stderr_lines: list[str] = []
    assistant_chunks: list[str] = []

    def send(method: str, params: dict[str, Any], *, request: bool = True) -> int | None:
        nonlocal next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        request_id: int | None = None
        if request:
            request_id = next_id
            next_id += 1
            message["id"] = request_id
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
        return request_id

    def wait_for(request_id: int) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"本地 Agent 超过 {timeout_seconds} 秒未完成")
            try:
                channel, line = output_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"本地 Agent 超过 {timeout_seconds} 秒未完成") from exc
            if line is None:
                if channel == "stdout":
                    raise RuntimeError("Kimi ACP 在返回结果前结束")
                continue
            if channel == "stderr":
                stderr_lines.append(line)
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = _acp_update_kind(message)
            if kind.startswith("tool_call") or kind.startswith("toolcall"):
                send("session/cancel", {"sessionId": session_id}, request=False)
                raise ForbiddenToolError("Kimi 尝试调用被禁用的工具；提案已拒绝")
            if "method" in message and "id" in message:
                response = {
                    "jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32001, "message": "Jingchang adapter denies all tools and permissions"},
                }
                process.stdin.write(json.dumps(response) + "\n")
                process.stdin.flush()
                raise ForbiddenToolError("Kimi 请求工具或权限；适配器已拒绝")
            chunk = _acp_message_text(message)
            if chunk:
                assistant_chunks.append(chunk)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"Kimi ACP 错误：{message['error']}")
                return message.get("result") or {}

    session_id = ""
    initialize_id = send(
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "jingchang", "title": "镜场", "version": "1"},
        },
    )
    assert initialize_id is not None
    wait_for(initialize_id)
    session_request_id = send("session/new", {"cwd": str(sandbox_root), "mcpServers": []})
    assert session_request_id is not None
    session_result = wait_for(session_request_id)
    session_id = str(session_result.get("sessionId") or "")
    if not session_id:
        raise RuntimeError("Kimi ACP 未返回 sessionId")
    prompt_id = send(
        "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]}
    )
    assert prompt_id is not None
    wait_for(prompt_id)
    if not assistant_chunks:
        raise RuntimeError("Kimi ACP 未返回文本提案")
    return "".join(assistant_chunks), "".join(stderr_lines)


def provider_command(
    adapter: str,
    executable: str,
    sandbox_root: Path,
    prompt: str,
    result_path: Path,
    model: str = "",
) -> tuple[list[str], str | None, dict[str, Any]]:
    prefix = _command_prefix(executable)
    if adapter == "codex":
        args = ["exec"]
        if model:
            args.extend(["--model", model])
        args.extend([
            "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
            "--ignore-rules", "--color", "never", "-C", str(sandbox_root), "-o", str(result_path), "-",
        ])
        return [*prefix, *args], prompt, {"adapter": adapter, "executable": Path(executable).name, "arguments": args[:-1] + ["<stdin>"]}
    if adapter == "kimi":
        skills_dir, profile = _write_kimi_profile(sandbox_root)
        args = []
        if model:
            args.extend(["--model", model])
        args.extend(["--skills-dir", str(skills_dir), "--agent-file", str(profile), "acp"])
        safe_args = (["--model", model] if model else []) + [
            "--skills-dir", "<isolated-skills-dir>", "--agent-file", "<no-tools-agent-profile>", "acp",
        ]
        return [*prefix, *args], prompt, {
            "adapter": adapter,
            "transport": "acp-stdio",
            "security_profile": "tools=[];subagents=[];mcp=[];client-fs=false;client-terminal=false",
            "executable": Path(executable).name,
            "arguments": safe_args,
        }
    raise ValueError("不支持的本地 Agent 适配器")


def _base_for_scope(db: sqlite3.Connection, project_id: str, payload: AgentRunCreate) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    brief = dict(db.execute("SELECT * FROM creative_briefs WHERE project_id = ?", (project_id,)).fetchone())
    finalized = db.execute(
        "SELECT * FROM creative_proposals WHERE project_id = ? AND status = 'finalized'", (project_id,)
    ).fetchone()
    context = {"brief": brief, "finalized_plot": dict(finalized) if finalized else None}
    revisions: list[dict[str, Any]] = []
    if payload.scope == "plot":
        if payload.operation != "generate" and not payload.target_id:
            raise HTTPException(422, "修改剧情时必须选择现有剧情提案")
        target = None
        if payload.target_id:
            target = db.execute(
                "SELECT * FROM creative_proposals WHERE id = ? AND project_id = ? AND status <> 'archived'",
                (payload.target_id, project_id),
            ).fetchone()
            if not target:
                raise HTTPException(404, "剧情提案不存在")
            revisions.append({"entity_type": "proposal", "id": target["id"], "revision": target["revision"]})
        return {**context, "target": dict(target) if target else None}, revisions
    if payload.scope == "outline":
        chapters = []
        for row in db.execute(
            "SELECT * FROM creative_chapters WHERE project_id = ? AND status <> 'archived' ORDER BY ordinal",
            (project_id,),
        ).fetchall():
            chapter = dict(row)
            revisions.append({"entity_type": "chapter", "id": row["id"], "revision": row["revision"]})
            chapter["sections"] = []
            for section in db.execute(
                "SELECT * FROM creative_sections WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal",
                (row["id"],),
            ).fetchall():
                chapter["sections"].append(dict(section))
                revisions.append({"entity_type": "section", "id": section["id"], "revision": section["revision"]})
            chapters.append(chapter)
        return {**context, "chapters": chapters}, revisions
    table = "creative_chapters" if payload.scope == "chapter" else "creative_sections"
    if payload.scope == "section" and payload.operation == "generate" and not payload.target_id:
        if not payload.parent_id:
            raise HTTPException(422, "生成小节时必须选择所属章节")
        parent = db.execute(
            "SELECT * FROM creative_chapters WHERE id = ? AND project_id = ? AND status <> 'archived'",
            (payload.parent_id, project_id),
        ).fetchone()
        if not parent:
            raise HTTPException(404, "所属章节不存在")
        revisions.append({"entity_type": "chapter", "id": parent["id"], "revision": parent["revision"]})
        return {**context, "parent": dict(parent), "target": None}, revisions
    if payload.scope == "chapter" and payload.operation == "generate" and not payload.target_id:
        return {**context, "target": None}, revisions
    if not payload.target_id:
        raise HTTPException(422, f"{payload.scope} 修改必须选择目标")
    target = db.execute(
        f"SELECT * FROM {table} WHERE id = ? AND project_id = ? AND status <> 'archived'",
        (payload.target_id, project_id),
    ).fetchone()
    if not target:
        raise HTTPException(404, "Agent 目标不存在")
    entity_type = "chapter" if payload.scope == "chapter" else "section"
    revisions.append({"entity_type": entity_type, "id": target["id"], "revision": target["revision"]})
    if payload.scope == "body" or payload.scope == "section":
        parent = db.execute("SELECT * FROM creative_chapters WHERE id = ?", (target["chapter_id"],)).fetchone()
        return {**context, "parent": dict(parent), "target": dict(target)}, revisions
    sections = [dict(row) for row in db.execute(
        "SELECT * FROM creative_sections WHERE chapter_id = ? AND status <> 'archived' ORDER BY ordinal",
        (target["id"],),
    ).fetchall()]
    revisions.extend(
        {"entity_type": "section", "id": section["id"], "revision": section["revision"]}
        for section in sections
    )
    return {**context, "target": {**dict(target), "sections": sections}}, revisions


def _contract_text(scope: str) -> str:
    contracts = {
        "plot": '{"proposal":{"title":"标题","synopsis":"完整梗概","core_conflict":"核心冲突","ending":"结局"}}',
        "outline": '{"proposal":{"chapters":[{"title":"章标题","summary":"摘要","pacing_goal":"节奏","planned_seconds":30,"sections":[{"title":"节标题","summary":"摘要","content":"正文","pacing_goal":"节奏","planned_seconds":10}]}]}}',
        "chapter": '{"proposal":{"title":"章标题","summary":"摘要","pacing_goal":"节奏","planned_seconds":30,"sections":[]}}',
        "section": '{"proposal":{"title":"节标题","summary":"摘要","content":"正文","pacing_goal":"节奏","planned_seconds":10}}',
        "body": '{"proposal":{"content":"可直接继续打磨的小节正文"}}',
    }
    return contracts[scope]


def _build_prompt(run: sqlite3.Row) -> str:
    base = _json(run["base_payload"], {})
    return (
        "你是中文短剧创作 Agent。只依据输入完成指定层级任务，不读取文件、不调用工具、不修改外部内容。\n"
        f"作用域：{run['scope']}\n操作：{run['operation']}\n用户要求：{run['instruction']}\n"
        f"当前内容：{json.dumps(base, ensure_ascii=False)}\n"
        "只输出一个 JSON 对象，不要 Markdown、解释或附加字段。严格合同：\n"
        f"{_contract_text(run['scope'])}"
    )


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        result: dict[str, str] = {}
        for key, item in value.items():
            result.update(_flatten(item, f"{prefix}.{key}" if prefix else key))
        return result
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            result.update(_flatten(item, f"{prefix}[{index}]"))
        return result
    return {prefix: "" if value is None else str(value)}


def build_diff(base_payload: dict[str, Any], proposal: dict[str, Any], scope: str) -> list[dict[str, str]]:
    before_source = base_payload.get("chapters") if scope == "outline" else base_payload.get("target")
    if scope == "body" and isinstance(before_source, dict):
        before_source = {"content": before_source.get("content", "")}
    before = _flatten(before_source or {})
    after = _flatten(proposal.get("proposal", {}))
    paths = sorted(set(before) | set(after))
    result = []
    for path in paths:
        old, new = before.get(path, ""), after.get(path, "")
        if old == new:
            continue
        unified = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))
        result.append({"path": path, "before": old, "after": new, "unified": unified})
    return result


def create_run_record(db_path: Path, payload: AgentRunCreate, *, retry_of: str | None = None, attempt: int = 1) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        provider = db.execute("SELECT * FROM local_agent_providers WHERE id = ?", (payload.provider_id,)).fetchone()
        if not provider:
            raise HTTPException(404, "本地 Agent 提供方不存在")
        if not provider["enabled"] or not provider["callable"]:
            raise HTTPException(409, f"{provider['label']} 当前不可调用，请到系统设置中重新探测")
        executable = _resolve_executable(provider)
        if not executable:
            raise HTTPException(409, f"{provider['label']} 的可执行文件已不可用，请重新探测")
        try:
            executable = str(Path(executable).resolve(strict=True))
            executable_fingerprint = _executable_fingerprint(executable)
        except OSError as exc:
            raise HTTPException(409, f"无法冻结提供方可执行文件：{exc}") from exc
        base_payload, base_revisions = _base_for_scope(db, project["id"], payload)
        active = db.execute(
            "SELECT id FROM creative_agent_runs WHERE project_id = ? AND state IN ('queued', 'running') LIMIT 1",
            (project["id"],),
        ).fetchone()
        if active:
            raise HTTPException(409, "当前项目已有本地 Agent 任务运行")
        run_id = f"creative-agent-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        encoded = json.dumps(base_payload, ensure_ascii=False, sort_keys=True)
        hashed_input = json.dumps(
            {
                "scope": payload.scope,
                "operation": payload.operation,
                "target_id": payload.target_id,
                "parent_id": payload.parent_id,
                "instruction": payload.instruction.strip(),
                "base": base_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        summary = f"{payload.scope}/{payload.operation} · {payload.instruction[:160]}"
        db.execute(
            """INSERT INTO creative_agent_runs
            (id, project_id, provider_id, scope, operation, target_id, parent_id, instruction, state, message,
             input_summary, input_hash, base_payload, base_revisions, provider_version, provider_adapter,
             executable_path, executable_fingerprint, timeout_seconds, model, retry_of, attempt, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', '等待本地 Agent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, project["id"], payload.provider_id, payload.scope, payload.operation,
                payload.target_id, payload.parent_id, payload.instruction.strip(), summary,
                hashlib.sha256(hashed_input.encode("utf-8")).hexdigest(), encoded,
                json.dumps(base_revisions, ensure_ascii=False), provider["version"], provider["adapter"],
                executable, executable_fingerprint, int(provider["timeout_seconds"]), provider["model"],
                retry_of, attempt, now, now,
            ),
        )
        db.commit()
        return _run_public(db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone())


def _set_failed(db_path: Path, run_id: str, error: str, raw_output: str = "", log: str = "") -> None:
    with closing(connect(db_path)) as db:
        now = utc_now()
        db.execute(
            """UPDATE creative_agent_runs SET state = 'failed', message = 'Agent 任务失败', error = ?,
            raw_output = ?, log = ?, updated_at = ?, completed_at = ? WHERE id = ? AND state = 'running'""",
            (error[:2000], raw_output[-50000:], log[-20000:], now, now, run_id),
        )
        db.commit()


def execute_agent_run(db_path: Path, run_id: str) -> None:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        now = utc_now()
        claimed = db.execute(
            """UPDATE creative_agent_runs SET state = 'running', message = '本地 Agent 正在生成提案',
            started_at = ?, updated_at = ?
            WHERE id = ? AND state = 'queued' AND cancel_requested = 0""",
            (now, now, run_id),
        )
        if claimed.rowcount != 1:
            db.rollback()
            return
        run = db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone()
        db.commit()

    executable = run["executable_path"]
    adapter = run["provider_adapter"]
    try:
        if not executable or _executable_fingerprint(executable) != run["executable_fingerprint"]:
            _set_failed(db_path, run_id, "已冻结的提供方可执行文件发生变化；请重试以创建新的运行快照")
            return
    except OSError:
        _set_failed(db_path, run_id, "已冻结的提供方可执行文件不存在；请重试")
        return

    raw = ""
    stderr = ""
    try:
        with tempfile.TemporaryDirectory(prefix="jingchang-agent-") as temp_name:
            sandbox = Path(temp_name)
            result_path = sandbox / "result.json"
            command, stdin_text, command_info = provider_command(
                adapter, executable, sandbox, _build_prompt(run), result_path, run["model"]
            )
            with closing(connect(db_path)) as db:
                db.execute(
                    "UPDATE creative_agent_runs SET command_info = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(command_info, ensure_ascii=False), utc_now(), run_id),
                )
                db.commit()
            with PROCESS_LOCK:
                with closing(connect(db_path)) as db:
                    state = db.execute(
                        "SELECT state, cancel_requested FROM creative_agent_runs WHERE id = ?", (run_id,)
                    ).fetchone()
                if not state or state["state"] != "running" or state["cancel_requested"]:
                    return
                process = subprocess.Popen(
                    command,
                    cwd=sandbox,
                    env=_isolated_environment(adapter),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                PROCESSES[run_id] = process
            try:
                if adapter == "kimi":
                    raw, stderr = _run_kimi_acp(process, stdin_text or "", int(run["timeout_seconds"]), sandbox)
                    if process.poll() is None:
                        process.terminate()
                    stdout = ""
                else:
                    try:
                        stdout, stderr = process.communicate(input=stdin_text, timeout=int(run["timeout_seconds"]))
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                        raise TimeoutError(f"本地 Agent 超过 {run['timeout_seconds']} 秒未完成")
            finally:
                with PROCESS_LOCK:
                    PROCESSES.pop(run_id, None)
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            raw = result_path.read_text(encoding="utf-8") if adapter == "codex" and result_path.exists() else raw or stdout
            with closing(connect(db_path)) as db:
                state = db.execute("SELECT state, cancel_requested FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone()
                if not state or state["state"] == "cancelled" or state["cancel_requested"]:
                    return
            if adapter == "codex" and process.returncode != 0:
                raise RuntimeError((stderr or raw or f"CLI 退出码 {process.returncode}")[-2000:])
            proposal = validate_proposal(run["scope"], extract_json(provider_result_text(adapter, raw)))
            diff = build_diff(_json(run["base_payload"], {}), proposal, run["scope"])
            now = utc_now()
            with closing(connect(db_path)) as db:
                db.execute(
                    """UPDATE creative_agent_runs SET state = 'completed', message = '提案已生成，等待人工确认',
                    proposed_payload = ?, diff_payload = ?, raw_output = ?, log = ?, error = NULL,
                    updated_at = ?, completed_at = ? WHERE id = ? AND state = 'running'""",
                    (
                        json.dumps(proposal, ensure_ascii=False), json.dumps(diff, ensure_ascii=False), raw[-50000:],
                        stderr[-20000:], now, now, run_id,
                    ),
                )
                db.commit()
    except TimeoutError as exc:
        _set_failed(db_path, run_id, str(exc), raw, stderr)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        _set_failed(db_path, run_id, str(exc), raw, stderr)


def enqueue_agent_run(db_path: Path, run_id: str) -> None:
    thread = threading.Thread(target=execute_agent_run, args=(db_path, run_id), daemon=True, name=f"creative-agent-{run_id}")
    THREADS[run_id] = thread
    thread.start()


def recover_local_agent_runs(db_path: Path) -> None:
    with closing(connect(db_path)) as db:
        now = utc_now()
        db.execute(
            """UPDATE creative_agent_runs SET state = 'failed', message = '服务重启中断了 Agent 调用',
            error = '调用被服务重启中断，可安全重试', recoverable = 1, updated_at = ?, completed_at = ?
            WHERE state = 'running'""",
            (now, now),
        )
        queued = [row["id"] for row in db.execute("SELECT id FROM creative_agent_runs WHERE state = 'queued'").fetchall()]
        db.commit()
    for run_id in queued:
        enqueue_agent_run(db_path, run_id)


def _verify_revisions(db: sqlite3.Connection, project_id: str, run: sqlite3.Row) -> None:
    expected = _json(run["base_revisions"], [])
    current_keys: set[tuple[str, str]] = set()
    for item in expected:
        table = "creative_proposals" if item["entity_type"] == "proposal" else f"creative_{item['entity_type']}s"
        row = db.execute(
            f"SELECT revision, status FROM {table} WHERE id = ? AND project_id = ?",
            (item["id"], project_id),
        ).fetchone()
        if not row or row["status"] == "archived" or int(row["revision"]) != int(item["revision"]):
            raise HTTPException(409, "正式内容已变化；请重新生成 Agent 提案")
        current_keys.add((item["entity_type"], item["id"]))
    if run["scope"] == "outline":
        actual = {
            ("chapter", row["id"])
            for row in db.execute(
                "SELECT id FROM creative_chapters WHERE project_id = ? AND status <> 'archived'", (project_id,)
            ).fetchall()
        }
        actual.update({
            ("section", row["id"])
            for row in db.execute(
                "SELECT id FROM creative_sections WHERE project_id = ? AND status <> 'archived'", (project_id,)
            ).fetchall()
        })
        if actual != current_keys:
            raise HTTPException(409, "正式大纲结构已变化；请重新生成 Agent 提案")


def _create_chapter(db: sqlite3.Connection, project_id: str, value: dict[str, Any], ordinal: int, source: str) -> str:
    now = utc_now()
    chapter_id = f"chapter-{uuid.uuid4().hex[:12]}"
    db.execute(
        """INSERT INTO creative_chapters
        (id, project_id, ordinal, title, summary, pacing_goal, planned_seconds, status, revision, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
        (
            chapter_id, project_id, ordinal, value["title"], value["summary"], value["pacing_goal"],
            value["planned_seconds"], now, now,
        ),
    )
    _save_revision(db, project_id, "chapter", chapter_id, source)
    for section_ordinal, section in enumerate(value.get("sections", []), start=1):
        _create_section(db, project_id, chapter_id, section, section_ordinal, source)
    if value.get("sections"):
        db.execute("UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?", (utc_now(), chapter_id))
        _save_revision(db, project_id, "chapter", chapter_id, f"{source}:sections")
    return chapter_id


def _create_section(db: sqlite3.Connection, project_id: str, chapter_id: str, value: dict[str, Any], ordinal: int, source: str) -> str:
    now = utc_now()
    section_id = f"section-{uuid.uuid4().hex[:12]}"
    db.execute(
        """INSERT INTO creative_sections
        (id, project_id, chapter_id, ordinal, title, summary, content, pacing_goal, planned_seconds,
         status, revision, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
        (
            section_id, project_id, chapter_id, ordinal, value["title"], value["summary"], value.get("content", ""),
            value["pacing_goal"], value["planned_seconds"], now, now,
        ),
    )
    _save_revision(db, project_id, "section", section_id, source)
    return section_id


def apply_agent_proposal(db_path: Path, run_id: str, confirmed_by: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        run = db.execute(
            "SELECT * FROM creative_agent_runs WHERE id = ? AND project_id = ?", (run_id, project["id"])
        ).fetchone()
        if not run:
            raise HTTPException(404, "Agent 任务不存在")
        now = utc_now()
        claimed = db.execute(
            """UPDATE creative_agent_runs SET state = 'applied',
            message = '提案已由人工确认并创建正式修订', confirmed_by = ?, applied_at = ?, updated_at = ?
            WHERE id = ? AND project_id = ? AND state = 'completed'""",
            (confirmed_by.strip(), now, now, run_id, project["id"]),
        )
        if claimed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "只有已完成且未处理的提案可以确认")
        _verify_revisions(db, project["id"], run)
        value = _json(run["proposed_payload"], {}).get("proposal", {})
        source = f"agent:{run['provider_id']}:{run_id}:confirmed:{confirmed_by.strip()}"
        if run["scope"] == "plot":
            if run["target_id"]:
                _advance(
                    db, "creative_proposals", "proposal", run["target_id"], project["id"],
                    _json(run["base_revisions"], [])[0]["revision"], source, value,
                )
            else:
                ordinal = int(db.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM creative_proposals WHERE project_id = ?",
                    (project["id"],),
                ).fetchone()[0])
                now = utc_now()
                proposal_id = f"proposal-{uuid.uuid4().hex[:12]}"
                db.execute(
                    """INSERT INTO creative_proposals
                    (id, project_id, ordinal, title, synopsis, core_conflict, ending, status, revision, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?)""",
                    (proposal_id, project["id"], ordinal, value["title"], value["synopsis"], value["core_conflict"], value["ending"], now, now),
                )
                _save_revision(db, project["id"], "proposal", proposal_id, source)
        elif run["scope"] == "outline":
            for section in db.execute(
                "SELECT * FROM creative_sections WHERE project_id = ? AND status <> 'archived'", (project["id"],)
            ).fetchall():
                _advance(db, "creative_sections", "section", section["id"], project["id"], section["revision"], source, {"status": "archived"})
            for chapter in db.execute(
                "SELECT * FROM creative_chapters WHERE project_id = ? AND status <> 'archived'", (project["id"],)
            ).fetchall():
                _advance(db, "creative_chapters", "chapter", chapter["id"], project["id"], chapter["revision"], source, {"status": "archived"})
            for ordinal, chapter in enumerate(value["chapters"], start=1):
                _create_chapter(db, project["id"], chapter, ordinal, source)
        elif run["scope"] == "chapter":
            if run["target_id"]:
                revisions = _json(run["base_revisions"], [])
                _advance(
                    db, "creative_chapters", "chapter", run["target_id"], project["id"], revisions[0]["revision"],
                    source, {key: value[key] for key in ("title", "summary", "pacing_goal", "planned_seconds")},
                )
                for section_revision in revisions[1:]:
                    section = db.execute(
                        "SELECT * FROM creative_sections WHERE id = ? AND project_id = ? AND status <> 'archived'",
                        (section_revision["id"], project["id"]),
                    ).fetchone()
                    _advance(
                        db, "creative_sections", "section", section["id"], project["id"], section["revision"],
                        source, {"status": "archived"},
                    )
                for ordinal, section in enumerate(value.get("sections", []), start=1):
                    _create_section(db, project["id"], run["target_id"], section, ordinal, source)
                if revisions[1:] or value.get("sections"):
                    db.execute(
                        "UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?",
                        (utc_now(), run["target_id"]),
                    )
                    _save_revision(db, project["id"], "chapter", run["target_id"], f"{source}:sections")
            else:
                ordinal = int(db.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM creative_chapters WHERE project_id = ? AND status <> 'archived'",
                    (project["id"],),
                ).fetchone()[0])
                _create_chapter(db, project["id"], value, ordinal, source)
        elif run["scope"] == "section":
            if run["target_id"]:
                revisions = _json(run["base_revisions"], [])
                _advance(
                    db, "creative_sections", "section", run["target_id"], project["id"], revisions[0]["revision"], source, value,
                )
            else:
                chapter = db.execute(
                    "SELECT * FROM creative_chapters WHERE id = ? AND project_id = ? AND status <> 'archived'",
                    (run["parent_id"], project["id"]),
                ).fetchone()
                ordinal = int(db.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM creative_sections WHERE chapter_id = ? AND status <> 'archived'",
                    (run["parent_id"],),
                ).fetchone()[0])
                _create_section(db, project["id"], chapter["id"], value, ordinal, source)
                db.execute("UPDATE creative_chapters SET revision = revision + 1, updated_at = ? WHERE id = ?", (utc_now(), chapter["id"]))
                _save_revision(db, project["id"], "chapter", chapter["id"], f"{source}:section")
        else:
            revisions = _json(run["base_revisions"], [])
            _advance(
                db, "creative_sections", "section", run["target_id"], project["id"], revisions[0]["revision"],
                source, {"content": value["content"]},
            )
        db.commit()
        return _run_public(db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone())


def create_local_agent_router(db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/api/local-agents", tags=["local-agents"])

    @router.get("/providers")
    def get_providers(probe: bool = True) -> list[dict[str, Any]]:
        return provider_statuses(db_path, probe=probe)

    @router.post("/providers", status_code=201)
    def register_provider(payload: ProviderRegistration) -> dict[str, Any]:
        provider_id = payload.adapter
        with closing(connect(db_path)) as db:
            now = utc_now()
            db.execute(
                """INSERT INTO local_agent_providers
                (id, adapter, label, executable_path, enabled, timeout_seconds, model, capabilities,
                 probe_state, installed, callable, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', 0, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET label = excluded.label, executable_path = excluded.executable_path,
                  enabled = excluded.enabled, timeout_seconds = excluded.timeout_seconds, model = excluded.model,
                  probe_state = 'unknown', callable = 0, auth_state = 'unknown', model_state = 'unknown',
                  callable_state = 'unknown', updated_at = excluded.updated_at""",
                (
                    provider_id, payload.adapter, payload.label or ("Codex CLI" if payload.adapter == "codex" else "Kimi Code CLI"),
                    payload.executable_path or None, int(payload.enabled), payload.timeout_seconds, payload.model.strip(),
                    json.dumps(_adapter_capabilities(payload.adapter), ensure_ascii=False), now, now,
                ),
            )
            db.commit()
        return probe_provider(db_path, provider_id)

    @router.patch("/providers/{provider_id}")
    def update_provider(provider_id: str, payload: ProviderUpdate) -> dict[str, Any]:
        values = payload.model_dump(exclude_none=True)
        if not values:
            return probe_provider(db_path, provider_id)
        if "enabled" in values:
            values["enabled"] = int(values["enabled"])
        if "model" in values:
            values["model"] = values["model"].strip()
        if "executable_path" in values:
            values["executable_path"] = values["executable_path"].strip() or None
        with closing(connect(db_path)) as db:
            if not db.execute("SELECT 1 FROM local_agent_providers WHERE id = ?", (provider_id,)).fetchone():
                raise HTTPException(404, "本地 Agent 提供方不存在")
            assignments = ", ".join(f"{key} = ?" for key in values)
            db.execute(
                f"""UPDATE local_agent_providers SET {assignments}, probe_state = 'unknown', callable = 0,
                auth_state = 'unknown', model_state = 'unknown', callable_state = 'unknown', updated_at = ?
                WHERE id = ?""",
                (*values.values(), utc_now(), provider_id),
            )
            db.commit()
        return probe_provider(db_path, provider_id)

    @router.post("/providers/{provider_id}/probe")
    def probe(provider_id: str) -> dict[str, Any]:
        return probe_provider(db_path, provider_id)

    @router.get("/runs")
    def list_runs() -> list[dict[str, Any]]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            return [
                _run_public(row)
                for row in db.execute(
                    "SELECT * FROM creative_agent_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 100",
                    (project["id"],),
                ).fetchall()
            ]

    @router.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            row = db.execute(
                "SELECT * FROM creative_agent_runs WHERE id = ? AND project_id = ?", (run_id, project["id"])
            ).fetchone()
            if not row:
                raise HTTPException(404, "Agent 任务不存在")
            return _run_public(row)

    @router.post("/runs", status_code=202)
    def create_run(payload: AgentRunCreate) -> dict[str, Any]:
        run = create_run_record(db_path, payload)
        enqueue_agent_run(db_path, run["id"])
        return run

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            row = db.execute(
                "SELECT * FROM creative_agent_runs WHERE id = ? AND project_id = ?", (run_id, project["id"])
            ).fetchone()
            if not row:
                raise HTTPException(404, "Agent 任务不存在")
            now = utc_now()
            cancelled = db.execute(
                """UPDATE creative_agent_runs SET state = 'cancelled', cancel_requested = 1,
                message = '任务已取消，正式内容未改变', updated_at = ?, completed_at = ?
                WHERE id = ? AND project_id = ? AND state IN ('queued', 'running') AND cancel_requested = 0""",
                (now, now, run_id, project["id"]),
            )
            if cancelled.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "当前任务无法取消")
            db.commit()
        with PROCESS_LOCK:
            process = PROCESSES.get(run_id)
            if process and process.poll() is None:
                process.terminate()
        with closing(connect(db_path)) as db:
            return _run_public(db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone())

    @router.post("/runs/{run_id}/retry", status_code=202)
    def retry_run(run_id: str) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            project = _active_project(db)
            row = db.execute(
                "SELECT * FROM creative_agent_runs WHERE id = ? AND project_id = ?", (run_id, project["id"])
            ).fetchone()
            if not row:
                raise HTTPException(404, "Agent 任务不存在")
            if row["state"] not in ("failed", "cancelled"):
                raise HTTPException(409, "只有失败或已取消的任务可以重试")
            payload = AgentRunCreate(
                provider_id=row["provider_id"], scope=row["scope"], operation=row["operation"],
                target_id=row["target_id"], parent_id=row["parent_id"], instruction=row["instruction"],
            )
            attempt = int(row["attempt"]) + 1
        run = create_run_record(db_path, payload, retry_of=run_id, attempt=attempt)
        enqueue_agent_run(db_path, run["id"])
        return run

    @router.post("/runs/{run_id}/apply")
    def apply_run(run_id: str, payload: AgentApply) -> dict[str, Any]:
        return apply_agent_proposal(db_path, run_id, payload.confirmed_by)

    @router.post("/runs/{run_id}/reject")
    def reject_run(run_id: str, payload: AgentApply) -> dict[str, Any]:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            project = _active_project(db)
            row = db.execute(
                "SELECT * FROM creative_agent_runs WHERE id = ? AND project_id = ?", (run_id, project["id"])
            ).fetchone()
            if not row:
                raise HTTPException(404, "Agent 任务不存在")
            now = utc_now()
            rejected = db.execute(
                """UPDATE creative_agent_runs SET state = 'rejected', message = '提案已拒绝，正式内容未改变',
                confirmed_by = ?, updated_at = ?, completed_at = COALESCE(completed_at, ?)
                WHERE id = ? AND project_id = ? AND state = 'completed'""",
                (payload.confirmed_by.strip(), now, now, run_id, project["id"]),
            )
            if rejected.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "只有待确认提案可以拒绝")
            db.commit()
            return _run_public(db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone())

    return router
