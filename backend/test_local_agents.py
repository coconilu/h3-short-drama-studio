from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend import app as studio
from backend.content_planning import SectionUpdate, _workspace, create_content_router
from backend.local_agents import (
    AgentApply,
    AgentRunCreate,
    ProviderRegistration,
    apply_agent_proposal,
    create_local_agent_router,
    create_run_record,
    execute_agent_run,
    init_local_agent_schema,
    probe_provider,
    recover_local_agent_runs,
    validate_proposal,
)


FAKE_CLI = r'''from __future__ import annotations
import json
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("fake-local-agent 1.0")
    raise SystemExit(0)

prompt = sys.stdin.read() if "exec" in sys.argv else sys.argv[sys.argv.index("--prompt") + 1]
if "TIMEOUT_TEST" in prompt:
    time.sleep(5)
if "FAIL_ONCE_TEST" in prompt:
    marker = Path(__file__).with_suffix(".once")
    if not marker.exists():
        marker.write_text("failed", encoding="utf-8")
        response = "not-json-first-attempt"
    else:
        response = json.dumps({"proposal": {"content": "重试后正文"}}, ensure_ascii=False)
elif "INVALID_JSON_TEST" in prompt:
    response = "not-json-output"
elif "作用域：plot" in prompt:
    response = json.dumps({"proposal": {"title": "Agent 剧情", "synopsis": "新的剧情梗概", "core_conflict": "真相与记忆", "ending": "她选择真相"}}, ensure_ascii=False)
elif "作用域：outline" in prompt:
    response = json.dumps({"proposal": {"chapters": [{"title": "第一章", "summary": "进入", "pacing_goal": "快", "planned_seconds": 20, "sections": [{"title": "第一节", "summary": "发现线索", "content": "林夏推门。", "pacing_goal": "悬疑", "planned_seconds": 8}]}]}}, ensure_ascii=False)
elif "作用域：chapter" in prompt:
    response = json.dumps({"proposal": {"title": "重写章节", "summary": "章节摘要", "pacing_goal": "递进", "planned_seconds": 24, "sections": []}}, ensure_ascii=False)
elif "作用域：section" in prompt:
    response = json.dumps({"proposal": {"title": "Agent 小节", "summary": "小节摘要", "content": "人物进入房间。", "pacing_goal": "紧张", "planned_seconds": 9}}, ensure_ascii=False)
else:
    response = json.dumps({"proposal": {"content": "Agent 生成的正式正文候选。"}}, ensure_ascii=False)

if "-o" in sys.argv:
    Path(sys.argv[sys.argv.index("-o") + 1]).write_text(response, encoding="utf-8")
else:
    print(json.dumps({"role": "assistant", "content": response}, ensure_ascii=False))
'''


class LocalAgentContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        studio.DB_PATH = root / "studio.db"
        studio.EXPORT_ROOT = root / "exports"
        studio.EXPORT_JOB_ROOT = root / "jobs"
        studio.EXPORT_ROOT.mkdir()
        studio.EXPORT_JOB_ROOT.mkdir()
        studio.init_db()
        self.fake_cli = root / "fake_agent.py"
        self.fake_cli.write_text(FAKE_CLI, encoding="utf-8")
        self.workspace = _workspace(studio.DB_PATH)
        self.section = self.workspace["chapters"][0]["sections"][0]
        with closing(studio.connect()) as db:
            init_local_agent_schema(db)
            db.commit()
        self.router = create_local_agent_router(studio.DB_PATH)
        self.endpoints = {}
        for route in self.router.routes:
            for method in route.methods or []:
                self.endpoints[(route.path, method)] = route.endpoint
        for adapter in ("codex", "kimi"):
            self.call(
                "/api/local-agents/providers", "POST",
                ProviderRegistration(adapter=adapter, executable_path=str(self.fake_cli), timeout_seconds=2),
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, path: str, method: str, *args):
        return self.endpoints[(path, method)](*args)

    def row(self, run_id: str) -> dict[str, object]:
        with closing(studio.connect()) as db:
            return dict(db.execute("SELECT * FROM creative_agent_runs WHERE id = ?", (run_id,)).fetchone())

    def run_sync(self, payload: AgentRunCreate) -> dict[str, object]:
        run = create_run_record(studio.DB_PATH, payload)
        execute_agent_run(studio.DB_PATH, run["id"])
        return self.row(run["id"])

    def test_provider_registration_probe_reports_both_adapters_and_actionable_unavailable_hint(self) -> None:
        providers = self.call("/api/local-agents/providers", "GET", False)
        self.assertEqual({item["id"] for item in providers}, {"codex", "kimi"})
        self.assertTrue(all(item["installed"] and item["callable"] for item in providers))
        unavailable = self.call(
            "/api/local-agents/providers", "POST",
            ProviderRegistration(adapter="kimi", executable_path=str(self.fake_cli.with_name("missing.py"))),
        )
        self.assertFalse(unavailable["installed"])
        self.assertFalse(unavailable["callable"])
        self.assertIn("可执行文件路径", unavailable["action_hint"])

    def test_all_five_scopes_have_strict_structured_contracts(self) -> None:
        cases = {
            "plot": {"proposal": {"title": "A", "synopsis": "B", "core_conflict": "C", "ending": "D"}},
            "outline": {"proposal": {"chapters": [{"title": "A", "summary": "B", "pacing_goal": "C", "planned_seconds": 10, "sections": []}]}},
            "chapter": {"proposal": {"title": "A", "summary": "B", "pacing_goal": "C", "planned_seconds": 10, "sections": []}},
            "section": {"proposal": {"title": "A", "summary": "B", "content": "C", "pacing_goal": "D", "planned_seconds": 10}},
            "body": {"proposal": {"content": "正文"}},
        }
        for scope, payload in cases.items():
            with self.subTest(scope=scope):
                self.assertEqual(validate_proposal(scope, payload)["proposal"], payload["proposal"])
        with self.assertRaises(ValueError):
            validate_proposal("body", {"proposal": {"content": ""}})

    def test_codex_success_stays_proposal_until_human_apply_creates_revision(self) -> None:
        before = self.section.copy()
        result = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="body", operation="generate", target_id=before["id"],
            instruction="补写一段正文",
        ))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(json.loads(result["proposed_payload"])["proposal"]["content"], "Agent 生成的正式正文候选。")
        self.assertTrue(json.loads(result["diff_payload"]))
        with closing(studio.connect()) as db:
            unchanged = db.execute("SELECT content, revision FROM creative_sections WHERE id = ?", (before["id"],)).fetchone()
            self.assertEqual(unchanged["content"], before["content"])
            self.assertEqual(unchanged["revision"], before["revision"])
        applied = apply_agent_proposal(studio.DB_PATH, result["id"], "human:test-reviewer")
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["confirmed_by"], "human:test-reviewer")
        with closing(studio.connect()) as db:
            changed = db.execute("SELECT content, revision FROM creative_sections WHERE id = ?", (before["id"],)).fetchone()
            self.assertEqual(changed["content"], "Agent 生成的正式正文候选。")
            self.assertEqual(changed["revision"], before["revision"] + 1)
            source = db.execute(
                "SELECT source FROM creative_revisions WHERE entity_type = 'section' AND entity_id = ? ORDER BY revision DESC LIMIT 1",
                (before["id"],),
            ).fetchone()["source"]
            self.assertIn("human:test-reviewer", source)

    def test_kimi_adapter_success_records_model_command_hash_and_no_prompt_in_command(self) -> None:
        self.call(
            "/api/local-agents/providers", "POST",
            ProviderRegistration(
                adapter="kimi", executable_path=str(self.fake_cli), timeout_seconds=2, model="fake-kimi-model",
            ),
        )
        result = self.run_sync(AgentRunCreate(
            provider_id="kimi", scope="plot", operation="generate", instruction="生成悬疑剧情 SECRET_INPUT",
        ))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(len(result["input_hash"]), 64)
        command = json.loads(result["command_info"])
        self.assertEqual(command["adapter"], "kimi")
        self.assertNotIn("SECRET_INPUT", json.dumps(command, ensure_ascii=False))
        self.assertIn("fake-kimi-model", command["arguments"])
        self.assertEqual(result["model"], "fake-kimi-model")
        self.assertEqual(result["provider_version"], "fake-local-agent 1.0")
        self.assertEqual(json.loads(result["proposed_payload"])["proposal"]["title"], "Agent 剧情")

    def test_plot_chapter_section_and_outline_proposals_apply_only_after_confirmation(self) -> None:
        plot = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="plot", operation="generate", instruction="生成另一个剧情",
        ))
        self.assertEqual(plot["state"], "completed")
        apply_agent_proposal(studio.DB_PATH, plot["id"], "human:test")
        self.assertIn("Agent 剧情", [item["title"] for item in _workspace(studio.DB_PATH)["proposals"]])

        chapter = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="chapter", operation="generate", instruction="生成一个章节",
        ))
        apply_agent_proposal(studio.DB_PATH, chapter["id"], "human:test")
        chapter_id = next(item["id"] for item in _workspace(studio.DB_PATH)["chapters"] if item["title"] == "重写章节")

        section = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="section", operation="generate", parent_id=chapter_id,
            instruction="生成一个小节",
        ))
        apply_agent_proposal(studio.DB_PATH, section["id"], "human:test")
        generated_chapter = next(item for item in _workspace(studio.DB_PATH)["chapters"] if item["id"] == chapter_id)
        self.assertEqual(generated_chapter["sections"][0]["content"], "人物进入房间。")

        outline = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="outline", operation="rewrite", instruction="重写完整大纲",
        ))
        before_apply = _workspace(studio.DB_PATH)["chapters"]
        before_ids = [item["id"] for item in before_apply]
        apply_agent_proposal(studio.DB_PATH, outline["id"], "human:test")
        after_apply = _workspace(studio.DB_PATH)["chapters"]
        self.assertEqual([item["title"] for item in after_apply], ["第一章"])
        self.assertTrue(set(before_ids).isdisjoint(item["id"] for item in after_apply))
        self.assertEqual(after_apply[0]["sections"][0]["content"], "林夏推门。")

    def test_invalid_json_fails_keeps_raw_output_and_does_not_mutate_content(self) -> None:
        before = self.section.copy()
        result = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="body", operation="rewrite", target_id=before["id"],
            instruction="INVALID_JSON_TEST",
        ))
        self.assertEqual(result["state"], "failed")
        self.assertIn("not-json-output", result["raw_output"])
        with closing(studio.connect()) as db:
            unchanged = db.execute("SELECT content, revision FROM creative_sections WHERE id = ?", (before["id"],)).fetchone()
            self.assertEqual(dict(unchanged), {"content": before["content"], "revision": before["revision"]})

    def test_timeout_is_failed_and_recoverable(self) -> None:
        with closing(studio.connect()) as db:
            db.execute("UPDATE local_agent_providers SET timeout_seconds = 1 WHERE id = 'codex'")
            db.commit()
        started = time.monotonic()
        result = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="body", operation="rewrite", target_id=self.section["id"],
            instruction="TIMEOUT_TEST",
        ))
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(result["state"], "failed")
        self.assertIn("超过 1 秒", result["error"])
        self.assertEqual(result["recoverable"], 1)

    def test_running_task_can_be_cancelled_without_mutation(self) -> None:
        run = create_run_record(studio.DB_PATH, AgentRunCreate(
            provider_id="codex", scope="body", operation="rewrite", target_id=self.section["id"],
            instruction="TIMEOUT_TEST",
        ))
        from backend.local_agents import enqueue_agent_run
        enqueue_agent_run(studio.DB_PATH, run["id"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.row(run["id"])["state"] == "queued":
            time.sleep(0.02)
        cancelled = self.call(f"/api/local-agents/runs/{{run_id}}/cancel", "POST", run["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        time.sleep(0.2)
        self.assertEqual(self.row(run["id"])["state"], "cancelled")
        with closing(studio.connect()) as db:
            current = db.execute("SELECT revision FROM creative_sections WHERE id = ?", (self.section["id"],)).fetchone()
            self.assertEqual(current["revision"], self.section["revision"])

    def test_failed_task_retry_uses_same_provider_and_succeeds_without_fallback(self) -> None:
        failed = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="body", operation="rewrite", target_id=self.section["id"],
            instruction="FAIL_ONCE_TEST",
        ))
        self.assertEqual(failed["state"], "failed")
        retry = self.call(f"/api/local-agents/runs/{{run_id}}/retry", "POST", failed["id"])
        deadline = time.monotonic() + 4
        state = self.row(retry["id"])
        while time.monotonic() < deadline and state["state"] in ("queued", "running"):
            time.sleep(0.03)
            state = self.row(retry["id"])
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["provider_id"], failed["provider_id"])
        self.assertEqual(state["retry_of"], failed["id"])
        self.assertEqual(state["attempt"], 2)

    def test_stale_proposal_cannot_apply_and_service_restart_marks_running_run_retryable(self) -> None:
        completed = self.run_sync(AgentRunCreate(
            provider_id="codex", scope="body", operation="rewrite", target_id=self.section["id"],
            instruction="改写正文",
        ))
        with closing(studio.connect()) as db:
            db.execute("UPDATE creative_sections SET revision = revision + 1 WHERE id = ?", (self.section["id"],))
            db.commit()
        with self.assertRaises(HTTPException) as stale:
            apply_agent_proposal(studio.DB_PATH, completed["id"], "human:test")
        self.assertEqual(stale.exception.status_code, 409)
        queued = create_run_record(studio.DB_PATH, AgentRunCreate(
            provider_id="codex", scope="plot", operation="generate", instruction="生成剧情",
        ))
        with closing(studio.connect()) as db:
            db.execute("UPDATE creative_agent_runs SET state = 'running' WHERE id = ?", (queued["id"],))
            db.commit()
        recover_local_agent_runs(studio.DB_PATH)
        recovered = self.row(queued["id"])
        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(recovered["recoverable"], 1)
        self.assertIn("服务重启", recovered["error"])

    def test_schema_has_no_plaintext_secret_columns(self) -> None:
        with closing(studio.connect()) as db:
            provider_columns = {row[1] for row in db.execute("PRAGMA table_info(local_agent_providers)")}
        self.assertFalse(provider_columns & {"api_key", "token", "secret", "password"})

    def test_unavailable_agent_does_not_block_manual_body_revision(self) -> None:
        self.call(
            "/api/local-agents/providers", "POST",
            ProviderRegistration(adapter="codex", executable_path=str(self.fake_cli.with_name("missing.py"))),
        )
        content_router = create_content_router(studio.DB_PATH)
        update_section = next(
            route.endpoint
            for route in content_router.routes
            if route.path == "/api/creative-planning/sections/{section_id}" and "PATCH" in (route.methods or set())
        )
        updated = update_section(
            self.section["id"],
            SectionUpdate(
                base_revision=self.section["revision"], content="纯人工写入的正文。", source="human:test-manual",
            ),
        )
        section = next(
            item
            for chapter in updated["chapters"]
            for item in chapter["sections"]
            if item["id"] == self.section["id"]
        )
        self.assertEqual(section["content"], "纯人工写入的正文。")
        self.assertEqual(section["revision"], self.section["revision"] + 1)


if __name__ == "__main__":
    unittest.main()
