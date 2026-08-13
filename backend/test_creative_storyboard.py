from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
from backend.prompt_compiler import (
    approve_plan,
    begin_validation_lease,
    compile_prompt_plan,
    end_validation_lease,
    record_validation,
)


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

    def bind_managed_image(self, shot_id: str, asset_id: str) -> None:
        asset_path = studio.ASSET_ROOT / f"{asset_id}.png"
        asset_path.write_bytes(f"fake-{asset_id}".encode())
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO assets
                (id, project_id, kind, name, description, preview, locked, source, managed_path, media_type)
                VALUES (?, ?, '角色参考', ?, '', '', 0, 'managed', ?, 'image')""",
                (asset_id, self.project["id"], asset_id, str(asset_path)),
            )
            db.commit()
        studio.bind_shot_reference(shot_id, studio.ReferenceCreate(asset_id=asset_id, role="identity"))

    def insert_locked_bible(self, entry_id: str, *, asset_id: str | None = None) -> None:
        with closing(studio.connect()) as db:
            db.execute(
                """INSERT INTO production_bible_entries
                (id, project_id, entry_type, name, summary, canonical_description, prompt_fragment,
                 negative_prompt, continuity_rules, apply_globally, status, revision, archived, created_at, updated_at)
                VALUES (?, ?, 'character', ?, '', ?, ?, '', ?, 1, 'locked', 1, 0, 'now', 'now')""",
                (
                    entry_id,
                    self.project["id"],
                    entry_id,
                    f"Canonical facts for {entry_id}",
                    f"Prompt facts for {entry_id}",
                    f"Continuity for {entry_id}",
                ),
            )
            if asset_id:
                asset_path = studio.ASSET_ROOT / f"{asset_id}.png"
                asset_path.write_bytes(f"fake-{asset_id}".encode())
                db.execute(
                    """INSERT INTO assets
                    (id, project_id, kind, name, description, preview, locked, source, managed_path, media_type)
                    VALUES (?, ?, '角色参考', ?, '', '', 0, 'managed', ?, 'image')""",
                    (asset_id, self.project["id"], asset_id, str(asset_path)),
                )
                db.execute(
                    "INSERT INTO production_bible_assets VALUES (?, ?, 'identity', 1, 'now')",
                    (entry_id, asset_id),
                )
            db.commit()

    def capture_generate_validation(
        self, shot_id: str, *, verify_gpu_command: bool = False
    ) -> tuple[list[str], dict, dict]:
        captured: list[str] = []

        def capture(arguments, **_kwargs):
            captured.extend(arguments)
            return SimpleNamespace(stdout='{"ok": true}')

        with patch.object(studio, "run_h3", side_effect=capture):
            result = studio.generate(shot_id, studio.GenerateRequest(confirm=False, dry_run=True))
        with closing(studio.connect()) as db:
            job = dict(db.execute(
                "SELECT * FROM jobs WHERE shot_id = ? AND kind = 'validation' ORDER BY id DESC LIMIT 1",
                (shot_id,),
            ).fetchone())
        snapshot = json.loads(job["source_snapshot"])
        self.assertEqual(snapshot, studio.normalized_h3_input(captured))
        self.assertEqual(job["plan_hash"], studio.h3_input_hash(snapshot))
        self.assertEqual(job["plan_hash"], result["validation_input_hash"])
        if verify_gpu_command:
            gpu_arguments: list[str] = []

            def capture_gpu(arguments, **_kwargs):
                gpu_arguments.extend(arguments)
                return SimpleNamespace(stdout='{"ok": true}')

            with (
                patch.object(studio, "run_h3", side_effect=capture_gpu),
                patch.object(studio, "read_manifest", return_value={"candidates": [], "promotions": []}),
            ):
                studio.generate(
                    shot_id,
                    studio.GenerateRequest(
                        confirm=True,
                        dry_run=False,
                        expected_validation_hash=job["plan_hash"],
                    ),
                )
            self.assertEqual(snapshot, studio.normalized_h3_input(gpu_arguments))
        return captured, snapshot, result

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

    def test_last_archived_section_still_previews_and_confirms_delete_idempotently(self) -> None:
        section = self.approve_section(self.create_section("唯一小节"))
        applied = self.apply(self.preview())
        shot_id = applied["sync"]["applied_snapshot"][0]["shot_id"]
        current = self.call_content("/api/creative-planning", "GET")["chapters"][0]["sections"][0]
        self.call_content(
            "/api/creative-planning/sections/{section_id}/archive", "POST", current["id"],
            RevisionBase(base_revision=current["revision"], source="human:test-archive-last"),
        )
        deletion = self.preview()
        self.assertEqual(deletion["summary"]["delete"], 1)
        first = self.apply(deletion)
        second = self.apply(deletion)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        with closing(studio.connect()) as db:
            self.assertIsNone(db.execute("SELECT id FROM shots WHERE id = ?", (shot_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT id FROM creative_storyboard_links WHERE section_id = ?", (section["id"],)).fetchone())

    def test_running_validation_lease_blocks_archive_sync_delete_without_partial_write(self) -> None:
        section = self.approve_section(self.create_section("验证中的镜头"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        plan = compile_prompt_plan(studio.DB_PATH, shot_id)
        lease_id = begin_validation_lease(studio.DB_PATH, plan)
        current = self.call_content("/api/creative-planning", "GET")["chapters"][0]["sections"][0]
        self.call_content(
            "/api/creative-planning/sections/{section_id}/archive", "POST", current["id"],
            RevisionBase(base_revision=current["revision"], source="human:test-archive-during-validation"),
        )
        before = self._storyboard_mutation_state()
        protected = self.preview()
        self.assertEqual(protected["summary"]["protected"], 1)
        self.assertIn("进行中的 H3 dry-run", protected["rows"][0]["protected_reasons"])
        with self.assertRaises(HTTPException) as blocked:
            self.apply(protected)
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(self._storyboard_mutation_state(), before)
        end_validation_lease(studio.DB_PATH, lease_id)

        deletion = self.preview()
        self.assertEqual(deletion["summary"]["delete"], 1)
        self.apply(deletion)
        with closing(studio.connect()) as db:
            self.assertIsNone(db.execute("SELECT id FROM shots WHERE id = ?", (shot_id,)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM h3_prompt_plans WHERE shot_id = ?", (shot_id,)).fetchone()[0], 0)

    def test_dry_run_failure_always_cleans_validation_lease(self) -> None:
        self.approve_section(self.create_section("适配器失败"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        plan = compile_prompt_plan(studio.DB_PATH, shot_id)
        with patch.object(studio, "run_h3", side_effect=RuntimeError("controlled adapter failure")):
            with self.assertRaisesRegex(RuntimeError, "controlled adapter failure"):
                studio.dry_run_prompt_plan(shot_id, studio.PromptPlanRequest(plan_hash=plan["plan_hash"]))
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM h3_validation_leases").fetchone()[0], 0)
            self.assertIsNotNone(db.execute("SELECT id FROM shots WHERE id = ?", (shot_id,)).fetchone())

    def test_generate_dry_run_lease_blocks_sync_delete_until_result_is_persisted(self) -> None:
        section = self.approve_section(self.create_section("主工作台校验"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        current = self.call_content("/api/creative-planning", "GET")["chapters"][0]["sections"][0]
        self.call_content(
            "/api/creative-planning/sections/{section_id}/archive", "POST", section["id"],
            RevisionBase(base_revision=current["revision"], source="human:test-generate-race"),
        )
        race: dict[str, object] = {}

        def attempt_delete_while_adapter_runs(*_args, **_kwargs):
            protected = self.preview()
            race["preview"] = protected
            before = self._storyboard_mutation_state()
            with self.assertRaises(HTTPException) as blocked:
                self.apply(protected)
            race["status_code"] = blocked.exception.status_code
            race["zero_partial_write"] = before == self._storyboard_mutation_state()
            return SimpleNamespace(stdout='{"ok": true}')

        with patch.object(studio, "run_h3", side_effect=attempt_delete_while_adapter_runs):
            result = studio.generate(shot_id, studio.GenerateRequest(confirm=False, dry_run=True))

        self.assertTrue(result["ok"])
        self.assertEqual(race["status_code"], 409)
        self.assertTrue(race["zero_partial_write"])
        self.assertEqual(race["preview"]["summary"]["protected"], 1)
        self.assertIn("进行中的 H3 dry-run", race["preview"]["rows"][0]["protected_reasons"])
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM h3_validation_leases").fetchone()[0], 0)
            validation = db.execute(
                "SELECT * FROM jobs WHERE shot_id = ? AND kind = 'validation'", (shot_id,)
            ).fetchone()
            self.assertIsNotNone(validation)
            self.assertEqual(validation["plan_hash"], result["validation_input_hash"])
            frozen = json.loads(validation["source_snapshot"])
            self.assertEqual(validation["plan_hash"], studio.h3_input_hash(frozen))
            self.assertEqual(frozen["arguments"][0], "draft")
            self.assertNotIn("--dry-run", frozen["arguments"])
            self.assertIsNotNone(db.execute("SELECT id FROM shots WHERE id = ?", (shot_id,)).fetchone())

    def test_validation_snapshot_matches_all_actual_adapter_input_variants(self) -> None:
        sections = [self.approve_section(self.create_section(title)) for title in (
            "未批准计划", "全局文字圣经", "圣经媒体引用", "已批准计划",
        )]
        applied = self.apply(self.preview())["sync"]["applied_snapshot"]
        shots = {item["section_id"]: item["shot_id"] for item in applied}

        unapproved_args, _, _ = self.capture_generate_validation(
            shots[sections[0]["id"]], verify_gpu_command=True
        )
        self.assertNotIn("--ref-image", unapproved_args)
        self.assertIn("no cuts, no subtitles", unapproved_args[unapproved_args.index("--prompt") + 1])

        self.insert_locked_bible("global-text-bible")
        text_args, _, _ = self.capture_generate_validation(
            shots[sections[1]["id"]], verify_gpu_command=True
        )
        self.assertIn("Canonical facts for global-text-bible", text_args[text_args.index("--prompt") + 1])
        self.assertNotIn("--ref-image", text_args)

        self.insert_locked_bible("global-media-bible", asset_id="bible-media-image")
        media_args, _, _ = self.capture_generate_validation(
            shots[sections[2]["id"]], verify_gpu_command=True
        )
        self.assertIn("--ref-image", media_args)
        self.assertEqual(
            Path(media_args[media_args.index("--ref-image") + 1]).name,
            "bible-media-image.png",
        )

        approved_shot_id = shots[sections[3]["id"]]
        approved_plan = compile_prompt_plan(studio.DB_PATH, approved_shot_id)
        self.assertTrue(record_validation(studio.DB_PATH, approved_plan, {"ok": True}))
        approve_plan(studio.DB_PATH, approved_shot_id, approved_plan["plan_hash"])
        approved_args, _, approved_result = self.capture_generate_validation(
            approved_shot_id, verify_gpu_command=True
        )
        self.assertIn("--ref-image", approved_args)
        self.assertEqual(approved_result["prompt_plan_hash"], approved_plan["plan_hash"])

    def test_batch_expected_credentials_survive_gate_and_block_late_source_changes(self) -> None:
        sections = [self.approve_section(self.create_section(title)) for title in (
            "门禁后改镜头", "门禁后改引用", "门禁后改圣经",
        )]
        applied = self.apply(self.preview())["sync"]["applied_snapshot"]
        shots = {item["section_id"]: item["shot_id"] for item in applied}
        shot_ids = [shots[section["id"]] for section in sections]
        with patch.object(studio, "run_h3", return_value=SimpleNamespace(stdout='{"ok": true}')):
            validation = studio.batch_dry_run(studio.BatchGenerationRequest(shot_ids=shot_ids))
        self.assertTrue(validation["ok"])
        with closing(studio.connect()) as db:
            before_jobs = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE shot_id IN (?, ?, ?)", tuple(shot_ids)
            ).fetchone()[0]
            before_candidates = db.execute(
                "SELECT COUNT(*) FROM candidates WHERE shot_id IN (?, ?, ?)", tuple(shot_ids)
            ).fetchone()[0]

        real_generate = studio.generate

        def mutate_after_batch_gate(current_shot_id, request):
            if current_shot_id == shot_ids[0]:
                studio.update_shot(current_shot_id, studio.ShotPatch(prompt="late changed shot prompt"))
            elif current_shot_id == shot_ids[1]:
                self.bind_managed_image(current_shot_id, "late-reference")
            else:
                self.insert_locked_bible("late-global-bible")
            return real_generate(current_shot_id, request)

        with patch.object(studio, "generate", side_effect=mutate_after_batch_gate), patch.object(studio, "run_h3") as adapter:
            submitted = studio.batch_submit(studio.BatchGenerationRequest(shot_ids=shot_ids, confirm=True))

        self.assertFalse(submitted["ok"])
        self.assertEqual(submitted["failed_count"], 3)
        adapter.assert_not_called()
        with closing(studio.connect()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM jobs WHERE shot_id IN (?, ?, ?)", tuple(shot_ids)).fetchone()[0],
                before_jobs,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM candidates WHERE shot_id IN (?, ?, ?)", tuple(shot_ids)).fetchone()[0],
                before_candidates,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'draft' AND shot_id IN (?, ?, ?)", tuple(shot_ids)).fetchone()[0],
                0,
            )

    def test_generate_dry_run_rejects_shot_change_during_adapter_without_partial_result(self) -> None:
        self.approve_section(self.create_section("运行中改镜头"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]

        def change_shot(*_args, **_kwargs):
            studio.update_shot(
                shot_id,
                studio.ShotPatch(prompt="changed while adapter runs", status="人工修改中"),
            )
            return SimpleNamespace(stdout='{"ok": true}')

        with patch.object(studio, "run_h3", side_effect=change_shot):
            with self.assertRaises(HTTPException) as conflict:
                studio.generate(shot_id, studio.GenerateRequest(confirm=False, dry_run=True))

        self.assertEqual(conflict.exception.status_code, 409)
        current = studio.require_active_shot(shot_id)
        self.assertEqual(current["prompt"], "changed while adapter runs")
        self.assertEqual(current["status"], "人工修改中")
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM h3_validation_leases").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE shot_id = ?", (shot_id,)).fetchone()[0], 0)

    def test_generate_dry_run_rejects_reference_change_during_adapter_without_partial_result(self) -> None:
        self.approve_section(self.create_section("运行中改引用"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        original_status = studio.require_active_shot(shot_id)["status"]

        def bind_reference(*_args, **_kwargs):
            self.bind_managed_image(shot_id, "race-reference")
            return SimpleNamespace(stdout='{"ok": true}')

        with patch.object(studio, "run_h3", side_effect=bind_reference):
            with self.assertRaises(HTTPException) as conflict:
                studio.generate(shot_id, studio.GenerateRequest(confirm=False, dry_run=True))

        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(studio.require_active_shot(shot_id)["status"], original_status)
        self.assertEqual(len(studio.get_shot_references(shot_id)), 1)
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM h3_validation_leases").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE shot_id = ?", (shot_id,)).fetchone()[0], 0)

    def test_batch_submit_rejects_reference_change_even_when_timestamps_are_equal(self) -> None:
        self.approve_section(self.create_section("校验后改引用"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        with patch.object(studio, "run_h3", return_value=SimpleNamespace(stdout='{"ok": true}')):
            studio.generate(shot_id, studio.GenerateRequest(confirm=False, dry_run=True))
        self.bind_managed_image(shot_id, "post-validation-reference")
        with closing(studio.connect()) as db:
            validation_time = db.execute(
                "SELECT updated_at FROM jobs WHERE shot_id = ? AND kind = 'validation' ORDER BY id DESC LIMIT 1",
                (shot_id,),
            ).fetchone()["updated_at"]
            db.execute("UPDATE shots SET updated_at = ? WHERE id = ?", (validation_time, shot_id))
            db.commit()

        with patch.object(studio, "run_h3") as adapter:
            with self.assertRaises(HTTPException) as conflict:
                studio.batch_submit(studio.BatchGenerationRequest(shot_ids=[shot_id], confirm=True))
        self.assertEqual(conflict.exception.status_code, 409)
        adapter.assert_not_called()
        with closing(studio.connect()) as db:
            times = db.execute(
                """SELECT shots.updated_at AS shot_time, jobs.updated_at AS validation_time
                FROM shots JOIN jobs ON jobs.shot_id = shots.id
                WHERE shots.id = ? AND jobs.kind = 'validation' ORDER BY jobs.id DESC LIMIT 1""",
                (shot_id,),
            ).fetchone()
            self.assertEqual(times["shot_time"], times["validation_time"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE shot_id = ?", (shot_id,)).fetchone()[0], 1)

    def test_batch_submit_rejects_bible_change_across_timestamp_ticks(self) -> None:
        self.approve_section(self.create_section("校验后改角色"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        with patch.object(studio, "run_h3", return_value=SimpleNamespace(stdout='{"ok": true}')):
            studio.generate(shot_id, studio.GenerateRequest(confirm=False, dry_run=True))
        character = self.call_content(
            "/api/creative-planning/characters",
            "POST",
            CharacterCreate(
                name="新角色", identity="调查员", appearance="短发、红衣",
                voice="低声线", goal="找到真相",
            ),
        )["characters"][0]
        self.call_content(
            "/api/creative-planning/characters/{character_id}",
            "PATCH",
            character["id"],
            CharacterUpdate(base_revision=character["revision"], status="approved", source="human:test-bible-change"),
        )
        with closing(studio.connect()) as db:
            db.execute("UPDATE jobs SET updated_at = '2026-08-14T00:00:00.000001+00:00' WHERE shot_id = ?", (shot_id,))
            db.execute("UPDATE shots SET updated_at = '2026-08-14T00:00:00+00:00' WHERE id = ?", (shot_id,))
            db.commit()

        with patch.object(studio, "run_h3") as adapter:
            with self.assertRaises(HTTPException) as conflict:
                studio.batch_submit(studio.BatchGenerationRequest(shot_ids=[shot_id], confirm=True))
        self.assertEqual(conflict.exception.status_code, 409)
        adapter.assert_not_called()
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE shot_id = ?", (shot_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM production_bible_entries WHERE status = 'locked'").fetchone()[0], 2)

    def test_generate_dry_run_delete_wins_before_lease_with_controlled_conflict(self) -> None:
        section = self.approve_section(self.create_section("删除先于校验"))
        shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        real_compile = studio.compile_prompt_plan
        deleted: dict[str, bool] = {"done": False}

        def compile_then_delete(db_path, current_shot_id):
            plan = real_compile(db_path, current_shot_id)
            current = self.call_content("/api/creative-planning", "GET")["chapters"][0]["sections"][0]
            self.call_content(
                "/api/creative-planning/sections/{section_id}/archive", "POST", section["id"],
                RevisionBase(base_revision=current["revision"], source="human:test-delete-wins"),
            )
            deletion = self.preview()
            self.assertEqual(deletion["summary"]["delete"], 1)
            self.apply(deletion)
            deleted["done"] = True
            return plan

        with patch.object(studio, "compile_prompt_plan", side_effect=compile_then_delete), patch.object(studio, "run_h3") as adapter:
            with self.assertRaises(HTTPException) as conflict:
                studio.generate(shot_id, studio.GenerateRequest(confirm=False, dry_run=True))

        self.assertTrue(deleted["done"])
        self.assertEqual(conflict.exception.status_code, 409)
        adapter.assert_not_called()
        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM h3_validation_leases").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE shot_id = ?", (shot_id,)).fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT id FROM shots WHERE id = ?", (shot_id,)).fetchone())

    def test_generate_and_batch_adapter_failures_leave_no_validation_lease_or_result(self) -> None:
        self.approve_section(self.create_section("单镜失败"))
        first_shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
        with patch.object(studio, "run_h3", side_effect=RuntimeError("controlled generate failure")):
            with self.assertRaisesRegex(RuntimeError, "controlled generate failure"):
                studio.generate(first_shot_id, studio.GenerateRequest(confirm=False, dry_run=True))

        second = self.approve_section(self.create_section("批量失败"))
        second_shot_id = next(
            item["shot_id"]
            for item in self.apply(self.preview())["sync"]["applied_snapshot"]
            if item["section_id"] == second["id"]
        )
        with patch.object(studio, "run_h3", side_effect=RuntimeError("controlled batch failure")):
            with self.assertRaisesRegex(RuntimeError, "controlled batch failure"):
                studio.batch_dry_run(studio.BatchGenerationRequest(shot_ids=[second_shot_id]))

        with closing(studio.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM h3_validation_leases").fetchone()[0], 0)
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE kind = 'validation' AND shot_id IN (?, ?)",
                    (first_shot_id, second_shot_id),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM shots WHERE id IN (?, ?)", (first_shot_id, second_shot_id)).fetchone()[0],
                2,
            )

    def test_each_shot_foreign_key_relation_protects_delete_with_zero_partial_writes(self) -> None:
        relation_factories = {
            "h3_prompt_plans": lambda db, shot_id: db.execute(
                """INSERT INTO h3_prompt_plans
                (id, shot_id, project_id, plan_hash, status, mode, creative_prompt, compiled_prompt,
                 source_snapshot, references_snapshot, bible_snapshot, warnings, adapter_output, created_at, validated_at)
                VALUES ('protect-plan', ?, ?, ?, 'validated', 'FL2VA', '', '', '{}', '[]', '[]', '[]', '{}', 'now', 'now')""",
                (shot_id, self.project["id"], "f" * 64),
            ),
            "shot_references": lambda db, shot_id: (
                db.execute(
                    """INSERT INTO assets
                    (id, project_id, kind, name, description, preview, locked, source, managed_path, media_type)
                    VALUES ('protect-asset', ?, '角色参考', '保护素材', '', '', 0, 'managed', 'protect.png', 'image')""",
                    (self.project["id"],),
                ),
                db.execute(
                    """INSERT INTO shot_references
                    (id, shot_id, asset_id, reference_type, ordinal, role, created_at, updated_at)
                    VALUES ('protect-reference', ?, 'protect-asset', 'image', 1, 'identity', 'now', 'now')""",
                    (shot_id,),
                ),
            ),
            "production_bible_shots": lambda db, shot_id: (
                db.execute(
                    """INSERT INTO production_bible_entries
                    (id, project_id, entry_type, name, summary, canonical_description, prompt_fragment,
                     negative_prompt, continuity_rules, apply_globally, status, revision, archived, created_at, updated_at)
                    VALUES ('protect-bible', ?, 'character', '保护设定', '', '设定', 'prompt', '', '', 0,
                            'locked', 1, 0, 'now', 'now')""",
                    (self.project["id"],),
                ),
                db.execute(
                    "INSERT INTO production_bible_shots VALUES ('protect-bible', ?, 'continuity', '', 'now')", (shot_id,)
                ),
            ),
        }
        for table, make_relation in relation_factories.items():
            with self.subTest(table=table):
                # Each subtest uses a fresh project/database so evidence classes cannot mask one another.
                self.tearDown()
                self.setUp()
                section = self.approve_section(self.create_section(f"保护 {table}"))
                shot_id = self.apply(self.preview())["sync"]["applied_snapshot"][0]["shot_id"]
                with closing(studio.connect()) as db:
                    make_relation(db, shot_id)
                    db.commit()
                current = self.call_content("/api/creative-planning", "GET")["chapters"][0]["sections"][0]
                self.call_content(
                    "/api/creative-planning/sections/{section_id}/archive", "POST", current["id"],
                    RevisionBase(base_revision=current["revision"], source="human:test-protect"),
                )
                before = self._storyboard_mutation_state()
                protected = self.preview()
                self.assertEqual(protected["summary"]["protected"], 1)
                self.assertTrue(protected["rows"][0]["protected_reasons"])
                with self.assertRaises(HTTPException) as blocked:
                    self.apply(protected)
                self.assertEqual(blocked.exception.status_code, 409)
                self.assertEqual(self._storyboard_mutation_state(), before)

    def _storyboard_mutation_state(self) -> dict:
        with closing(studio.connect()) as db:
            return {
                "shots": [tuple(row) for row in db.execute("SELECT * FROM shots ORDER BY id").fetchall()],
                "links": [tuple(row) for row in db.execute("SELECT * FROM creative_storyboard_links ORDER BY id").fetchall()],
                "syncs": [tuple(row) for row in db.execute("SELECT * FROM creative_storyboard_syncs ORDER BY id").fetchall()],
                "plans": [tuple(row) for row in db.execute("SELECT * FROM h3_prompt_plans ORDER BY id").fetchall()],
            }


if __name__ == "__main__":
    unittest.main()
