from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import app as studio
from backend.content_planning import (
    BriefUpdate,
    ChapterCreate,
    ChapterUpdate,
    CharacterCreate,
    CharacterUpdate,
    ProposalCreate,
    RevisionBase,
    SectionCreate,
    SectionDecision,
    create_content_router,
)
from backend.creative_storyboard import (
    StoryboardApplyRequest,
    StoryboardPreviewRequest,
    create_storyboard_router,
)
from backend.delivery_plan import (
    DeliveryPlanItemInput,
    DeliveryPlanPatch,
    lock_delivery_plan,
    save_delivery_plan,
)
from backend.hd_delivery import (
    HDPlanCreate,
    HDReviewRequest,
    HDScores,
    HDSelectRequest,
    HDSubmitRequest,
    HDValidationRequest,
    create_hd_plan,
    process_next_hd_job,
    save_hd_review,
    select_hd_artifact,
    submit_hd_job,
    validate_hd_plan,
)
from backend.project_archive import create_project_archive, verify_project_archive
from backend.review_gate import (
    AudioChecks,
    CandidateReviewRequest,
    ReviewScores,
    record_master_selection,
    review_workspace,
    save_candidate_review,
)


class ControlledDeliveryE2ETests(unittest.TestCase):
    """An isolated production-flow fixture; it never calls Agent, ComfyUI, or a GPU."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.previous = {
            "DB_PATH": studio.DB_PATH,
            "COMFY_OUTPUT_ROOT": studio.COMFY_OUTPUT_ROOT,
            "EXPORT_ROOT": studio.EXPORT_ROOT,
            "EXPORT_JOB_ROOT": studio.EXPORT_JOB_ROOT,
            "BACKUP_ROOT": studio.BACKUP_ROOT,
        }
        studio.DB_PATH = self.root / "studio.db"
        studio.COMFY_OUTPUT_ROOT = self.root / "controlled-fake-comfy-root" / "output"
        studio.EXPORT_ROOT = self.root / "exports"
        studio.EXPORT_JOB_ROOT = self.root / "export-jobs"
        studio.BACKUP_ROOT = self.root / "archives"
        for path in (
            studio.COMFY_OUTPUT_ROOT,
            studio.EXPORT_ROOT,
            studio.EXPORT_JOB_ROOT,
            studio.BACKUP_ROOT,
        ):
            path.mkdir(parents=True)
        studio.init_db()
        self.project = studio.create_project(
            studio.ProjectCreate(
                title="受控交付演练",
                episode="EP01",
                logline="雨夜来电迫使主角在十秒内作出选择。",
                target_duration=8,
                shots=[],
            )
        )
        self.content = self._routes(create_content_router(studio.DB_PATH))
        self.storyboard = self._routes(create_storyboard_router(studio.DB_PATH))

    def tearDown(self) -> None:
        studio.EXPORT_WAKE_EVENT.clear()
        for name, value in self.previous.items():
            setattr(studio, name, value)
        self.temp_dir.cleanup()

    @staticmethod
    def _routes(router) -> dict[tuple[str, str], object]:
        result = {}
        for route in router.routes:
            for method in route.methods:
                result[(route.path, method)] = route.endpoint
        return result

    @staticmethod
    def _endpoint(routes: dict[tuple[str, str], object], path: str, method: str):
        return routes[(path, method)]

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _make_media(self, name: str, color: str, frequency: int) -> Path:
        path = studio.COMFY_OUTPUT_ROOT / name
        completed = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=608x352:r=24:d=0.8",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=0.8",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode:
            self.fail(completed.stderr)
        return path.resolve()

    def _build_storyboard_from_empty_project(self) -> str:
        workspace = self._endpoint(self.content, "/api/creative-planning", "GET")()
        workspace = self._endpoint(self.content, "/api/creative-planning/brief", "PUT")(
            BriefUpdate(
                base_revision=workspace["brief"]["revision"],
                source="fixture:controlled-fake-agent",
                theme="雨夜里一通来自未来的电话",
                genre="悬疑",
                tone="克制、连续、写实",
                audience="短剧观众",
                target_duration=8,
                constraints="单场景、单镜头、小样仅用于验收平台流程",
                status="approved",
            )
        )
        for title, ending in (("她接了电话", "她听见自己的声音"), ("她拒绝来电", "门外脚步停下")):
            workspace = self._endpoint(self.content, "/api/creative-planning/proposals", "POST")(
                ProposalCreate(
                    title=title,
                    synopsis="主角在雨夜便利店外收到不可能的来电。",
                    core_conflict="接听会暴露位置，不接会错过警告。",
                    ending=ending,
                    source="fixture:controlled-fake-agent",
                )
            )
        chosen = workspace["proposals"][0]
        workspace = self._endpoint(
            self.content,
            "/api/creative-planning/proposals/{proposal_id}/finalize",
            "POST",
        )(
            chosen["id"],
            RevisionBase(base_revision=chosen["revision"], source="fixture:human-confirmation"),
        )
        workspace = self._endpoint(self.content, "/api/creative-planning/characters", "POST")(
            CharacterCreate(
                name="林夏",
                identity="夜班归来的年轻女性",
                goal="安全离开便利店停车场",
                obstacle="陌生车辆与未知来电",
                personality="谨慎但果断",
                appearance="红色雨衣，短发，东亚面孔",
                voice="低声、清晰、略带喘息",
                relationships="独自行动",
                reference_notes="受控 fixture，没有真实参考素材",
                source="fixture:controlled-fake-agent",
            )
        )
        character = workspace["characters"][0]
        self._endpoint(self.content, "/api/creative-planning/characters/{character_id}", "PATCH")(
            character["id"],
            CharacterUpdate(
                base_revision=character["revision"],
                source="fixture:human-confirmation",
                status="approved",
            ),
        )
        workspace = self._endpoint(self.content, "/api/creative-planning/chapters", "POST")(
            ChapterCreate(
                title="雨夜来电",
                summary="林夏在车旁迟疑，电话响起。",
                pacing_goal="八秒内完成犹豫、接听与回望",
                planned_seconds=8,
                source="fixture:controlled-fake-agent",
            )
        )
        chapter = workspace["chapters"][0]
        workspace = self._endpoint(
            self.content,
            "/api/creative-planning/chapters/{chapter_id}/sections",
            "POST",
        )(
            chapter["id"],
            SectionCreate(
                title="电话响起",
                summary="雨夜停车场，林夏停在越野车旁。",
                content="她拿出手机，接听前回望便利店玻璃门。",
                scene="雨夜便利店外的停车场",
                action="林夏停步、拿出手机、接听并回望",
                dialogue="别回头。",
                sound="大雨、手机铃声、短促呼吸",
                visual="横屏连续镜头，红色雨衣保持一致，缓慢推近",
                pacing_goal="动作连贯，不跳切",
                planned_seconds=8,
                source="fixture:controlled-fake-agent",
            ),
        )
        section = workspace["chapters"][0]["sections"][0]
        workspace = self._endpoint(
            self.content,
            "/api/creative-planning/sections/{section_id}/approve",
            "POST",
        )(
            section["id"],
            SectionDecision(
                base_revision=section["revision"],
                source="fixture:human-confirmation",
                note="结构化剧本已人工确认",
            ),
        )
        chapter = workspace["chapters"][0]
        self._endpoint(self.content, "/api/creative-planning/chapters/{chapter_id}", "PATCH")(
            chapter["id"],
            ChapterUpdate(
                base_revision=chapter["revision"],
                source="fixture:human-confirmation",
                status="approved",
            ),
        )
        preview = self._endpoint(
            self.storyboard,
            "/api/creative-planning/storyboard-sync/preview",
            "POST",
        )(StoryboardPreviewRequest())
        self.assertTrue(preview["can_apply"])
        applied = self._endpoint(
            self.storyboard,
            "/api/creative-planning/storyboard-sync/apply",
            "POST",
        )(
            StoryboardApplyRequest(
                plan_hash=preview["plan_hash"],
                confirm=True,
                confirmed_by="fixture:human-confirmation",
            )
        )
        self.assertFalse(applied["idempotent"])
        shots = studio.get_project()["shots"]
        self.assertEqual(len(shots), 1)
        return shots[0]["id"]

    def _seed_two_controlled_candidates(self, shot_id: str) -> list[str]:
        files = [
            self._make_media("controlled-fake-a.mp4", "0x162338", 440),
            self._make_media("controlled-fake-b.mp4", "0x301d31", 520),
        ]
        external_ids = ["fixture-external-a", "fixture-external-b"]
        internal_ids = ["fixture-candidate-a", "fixture-candidate-b"]
        prompt_ids = ["fixture-prompt-a", "fixture-prompt-b"]
        plan_hash = "e" * 64
        now = studio.utc_now()
        metadata_base = {
            "prompt": "controlled fake only; no Agent, ComfyUI or GPU was called",
            "mode": "fl2va",
            "width": 608,
            "height": 352,
            "actual_seconds": 0.8,
            "fixture_provenance": {
                "external_execution": "controlled-fake-h3-no-gpu",
                "real_model_output": False,
            },
        }
        with closing(studio.connect()) as db:
            cursor = db.execute(
                """INSERT INTO jobs
                (shot_id, kind, state, message, created_at, h3_project, prompt_ids, candidate_ids,
                 updated_at, completed_at, plan_hash, source_snapshot)
                VALUES (?, 'draft', '完成', 'controlled fake completed without GPU', ?,
                        'fixture-controlled-fake', ?, ?, ?, ?, ?, ?)""",
                (
                    shot_id,
                    now,
                    json.dumps(prompt_ids),
                    json.dumps(external_ids),
                    now,
                    now,
                    plan_hash,
                    json.dumps(
                        {
                            "adapter": "controlled-fake-h3-no-gpu",
                            "real_model_output": False,
                            "arguments": [
                                "--prompt",
                                metadata_base["prompt"],
                                "--width",
                                "608",
                                "--height",
                                "352",
                                "--seconds",
                                "0.8",
                            ],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            draft_job_id = int(cursor.lastrowid)
            for index, (candidate_id, external_id, prompt_id, path) in enumerate(
                zip(internal_ids, external_ids, prompt_ids, files),
                start=1,
            ):
                db.execute(
                    """INSERT INTO candidates
                    (id, shot_id, label, seed, created_at, thumbnail, selected, scores, note, status,
                     source, external_id, prompt_id, output_file, archived, metadata)
                    VALUES (?, ?, ?, ?, ?, '', 0, '{}', 'controlled fake candidate', 'completed',
                            'h3', ?, ?, ?, 0, ?)""",
                    (
                        candidate_id,
                        shot_id,
                        chr(64 + index),
                        1000 + index,
                        now,
                        external_id,
                        prompt_id,
                        str(path),
                        json.dumps(metadata_base, ensure_ascii=False),
                    ),
                )
            db.execute(
                """INSERT INTO production_batches
                (id, project_id, name, state, item_count, submitted_count, completed_count,
                 failed_count, cancelled_count, config, message, created_at, updated_at, completed_at)
                VALUES ('fixture-batch', ?, 'controlled fake low-res batch', 'completed', 1, 1, 1,
                        0, 0, ?, 'controlled fake only', ?, ?, ?)""",
                (
                    self.project["id"],
                    json.dumps({"external_execution": "controlled-fake-h3-no-gpu", "real_model_output": False}),
                    now,
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO production_batch_items
                (id, batch_id, shot_id, ordinal, title, state, plan_hash, plan_snapshot, attempts,
                 h3_project, prompt_ids, message, created_at, updated_at, completed_at,
                 validation_hash, draft_job_id, draft_job_revision)
                VALUES ('fixture-item', 'fixture-batch', ?, 1, 'controlled fake shot', 'completed', ?,
                        ?, 1, 'fixture-controlled-fake', ?, 'completed without external submission',
                        ?, ?, ?, ?, ?, 0)""",
                (
                    shot_id,
                    plan_hash,
                    json.dumps({"fake": True, "candidate_count": 2}),
                    json.dumps(prompt_ids),
                    now,
                    now,
                    now,
                    plan_hash,
                    draft_job_id,
                ),
            )
            media_evidence = [
                {
                    "candidate_id": external_id,
                    "output_file": str(path),
                    "seed": 1000 + index,
                    "checksum_sha256": self._sha(path),
                    "fixture": "controlled-fake-h3-no-gpu",
                }
                for index, (external_id, path) in enumerate(zip(external_ids, files), start=1)
            ]
            db.execute(
                """INSERT INTO production_item_attempts
                (id, item_id, batch_id, shot_id, attempt, state, plan_hash, plan_snapshot,
                 h3_project, prompt_ids, candidate_ids, source_snapshot, media_evidence,
                 created_at, updated_at, completed_at, validation_hash, draft_job_id, draft_job_revision)
                VALUES ('fixture-attempt', 'fixture-item', 'fixture-batch', ?, 1, 'completed', ?, ?,
                        'fixture-controlled-fake', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    shot_id,
                    plan_hash,
                    json.dumps({"fake": True, "candidate_count": 2}),
                    json.dumps(prompt_ids),
                    json.dumps(external_ids),
                    json.dumps({"adapter": "controlled-fake-h3-no-gpu", "real_model_output": False}),
                    json.dumps(media_evidence),
                    now,
                    now,
                    now,
                    plan_hash,
                    draft_job_id,
                ),
            )
            db.commit()
        return internal_ids

    def test_empty_project_to_verified_hd_archive_with_only_controlled_external_fakes(self) -> None:
        shot_id = self._build_storyboard_from_empty_project()
        candidate_ids = self._seed_two_controlled_candidates(shot_id)
        review_scores = ReviewScores(
            story_match=4,
            continuity=4,
            action=4,
            visual_quality=4,
            audio_quality=4,
        )
        audio_checks = AudioChecks(dialogue_match="pass", lip_sync="pass", ambience="pass")
        for candidate_id in candidate_ids:
            saved = save_candidate_review(
                studio.DB_PATH,
                studio.COMFY_OUTPUT_ROOT,
                shot_id,
                CandidateReviewRequest(
                    candidate_id=candidate_id,
                    decision="pass",
                    scores=review_scores,
                    audio_checks=audio_checks,
                    watched_seconds=0.8,
                    note="人工检查受控小媒体候选",
                ),
            )
            self.assertTrue(saved["can_select"])
        comparison = review_workspace(studio.DB_PATH, shot_id, studio.COMFY_OUTPUT_ROOT)
        self.assertEqual(comparison["summary"]["comparable_count"], 2)
        low_master = record_master_selection(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            candidate_ids[0],
            note="受控演练低清母版",
            base_revision=0,
        )

        hd_plan = create_hd_plan(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDPlanCreate(strategy_type="deterministic_scale", target_width=1344, target_height=768),
        )
        validation = validate_hd_plan(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            HDValidationRequest(plan_id=hd_plan["id"], expected_plan_hash=hd_plan["plan_hash"]),
            studio.run_hd_operation,
        )
        self.assertFalse(validation["gpu_submitted"])
        job = submit_hd_job(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDSubmitRequest(
                validation_id=validation["id"],
                expected_validation_hash=validation["validation_hash"],
                idempotency_key="fixture-deterministic-scale-0001",
                confirm=True,
            ),
        )
        self.assertTrue(process_next_hd_job(studio.DB_PATH, studio.COMFY_OUTPUT_ROOT, studio.run_hd_operation))
        with closing(studio.connect()) as db:
            artifact = dict(db.execute("SELECT * FROM hd_artifacts WHERE job_id = ?", (job["id"],)).fetchone())
        hd_review = save_hd_review(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDReviewRequest(
                artifact_id=artifact["id"],
                decision="pass",
                scores=HDScores(
                    story_match=4,
                    identity_continuity=5,
                    temporal_stability=5,
                    visual_detail=3,
                    audio_quality=4,
                ),
                watched_seconds=0.8,
                note="确定性放大保持动作与声音，不声明生成了新细节",
            ),
        )
        self.assertFalse(hd_review["drift_confirmed"])
        hd_master = select_hd_artifact(
            studio.DB_PATH,
            studio.COMFY_OUTPUT_ROOT,
            shot_id,
            HDSelectRequest(artifact_id=artifact["id"], base_revision=0, note="受控演练高清母版"),
        )

        saved_plan = save_delivery_plan(
            studio.DB_PATH,
            DeliveryPlanPatch(
                base_revision=0,
                items=[
                    DeliveryPlanItemInput(
                        shot_id=shot_id,
                        subtitle_enabled=True,
                        subtitle_start_seconds=0,
                        in_point_seconds=0,
                        out_point_seconds=0.8,
                        dialogue_mode="original",
                    )
                ],
            ),
            studio.COMFY_OUTPUT_ROOT,
        )
        locked = lock_delivery_plan(
            studio.DB_PATH,
            saved_plan["plan"]["revision"],
            studio.COMFY_OUTPUT_ROOT,
        )
        source = locked["plan"]["items"][0]["source_snapshot"]
        self.assertEqual(source["source_type"], "hd_artifact")
        self.assertEqual(source["hd_artifact_id"], artifact["id"])

        export = studio.create_export(studio.ExportRequest(width=1344, height=768, polish_audio=False))
        claimed = studio.claim_next_export_run()
        self.assertEqual(claimed["id"], export["id"])
        studio.run_export_job(export["id"])
        completed = studio.row("SELECT * FROM export_runs WHERE id = ?", (export["id"],))
        self.assertEqual(completed["state"], "已完成", completed)
        current = studio.current_export()
        self.assertTrue(current["available"])
        self.assertEqual((current["width"], current["height"]), (1344, 768))
        self.assertTrue(current["has_audio"])

        for category, note in (
            ("picture_continuity", "完整观看，画面与连续性通过"),
            ("sound", "完整听审，对白、环境声与音量通过"),
        ):
            studio.create_delivery_signoff(
                studio.DeliverySignoffRequest(
                    category=category,
                    decision="pass",
                    note=note,
                    source="fixture:human-confirmation",
                )
            )
        archive = create_project_archive(
            studio.DB_PATH,
            studio.BACKUP_ROOT,
            self.project["id"],
            studio.EXPORT_ROOT,
        )
        verification = verify_project_archive(studio.DB_PATH, archive["id"])
        self.assertTrue(verification["ok"])
        acceptance = studio.run_production_acceptance()
        self.assertEqual(acceptance["status"], "deliverable", acceptance)
        self.assertTrue(acceptance["delivery"]["ready"])
        self.assertEqual({stage["status"] for stage in acceptance["delivery"]["stages"]} - {"pass", "warn"}, set())
        self.assertEqual(archive["omitted_count"], 0)

        with closing(sqlite3.connect(studio.DB_PATH)) as db:
            db.row_factory = sqlite3.Row
            trace = json.loads(db.execute("SELECT metadata FROM candidates WHERE id = ?", (candidate_ids[0],)).fetchone()[0])
            self.assertFalse(trace["fixture_provenance"]["real_model_output"])
            self.assertEqual(low_master["revision"], 1)
            self.assertEqual(hd_master["revision"], 1)


if __name__ == "__main__":
    unittest.main()
