from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend import app as studio
from backend.content_planning import (
    ChapterCreate,
    CharacterCreate,
    CharacterUpdate,
    OrderUpdate,
    RevisionBase,
    SectionCreate,
    SectionDecision,
    SectionUpdate,
    create_content_router,
)
from backend.creative_storyboard import (
    StoryboardApplyRequest,
    StoryboardPreviewRequest,
    create_storyboard_router,
)
from backend.prompt_compiler import approve_plan, compile_prompt_plan, record_validation


class CreativeStoryboardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        studio.DB_PATH = root / "studio.db"
        studio.ASSET_ROOT = root / "assets"
        studio.EXPORT_ROOT = root / "exports"
        studio.EXPORT_JOB_ROOT = root / "jobs"
        studio.ASSET_ROOT.mkdir()
        studio.EXPORT_ROOT.mkdir()
        studio.EXPORT_JOB_ROOT.mkdir()
        studio.init_db()
        self.project = studio.create_project(
            studio.ProjectCreate(title="同步合同项目", episode="EP01", logline="", target_duration=60, shots=[])
        )
        self.content = self._endpoints(create_content_router(studio.DB_PATH))
        self.storyboard = self._endpoints(create_storyboard_router(studio.DB_PATH))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _endpoints(router):
        endpoints = {}
        for route in router.routes:
            for method in route.methods or []:
                endpoints[(route.path, method)] = route.endpoint
        return endpoints

    def call_content(self, path: str, method: str, *args):
        return self.content[(path, method)](*args)

    def call_storyboard(self, path: str, method: str, *args):
        return self.storyboard[(path, method)](*args)

    def create_section(self, title: str, *, chapter_id: str | None = None, seconds: float = 5.17):
        if chapter_id is None:
            workspace = self.call_content("/api/creative-planning", "GET")
            if not workspace["chapters"]:
                workspace = self.call_content(
                    "/api/creative-planning/chapters", "POST", ChapterCreate(title="第一章", summary="测试章节")
                )
            chapter_id = workspace["chapters"][0]["id"]
        workspace = self.call_content(
            "/api/creative-planning/chapters/{chapter_id}/sections",
            "POST",
            chapter_id,
            SectionCreate(
                title=title,
                summary=f"{title}摘要",
                content=f"{title}完整正文",
                scene=f"雨夜室内，{title}",
                action=f"人物完成{title}动作",
                dialogue=f"{title}对白",
                sound=f"{title}环境声",
                visual=f"{title}中景缓慢推进",
                pacing_goal="克制",
                planned_seconds=seconds,
            ),
        )
        return next(
            section
            for chapter in workspace["chapters"]
            for section in chapter["sections"]
            if section["title"] == title
        )

    def approve_section(self, section: dict):
        workspace = self.call_content(
            "/api/creative-planning/sections/{section_id}/approve",
            "POST",
            section["id"],
            SectionDecision(base_revision=section["revision"], source="human:test-approve"),
        )
        return next(
            item
            for chapter in workspace["chapters"]
            for item in chapter["sections"]
            if item["id"] == section["id"]
        )

    def preview(self):
        return self.call_storyboard(
            "/api/creative-planning/storyboard-sync/preview", "POST", StoryboardPreviewRequest()
        )

    def apply(self, preview: dict):
        return self.call_storyboard(
            "/api/creative-planning/storyboard-sync/apply",
            "POST",
            StoryboardApplyRequest(plan_hash=preview["plan_hash"], confirm=True, confirmed_by="human:test"),
        )

    def test_structured_section_approve_return_and_immutable_versions(self) -> None:
        section = self.create_section("门口迟疑")
        approved = self.approve_section(section)
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["approved_at"])
        self.assertEqual(approved["scene"], "雨夜室内，门口迟疑")
        self.assertEqual(approved["version_count"], 2)

        with self.assertRaises(HTTPException) as missing_note:
            self.call_content(
                "/api/creative-planning/sections/{section_id}/return",
                "POST",
                approved["id"],
                SectionDecision(base_revision=approved["revision"], note="", source="human:test-return"),
            )
        self.assertEqual(missing_note.exception.status_code, 422)

        returned_workspace = self.call_content(
            "/api/creative-planning/sections/{section_id}/return",
            "POST",
            approved["id"],
            SectionDecision(base_revision=approved["revision"], note="补足声音节奏", source="human:test-return"),
        )
        returned = returned_workspace["chapters"][0]["sections"][0]
        self.assertEqual(returned["status"], "draft")
        self.assertEqual(returned["review_note"], "补足声音节奏")
        self.assertIsNone(returned["approved_at"])
        history = self.call_content(
            "/api/creative-planning/history/{entity_type}/{entity_id}", "GET", "section", returned["id"]
        )
        self.assertEqual([item["snapshot"]["status"] for item in history["revisions"][:3]], ["draft", "approved", "draft"])

    def test_preview_is_side_effect_free_confirm_is_explicit_and_retry_is_idempotent(self) -> None:
        section = self.approve_section(self.create_section("新小节"))
        studio.create_shot(
            studio.ShotCreate(title="历史镜头", description="手工画面", prompt="manual prompt", dialogue="")
        )
        with closing(studio.connect()) as db:
            before = {
                "shots": db.execute("SELECT COUNT(*) FROM shots WHERE project_id = ?", (self.project["id"],)).fetchone()[0],
                "links": db.execute("SELECT COUNT(*) FROM creative_storyboard_links WHERE project_id = ?", (self.project["id"],)).fetchone()[0],
                "syncs": db.execute("SELECT COUNT(*) FROM creative_storyboard_syncs WHERE project_id = ?", (self.project["id"],)).fetchone()[0],
            }
        preview = self.preview()
        self.assertEqual(preview["summary"]["create"], 1)
        self.assertEqual(preview["summary"]["preserve"], 1)
        self.assertFalse(preview["safety"]["preview_has_side_effects"])
        with closing(studio.connect()) as db:
            after_preview = {
                "shots": db.execute("SELECT COUNT(*) FROM shots WHERE project_id = ?", (self.project["id"],)).fetchone()[0],
                "links": db.execute("SELECT COUNT(*) FROM creative_storyboard_links WHERE project_id = ?", (self.project["id"],)).fetchone()[0],
                "syncs": db.execute("SELECT COUNT(*) FROM creative_storyboard_syncs WHERE project_id = ?", (self.project["id"],)).fetchone()[0],
            }
        self.assertEqual(after_preview, before)

        with self.assertRaises(HTTPException) as not_confirmed:
            self.call_storyboard(
                "/api/creative-planning/storyboard-sync/apply",
                "POST",
                StoryboardApplyRequest(plan_hash=preview["plan_hash"], confirm=False),
            )
        self.assertEqual(not_confirmed.exception.status_code, 422)

        first = self.apply(preview)
        second = self.apply(preview)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        with closing(studio.connect()) as db:
            shots = db.execute("SELECT * FROM shots WHERE project_id = ? ORDER BY ordinal", (self.project["id"],)).fetchall()
            links = db.execute("SELECT * FROM creative_storyboard_links WHERE project_id = ?", (self.project["id"],)).fetchall()
        self.assertEqual(len(shots), 2)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["section_id"], section["id"])
        self.assertEqual(self.preview()["summary"]["unchanged"], 1)

    def test_update_reorder_and_protected_delete_diffs(self) -> None:
        first = self.approve_section(self.create_section("第一节"))
        second = self.approve_section(self.create_section("第二节", chapter_id=first["chapter_id"]))
        self.apply(self.preview())

        workspace = self.call_content(
            "/api/creative-planning/sections/{section_id}",
            "PATCH",
            first["id"],
            SectionUpdate(base_revision=first["revision"], visual="新的低机位画面", source="human:test-edit"),
        )
        changed = workspace["chapters"][0]["sections"][0]
        self.assertEqual(changed["status"], "draft")
        blocked = self.preview()
        self.assertFalse(blocked["can_apply"])
        self.assertIn("尚未批准", blocked["blockers"][0])
        changed = self.approve_section(changed)
        update_preview = self.preview()
        update_row = next(row for row in update_preview["rows"] if row["section"] and row["section"]["id"] == changed["id"])
        self.assertEqual(update_row["action"], "update")
        self.assertTrue(next(item for item in update_row["field_diffs"] if item["field"] == "description")["changed"])
        self.apply(update_preview)

        current = self.call_content("/api/creative-planning", "GET")
        chapter = current["chapters"][0]
        self.call_content(
            "/api/creative-planning/chapters/{chapter_id}/sections/order",
            "PUT",
            chapter["id"],
            OrderUpdate(
                ids=[chapter["sections"][1]["id"], chapter["sections"][0]["id"]],
                base_revisions={item["id"]: item["revision"] for item in chapter["sections"]},
                parent_base_revision=chapter["revision"],
                source="human:test-reorder",
            ),
        )
        reorder_preview = self.preview()
        self.assertGreaterEqual(reorder_preview["summary"]["reorder"], 1)
        self.apply(reorder_preview)

        current = self.call_content("/api/creative-planning", "GET")
        archived = current["chapters"][0]["sections"][0]
        with closing(studio.connect()) as db:
            shot_id = db.execute(
                "SELECT shot_id FROM creative_storyboard_links WHERE section_id = ?", (archived["id"],)
            ).fetchone()[0]
            db.execute(
                """INSERT INTO candidates
                (id, shot_id, label, seed, created_at, thumbnail, selected, scores, note)
                VALUES ('protected-candidate', ?, '候选 A', 1, 'now', '', 0, '{}', '')""",
                (shot_id,),
            )
            db.commit()
        self.call_content(
            "/api/creative-planning/sections/{section_id}/archive",
            "POST",
            archived["id"],
            RevisionBase(base_revision=archived["revision"], source="human:test-archive"),
        )

        protected = self.preview()
        self.assertEqual(protected["summary"]["protected"], 1)
        self.assertFalse(protected["can_apply"])
        with self.assertRaises(HTTPException) as deletion_blocked:
            self.apply(protected)
        self.assertEqual(deletion_blocked.exception.status_code, 409)
        with closing(studio.connect()) as db:
            self.assertIsNotNone(db.execute("SELECT id FROM shots WHERE id = ?", (shot_id,)).fetchone())
            db.execute("DELETE FROM candidates WHERE id = 'protected-candidate'")
            db.commit()
        deletion = self.preview()
        self.assertEqual(deletion["summary"]["delete"], 1)
        self.apply(deletion)
        with closing(studio.connect()) as db:
            self.assertIsNone(db.execute("SELECT id FROM shots WHERE id = ?", (shot_id,)).fetchone())

    def test_character_rules_and_section_or_reference_changes_stale_h3_plan_with_sources(self) -> None:
        section = self.approve_section(self.create_section("角色登场"))
        applied = self.apply(self.preview())
        shot_id = next(item["shot_id"] for item in applied["sync"]["applied_snapshot"] if item["section_id"] == section["id"])
        plan = compile_prompt_plan(studio.DB_PATH, shot_id)
        record_validation(studio.DB_PATH, plan, {"ok": True})
        approve_plan(studio.DB_PATH, shot_id, plan["plan_hash"])
        self.assertEqual(compile_prompt_plan(studio.DB_PATH, shot_id)["status"], "approved")

        workspace = self.call_content(
            "/api/creative-planning/sections/{section_id}",
            "PATCH",
            section["id"],
            SectionUpdate(base_revision=section["revision"], action="角色突然停步", source="human:test-change"),
        )
        stale = compile_prompt_plan(studio.DB_PATH, shot_id)
        self.assertTrue(stale["stale"])
        self.assertTrue(any("小节" in reason for reason in stale["stale_reasons"]))
        section = self.approve_section(workspace["chapters"][0]["sections"][0])
        self.apply(self.preview())
        plan = compile_prompt_plan(studio.DB_PATH, shot_id)
        record_validation(studio.DB_PATH, plan, {"ok": True})
        approve_plan(studio.DB_PATH, shot_id, plan["plan_hash"])

        character = self.call_content(
            "/api/creative-planning/characters",
            "POST",
            CharacterCreate(
                name="林夏", identity="年轻调查员", appearance="短发、红雨衣、黑色雨靴",
                voice="低声线、语速偏慢、普通话", goal="查明真相",
            ),
        )["characters"][0]
        self.call_content(
            "/api/creative-planning/characters/{character_id}",
            "PATCH",
            character["id"],
            CharacterUpdate(base_revision=character["revision"], status="approved", source="human:test-lock"),
        )
        with closing(studio.connect()) as db:
            sources = db.execute(
                """SELECT entry_type, status, source_type, source_id, source_revision
                FROM production_bible_entries WHERE source_id = ? ORDER BY entry_type""",
                (character["id"],),
            ).fetchall()
        self.assertEqual([row["entry_type"] for row in sources], ["character", "voice"])
        self.assertTrue(all(row["status"] == "locked" and row["source_type"] == "creative_character" for row in sources))
        character_stale = compile_prompt_plan(studio.DB_PATH, shot_id)
        self.assertTrue(character_stale["stale"])
        self.assertTrue(any("角色卡" in reason for reason in character_stale["stale_reasons"]))
        self.assertEqual({item["source_id"] for item in character_stale["bible"]}, {character["id"]})
        self.assertEqual(character_stale["storyboard_source"]["section_id"], section["id"])

        record_validation(studio.DB_PATH, character_stale, {"ok": True})
        approve_plan(studio.DB_PATH, shot_id, character_stale["plan_hash"])
        asset_path = studio.ASSET_ROOT / "reference.png"
        asset_path.write_bytes(b"fake-image")
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO assets
                (id, project_id, kind, name, description, preview, locked, source, managed_path, media_type)
                VALUES ('reference-asset', ?, '角色参考', '林夏参考', '', '', 0, 'managed', ?, 'image')""",
                (self.project["id"], str(asset_path)),
            )
            db.commit()
        studio.bind_shot_reference(shot_id, studio.ReferenceCreate(asset_id="reference-asset", role="identity"))
        reference_stale = compile_prompt_plan(studio.DB_PATH, shot_id)
        self.assertTrue(reference_stale["stale"])
        self.assertIn("镜头参考素材已绑定", reference_stale["stale_reasons"])

    def test_project_isolation_and_manual_shots_never_gain_fake_links(self) -> None:
        first_section = self.approve_section(self.create_section("项目一小节"))
        self.apply(self.preview())
        other = studio.create_project(
            studio.ProjectCreate(
                title="另一个项目", episode="EP02", logline="", target_duration=30,
                shots=[studio.ShotCreate(title="手工镜头", description="人工镜头", prompt="manual prompt")],
            )
        )
        self.call_content("/api/creative-planning", "GET")
        other_preview = self.call_storyboard(
            "/api/creative-planning/storyboard-sync/preview", "POST", StoryboardPreviewRequest()
        )
        self.assertEqual(other_preview["project_id"], other["id"])
        self.assertEqual(other_preview["summary"]["preserve"], 1)
        with closing(studio.connect()) as db:
            manual = db.execute(
                """SELECT shots.id FROM shots LEFT JOIN creative_storyboard_links links ON links.shot_id = shots.id
                WHERE shots.project_id = ? AND links.id IS NULL""",
                (other["id"],),
            ).fetchall()
            first_links = db.execute(
                "SELECT * FROM creative_storyboard_links WHERE project_id = ?", (self.project["id"],)
            ).fetchall()
        self.assertEqual(len(manual), 1)
        self.assertEqual(len(first_links), 1)
        self.assertEqual(first_links[0]["section_id"], first_section["id"])


if __name__ == "__main__":
    unittest.main()
