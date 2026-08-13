from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend import app as studio
from backend.content_planning import (
    BriefUpdate,
    ChapterCreate,
    ChapterSplit,
    ChapterUpdate,
    CharacterCreate,
    CharacterUpdate,
    MergeRequest,
    OrderUpdate,
    ProposalCreate,
    ProposalUpdate,
    RevisionBase,
    SectionCreate,
    SectionSplit,
    SectionUpdate,
    create_content_router,
    init_content_schema,
)
from backend.project_archive import project_snapshot


class ContentPlanningContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        studio.DB_PATH = root / "studio.db"
        studio.EXPORT_ROOT = root / "exports"
        studio.EXPORT_JOB_ROOT = root / "jobs"
        studio.EXPORT_ROOT.mkdir()
        studio.EXPORT_JOB_ROOT.mkdir()
        studio.init_db()
        self.legacy_project = studio.get_project()
        self.empty_project = studio.create_project(
            studio.ProjectCreate(
                title="空白短剧",
                episode="EP01",
                logline="",
                target_duration=90,
                shots=[],
            )
        )
        router = create_content_router(studio.DB_PATH)
        self.endpoints = {}
        for route in router.routes:
            for method in route.methods or []:
                self.endpoints[(route.path, method)] = route.endpoint

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, path: str, method: str, *args):
        return self.endpoints[(path, method)](*args)

    def workspace(self):
        return self.call("/api/creative-planning", "GET")

    def mutation_state(self) -> dict[str, object]:
        with closing(studio.connect()) as db:
            chapters = [
                dict(item)
                for item in db.execute(
                    """SELECT id, ordinal, title, summary, status, revision FROM creative_chapters
                    WHERE project_id = ? ORDER BY id""",
                    (self.empty_project["id"],),
                ).fetchall()
            ]
            sections = [
                dict(item)
                for item in db.execute(
                    """SELECT id, chapter_id, ordinal, title, summary, status, revision FROM creative_sections
                    WHERE project_id = ? ORDER BY id""",
                    (self.empty_project["id"],),
                ).fetchall()
            ]
            revisions = int(db.execute(
                "SELECT COUNT(*) FROM creative_revisions WHERE project_id = ?",
                (self.empty_project["id"],),
            ).fetchone()[0])
        return {"chapters": chapters, "sections": sections, "revision_count": revisions}

    def test_empty_project_brief_multiple_proposals_single_final_and_immutable_history(self) -> None:
        initial = self.workspace()
        self.assertEqual(initial["project"]["id"], self.empty_project["id"])
        self.assertEqual(initial["chapters"], [])
        self.assertEqual(initial["brief"]["version_count"], 1)

        updated = self.call(
            "/api/creative-planning/brief",
            "PUT",
            BriefUpdate(
                base_revision=initial["brief"]["revision"],
                theme="人在记忆和真相之间作出选择",
                genre="都市悬疑",
                tone="冷峻、克制",
                audience="18—35 岁悬疑受众",
                target_duration=90,
                constraints="横屏；单集；室内主场景",
                status="approved",
                source="human:test-brief",
            ),
        )
        self.assertEqual(updated["brief"]["status"], "approved")
        self.assertEqual(updated["brief"]["revision"], 2)

        first = updated["proposals"][0]
        after_second = self.call(
            "/api/creative-planning/proposals",
            "POST",
            ProposalCreate(
                title="记忆电梯",
                synopsis="女人进入一部只停靠遗忘楼层的电梯。",
                core_conflict="她必须放弃最珍贵的记忆才能离开。",
                ending="她选择记住真相，电梯门在童年旧宅打开。",
                source="human:test-proposal",
            ),
        )
        second = next(item for item in after_second["proposals"] if item["title"] == "记忆电梯")
        after_edit = self.call(
            "/api/creative-planning/proposals/{proposal_id}",
            "PATCH",
            second["id"],
            ProposalUpdate(
                base_revision=second["revision"],
                synopsis="女人进入一部只停靠被遗忘楼层的电梯。",
                source="human:test-rewrite",
            ),
        )
        edited = next(item for item in after_edit["proposals"] if item["id"] == second["id"])
        finalized = self.call(
            "/api/creative-planning/proposals/{proposal_id}/finalize",
            "POST",
            edited["id"],
            RevisionBase(base_revision=edited["revision"], source="human:test-finalize"),
        )
        self.assertEqual(len(finalized["proposals"]), 2)
        self.assertEqual(sum(item["status"] == "finalized" for item in finalized["proposals"]), 1)
        self.assertEqual(next(item for item in finalized["proposals"] if item["id"] == first["id"])["status"], "draft")

        history = self.call(
            "/api/creative-planning/history/{entity_type}/{entity_id}",
            "GET",
            "proposal",
            second["id"],
        )
        self.assertEqual([item["revision"] for item in history["revisions"]], [3, 2, 1])
        self.assertEqual(history["revisions"][1]["source"], "human:test-rewrite")
        self.assertEqual(history["revisions"][0]["snapshot"]["status"], "finalized")
        self.assertEqual(history["revisions"][1]["snapshot"]["status"], "draft")
        with closing(studio.connect()) as db:
            stored = db.execute(
                "SELECT snapshot FROM creative_revisions WHERE entity_type = 'proposal' AND entity_id = ? AND revision = 1",
                (second["id"],),
            ).fetchone()["snapshot"]
        self.assertEqual(json.loads(stored)["synopsis"], "女人进入一部只停靠遗忘楼层的电梯。")

    def test_character_cards_are_versioned_approvable_and_archived(self) -> None:
        created = self.call(
            "/api/creative-planning/characters",
            "POST",
            CharacterCreate(
                name="林夏",
                identity="失踪案调查记者",
                goal="找到哥哥失踪的真相",
                obstacle="她的记忆正在被篡改",
                personality="谨慎、执拗",
                appearance="短发，深色风衣，左眉有浅疤",
                voice="中低音，语速克制，紧张时尾音发抖",
                relationships="林远：哥哥；陈默：前搭档",
                reference_notes="后续可绑定正面、侧面和声音样本",
                source="human:test-character",
            ),
        )
        character = created["characters"][0]
        approved = self.call(
            "/api/creative-planning/characters/{character_id}",
            "PATCH",
            character["id"],
            CharacterUpdate(
                base_revision=character["revision"],
                status="approved",
                appearance="短发，深色风衣，左眉浅疤；所有章节保持一致",
                source="human:test-approve",
            ),
        )["characters"][0]
        self.assertEqual(approved["status"], "approved")
        self.assertIn("哥哥", approved["relationships"])
        self.assertIn("尾音发抖", approved["voice"])
        self.assertEqual(approved["version_count"], 2)
        archived = self.call(
            "/api/creative-planning/characters/{character_id}/archive",
            "POST",
            approved["id"],
            RevisionBase(base_revision=approved["revision"], source="human:test-archive"),
        )
        self.assertEqual(archived["characters"], [])
        history = self.call(
            "/api/creative-planning/history/{entity_type}/{entity_id}",
            "GET",
            "character",
            approved["id"],
        )
        self.assertEqual(history["revisions"][0]["snapshot"]["status"], "archived")

    def test_chapter_and_section_crud_order_split_merge_persist_and_isolate_projects(self) -> None:
        first = self.call(
            "/api/creative-planning/chapters",
            "POST",
            ChapterCreate(title="进入禁区", summary="林夏进入旧楼。", pacing_goal="快速建立规则", planned_seconds=30),
        )["chapters"][0]
        with_sections = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections",
            "POST",
            first["id"],
            SectionCreate(title="雨夜抵达", summary="林夏在暴雨中抵达旧楼。", pacing_goal="制造不安", planned_seconds=8),
        )
        with_sections = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections",
            "POST",
            first["id"],
            SectionCreate(title="电梯开门", summary="无人电梯主动开门。", pacing_goal="抛出规则", planned_seconds=8),
        )
        with_sections = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections",
            "POST",
            first["id"],
            SectionCreate(title="十三层", summary="楼层显示跳过十二。", pacing_goal="第一次反转", planned_seconds=8),
        )
        chapter = with_sections["chapters"][0]
        original_ids = [item["id"] for item in chapter["sections"]]
        reordered_ids = [original_ids[2], original_ids[0], original_ids[1]]
        reordered = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections/order",
            "PUT",
            chapter["id"],
            OrderUpdate(
                ids=reordered_ids,
                base_revisions={item["id"]: item["revision"] for item in chapter["sections"]},
                parent_base_revision=chapter["revision"],
                source="human:test-section-order",
            ),
        )
        self.assertEqual([item["id"] for item in reordered["chapters"][0]["sections"]], reordered_ids)

        first_section = reordered["chapters"][0]["sections"][0]
        split_sections = self.call(
            "/api/creative-planning/sections/{section_id}/split",
            "POST",
            first_section["id"],
            SectionSplit(
                new_title="楼层熄灭",
                summary_before="楼层显示跳过十二。",
                summary_after="显示屏突然熄灭。",
                base_revisions={item["id"]: item["revision"] for item in reordered["chapters"][0]["sections"]},
                parent_base_revision=reordered["chapters"][0]["revision"],
                source="human:test-section-split",
            ),
        )
        sections = split_sections["chapters"][0]["sections"]
        self.assertEqual(len(sections), 4)
        split_new = next(item for item in sections if item["title"] == "楼层熄灭")
        merge_target = sections[0]
        merged_sections = self.call(
            "/api/creative-planning/sections/{section_id}/merge",
            "POST",
            split_new["id"],
            MergeRequest(
                target_id=merge_target["id"],
                base_revisions={item["id"]: item["revision"] for item in sections},
                parent_base_revision=split_sections["chapters"][0]["revision"],
                source="human:test-section-merge",
            ),
        )
        self.assertEqual(len(merged_sections["chapters"][0]["sections"]), 3)
        self.assertIn("突然熄灭", merged_sections["chapters"][0]["sections"][0]["summary"])

        chapter = merged_sections["chapters"][0]
        chapter_split = self.call(
            "/api/creative-planning/chapters/{chapter_id}/split",
            "POST",
            chapter["id"],
            ChapterSplit(
                section_id=chapter["sections"][1]["id"],
                new_title="深入禁区",
                base_revisions={item["id"]: item["revision"] for item in merged_sections["chapters"]},
                child_base_revisions={item["id"]: item["revision"] for item in chapter["sections"]},
                source="human:test-chapter-split",
            ),
        )
        self.assertEqual(len(chapter_split["chapters"]), 2)
        first_chapter, second_chapter = chapter_split["chapters"]
        self.assertEqual(len(first_chapter["sections"]), 1)
        self.assertEqual(len(second_chapter["sections"]), 2)
        first_section_history = self.call(
            "/api/creative-planning/history/{entity_type}/{entity_id}",
            "GET",
            "section",
            first_chapter["sections"][0]["id"],
        )["revisions"]
        self.assertEqual(first_section_history[0]["revision"], first_chapter["sections"][0]["revision"])
        self.assertEqual(len(first_section_history), first_chapter["sections"][0]["revision"])

        chapter_reordered = self.call(
            "/api/creative-planning/chapters/order",
            "PUT",
            OrderUpdate(
                ids=[second_chapter["id"], first_chapter["id"]],
                base_revisions={item["id"]: item["revision"] for item in chapter_split["chapters"]},
                source="human:test-chapter-order",
            ),
        )
        self.assertEqual([item["id"] for item in chapter_reordered["chapters"]], [second_chapter["id"], first_chapter["id"]])
        reordered_first = next(item for item in chapter_reordered["chapters"] if item["id"] == first_chapter["id"])
        after_merge = self.call(
            "/api/creative-planning/chapters/{chapter_id}/merge",
            "POST",
            first_chapter["id"],
            MergeRequest(
                target_id=second_chapter["id"],
                base_revisions={item["id"]: item["revision"] for item in chapter_reordered["chapters"]},
                child_base_revisions={item["id"]: item["revision"] for item in reordered_first["sections"]},
                source="human:test-chapter-merge",
            ),
        )
        self.assertEqual(len(after_merge["chapters"]), 1)
        persisted_title = after_merge["chapters"][0]["title"]
        self.assertEqual(self.workspace()["chapters"][0]["title"], persisted_title)

        studio.set_active_project(self.legacy_project["id"])
        legacy_workspace = self.workspace()
        self.assertNotEqual(legacy_workspace["project"]["id"], self.empty_project["id"])
        self.assertTrue(legacy_workspace["chapters"])
        studio.set_active_project(self.empty_project["id"])
        self.assertEqual(self.workspace()["chapters"][0]["title"], persisted_title)

    def test_stale_reorder_is_409_and_leaves_content_status_order_and_revisions_unchanged(self) -> None:
        self.call("/api/creative-planning/chapters", "POST", ChapterCreate(title="第一章", summary="旧摘要"))
        stale = self.call("/api/creative-planning/chapters", "POST", ChapterCreate(title="第二章", summary="稳定摘要"))
        first, second = stale["chapters"]
        self.call(
            "/api/creative-planning/chapters/{chapter_id}", "PATCH", first["id"],
            ChapterUpdate(base_revision=first["revision"], summary="R2 新摘要", source="human:newer-tab"),
        )
        before = self.mutation_state()
        with self.assertRaises(HTTPException) as stale_error:
            self.call(
                "/api/creative-planning/chapters/order", "PUT",
                OrderUpdate(
                    ids=[second["id"], first["id"]],
                    base_revisions={item["id"]: item["revision"] for item in stale["chapters"]},
                    source="human:stale-tab",
                ),
            )
        self.assertEqual(stale_error.exception.status_code, 409)
        self.assertEqual(self.mutation_state(), before)

    def test_stale_section_split_is_409_and_cannot_overwrite_newer_summary(self) -> None:
        chapter = self.call(
            "/api/creative-planning/chapters", "POST", ChapterCreate(title="第一章", summary="章节"),
        )["chapters"][0]
        self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", chapter["id"],
            SectionCreate(title="起点", summary="R1 旧摘要", planned_seconds=8),
        )
        stale = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", chapter["id"],
            SectionCreate(title="转折", summary="稳定摘要", planned_seconds=8),
        )["chapters"][0]
        source = stale["sections"][0]
        self.call(
            "/api/creative-planning/sections/{section_id}", "PATCH", source["id"],
            SectionUpdate(base_revision=source["revision"], summary="R2 newer summary", source="human:newer-tab"),
        )
        current = self.workspace()["chapters"][0]
        before = self.mutation_state()
        with self.assertRaises(HTTPException) as stale_error:
            self.call(
                "/api/creative-planning/sections/{section_id}/split", "POST", source["id"],
                SectionSplit(
                    new_title="被阻止的新节",
                    summary_before="R1 旧摘要",
                    summary_after="旧标签页内容",
                    base_revisions={item["id"]: item["revision"] for item in stale["sections"]},
                    parent_base_revision=current["revision"],
                    source="human:stale-tab",
                ),
            )
        self.assertEqual(stale_error.exception.status_code, 409)
        self.assertEqual(self.mutation_state(), before)
        self.assertEqual(self.workspace()["chapters"][0]["sections"][0]["summary"], "R2 newer summary")

    def test_stale_chapter_split_is_409_and_moves_no_sections(self) -> None:
        chapter = self.call(
            "/api/creative-planning/chapters", "POST", ChapterCreate(title="第一章", summary="章节"),
        )["chapters"][0]
        self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", chapter["id"],
            SectionCreate(title="起点", summary="第一节"),
        )
        stale = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", chapter["id"],
            SectionCreate(title="转折", summary="R1 旧摘要"),
        )
        stale_chapter = stale["chapters"][0]
        moved = stale_chapter["sections"][1]
        self.call(
            "/api/creative-planning/sections/{section_id}", "PATCH", moved["id"],
            SectionUpdate(base_revision=moved["revision"], summary="R2 新摘要", source="human:newer-tab"),
        )
        before = self.mutation_state()
        with self.assertRaises(HTTPException) as stale_error:
            self.call(
                "/api/creative-planning/chapters/{chapter_id}/split", "POST", stale_chapter["id"],
                ChapterSplit(
                    section_id=moved["id"],
                    new_title="被阻止的新章",
                    base_revisions={item["id"]: item["revision"] for item in stale["chapters"]},
                    child_base_revisions={item["id"]: item["revision"] for item in stale_chapter["sections"]},
                    source="human:stale-tab",
                ),
            )
        self.assertEqual(stale_error.exception.status_code, 409)
        self.assertEqual(self.mutation_state(), before)

    def test_stale_section_and_chapter_merge_are_409_and_archive_nothing(self) -> None:
        first = self.call(
            "/api/creative-planning/chapters", "POST", ChapterCreate(title="第一章", summary="一"),
        )["chapters"][0]
        self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", first["id"],
            SectionCreate(title="起点", summary="R1 目标"),
        )
        stale_sections = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", first["id"],
            SectionCreate(title="来源", summary="待合并"),
        )["chapters"][0]
        target, source = stale_sections["sections"]
        self.call(
            "/api/creative-planning/sections/{section_id}", "PATCH", target["id"],
            SectionUpdate(base_revision=target["revision"], summary="R2 目标", source="human:newer-tab"),
        )
        current_parent = self.workspace()["chapters"][0]
        before_section_merge = self.mutation_state()
        with self.assertRaises(HTTPException) as section_error:
            self.call(
                "/api/creative-planning/sections/{section_id}/merge", "POST", source["id"],
                MergeRequest(
                    target_id=target["id"],
                    base_revisions={item["id"]: item["revision"] for item in stale_sections["sections"]},
                    parent_base_revision=current_parent["revision"],
                    source="human:stale-tab",
                ),
            )
        self.assertEqual(section_error.exception.status_code, 409)
        self.assertEqual(self.mutation_state(), before_section_merge)

        stale_chapters = self.call(
            "/api/creative-planning/chapters", "POST", ChapterCreate(title="第二章", summary="R1 第二章"),
        )
        source_chapter = stale_chapters["chapters"][0]
        target_chapter = stale_chapters["chapters"][1]
        self.call(
            "/api/creative-planning/chapters/{chapter_id}", "PATCH", target_chapter["id"],
            ChapterUpdate(base_revision=target_chapter["revision"], summary="R2 第二章", source="human:newer-tab"),
        )
        before_chapter_merge = self.mutation_state()
        with self.assertRaises(HTTPException) as chapter_error:
            self.call(
                "/api/creative-planning/chapters/{chapter_id}/merge", "POST", source_chapter["id"],
                MergeRequest(
                    target_id=target_chapter["id"],
                    base_revisions={item["id"]: item["revision"] for item in stale_chapters["chapters"]},
                    child_base_revisions={item["id"]: item["revision"] for item in source_chapter["sections"]},
                    source="human:stale-tab",
                ),
            )
        self.assertEqual(chapter_error.exception.status_code, 409)
        self.assertEqual(self.mutation_state(), before_chapter_merge)

    def test_section_merge_preserves_all_structured_fields_in_story_order(self) -> None:
        chapter = self.call(
            "/api/creative-planning/chapters", "POST", ChapterCreate(title="第一章", summary="合并合同"),
        )["chapters"][0]
        workspace = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", chapter["id"],
            SectionCreate(
                title="先发生", summary="摘要一", content="正文一", scene="场景一", action="动作一",
                dialogue="共享对白", sound="声音一", visual="视觉一", planned_seconds=3,
            ),
        )
        workspace = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", chapter["id"],
            SectionCreate(
                title="后发生", summary="摘要二", content="正文二", scene="", action="动作二",
                dialogue="共享对白", sound="", visual="视觉二", planned_seconds=4,
            ),
        )
        chapter = workspace["chapters"][0]
        first, second = chapter["sections"]
        merged = self.call(
            "/api/creative-planning/sections/{section_id}/merge", "POST", first["id"],
            MergeRequest(
                target_id=second["id"], base_revisions={item["id"]: item["revision"] for item in chapter["sections"]},
                parent_base_revision=chapter["revision"], source="human:test-structured-merge",
            ),
        )["chapters"][0]["sections"][0]
        self.assertEqual(merged["summary"], "摘要一\n\n摘要二")
        self.assertEqual(merged["content"], "正文一\n\n正文二")
        self.assertEqual(merged["scene"], "场景一")
        self.assertEqual(merged["action"], "动作一\n\n动作二")
        self.assertEqual(merged["dialogue"], "共享对白")
        self.assertEqual(merged["sound"], "声音一")
        self.assertEqual(merged["visual"], "视觉一\n\n视觉二")
        self.assertEqual(merged["planned_seconds"], 7)
        self.assertEqual(merged["status"], "draft")
        self.assertIn("重新检查", merged["review_note"])

    def test_archived_proposal_character_chapter_and_section_keep_reachable_history(self) -> None:
        proposal_workspace = self.call(
            "/api/creative-planning/proposals", "POST",
            ProposalCreate(title="待归档提案", synopsis="归档后仍可查", source="human:archive-test"),
        )
        proposal = next(item for item in proposal_workspace["proposals"] if item["title"] == "待归档提案")
        self.call(
            "/api/creative-planning/proposals/{proposal_id}/archive", "POST", proposal["id"],
            RevisionBase(base_revision=proposal["revision"], source="human:archive-proposal"),
        )

        character = self.call(
            "/api/creative-planning/characters", "POST",
            CharacterCreate(name="归档角色", identity="测试角色", source="human:archive-test"),
        )["characters"][0]
        self.call(
            "/api/creative-planning/characters/{character_id}/archive", "POST", character["id"],
            RevisionBase(base_revision=character["revision"], source="human:archive-character"),
        )

        section_chapter = self.call(
            "/api/creative-planning/chapters", "POST", ChapterCreate(title="保留章节", summary="保留"),
        )["chapters"][0]
        section_workspace = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", section_chapter["id"],
            SectionCreate(title="单独归档小节", summary="小节历史"),
        )
        section = section_workspace["chapters"][0]["sections"][0]
        self.call(
            "/api/creative-planning/sections/{section_id}/archive", "POST", section["id"],
            RevisionBase(base_revision=section["revision"], source="human:archive-section"),
        )

        chapter = self.call(
            "/api/creative-planning/chapters", "POST", ChapterCreate(title="归档章节", summary="章节历史"),
        )["chapters"][-1]
        chapter_workspace = self.call(
            "/api/creative-planning/chapters/{chapter_id}/sections", "POST", chapter["id"],
            SectionCreate(title="随章节归档", summary="级联归档历史"),
        )
        chapter = next(item for item in chapter_workspace["chapters"] if item["id"] == chapter["id"])
        self.call(
            "/api/creative-planning/chapters/{chapter_id}/archive", "POST", chapter["id"],
            RevisionBase(base_revision=chapter["revision"], source="human:archive-chapter"),
        )

        archive = self.call("/api/creative-planning/archive", "GET")
        expected = {
            ("proposal", proposal["id"]),
            ("character", character["id"]),
            ("chapter", chapter["id"]),
            ("section", section["id"]),
        }
        actual = {(item["entity_type"], item["id"]) for item in archive["entries"]}
        self.assertTrue(expected.issubset(actual))
        self.assertGreaterEqual(archive["summary"]["by_type"]["section"], 2)
        for entity_type, entity_id in expected:
            with self.subTest(entity_type=entity_type):
                entry = next(item for item in archive["entries"] if item["entity_type"] == entity_type and item["id"] == entity_id)
                self.assertEqual(entry["status"], "archived")
                self.assertTrue(entry["title"])
                self.assertTrue(entry["archived_at"])
                self.assertTrue(entry["source"].startswith("human:archive"))
                history = self.call(
                    "/api/creative-planning/history/{entity_type}/{entity_id}", "GET", entity_type, entity_id,
                )
                self.assertEqual(history["revisions"][0]["snapshot"]["status"], "archived")
                self.assertEqual(history["revisions"][0]["source"], entry["source"])

    def test_legacy_production_evidence_survives_idempotent_migration_and_is_archived(self) -> None:
        studio.set_active_project(self.legacy_project["id"])
        with closing(studio.connect()) as db:
            before = {
                "shots": db.execute("SELECT COUNT(*) FROM shots WHERE project_id = ?", (self.legacy_project["id"],)).fetchone()[0],
                "candidates": db.execute(
                    """SELECT COUNT(*) FROM candidates JOIN shots ON shots.id = candidates.shot_id
                    WHERE shots.project_id = ?""",
                    (self.legacy_project["id"],),
                ).fetchone()[0],
                "acceptance": db.execute(
                    "SELECT COUNT(*) FROM production_acceptance_runs WHERE project_id = ?",
                    (self.legacy_project["id"],),
                ).fetchone()[0],
            }
            init_content_schema(db)
            init_content_schema(db)
            db.commit()
        workspace = self.workspace()
        self.assertEqual(workspace["project"]["id"], self.legacy_project["id"])
        with closing(studio.connect()) as db:
            after = {
                "shots": db.execute("SELECT COUNT(*) FROM shots WHERE project_id = ?", (self.legacy_project["id"],)).fetchone()[0],
                "candidates": db.execute(
                    """SELECT COUNT(*) FROM candidates JOIN shots ON shots.id = candidates.shot_id
                    WHERE shots.project_id = ?""",
                    (self.legacy_project["id"],),
                ).fetchone()[0],
                "acceptance": db.execute(
                    "SELECT COUNT(*) FROM production_acceptance_runs WHERE project_id = ?",
                    (self.legacy_project["id"],),
                ).fetchone()[0],
            }
            snapshot = project_snapshot(db, self.legacy_project["id"])
        self.assertEqual(before, after)
        self.assertEqual(len(snapshot["creative_briefs"]), 1)
        self.assertGreaterEqual(len(snapshot["creative_proposals"]), 1)
        self.assertGreaterEqual(len(snapshot["creative_chapters"]), 1)
        self.assertGreaterEqual(len(snapshot["creative_revisions"]), 1)

if __name__ == "__main__":
    unittest.main()
