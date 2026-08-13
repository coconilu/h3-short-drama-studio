from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException


REFERENCE_LIMITS = {"image": 9, "video": 3, "audio": 3}
MEDIA_ORDER = {"image": 1, "video": 2, "audio": 3}
BIBLE_ROLE = {
    "character": "identity",
    "location": "location",
    "prop": "prop",
    "style": "style",
    "voice": "voice",
}
REFERENCE_INSTRUCTIONS = {
    "identity": "Use {tag} as the exact character identity and visible costume reference; preserve face, age, hair, body proportions, clothing design, and clothing color.",
    "costume": "Use {tag} as the costume reference; preserve design, color, material, and accessories.",
    "location": "Use {tag} as the location continuity reference; preserve layout, weather, vehicles, materials, and practical lighting.",
    "style": "Use {tag} as the visual style reference; preserve palette, contrast, lens character, and cinematic texture.",
    "prop": "Use {tag} as the exact prop reference; preserve shape, color, material, and distinctive details.",
    "action": "Use {tag} as the action reference; follow motion rhythm and body mechanics while preserving the story subject.",
    "camera": "Use {tag} as the camera reference; follow framing, movement, and timing.",
    "performance": "Use {tag} as the performance reference; follow expression and gesture timing.",
    "voice": "Use {tag} as the voice reference; preserve speaker identity, pacing, and emotional tone.",
    "ambience": "Use {tag} as the ambience reference; preserve acoustic environment and intensity.",
    "effects": "Use {tag} as the sound-effects reference; follow timing and texture.",
    "music": "Use {tag} as the music reference; preserve mood and rhythm without changing shot action.",
    "generic": "Use {tag} as a continuity reference and retain its relevant visual or audio characteristics.",
}
TAG_PATTERN = re.compile(r"<(Picture|Video|Audio)\s+\d+>", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_prompt_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS h3_prompt_plans (
          id TEXT PRIMARY KEY,
          shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          plan_hash TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('validated', 'approved', 'superseded')),
          mode TEXT NOT NULL,
          creative_prompt TEXT NOT NULL,
          compiled_prompt TEXT NOT NULL,
          source_snapshot TEXT NOT NULL,
          references_snapshot TEXT NOT NULL,
          bible_snapshot TEXT NOT NULL,
          warnings TEXT NOT NULL,
          adapter_output TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          validated_at TEXT NOT NULL,
          approved_at TEXT,
          UNIQUE(shot_id, plan_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_h3_prompt_plans_shot
          ON h3_prompt_plans(shot_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS h3_validation_leases (
          id TEXT PRIMARY KEY,
          shot_id TEXT NOT NULL UNIQUE REFERENCES shots(id) ON DELETE RESTRICT,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          plan_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_h3_validation_leases_project
          ON h3_validation_leases(project_id, shot_id);
        CREATE TABLE IF NOT EXISTS h3_generation_leases (
          id TEXT PRIMARY KEY,
          shot_id TEXT NOT NULL UNIQUE REFERENCES shots(id) ON DELETE RESTRICT,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          validation_hash TEXT NOT NULL,
          input_snapshot TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_h3_generation_leases_project
          ON h3_generation_leases(project_id, shot_id);
        """
    )
    # The application uses a single API worker. A new process cannot own leases
    # left by a previous process. The durable job is deliberately retained as
    # unknown until manifest/queue evidence is reconciled; it is never made
    # retry-safe merely because the local process restarted.
    db.execute("DELETE FROM h3_validation_leases")
    db.execute("DELETE FROM h3_generation_leases")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone():
        now = utc_now()
        job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
        retry_clause = ", retry_safe = 0" if "retry_safe" in job_columns else ""
        db.execute(
            f"""UPDATE jobs SET state = '提交状态未知',
            message = '服务重启时 H3 提交尚未完成对账，不会自动重试',
            updated_at = ?, completed_at = NULL{retry_clause}
            WHERE kind = 'draft' AND state = '提交中'""",
            (now,),
        )
    columns = {row[1] for row in db.execute("PRAGMA table_info(h3_prompt_plans)").fetchall()}
    if "stale_reasons" not in columns:
        db.execute("ALTER TABLE h3_prompt_plans ADD COLUMN stale_reasons TEXT NOT NULL DEFAULT '[]'")
    if "superseded_at" not in columns:
        db.execute("ALTER TABLE h3_prompt_plans ADD COLUMN superseded_at TEXT")


def mark_prompt_plans_stale(
    db: sqlite3.Connection,
    project_id: str,
    reason: str,
    *,
    shot_ids: list[str] | None = None,
) -> int:
    """Supersede currently approved H3 plans and retain a human-readable cause."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'h3_prompt_plans'"
    ).fetchone():
        return 0
    if shot_ids is not None and not shot_ids:
        return 0
    query = "SELECT id, stale_reasons FROM h3_prompt_plans WHERE project_id = ? AND status = 'approved'"
    params: list[Any] = [project_id]
    if shot_ids is not None:
        query += f" AND shot_id IN ({','.join('?' for _ in shot_ids)})"
        params.extend(shot_ids)
    rows = db.execute(query, params).fetchall()
    now = utc_now()
    for row in rows:
        try:
            reasons = json.loads(row["stale_reasons"] or "[]")
        except (TypeError, ValueError):
            reasons = []
        reasons = list(dict.fromkeys([*reasons, reason.strip()]))
        db.execute(
            """UPDATE h3_prompt_plans SET status = 'superseded', stale_reasons = ?, superseded_at = ?
            WHERE id = ? AND status = 'approved'""",
            (json.dumps(reasons, ensure_ascii=False), now, row["id"]),
        )
    return len(rows)


def _active_project(db: sqlite3.Connection) -> dict[str, Any]:
    setting = db.execute("SELECT value FROM workspace_settings WHERE key = 'active_project_id'").fetchone()
    if not setting:
        raise HTTPException(404, "当前没有已激活的项目")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (setting["value"],)).fetchone()
    if not project:
        raise HTTPException(404, "当前项目不存在")
    return dict(project)


def _json_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _asset_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except (ValueError, TypeError):
        item["metadata"] = {}
    return item


def _explicit_references(db: sqlite3.Connection, shot_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "asset_id": item["asset_id"],
            "reference_type": item["reference_type"],
            "role": item["role"],
            "source": "shot",
            "source_label": "镜头手工引用",
            "source_ordinal": item["ordinal"],
            "asset": _asset_dict(item),
        }
        for item in db.execute(
            """SELECT refs.*, assets.name, assets.kind, assets.description, assets.source AS asset_source,
            assets.managed_path, assets.media_type, assets.duration_seconds, assets.has_audio,
            assets.checksum_sha256, assets.archived, assets.metadata
            FROM shot_references refs JOIN assets ON assets.id = refs.asset_id
            WHERE refs.shot_id = ?
            ORDER BY CASE refs.reference_type WHEN 'image' THEN 1 WHEN 'video' THEN 2 ELSE 3 END,
            refs.ordinal""",
            (shot_id,),
        ).fetchall()
    ]


def _effective_bible(db: sqlite3.Connection, shot_id: str, project_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = db.execute(
        """SELECT DISTINCT entries.* FROM production_bible_entries entries
        LEFT JOIN production_bible_shots links ON links.entry_id = entries.id AND links.shot_id = ?
        WHERE entries.project_id = ? AND entries.archived = 0
          AND (entries.apply_globally = 1 OR links.shot_id IS NOT NULL)
        ORDER BY CASE entries.entry_type
          WHEN 'character' THEN 1 WHEN 'location' THEN 2 WHEN 'prop' THEN 3
          WHEN 'style' THEN 4 ELSE 5 END, entries.name""",
        (shot_id, project_id),
    ).fetchall()
    locked: list[dict[str, Any]] = []
    draft: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        entry["apply_globally"] = bool(entry["apply_globally"])
        entry["assets"] = [
            _asset_dict(asset)
            for asset in db.execute(
                """SELECT links.role AS bible_role, links.ordinal AS bible_ordinal, assets.*
                FROM production_bible_assets links JOIN assets ON assets.id = links.asset_id
                WHERE links.entry_id = ? ORDER BY links.ordinal""",
                (entry["id"],),
            ).fetchall()
        ]
        (locked if entry["status"] == "locked" else draft).append(entry)
    return locked, draft


def _merge_references(explicit: list[dict[str, Any]], bible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references = list(explicit)
    seen = {item["asset_id"] for item in references}
    bible_order = 0
    for entry in bible:
        for asset in entry["assets"]:
            if asset["id"] in seen:
                continue
            seen.add(asset["id"])
            bible_order += 1
            references.append(
                {
                    "id": f"bible-ref-{entry['id']}-{asset['id']}",
                    "asset_id": asset["id"],
                    "reference_type": asset.get("media_type") or "image",
                    "role": BIBLE_ROLE[entry["entry_type"]],
                    "source": "bible",
                    "source_label": entry["name"],
                    "source_ordinal": bible_order,
                    "bible_entry_id": entry["id"],
                    "asset": asset,
                }
            )
    references.sort(key=lambda item: (MEDIA_ORDER.get(item["reference_type"], 9), 0 if item["source"] == "shot" else 1, item["source_ordinal"]))
    media_ordinals = {"image": 0, "video": 0, "audio": 0}
    embedded_audio = 0
    for reference in references:
        media_type = reference["reference_type"]
        media_ordinals[media_type] += 1
        if media_type == "image":
            reference["tag"] = f"<Picture {media_ordinals[media_type]}>"
        elif media_type == "video":
            if reference["asset"].get("has_audio"):
                embedded_audio += 1
                reference["audio_tag"] = f"<Audio {embedded_audio}>"
            reference["tag"] = f"<Video {media_ordinals[media_type]}>"
        else:
            reference["tag"] = f"<Audio {embedded_audio + media_ordinals[media_type]}>"
    return references


def _frame_plan(seconds: float) -> tuple[float, int, float]:
    adapter_seconds = 5.0 if abs(seconds - 5.17) < 0.05 else seconds
    raw_frames = adapter_seconds * 24
    k = max(0, math.ceil((raw_frames - 5) / 17))
    frames = 17 * k + 5
    return adapter_seconds, frames, round(frames / 24, 3)


def _source_snapshot(
    db: sqlite3.Connection,
    shot: dict[str, Any],
    bible_public: list[dict[str, Any]],
    references_public: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "shot": {
            key: shot.get(key)
            for key in ("id", "project_id", "prompt", "dialogue", "sound", "width", "height", "seconds", "candidate_count", "strategy")
        },
        "bible": bible_public,
        "references": references_public,
    }
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'creative_storyboard_links'"
    ).fetchone():
        source = db.execute(
            """SELECT links.section_id, links.last_synced_revision, sections.title AS section_title,
            sections.revision AS current_section_revision
            FROM creative_storyboard_links links JOIN creative_sections sections ON sections.id = links.section_id
            WHERE links.project_id = ? AND links.shot_id = ?""",
            (shot["project_id"], shot["id"]),
        ).fetchone()
        snapshot["storyboard_source"] = dict(source) if source else None
    return snapshot


def compile_prompt_plan(db_path: Path, shot_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        shot_row = db.execute(
            "SELECT * FROM shots WHERE id = ? AND project_id = ?",
            (shot_id, project["id"]),
        ).fetchone()
        if not shot_row:
            raise HTTPException(404, "当前项目中没有这个镜头")
        shot = dict(shot_row)
        bible, draft_bible = _effective_bible(db, shot_id, project["id"])
        references = _merge_references(_explicit_references(db, shot_id), bible)

        blocking: list[str] = []
        warnings: list[str] = []
        counts = {media: sum(item["reference_type"] == media for item in references) for media in REFERENCE_LIMITS}
        if not shot.get("prompt", "").strip():
            blocking.append("镜头原始提示词为空")
        if shot["width"] % 32 or shot["height"] % 32:
            blocking.append("H3 宽高必须能被 32 整除")
        if shot["width"] > 1344 or shot["height"] > 768 or shot["width"] * shot["height"] > 1344 * 768:
            blocking.append("生成尺寸超过本机 H3 1344×768 节点上限")
        for media_type, count in counts.items():
            if count > REFERENCE_LIMITS[media_type]:
                blocking.append(f"{media_type} 参考 {count} 个，超过 H3 上限 {REFERENCE_LIMITS[media_type]}")
        for reference in references:
            asset = reference["asset"]
            if asset.get("asset_source", asset.get("source")) != "managed" or not asset.get("managed_path") or asset.get("archived"):
                blocking.append(f"参考素材“{asset.get('name') or asset.get('id')}”不是可用的真实受管文件")
            if reference["reference_type"] == "video":
                duration = asset.get("duration_seconds")
                if duration is None or not 2 <= float(duration) <= 15:
                    blocking.append(f"参考视频“{asset.get('name')}”需为 2–15 秒")
        if draft_bible:
            warnings.append("未锁定条目不会进入编译：" + "、".join(entry["name"] for entry in draft_bible))
        if not bible:
            warnings.append("当前镜头没有已锁定的生产圣经输入，将只使用原始镜头提示词")
        if any(entry["entry_type"] == "character" and not entry["assets"] for entry in bible):
            warnings.append("角色只有文本设定，没有真实身份素材；跨镜一致性仍有明显漂移风险")

        reference_lines: list[str] = []
        for reference in references:
            template = REFERENCE_INSTRUCTIONS.get(reference["role"], REFERENCE_INSTRUCTIONS["generic"])
            prefix = f"Use {reference.get('audio_tag')} for its soundtrack. " if reference.get("audio_tag") else ""
            reference_lines.append(prefix + template.format(tag=reference["tag"]))

        bible_lines: list[str] = []
        negative_parts: list[str] = []
        continuity_parts: list[str] = []
        bible_public: list[dict[str, Any]] = []
        for entry in bible:
            fragment = entry["prompt_fragment"].strip()
            if TAG_PATTERN.search(fragment):
                warnings.append(f"“{entry['name']}”的圣经片段含手写引用标签；编译时已移除并按真实顺序重新分配")
                fragment = TAG_PATTERN.sub("", fragment).strip()
            bible_lines.append(
                f"[{entry['entry_type'].upper()}: {entry['name']}] {entry['canonical_description'].strip()}"
                + (f" Prompt facts: {fragment}" if fragment else "")
            )
            if entry["negative_prompt"].strip():
                negative_parts.append(entry["negative_prompt"].strip())
            if entry["continuity_rules"].strip():
                continuity_parts.append(f"{entry['name']}: {entry['continuity_rules'].strip()}")
            bible_public.append(
                {
                    "id": entry["id"],
                    "entry_type": entry["entry_type"],
                    "name": entry["name"],
                    "revision": entry["revision"],
                    "apply_globally": entry["apply_globally"],
                    "asset_ids": [asset["id"] for asset in entry["assets"]],
                    "source_type": entry.get("source_type", "manual"),
                    "source_id": entry.get("source_id"),
                    "source_revision": entry.get("source_revision"),
                }
            )

        sections: list[dict[str, str]] = []
        if reference_lines:
            sections.append({"id": "references", "label": "动态参考绑定", "content": "\n".join(reference_lines)})
        if bible_lines:
            sections.append({"id": "bible", "label": "锁定生产事实", "content": "\n".join(bible_lines)})
        sections.append({"id": "shot", "label": "镜头动作与运镜", "content": shot["prompt"].strip()})
        if shot.get("dialogue", "").strip():
            sections.append({"id": "dialogue", "label": "对白与声音", "content": f"The spoken Mandarin dialogue must be exactly: “{shot['dialogue'].strip()}”. Keep natural timing and synchronized ambience."})
        if shot.get("sound", "").strip():
            sections.append({"id": "sound", "label": "声音提示", "content": f"Sound direction: {shot['sound'].strip()}"})
        if continuity_parts:
            sections.append({"id": "continuity", "label": "跨镜连续性", "content": "\n".join(continuity_parts)})
        exclusions = [*negative_parts, "no cuts, no subtitles, no text overlays, no logos, no duplicated people, no extra limbs"]
        sections.append({"id": "safety", "label": "排除项", "content": "; ".join(exclusions)})
        compiled_prompt = "\n\n".join(section["content"] for section in sections if section["content"])
        adapter_seconds, frames, actual_seconds = _frame_plan(float(shot["seconds"]))
        if not 124 <= frames <= 362:
            warnings.append(f"{frames} 帧超出本机记录的 H3 约 124–362 帧训练区间，按实验规格处理")
        mode = "REF2VA" if references else "FL2VA"
        references_public = [
            {
                "id": item["id"], "asset_id": item["asset_id"], "asset_name": item["asset"].get("name"),
                "media_type": item["reference_type"], "role": item["role"], "tag": item["tag"],
                "audio_tag": item.get("audio_tag"), "source": item["source"], "source_label": item["source_label"],
                "checksum_sha256": item["asset"].get("checksum_sha256"),
            }
            for item in references
        ]
        source_snapshot = _source_snapshot(db, shot, bible_public, references_public)
        plan_hash = _json_hash(source_snapshot)
        stored = db.execute(
            """SELECT status, validated_at, approved_at FROM h3_prompt_plans
            WHERE shot_id = ? AND plan_hash = ? ORDER BY created_at DESC LIMIT 1""",
            (shot_id, plan_hash),
        ).fetchone()
        stale = db.execute(
            """SELECT plan_hash, stale_reasons, superseded_at FROM h3_prompt_plans
            WHERE shot_id = ? AND project_id = ? AND status = 'superseded'
              AND approved_at IS NOT NULL AND stale_reasons <> '[]'
            ORDER BY superseded_at DESC, approved_at DESC LIMIT 1""",
            (shot_id, project["id"]),
        ).fetchone()
        try:
            stale_reasons = json.loads(stale["stale_reasons"] or "[]") if stale else []
        except (TypeError, ValueError):
            stale_reasons = []
        return {
            "shot": {key: shot.get(key) for key in ("id", "ordinal", "scene_code", "title", "description", "dialogue", "sound", "prompt", "width", "height", "seconds", "candidate_count", "strategy")},
            "plan_hash": plan_hash,
            "ready": not blocking,
            "status": stored["status"] if stored else "preview",
            "validated_at": stored["validated_at"] if stored else None,
            "approved_at": stored["approved_at"] if stored else None,
            "stale": bool(stale_reasons) and (stored is None or stored["status"] != "approved"),
            "stale_reasons": stale_reasons,
            "stale_plan_hash": stale["plan_hash"] if stale else None,
            "superseded_at": stale["superseded_at"] if stale else None,
            "mode": mode,
            "sections": sections,
            "compiled_prompt": compiled_prompt,
            "references": references_public,
            "reference_counts": counts,
            "bible": bible_public,
            "storyboard_source": source_snapshot.get("storyboard_source"),
            "blocking": blocking,
            "warnings": list(dict.fromkeys(warnings)),
            "spec": {
                "resolution": f"{shot['width']}×{shot['height']}",
                "requested_seconds": shot["seconds"],
                "adapter_seconds": adapter_seconds,
                "frames": frames,
                "actual_seconds": actual_seconds,
                "candidate_count": shot["candidate_count"],
                "steps": 20,
                "fps": 24,
            },
            "source_snapshot": source_snapshot,
            "_references": references,
        }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("_") and key != "source_snapshot"}


def begin_validation_lease(db_path: Path, plan: dict[str, Any]) -> str:
    lease_id = f"h3-validation-{uuid.uuid4().hex[:12]}"
    shot_id = str(plan["shot"]["id"])
    project_id = str(plan["source_snapshot"]["shot"]["project_id"])
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        snapshot = _current_source_snapshot(db, shot_id, project_id)
        if snapshot is None:
            raise HTTPException(409, "镜头已删除，不能开始 H3 dry-run")
        if _json_hash(snapshot) != plan["plan_hash"]:
            raise HTTPException(409, "镜头、生产圣经或引用素材已变化，请刷新编译预览后重试")
        try:
            db.execute(
                """INSERT INTO h3_validation_leases (id, shot_id, project_id, plan_hash, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (lease_id, shot_id, project_id, plan["plan_hash"], utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该镜头已有进行中的 H3 dry-run") from exc
        db.commit()
    return lease_id


def end_validation_lease(db_path: Path, lease_id: str) -> None:
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM h3_validation_leases WHERE id = ?", (lease_id,))
        db.commit()


def current_plan_input(db: sqlite3.Connection, shot_id: str, project_id: str) -> dict[str, Any] | None:
    """Read the canonical H3 input while the caller holds its write transaction."""
    snapshot = _current_source_snapshot(db, shot_id, project_id)
    if snapshot is None:
        return None
    return {"plan_hash": _json_hash(snapshot), "source_snapshot": snapshot}


def _current_source_snapshot(db: sqlite3.Connection, shot_id: str, project_id: str) -> dict[str, Any] | None:
    shot_row = db.execute(
        "SELECT * FROM shots WHERE id = ? AND project_id = ?", (shot_id, project_id)
    ).fetchone()
    if not shot_row:
        return None
    shot = dict(shot_row)
    bible, _ = _effective_bible(db, shot_id, project_id)
    references = _merge_references(_explicit_references(db, shot_id), bible)
    bible_public = [
        {
            "id": entry["id"],
            "entry_type": entry["entry_type"],
            "name": entry["name"],
            "revision": entry["revision"],
            "apply_globally": entry["apply_globally"],
            "asset_ids": [asset["id"] for asset in entry["assets"]],
            "source_type": entry.get("source_type", "manual"),
            "source_id": entry.get("source_id"),
            "source_revision": entry.get("source_revision"),
        }
        for entry in bible
    ]
    references_public = [
        {
            "id": item["id"], "asset_id": item["asset_id"], "asset_name": item["asset"].get("name"),
            "media_type": item["reference_type"], "role": item["role"], "tag": item["tag"],
            "audio_tag": item.get("audio_tag"), "source": item["source"], "source_label": item["source_label"],
            "checksum_sha256": item["asset"].get("checksum_sha256"),
        }
        for item in references
    ]
    return _source_snapshot(db, shot, bible_public, references_public)


def record_validation(db_path: Path, plan: dict[str, Any], adapter_output: Any) -> bool:
    now = utc_now()
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        shot_id = str(plan["shot"]["id"])
        project_id = str(plan["source_snapshot"]["shot"]["project_id"])
        existing = db.execute(
            "SELECT id, status FROM h3_prompt_plans WHERE shot_id = ? AND project_id = ? AND plan_hash = ?",
            (shot_id, project_id, plan["plan_hash"]),
        ).fetchone()
        current_snapshot = _current_source_snapshot(db, shot_id, project_id)
        if current_snapshot is None:
            # Do not retain a late result in a table whose shot FK no longer exists.
            db.commit()
            return False
        current_hash = _json_hash(current_snapshot) if current_snapshot else None
        if existing and existing["status"] == "superseded":
            db.commit()
            return False
        if current_hash != plan["plan_hash"]:
            if existing:
                cursor = db.execute(
                    """UPDATE h3_prompt_plans SET status = 'superseded', stale_reasons = ?, superseded_at = ?
                    WHERE id = ? AND status IN ('validated', 'approved')""",
                    (json.dumps(["H3 dry-run 返回时输入已变化"], ensure_ascii=False), now, existing["id"]),
                )
                if cursor.rowcount == 0:
                    db.commit()
                    return False
            else:
                db.execute(
                    """INSERT INTO h3_prompt_plans
                    (id, shot_id, project_id, plan_hash, status, mode, creative_prompt, compiled_prompt,
                     source_snapshot, references_snapshot, bible_snapshot, warnings, adapter_output,
                     created_at, validated_at, stale_reasons, superseded_at)
                    VALUES (?, ?, ?, ?, 'superseded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"prompt-plan-{uuid.uuid4().hex[:12]}", shot_id, project_id, plan["plan_hash"],
                        plan["mode"], plan["shot"]["prompt"], plan["compiled_prompt"],
                        json.dumps(plan["source_snapshot"], ensure_ascii=False), json.dumps(plan["references"], ensure_ascii=False),
                        json.dumps(plan["bible"], ensure_ascii=False), json.dumps(plan["warnings"], ensure_ascii=False),
                        json.dumps(adapter_output, ensure_ascii=False), now, now,
                        json.dumps(["H3 dry-run 返回时输入已变化"], ensure_ascii=False), now,
                    ),
                )
            db.commit()
            return False
        if existing:
            cursor = db.execute(
                """UPDATE h3_prompt_plans SET adapter_output = ?, validated_at = ?
                WHERE id = ? AND status IN ('validated', 'approved')""",
                (json.dumps(adapter_output, ensure_ascii=False), now, existing["id"]),
            )
            if cursor.rowcount != 1:
                db.commit()
                return False
        else:
            db.execute(
                """INSERT INTO h3_prompt_plans
                (id, shot_id, project_id, plan_hash, status, mode, creative_prompt, compiled_prompt,
                 source_snapshot, references_snapshot, bible_snapshot, warnings, adapter_output,
                 created_at, validated_at)
                VALUES (?, ?, ?, ?, 'validated', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"prompt-plan-{uuid.uuid4().hex[:12]}", shot_id, project_id,
                    plan["plan_hash"], plan["mode"], plan["shot"]["prompt"], plan["compiled_prompt"],
                    json.dumps(plan["source_snapshot"], ensure_ascii=False), json.dumps(plan["references"], ensure_ascii=False),
                    json.dumps(plan["bible"], ensure_ascii=False), json.dumps(plan["warnings"], ensure_ascii=False),
                    json.dumps(adapter_output, ensure_ascii=False), now, now,
                ),
            )
        db.commit()
        return True


def approve_plan(db_path: Path, shot_id: str, plan_hash: str) -> None:
    now = utc_now()
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        project = _active_project(db)
        plan = db.execute(
            """SELECT * FROM h3_prompt_plans WHERE shot_id = ? AND project_id = ? AND plan_hash = ?
            AND status IN ('validated', 'approved')""",
            (shot_id, project["id"], plan_hash),
        ).fetchone()
        if not plan:
            raise HTTPException(409, "当前编译计划尚未通过 H3 dry-run，不能批准")
        previous = db.execute(
            "SELECT id FROM h3_prompt_plans WHERE shot_id = ? AND status = 'approved' AND id <> ?",
            (shot_id, plan["id"]),
        ).fetchall()
        for item in previous:
            db.execute(
                """UPDATE h3_prompt_plans SET status = 'superseded', stale_reasons = ?, superseded_at = ?
                WHERE id = ?""",
                (json.dumps(["已批准新的 H3 计划"], ensure_ascii=False), now, item["id"]),
            )
        db.execute(
            """UPDATE h3_prompt_plans SET status = 'approved', approved_at = ?, stale_reasons = '[]',
            superseded_at = NULL WHERE id = ?""",
            (now, plan["id"]),
        )
        db.commit()


def project_prompt_status(db_path: Path) -> list[dict[str, Any]]:
    with closing(connect(db_path)) as db:
        project = _active_project(db)
        shot_ids = [item["id"] for item in db.execute("SELECT id FROM shots WHERE project_id = ? ORDER BY ordinal", (project["id"],)).fetchall()]
    return [public_plan(compile_prompt_plan(db_path, shot_id)) for shot_id in shot_ids]
