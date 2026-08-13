from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

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
            OrderUpdate(ids=reordered_ids, source="human:test-section-order"),
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
            MergeRequest(target_id=merge_target["id"], source="human:test-section-merge"),
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
            OrderUpdate(ids=[second_chapter["id"], first_chapter["id"]], source="human:test-chapter-order"),
        )
        self.assertEqual([item["id"] for item in chapter_reordered["chapters"]], [second_chapter["id"], first_chapter["id"]])
        after_merge = self.call(
            "/api/creative-planning/chapters/{chapter_id}/merge",
            "POST",
            first_chapter["id"],
            MergeRequest(target_id=second_chapter["id"], source="human:test-chapter-merge"),
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
