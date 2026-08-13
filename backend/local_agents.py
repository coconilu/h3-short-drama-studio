from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
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
    if result["callable"]:
        result["action_hint"] = "已可调用，可在创作规划中选择此提供方。"
    elif not result["enabled"]:
        result["action_hint"] = "提供方已停用；在系统设置中重新启用后再探测。"
    elif result["installed"]:
        result["action_hint"] = "CLI 已找到但调用失败；请检查登录状态，并重新探测。"
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


def _command_prefix(executable: str) -> list[str]:
    return [sys.executable, executable] if Path(executable).suffix.lower() == ".py" else [executable]


def probe_provider(db_path: Path, provider_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM local_agent_providers WHERE id = ?", (provider_id,)).fetchone()
        if not row:
            raise HTTPException(404, "本地 Agent 提供方不存在")
        executable = _resolve_executable(row)
        installed = bool(executable)
        callable_state = False
        version: str | None = None
        error: str | None = None
        if not row["enabled"]:
            error = "提供方已停用"
        elif not executable:
            error = "未找到可执行文件"
        else:
            try:
                result = subprocess.run(
                    [*_command_prefix(executable), "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=min(10, int(row["timeout_seconds"])),
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                version = (result.stdout or result.stderr or "已检测").strip().splitlines()[0][:160]
                callable_state = result.returncode == 0
                if not callable_state:
                    error = f"版本探测退出码 {result.returncode}"
            except subprocess.TimeoutExpired:
                error = "版本探测超时"
            except OSError as exc:
                error = str(exc)[:500]
        now = utc_now()
        db.execute(
            """UPDATE local_agent_providers
            SET executable_path = COALESCE(executable_path, ?), probe_state = ?, installed = ?, callable = ?,
                version = ?, last_error = ?, last_probe_at = ?, updated_at = ? WHERE id = ?""",
            (
                executable,
                "available" if callable_state else "unavailable",
                int(installed),
                int(callable_state),
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
    if adapter != "kimi":
        return raw
    assistant_messages: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("role") == "assistant" and isinstance(event.get("content"), str):
            assistant_messages.append(event["content"])
    if not assistant_messages:
        raise ValueError("Kimi stream-json 中没有 assistant 输出")
    return assistant_messages[-1]


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
        args = []
        if model:
            args.extend(["--model", model])
        args.extend(["--prompt", prompt, "--output-format", "stream-json"])
        safe_args = (["--model", model] if model else []) + ["--prompt", "<redacted-input>", "--output-format", "stream-json"]
        return [*prefix, *args], None, {"adapter": adapter, "executable": Path(executable).name, "arguments": safe_args}
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
             input_summary, input_hash, base_payload, base_revisions, model, retry_of, attempt, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', '等待本地 Agent', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, project["id"], payload.provider_id, payload.scope, payload.operation,
                payload.target_id, payload.parent_id, payload.instruction.strip(), summary,
                hashlib.sha256(hashed_input.encode("utf-8")).hexdigest(), encoded,
                json.dumps(base_revisions, ensure_ascii=False), provider["model"], retry_of, attempt, now, now,
            ),
        )
        db.commit()
        return _run_public(db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone())


def _set_failed(db_path: Path, run_id: str, error: str, raw_output: str = "", log: str = "") -> None:
    with closing(connect(db_path)) as db:
        current = db.execute("SELECT state FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone()
        if not current or current["state"] == "cancelled":
            return
        now = utc_now()
        db.execute(
            """UPDATE creative_agent_runs SET state = 'failed', message = 'Agent 任务失败', error = ?,
            raw_output = ?, log = ?, updated_at = ?, completed_at = ? WHERE id = ?""",
            (error[:2000], raw_output[-50000:], log[-20000:], now, now, run_id),
        )
        db.commit()


def execute_agent_run(db_path: Path, run_id: str) -> None:
    with closing(connect(db_path)) as db:
        run = db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone()
        if not run or run["state"] != "queued":
            return
        provider = db.execute("SELECT * FROM local_agent_providers WHERE id = ?", (run["provider_id"],)).fetchone()
        executable = _resolve_executable(provider) if provider else None
        if not provider or not provider["enabled"] or not provider["callable"] or not executable:
            _set_failed(db_path, run_id, "所选提供方已不可调用；任务不会自动切换到其他提供方")
            return
        now = utc_now()
        db.execute(
            """UPDATE creative_agent_runs SET state = 'running', message = '本地 Agent 正在生成提案',
            provider_version = ?, started_at = ?, updated_at = ? WHERE id = ?""",
            (provider["version"], now, now, run_id),
        )
        db.commit()

    raw = ""
    stderr = ""
    try:
        with tempfile.TemporaryDirectory(prefix="jingchang-agent-") as temp_name:
            sandbox = Path(temp_name)
            result_path = sandbox / "result.json"
            command, stdin_text, command_info = provider_command(
                provider["adapter"], executable, sandbox, _build_prompt(run), result_path, run["model"]
            )
            with closing(connect(db_path)) as db:
                db.execute(
                    "UPDATE creative_agent_runs SET command_info = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(command_info, ensure_ascii=False), utc_now(), run_id),
                )
                db.commit()
            process = subprocess.Popen(
                command,
                cwd=sandbox,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with PROCESS_LOCK:
                PROCESSES[run_id] = process
            try:
                stdout, stderr = process.communicate(input=stdin_text, timeout=int(provider["timeout_seconds"]))
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                raise TimeoutError(f"本地 Agent 超过 {provider['timeout_seconds']} 秒未完成")
            finally:
                with PROCESS_LOCK:
                    PROCESSES.pop(run_id, None)
            raw = result_path.read_text(encoding="utf-8") if provider["adapter"] == "codex" and result_path.exists() else stdout
            with closing(connect(db_path)) as db:
                state = db.execute("SELECT state, cancel_requested FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone()
                if not state or state["state"] == "cancelled" or state["cancel_requested"]:
                    return
            if process.returncode != 0:
                raise RuntimeError((stderr or raw or f"CLI 退出码 {process.returncode}")[-2000:])
            proposal = validate_proposal(run["scope"], extract_json(provider_result_text(provider["adapter"], raw)))
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
        if run["state"] != "completed":
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
        now = utc_now()
        db.execute(
            """UPDATE creative_agent_runs SET state = 'applied', message = '提案已由人工确认并创建正式修订',
            confirmed_by = ?, applied_at = ?, updated_at = ? WHERE id = ?""",
            (confirmed_by.strip(), now, now, run_id),
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
                  probe_state = 'unknown', callable = 0, updated_at = excluded.updated_at""",
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
                f"UPDATE local_agent_providers SET {assignments}, probe_state = 'unknown', callable = 0, updated_at = ? WHERE id = ?",
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
            project = _active_project(db)
            row = db.execute(
                "SELECT * FROM creative_agent_runs WHERE id = ? AND project_id = ?", (run_id, project["id"])
            ).fetchone()
            if not row:
                raise HTTPException(404, "Agent 任务不存在")
            if row["state"] not in ACTIVE_STATES:
                raise HTTPException(409, "当前任务无法取消")
            now = utc_now()
            db.execute(
                """UPDATE creative_agent_runs SET state = 'cancelled', cancel_requested = 1,
                message = '任务已取消，正式内容未改变', updated_at = ?, completed_at = ? WHERE id = ?""",
                (now, now, run_id),
            )
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
            project = _active_project(db)
            row = db.execute(
                "SELECT * FROM creative_agent_runs WHERE id = ? AND project_id = ?", (run_id, project["id"])
            ).fetchone()
            if not row:
                raise HTTPException(404, "Agent 任务不存在")
            if row["state"] != "completed":
                raise HTTPException(409, "只有待确认提案可以拒绝")
            now = utc_now()
            db.execute(
                """UPDATE creative_agent_runs SET state = 'rejected', message = '提案已拒绝，正式内容未改变',
                confirmed_by = ?, updated_at = ?, completed_at = COALESCE(completed_at, ?) WHERE id = ?""",
                (payload.confirmed_by.strip(), now, now, run_id),
            )
            db.commit()
            return _run_public(db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone())

    return router
