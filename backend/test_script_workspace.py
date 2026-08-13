from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

try:
    from .script_workspace import (
        _apply_proposal,
        _apply_storyboard_sync,
        _build_storyboard_sync_plan,
        _extract_json,
        _provider_command,
        _provider_result_text,
        _sync_public,
        _snapshot_from_db,
        _save_version,
        connect,
        ensure_script_document,
        init_script_schema,
    )
except ImportError:
    from script_workspace import (
        _apply_proposal,
        _apply_storyboard_sync,
        _build_storyboard_sync_plan,
        _extract_json,
        _provider_command,
        _provider_result_text,
        _sync_public,
        _snapshot_from_db,
        _save_version,
        connect,
        ensure_script_document,
        init_script_schema,
    )


class ScriptWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.db"
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE projects (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, episode TEXT NOT NULL,
              logline TEXT NOT NULL, target_duration REAL NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE shots (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
              scene_code TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, dialogue TEXT NOT NULL,
              prompt TEXT NOT NULL, status TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
              seconds REAL NOT NULL, candidate_count INTEGER NOT NULL, strategy TEXT NOT NULL,
              thumbnail TEXT NOT NULL, video TEXT, subtitle_enabled INTEGER NOT NULL DEFAULT 1,
              subtitle_start_seconds REAL, updated_at TEXT NOT NULL
            );
            CREATE TABLE candidates (
              id TEXT PRIMARY KEY, shot_id TEXT NOT NULL REFERENCES shots(id), archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE promotions (
              id TEXT PRIMARY KEY, shot_id TEXT NOT NULL REFERENCES shots(id)
            );
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, shot_id TEXT NOT NULL REFERENCES shots(id),
              kind TEXT NOT NULL, state TEXT NOT NULL
            );
            """
        )
        init_script_schema(db)
        db.execute(
            "INSERT INTO projects VALUES ('p1', '电梯十三层', 'EP01', '林夏误入不存在的十三层。', 18, '2026-08-12')"
        )
        db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', '2026-08-12')")
        for ordinal, title in enumerate(("进入电梯", "越过十二层", "门外的自己"), start=1):
            db.execute(
                """INSERT INTO shots VALUES (?, 'p1', ?, 'S01', ?, ?, '', 'prompt', '未生成',
                608, 352, 6, 2, 'Ref2VA 精修', '', NULL, 1, NULL, '2026-08-12')""",
                (f"shot-{ordinal}", ordinal, title, f"{title}的剧情动作。"),
            )
        db.commit()
        db.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_seed_builds_three_act_structure_from_existing_shots(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            snapshot = _snapshot_from_db(db, document_id)
        self.assertEqual([act["code"] for act in snapshot["acts"]], ["第一幕", "第二幕", "第三幕"])
        self.assertEqual([len(act["scenes"]) for act in snapshot["acts"]], [1, 1, 1])
        self.assertEqual(snapshot["acts"][1]["scenes"][0]["title"], "越过十二层")
        self.assertEqual(snapshot["document"]["version"], 1)

    def test_scene_agent_proposal_creates_new_version_without_touching_shots(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            snapshot = _snapshot_from_db(db, document_id)
            scene = snapshot["acts"][1]["scenes"][0]
            proposal = {
                "scene": {
                    "title": "电梯女声",
                    "summary": "电梯女声叫出林夏的名字。",
                    "goal": "迫使林夏回应。",
                    "conflict": "她无法确认声音来自哪里。",
                    "turning_point": "女声说出只有哥哥知道的秘密。",
                    "hook": "楼层显示从 12 跳到 13。",
                    "content": "女声：林夏，别按开门。",
                    "planned_seconds": 8,
                    "tension": 5,
                }
            }
            run = {
                "id": "run-1",
                "document_id": document_id,
                "scope": "scene",
                "target_id": scene["id"],
                "provider": "codex",
                "proposed_payload": json.dumps(proposal, ensure_ascii=False),
            }
            db.execute(
                """INSERT INTO script_agent_runs
                (id, document_id, scope, target_id, provider, instruction, state, message, proposed_payload,
                 created_at, updated_at) VALUES ('run-1', ?, 'scene', ?, 'codex', '增强悬念', 'completed',
                 '待审阅', ?, '2026-08-12', '2026-08-12')""",
                (document_id, scene["id"], run["proposed_payload"]),
            )
            _apply_proposal(db, run)
            db.commit()
            changed = db.execute("SELECT * FROM script_sections WHERE id = ?", (scene["id"],)).fetchone()
            document = db.execute("SELECT * FROM script_documents WHERE id = ?", (document_id,)).fetchone()
            shot_count = db.execute("SELECT COUNT(*) AS count FROM shots").fetchone()["count"]
            run_state = db.execute("SELECT state FROM script_agent_runs WHERE id = 'run-1'").fetchone()["state"]
        self.assertEqual(changed["title"], "电梯女声")
        self.assertEqual(changed["tension"], 5)
        self.assertEqual(document["version"], 2)
        self.assertEqual(shot_count, 3)
        self.assertEqual(run_state, "applied")

    def test_act_agent_proposal_replaces_only_that_chapters_scenes(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            snapshot = _snapshot_from_db(db, document_id)
            target_act = snapshot["acts"][1]
            proposal = {
                "act": {
                    "title": "困局升级",
                    "summary": "电梯拒绝停靠，规则逐渐显形。",
                    "planned_seconds": 14,
                    "tension": 4,
                    "scenes": [
                        {"title": "电梯女声", "summary": "女声叫出林夏的名字。", "planned_seconds": 7, "tension": 4},
                        {"title": "规则暗示", "summary": "按钮上浮现新的禁令。", "planned_seconds": 7, "tension": 5},
                    ],
                }
            }
            run = {
                "id": "run-act",
                "document_id": document_id,
                "scope": "act",
                "target_id": target_act["id"],
                "provider": "kimi",
                "proposed_payload": json.dumps(proposal, ensure_ascii=False),
            }
            db.execute(
                """INSERT INTO script_agent_runs
                (id, document_id, scope, target_id, provider, instruction, state, message, proposed_payload,
                 created_at, updated_at) VALUES ('run-act', ?, 'act', ?, 'kimi', '升级第二幕', 'completed',
                 '待审阅', ?, '2026-08-12', '2026-08-12')""",
                (document_id, target_act["id"], run["proposed_payload"]),
            )
            _apply_proposal(db, run)
            db.commit()
            changed = _snapshot_from_db(db, document_id)
        self.assertEqual(changed["acts"][1]["title"], "困局升级")
        self.assertEqual([scene["title"] for scene in changed["acts"][1]["scenes"]], ["电梯女声", "规则暗示"])
        self.assertEqual(changed["acts"][0]["scenes"][0]["title"], "进入电梯")

    def test_episode_agent_proposal_rebuilds_outline_but_preserves_production_shots(self) -> None:
        document_id = ensure_script_document(self.db_path)
        acts = []
        for ordinal, title in enumerate(("进入", "困局", "反转"), start=1):
            acts.append({
                "code": f"第{ordinal}幕",
                "title": title,
                "summary": f"{title}阶段",
                "planned_seconds": 10,
                "tension": ordinal + 2,
                "scenes": [{"title": f"新场景{ordinal}", "summary": f"第{ordinal}场", "planned_seconds": 10, "tension": ordinal + 2}],
            })
        proposal = {"title": "电梯十三层·重构稿", "summary": "三幕结构重构。", "total_seconds": 30, "acts": acts}
        with closing(connect(self.db_path)) as db:
            run = {
                "id": "run-episode",
                "document_id": document_id,
                "scope": "episode",
                "target_id": None,
                "provider": "codex",
                "proposed_payload": json.dumps(proposal, ensure_ascii=False),
            }
            db.execute(
                """INSERT INTO script_agent_runs
                (id, document_id, scope, target_id, provider, instruction, state, message, proposed_payload,
                 created_at, updated_at) VALUES ('run-episode', ?, 'episode', NULL, 'codex', '重构整集', 'completed',
                 '待审阅', ?, '2026-08-12', '2026-08-12')""",
                (document_id, run["proposed_payload"]),
            )
            _apply_proposal(db, run)
            db.commit()
            changed = _snapshot_from_db(db, document_id)
            shot_count = db.execute("SELECT COUNT(*) AS count FROM shots").fetchone()["count"]
        self.assertEqual(changed["document"]["title"], "电梯十三层·重构稿")
        self.assertEqual([act["scenes"][0]["title"] for act in changed["acts"]], ["新场景1", "新场景2", "新场景3"])
        self.assertEqual(shot_count, 3)

    def test_provider_commands_are_argument_arrays_and_codex_uses_stdin(self) -> None:
        sandbox = Path(self.temp_dir.name) / "sandbox"
        result = Path(self.temp_dir.name) / "result.json"
        codex_command, codex_stdin = _provider_command("codex", "codex.exe", sandbox, "prompt", result)
        kimi_command, kimi_stdin = _provider_command("kimi", "kimi.exe", sandbox, "prompt", result)
        self.assertEqual(codex_command[0], "codex.exe")
        self.assertIn("read-only", codex_command)
        self.assertEqual(codex_stdin, "prompt")
        self.assertEqual(kimi_command[0], "kimi.exe")
        self.assertIn("--prompt", kimi_command)
        self.assertIn("stream-json", kimi_command)
        self.assertNotIn("--auto", kimi_command)
        self.assertNotIn("--yolo", kimi_command)
        self.assertEqual(kimi_stdin, None)
        self.assertNotIn("shell", codex_command)

    def test_extract_json_accepts_plain_and_fenced_outputs(self) -> None:
        self.assertEqual(_extract_json('{"scene":{"title":"A"}}')["scene"]["title"], "A")
        self.assertEqual(_extract_json('```json\n{"act":{"scenes":[]}}\n```')["act"]["scenes"], [])

    def test_kimi_stream_json_returns_the_assistant_content(self) -> None:
        stream = '\n'.join((
            '{"role":"meta","type":"system.version","version":"0.35.0"}',
            '{"role":"assistant","content":"{\\"scene\\":{\\"title\\":\\"A\\"}}"}',
        ))
        self.assertEqual(_provider_result_text("kimi", stream), '{"scene":{"title":"A"}}')

    def _approve_current_script(self, document_id: str) -> int:
        with closing(connect(self.db_path)) as db:
            document = db.execute("SELECT * FROM script_documents WHERE id = ?", (document_id,)).fetchone()
            version = int(document["version"]) + 1
            db.execute(
                "UPDATE script_documents SET version = ?, status = 'approved', updated_at = '2026-08-13' WHERE id = ?",
                (version, document_id),
            )
            _save_version(db, document_id, version, "approved", "human:lock")
            db.commit()
        return version

    def test_storyboard_sync_requires_an_approved_script(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            with self.assertRaisesRegex(Exception, "请先锁定"):
                _build_storyboard_sync_plan(db, document_id)

    def test_storyboard_sync_updates_narrative_but_preserves_existing_prompt(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            scene = db.execute(
                "SELECT * FROM script_sections WHERE document_id = ? AND section_type = 'scene' ORDER BY ordinal LIMIT 1",
                (document_id,),
            ).fetchone()
            db.execute(
                """UPDATE script_sections SET title = '镜中异影', summary = '镜面里多出一个人。',
                content = '林夏抬头。\n\n林夏：谁在那里？', planned_seconds = 8 WHERE id = ?""",
                (scene["id"],),
            )
            db.execute("UPDATE script_documents SET total_seconds = 20 WHERE id = ?", (document_id,))
            db.commit()
        version = self._approve_current_script(document_id)
        with closing(connect(self.db_path)) as db:
            plan = _build_storyboard_sync_plan(db, document_id)
            first = plan["rows"][0]
            self.assertEqual(plan["script_version"], version)
            self.assertEqual(first["action"], "update")
            self.assertEqual(first["proposed"]["prompt"], "prompt")
            field_diffs = {item["field"]: item for item in first["field_diffs"]}
            self.assertEqual(field_diffs["title"]["before"], "进入电梯")
            self.assertEqual(field_diffs["title"]["after"], "镜中异影")
            self.assertEqual(field_diffs["title"]["decision"], "update")
            self.assertEqual(field_diffs["prompt"]["decision"], "keep")
            result = _apply_storyboard_sync(db, plan)
            db.commit()
            shot = db.execute("SELECT * FROM shots WHERE id = 'shot-1'").fetchone()
            links = db.execute("SELECT COUNT(*) AS count FROM script_storyboard_links").fetchone()["count"]
            legacy_plan = json.loads(db.execute(
                "SELECT plan FROM script_storyboard_syncs WHERE id = ?", (result["id"],),
            ).fetchone()["plan"])
            for item in legacy_plan:
                item.pop("field_diffs", None)
            db.execute(
                "UPDATE script_storyboard_syncs SET plan = ? WHERE id = ?",
                (json.dumps(legacy_plan, ensure_ascii=False), result["id"]),
            )
            db.commit()
            legacy_public = _sync_public(db.execute(
                "SELECT * FROM script_storyboard_syncs WHERE id = ?", (result["id"],),
            ).fetchone())
        self.assertEqual(shot["title"], "镜中异影")
        self.assertEqual(shot["dialogue"], "林夏：谁在那里？")
        self.assertEqual(shot["seconds"], 8)
        self.assertEqual(shot["prompt"], "prompt")
        self.assertEqual(links, 3)
        self.assertEqual(result["state"], "applied")
        self.assertTrue(all(item["field_diffs"] for item in legacy_public["plan"]))

    def test_storyboard_sync_protects_shots_with_generated_candidates(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            scene = db.execute(
                "SELECT id FROM script_sections WHERE document_id = ? AND ordinal = 2 AND section_type = 'scene'",
                (document_id,),
            ).fetchone()
            db.execute("UPDATE script_sections SET title = '重写后的第二场' WHERE id = ?", (scene["id"],))
            db.execute("INSERT INTO candidates VALUES ('candidate-1', 'shot-2', 0)")
            db.commit()
        self._approve_current_script(document_id)
        with closing(connect(self.db_path)) as db:
            plan = _build_storyboard_sync_plan(db, document_id)
            protected = next(item for item in plan["rows"] if item["shot_id"] == "shot-2")
            title_diff = next(item for item in protected["field_diffs"] if item["field"] == "title")
            _apply_storyboard_sync(db, plan)
            db.commit()
            shot = db.execute("SELECT title FROM shots WHERE id = 'shot-2'").fetchone()
        self.assertEqual(protected["action"], "protected")
        self.assertIn("已有 1 条候选", protected["protected_reasons"])
        self.assertEqual(title_diff["decision"], "protected")
        self.assertEqual(title_diff["after"], "重写后的第二场")
        self.assertEqual(shot["title"], "越过十二层")

    def test_storyboard_sync_creates_a_shot_for_a_new_scene(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            act = db.execute(
                "SELECT id FROM script_sections WHERE document_id = ? AND section_type = 'act' ORDER BY ordinal LIMIT 1",
                (document_id,),
            ).fetchone()
            db.execute(
                """INSERT INTO script_sections
                (id, document_id, parent_id, section_type, ordinal, code, title, summary, goal, conflict,
                 turning_point, hook, content, planned_seconds, tension, status, updated_at)
                VALUES ('new-scene-4', ?, ?, 'scene', 2, 'S01B', '新增结尾', '门再次打开。', '', '', '', '',
                        '门再次打开。', 7, 5, 'draft', '2026-08-13')""",
                (document_id, act["id"]),
            )
            db.commit()
        self._approve_current_script(document_id)
        with closing(connect(self.db_path)) as db:
            plan = _build_storyboard_sync_plan(db, document_id)
            self.assertEqual(plan["summary"]["create"], 1)
            created_plan = next(item for item in plan["rows"] if item["action"] == "create")
            self.assertTrue(all(item["decision"] == "create" for item in created_plan["field_diffs"]))
            self.assertTrue(all(item["before"] is None for item in created_plan["field_diffs"]))
            _apply_storyboard_sync(db, plan)
            db.commit()
            shot_count = db.execute("SELECT COUNT(*) AS count FROM shots").fetchone()["count"]
            created = db.execute("SELECT prompt FROM shots WHERE title = '新增结尾'").fetchone()
        self.assertEqual(shot_count, 4)
        self.assertIn("横屏 16:9", created["prompt"])

    def test_storyboard_sync_preserves_a_shot_removed_from_the_script(self) -> None:
        document_id = ensure_script_document(self.db_path)
        with closing(connect(self.db_path)) as db:
            third = db.execute(
                "SELECT id FROM script_sections WHERE document_id = ? AND section_type = 'scene' ORDER BY ordinal DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            db.execute("DELETE FROM script_sections WHERE id = ?", (third["id"],))
            db.commit()
        self._approve_current_script(document_id)
        with closing(connect(self.db_path)) as db:
            plan = _build_storyboard_sync_plan(db, document_id)
            self.assertEqual(plan["summary"]["preserve"], 1)
            preserved_plan = next(item for item in plan["rows"] if item["action"] == "preserve")
            self.assertEqual([item["decision"] for item in preserved_plan["field_diffs"]], ["keep", "keep"])
            _apply_storyboard_sync(db, plan)
            db.commit()
            shot_count = db.execute("SELECT COUNT(*) AS count FROM shots").fetchone()["count"]
            preserved = db.execute("SELECT title FROM shots WHERE id = 'shot-3'").fetchone()
        self.assertEqual(shot_count, 3)
        self.assertEqual(preserved["title"], "门外的自己")

    def test_storyboard_sync_unchanged_rows_expose_all_review_fields(self) -> None:
        document_id = ensure_script_document(self.db_path)
        self._approve_current_script(document_id)
        with closing(connect(self.db_path)) as db:
            first_plan = _build_storyboard_sync_plan(db, document_id)
            _apply_storyboard_sync(db, first_plan)
            db.commit()
            current_plan = _build_storyboard_sync_plan(db, document_id)
        first = current_plan["rows"][0]
        self.assertEqual(first["action"], "unchanged")
        self.assertEqual(len(first["field_diffs"]), 7)
        self.assertTrue(all(item["decision"] == "keep" for item in first["field_diffs"]))


if __name__ == "__main__":
    unittest.main()
