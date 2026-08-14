from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from backend.hd_delivery import (
    HDPlanCreate,
    HDReviewRequest,
    HDRollbackRequest,
    HDRunnerError,
    HDScores,
    HDSelectRequest,
    HDSubmitRequest,
    HDUnknownResolution,
    HDValidationRequest,
    create_hd_plan,
    hd_project_workspace,
    hd_shot_workspace,
    init_hd_schema,
    process_next_hd_job,
    recover_hd_jobs,
    resolve_unknown_job,
    retry_hd_job,
    rollback_hd_master,
    save_hd_review,
    select_hd_artifact,
    submit_hd_job,
    validate_hd_plan,
)
from backend.delivery_plan import (
    DeliveryPlanItemInput,
    DeliveryPlanPatch,
    delivery_workspace,
    init_delivery_schema,
    lock_delivery_plan,
    save_delivery_plan,
)


class HDDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "studio.db"
        self.source = self.root / "draft.mp4"
        self.source.write_bytes(b"trusted-low-resolution-master")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE projects (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, episode TEXT NOT NULL,
                  logline TEXT NOT NULL, target_duration REAL NOT NULL, created_at TEXT NOT NULL,
                  archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE shots (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
                  scene_code TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, prompt TEXT NOT NULL,
                  status TEXT NOT NULL, dialogue TEXT NOT NULL, seconds REAL NOT NULL, width INTEGER NOT NULL,
                  height INTEGER NOT NULL, subtitle_enabled INTEGER NOT NULL DEFAULT 1,
                  subtitle_start_seconds REAL, updated_at TEXT NOT NULL DEFAULT 'now'
                );
                CREATE TABLE candidates (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL REFERENCES shots(id), selected INTEGER NOT NULL,
                  archived INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, output_file TEXT,
                  external_id TEXT, prompt_id TEXT, seed INTEGER, metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE candidate_master_versions (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), shot_id TEXT NOT NULL,
                  revision INTEGER NOT NULL, candidate_id TEXT NOT NULL REFERENCES candidates(id),
                  review_id TEXT NOT NULL, review_revision INTEGER NOT NULL,
                  candidate_snapshot TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE assets (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, managed_path TEXT, checksum_sha256 TEXT
                );
                CREATE TABLE shot_references (
                  id TEXT PRIMARY KEY, shot_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                  reference_type TEXT NOT NULL, ordinal INTEGER NOT NULL, role TEXT NOT NULL
                );
                """
            )
            db.execute("INSERT INTO projects VALUES ('p1', '测试剧', 'EP01', '', 10, 'now', 0)")
            db.execute("INSERT INTO projects VALUES ('p2', '隔离剧', 'EP01', '', 10, 'now', 0)")
            db.execute("INSERT INTO workspace_settings VALUES ('active_project_id', 'p1', 'now')")
            db.execute("INSERT INTO shots VALUES ('s1', 'p1', 1, 'S01', '雨夜迟疑', '人物迟疑', 'one continuous shot', '草稿已选', '', 5.167, 608, 352, 0, NULL, 'now')")
            db.execute("INSERT INTO shots VALUES ('s2', 'p2', 1, 'S01', '另一镜头', '另一场景', 'other', '草稿已选', '', 5.167, 608, 352, 0, NULL, 'now')")
            metadata = json.dumps({"prompt": "one continuous shot", "mode": "fl2va", "width": 608, "height": 352, "actual_seconds": 5.167})
            db.execute(
                "INSERT INTO candidates VALUES ('c1', 's1', 1, 0, 'completed', ?, 'draft-001', 'prompt-001', 42, ?)",
                (str(self.source), metadata),
            )
            snapshot = {
                "output_file": str(self.source.resolve()),
                "size_bytes": self.source.stat().st_size,
                "checksum_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "trace": {"plan_hash": "p" * 64, "plan_snapshot": {"mode": "fl2va"}},
            }
            db.execute("INSERT INTO candidate_master_versions VALUES ('m1', 'p1', 's1', 1, 'c1', 'r1', 1, ?, 'now')", (json.dumps(snapshot),))
            init_hd_schema(db)
            init_delivery_schema(db)
            db.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def probe(path: Path) -> dict:
        return {
            "ok": path.is_file(), "issues": [], "duration_seconds": 5.167,
            "video": {"codec": "h264", "width": 1344, "height": 768},
            "audio": {"present": True, "codec": "aac", "channels": 2, "sample_rate": 48000},
        }

    def runner(self, request: dict, dry_run: bool) -> dict:
        strategy = request["strategy_type"]
        source = request["source"]
        if strategy == "deterministic_scale":
            model_id = "ffmpeg-lanczos"
            workflow_id = "deterministic-scale-contain-v1"
        else:
            model_id = (
                "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
                if strategy == "ref2va_regenerate" or source.get("original_mode") == "ref2va"
                else "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
            )
            workflow_id = "h3-ref2va-regenerate-v2" if strategy == "ref2va_regenerate" else f"h3-draft-{source.get('original_mode')}-target-v2"
        if dry_run:
            return {
                "gpu_submitted": False, "adapter": "controlled-fake-h3",
                "model_id": model_id, "workflow_id": workflow_id, "command": request,
            }
        output_root = Path(request["attempt_output_root"])
        output_root.mkdir(parents=True, exist_ok=True)
        output = output_root / f"{request['expected_artifact_id']}.mp4"
        output.write_bytes(b"controlled-fake-hd-media-" + strategy.encode())
        result = {
            "gpu_submitted": strategy != "deterministic_scale",
            "output_file": str(output), "model_id": model_id, "workflow_id": workflow_id,
            "prompt_id": "fake-prompt", "comfy_task_id": "fake-comfy-task",
        }
        if strategy != "deterministic_scale":
            refs = {"ref_images": [], "ref_videos": [], "ref_audios": [], "first_frame": None, "last_frame": None}
            if strategy == "ref2va_regenerate":
                refs["ref_videos"].append(source["media"]["path"])
            for reference in source.get("references") or []:
                refs[f"ref_{reference['reference_type']}s"].append(reference["managed_path"])
            adapter_input = {
                "seed": source["generation_seed"],
                "prompt_sha256": hashlib.sha256(source["prompt"].encode()).hexdigest(),
                "model_id": model_id, "workflow_id": workflow_id, "references": refs,
            }
            result["adapter_input"] = adapter_input
            result["manifest_record"] = {
                "status": "completed", "prompt_id": "fake-prompt", "seed": source["generation_seed"],
                "prompt": source["prompt"], "width": request["target_width"], "height": request["target_height"],
                "references": refs,
            }
        return result

    def plan_and_validation(self, strategy: str = "ref2va_regenerate"):
        plan = create_hd_plan(self.db_path, self.root, "s1", HDPlanCreate(strategy_type=strategy))
        validation = validate_hd_plan(
            self.db_path, self.root,
            HDValidationRequest(plan_id=plan["id"], expected_plan_hash=plan["plan_hash"]), self.runner,
        )
        return plan, validation

    def completed_artifact(self, strategy: str = "ref2va_regenerate"):
        plan, validation = self.plan_and_validation(strategy)
        job = submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key=f"key-{strategy}-0001", confirm=True,
            ),
        )
        self.assertTrue(process_next_hd_job(self.db_path, self.root, self.runner, self.probe))
        workspace = hd_shot_workspace(self.db_path, self.root, "s1")
        return plan, validation, job, workspace["artifacts"][0]

    def test_locked_master_required_and_three_strategy_types_never_conflate_scale(self) -> None:
        with self.assertRaises(HTTPException) as missing:
            create_hd_plan(self.db_path, self.root, "s2", HDPlanCreate(strategy_type="deterministic_scale"))
        self.assertEqual(missing.exception.status_code, 404)  # cross-project lookup never reveals the other project

        strategies = [
            create_hd_plan(self.db_path, self.root, "s1", HDPlanCreate(strategy_type=name))
            for name in ("ref2va_regenerate", "original_model_regenerate", "deterministic_scale")
        ]
        self.assertEqual([item["revision"] for item in strategies], [1, 2, 3])
        self.assertEqual([item["strategy"]["kind"] for item in strategies], ["model_regeneration", "model_regeneration", "pixel_scaling"])
        self.assertTrue(strategies[0]["strategy"]["gpu_required"])
        self.assertFalse(strategies[2]["strategy"]["gpu_required"])
        self.assertIn("不会生成新细节", strategies[2]["estimated_operation"])

    def test_dry_run_is_zero_gpu_and_submit_is_explicit_idempotent_and_single_lease(self) -> None:
        plan, validation = self.plan_and_validation()
        self.assertFalse(validation["gpu_submitted"])
        payload = HDSubmitRequest(
            validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
            idempotency_key="stable-idempotency-key", confirm=True,
        )
        first = submit_hd_job(self.db_path, self.root, "s1", payload)
        again = submit_hd_job(self.db_path, self.root, "s1", payload)
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(HTTPException) as active:
            submit_hd_job(
                self.db_path, self.root, "s1",
                HDSubmitRequest(
                    validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                    idempotency_key="different-key-000000", confirm=True,
                ),
            )
        self.assertEqual(active.exception.status_code, 409)
        self.assertTrue(process_next_hd_job(self.db_path, self.root, self.runner, self.probe))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_artifacts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_shot_leases").fetchone()[0], 0)

    def test_artifact_provenance_review_drift_gate_and_append_only_rollback(self) -> None:
        _, _, _, artifact = self.completed_artifact("ref2va_regenerate")
        self.assertEqual(artifact["strategy_kind"], "model_regeneration")
        self.assertEqual(artifact["prompt"], "one continuous shot")
        self.assertEqual(artifact["seed"], 42)
        self.assertEqual(artifact["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(len(artifact["output_sha256"]), 64)
        scores = HDScores(story_match=4, identity_continuity=4, temporal_stability=4, visual_detail=4, audio_quality=4)
        with self.assertRaises(HTTPException) as drift:
            save_hd_review(
                self.db_path, self.root, "s1",
                HDReviewRequest(artifact_id=artifact["id"], decision="pass", scores=scores, watched_seconds=5, drift_confirmed=False),
                self.probe,
            )
        self.assertEqual(drift.exception.status_code, 422)
        review = save_hd_review(
            self.db_path, self.root, "s1",
            HDReviewRequest(
                artifact_id=artifact["id"], decision="pass", scores=scores, watched_seconds=5,
                drift_confirmed=True, note="已与低清母版逐段比较，漂移可接受",
            ), self.probe,
        )
        self.assertEqual(review["decision"], "pass")
        selected = select_hd_artifact(
            self.db_path, self.root, "s1", HDSelectRequest(artifact_id=artifact["id"], base_revision=0, note="高清定稿"),
        )
        rolled = rollback_hd_master(
            self.db_path, self.root, "s1",
            HDRollbackRequest(target_revision=1, base_revision=1, note="演练追加式回滚", confirm=True),
        )
        self.assertEqual((selected["revision"], rolled["revision"]), (1, 2))
        self.assertEqual(rolled["action"], "rollback")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_master_versions").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_artifact_reviews").fetchone()[0], 1)

    def test_scale_has_pixel_scaling_provenance_and_does_not_require_drift_confirmation(self) -> None:
        _, _, _, artifact = self.completed_artifact("deterministic_scale")
        scores = HDScores(story_match=4, identity_continuity=5, temporal_stability=5, visual_detail=3, audio_quality=4)
        review = save_hd_review(
            self.db_path, self.root, "s1",
            HDReviewRequest(artifact_id=artifact["id"], decision="pass", scores=scores, watched_seconds=5), self.probe,
        )
        self.assertEqual(artifact["strategy_kind"], "pixel_scaling")
        self.assertFalse(artifact["strategy"]["gpu_required"])
        self.assertFalse(review["drift_confirmed"])

    def test_locked_assembly_freezes_selected_hd_artifact_not_low_resolution_candidate(self) -> None:
        _, _, _, artifact = self.completed_artifact("deterministic_scale")
        scores = HDScores(story_match=4, identity_continuity=5, temporal_stability=5, visual_detail=3, audio_quality=4)
        save_hd_review(
            self.db_path, self.root, "s1",
            HDReviewRequest(artifact_id=artifact["id"], decision="pass", scores=scores, watched_seconds=5), self.probe,
        )
        selection = select_hd_artifact(
            self.db_path, self.root, "s1", HDSelectRequest(artifact_id=artifact["id"], base_revision=0),
        )
        saved = save_delivery_plan(
            self.db_path,
            DeliveryPlanPatch(base_revision=0, items=[DeliveryPlanItemInput(shot_id="s1", subtitle_enabled=False)]),
            self.root,
        )
        source = saved["plan"]["items"][0]["source_snapshot"]
        self.assertEqual(source["source_type"], "hd_artifact")
        self.assertEqual(source["hd_artifact_id"], artifact["id"])
        self.assertEqual(source["hd_master_version_id"], selection["id"])
        self.assertEqual(source["hd_strategy_kind"], "pixel_scaling")
        self.assertEqual(source["media"]["checksum_sha256"], artifact["output_sha256"])
        locked = lock_delivery_plan(self.db_path, saved["plan"]["revision"], self.root)
        self.assertEqual(locked["plan"]["status"], "locked")
        self.assertEqual(delivery_workspace(self.db_path, self.root)["plan"]["items"][0]["hd_artifact_id"], artifact["id"])

    def test_source_change_after_dry_run_blocks_submission_without_job(self) -> None:
        _, validation = self.plan_and_validation()
        self.source.write_bytes(b"source-was-replaced")
        with self.assertRaises(HTTPException) as changed:
            submit_hd_job(
                self.db_path, self.root, "s1",
                HDSubmitRequest(
                    validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                    idempotency_key="source-drift-key", confirm=True,
                ),
            )
        self.assertEqual(changed.exception.status_code, 409)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_generation_jobs").fetchone()[0], 0)

    def test_source_change_after_submit_fails_before_runner_and_releases_lease(self) -> None:
        _, validation = self.plan_and_validation()
        job = submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key="stale-after-submit", confirm=True,
            ),
        )
        self.source.write_bytes(b"changed-after-the-frozen-job-was-created")
        calls: list[dict] = []

        def must_not_run(request: dict, _dry_run: bool) -> dict:
            calls.append(request)
            return self.runner(request, False)

        self.assertTrue(process_next_hd_job(self.db_path, self.root, must_not_run, self.probe))
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            failed = dict(db.execute("SELECT * FROM hd_generation_jobs WHERE id = ?", (job["id"],)).fetchone())
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["retry_safe"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_artifacts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_shot_leases").fetchone()[0], 0)
        self.assertFalse((self.root / "hd-delivery" / "attempts" / job["id"]).exists())

    def test_reference_change_after_submit_fails_before_runner(self) -> None:
        reference = self.root / "reference.png"
        reference.write_bytes(b"trusted-reference")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO assets (id, name, managed_path, checksum_sha256) VALUES (?, ?, ?, ?)",
                ("a1", "reference", str(reference.resolve()), hashlib.sha256(reference.read_bytes()).hexdigest()),
            )
            db.execute(
                "INSERT INTO shot_references (id, shot_id, asset_id, reference_type, ordinal, role) VALUES (?, ?, ?, ?, ?, ?)",
                ("ref1", "s1", "a1", "image", 1, "character"),
            )
            db.commit()
        _, validation = self.plan_and_validation()
        submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key="reference-stale-after-submit", confirm=True,
            ),
        )
        reference.write_bytes(b"reference-was-replaced")
        calls = 0

        def must_not_run(_request: dict, _dry_run: bool) -> dict:
            nonlocal calls
            calls += 1
            raise AssertionError("runner must not be called")

        self.assertTrue(process_next_hd_job(self.db_path, self.root, must_not_run, self.probe))
        self.assertEqual(calls, 0)
        with closing(sqlite3.connect(self.db_path)) as db:
            job = db.execute("SELECT state, retry_safe FROM hd_generation_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            self.assertEqual(tuple(job), ("failed", 1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_artifacts").fetchone()[0], 0)

    def test_manifest_mismatch_keeps_full_reconciliation_evidence_without_artifact(self) -> None:
        _, validation = self.plan_and_validation()
        submitted = submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key="manifest-mismatch", confirm=True,
            ),
        )

        def mismatched(request: dict, dry_run: bool) -> dict:
            result = self.runner(request, dry_run)
            result["manifest_record"]["seed"] += 1
            return result

        self.assertTrue(process_next_hd_job(self.db_path, self.root, mismatched, self.probe))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            job = dict(db.execute("SELECT * FROM hd_generation_jobs WHERE id = ?", (submitted["id"],)).fetchone())
            evidence = json.loads(job["evidence"])
            self.assertEqual(job["state"], "submission_outcome_unknown")
            self.assertEqual(evidence["runner_result"]["manifest_record"]["seed"], 43)
            self.assertTrue(evidence["runner_called"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_artifacts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM hd_shot_leases WHERE job_id = ?", (job["id"],)).fetchone()[0], "unknown")
        artifact_root = self.root / "hd-delivery" / "artifacts"
        self.assertEqual(list(artifact_root.glob("*")) if artifact_root.exists() else [], [])

    def test_windows_publication_lock_rejects_probe_time_tamper(self) -> None:
        _, validation = self.plan_and_validation("deterministic_scale")
        submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key="publication-lock", confirm=True,
            ),
        )
        denied: list[bool] = []

        def hostile_probe(path: Path) -> dict:
            try:
                path.chmod(0o600)
                path.write_bytes(b"tampered-during-final-publication")
            except OSError:
                denied.append(True)
            return self.probe(path)

        self.assertTrue(process_next_hd_job(self.db_path, self.root, self.runner, hostile_probe))
        self.assertEqual(denied, [True])
        with closing(sqlite3.connect(self.db_path)) as db:
            artifact = db.execute("SELECT output_path, output_sha256 FROM hd_artifacts").fetchone()
            self.assertIsNotNone(artifact)
            output = Path(artifact[0])
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), artifact[1])

    def test_archive_lease_keeps_queued_job_unclaimed_until_freeze_ends(self) -> None:
        _, validation = self.plan_and_validation("deterministic_scale")
        submitted = submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key="archive-freeze", confirm=True,
            ),
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "CREATE TABLE project_archive_leases (project_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, operation TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            db.execute("INSERT INTO project_archive_leases VALUES ('p1', 'freeze-1', 'archive', 'now')")
            db.commit()
        self.assertFalse(process_next_hd_job(self.db_path, self.root, self.runner, self.probe))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT state FROM hd_generation_jobs WHERE id = ?", (submitted["id"],)).fetchone()[0], "queued")
            db.execute("DELETE FROM project_archive_leases")
            db.commit()
        self.assertTrue(process_next_hd_job(self.db_path, self.root, self.runner, self.probe))

    def test_archive_freeze_rejects_hd_dry_run_and_new_submission(self) -> None:
        plan = create_hd_plan(self.db_path, self.root, "s1", HDPlanCreate(strategy_type="deterministic_scale"))
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "CREATE TABLE project_archive_leases (project_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, operation TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            db.execute("INSERT INTO project_archive_leases VALUES ('p1', 'freeze-1', 'archive', 'now')")
            db.commit()
        calls = 0

        def must_not_run(_request: dict, _dry_run: bool) -> dict:
            nonlocal calls
            calls += 1
            raise AssertionError("dry-run adapter must not start while archive is frozen")

        with self.assertRaises(HTTPException) as frozen:
            validate_hd_plan(
                self.db_path, self.root,
                HDValidationRequest(plan_id=plan["id"], expected_plan_hash=plan["plan_hash"]), must_not_run,
            )
        self.assertEqual(frozen.exception.status_code, 409)
        self.assertEqual(calls, 0)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_validation_leases").fetchone()[0], 0)
            db.execute("DELETE FROM project_archive_leases")
            db.commit()
        validation = validate_hd_plan(
            self.db_path, self.root,
            HDValidationRequest(plan_id=plan["id"], expected_plan_hash=plan["plan_hash"]), self.runner,
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO project_archive_leases VALUES ('p1', 'freeze-2', 'archive', 'now')")
            db.commit()
        with self.assertRaises(HTTPException) as blocked:
            submit_hd_job(
                self.db_path, self.root, "s1",
                HDSubmitRequest(
                    validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                    idempotency_key="frozen-new-submit", confirm=True,
                ),
            )
        self.assertEqual(blocked.exception.status_code, 409)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_generation_jobs").fetchone()[0], 0)

    def test_pre_spawn_failure_is_retryable_but_started_failure_requires_audited_resolution(self) -> None:
        _, validation = self.plan_and_validation()
        first = submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key="pre-spawn-failure", confirm=True,
            ),
        )

        def pre_spawn(_: dict, __: bool) -> dict:
            raise HDRunnerError("missing executable", process_started=False)

        process_next_hd_job(self.db_path, self.root, pre_spawn, self.probe)
        retry = retry_hd_job(self.db_path, self.root, first["id"], "retry-after-safe-failure", True)
        self.assertEqual(retry["state"], "queued")

        def started(_: dict, __: bool) -> dict:
            raise HDRunnerError("process timeout", process_started=True)

        process_next_hd_job(self.db_path, self.root, started, self.probe)
        workspace = hd_shot_workspace(self.db_path, self.root, "s1")
        unknown = workspace["jobs"][0]
        self.assertEqual(unknown["state"], "submission_outcome_unknown")
        with self.assertRaises(HTTPException):
            retry_hd_job(self.db_path, self.root, unknown["id"], "unsafe-retry-key", True)
        resolved = resolve_unknown_job(
            self.db_path, unknown["id"],
            HDUnknownResolution(expected_revision=unknown["revision"], note="已核对 fake 记录，无外部任务", confirm_no_external_submission=True),
        )
        self.assertTrue(resolved["retry_safe"])
        self.assertEqual(resolved["state"], "failed")

    def test_restart_marks_running_unknown_and_project_workspace_is_isolated(self) -> None:
        _, validation = self.plan_and_validation()
        job = submit_hd_job(
            self.db_path, self.root, "s1",
            HDSubmitRequest(
                validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                idempotency_key="restart-recovery-key", confirm=True,
            ),
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE hd_generation_jobs SET state = 'running' WHERE id = ?", (job["id"],))
            db.commit()
        self.assertEqual(recover_hd_jobs(self.db_path), 1)
        workspace = hd_project_workspace(self.db_path, self.root)
        self.assertEqual(workspace["project"]["id"], "p1")
        self.assertEqual([item["shot"]["id"] for item in workspace["shots"]], ["s1"])
        self.assertEqual(workspace["shots"][0]["jobs"][0]["state"], "submission_outcome_unknown")

    def test_concurrent_submissions_create_only_one_attempt_and_one_lease(self) -> None:
        _, validation = self.plan_and_validation()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def submit(index: int) -> None:
            try:
                barrier.wait()
                result = submit_hd_job(
                    self.db_path, self.root, "s1",
                    HDSubmitRequest(
                        validation_id=validation["id"], expected_validation_hash=validation["validation_hash"],
                        idempotency_key=f"concurrent-key-{index}", confirm=True,
                    ),
                )
                outcomes.append(result["id"])
            except HTTPException as exc:
                outcomes.append(f"error-{exc.status_code}")

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len([item for item in outcomes if not item.startswith("error")]), 1)
        self.assertEqual(outcomes.count("error-409"), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_generation_jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hd_shot_leases").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
