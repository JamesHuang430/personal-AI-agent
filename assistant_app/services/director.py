from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import desc, select

from assistant_app.core.config import Settings
from assistant_app.db.models import (
    DirectorAgentRun,
    DirectorProject,
    DirectorShot,
    SpeechJob,
    VideoJob,
)
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.agent_model_router import (
    AGENT_MODEL_PROFILES,
    route_agent_models,
)
from assistant_app.services.generated_files import GENERATED_ROOT
from assistant_app.services.model_gateway import agent_text_completion, list_available_models
from assistant_app.services.speech_gateway import (
    SPEECH_EMOTIONS,
    SPEECH_VOICE_ROLES,
    create_speech_job,
    run_speech_job,
)
from assistant_app.services.video_gateway import create_video_job, run_video_job, video_job_payload


class DirectorProjectNotFoundError(LookupError):
    pass


AGENT_BRIEFS = {
    "story": "把创意收敛为受众、主题、人物、节拍、可表演对白和完整剧本",
    "visual": "建立连续性资产并输出可直接驱动视频、配音和字幕的逐镜方案",
    "media": "调用视频和语音渠道，完成混音、字幕烧录与合片",
    "quality": "检查真实媒体文件的画面、音轨、字幕、时长和可交付性",
}

DIRECTOR_RESOLUTIONS = {"768P", "2K"}

SHOT_BEATS = (
    "建立独特环境、时间与空间方向",
    "主角第一次出场并展示固定外形",
    "用标志性道具强化主角身份",
    "展示主角原本的目标与日常行动",
    "环境中出现第一处异常征兆",
    "主角发现关键人物或关键物件",
    "突发事件打断原有行动",
    "主角犹豫并显露内在弱点",
    "主角接受任务并明确短期目标",
    "角色离开安全区进入新空间",
    "第一个实体障碍迫使角色行动",
    "主配角通过合作跨过小障碍",
    "一次细节互动揭示人物关系",
    "局势短暂好转并制造错误希望",
    "重大挫折改变路径或目标",
    "新线索揭示此前未知的真相",
    "角色面对两难选择与时间压力",
    "主角克服弱点并作出不可逆决定",
    "角色为最终行动进行具体准备",
    "逼近高潮地点并持续增加压迫感",
    "主角与核心障碍正面交锋",
    "角色付出代价保护重要的人或目标",
    "关键反转让行动获得成功机会",
    "冲突解决并清楚展示结果",
    "用新的日常或标志物完成情绪回收",
)


def _project_title(premise: str) -> str:
    compact = " ".join(premise.split())
    return (compact[:28] + "…") if len(compact) > 28 else (compact or "未命名短剧")


def _director_video_size(aspect_ratio: str, resolution: str | None) -> str:
    if resolution == "2K":
        return "1024x1792" if aspect_ratio == "9:16" else "1792x1024"
    return "720x1280" if aspect_ratio == "9:16" else "1280x720"


def _split_agent_output(content: str) -> tuple[str, str]:
    normalized = content.strip()
    for marker in ("【交付物】", "## 交付物", "交付物："):
        if marker in normalized:
            summary, deliverable = normalized.split(marker, 1)
            summary = summary.replace("【判断摘要】", "").replace("## 判断摘要", "").strip()
            return summary[:1200], deliverable.strip()[:64_000]
    paragraphs = [item.strip() for item in normalized.split("\n\n") if item.strip()]
    summary = paragraphs[0] if paragraphs else normalized
    return summary[:1200], normalized[:64_000]


def _extract_tagged_json(content: str, marker: str) -> dict[str, object]:
    if marker not in content:
        raise ValueError(f"Agent 交付物缺少 {marker}")
    tail = content.split(marker, 1)[1].strip().replace("```json", "").replace("```", "")
    start = tail.find("{")
    if start < 0:
        raise ValueError(f"{marker} 后没有 JSON 对象")
    try:
        parsed, _ = json.JSONDecoder().raw_decode(tail[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{marker} JSON 无法解析：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{marker} 必须是 JSON 对象")
    return parsed


def _validate_story_data(data: dict[str, object]) -> dict[str, object]:
    required_text = ("logline", "audience", "theme", "script")
    if any(not str(data.get(key) or "").strip() for key in required_text):
        raise ValueError("故事 JSON 缺少 logline、audience、theme 或 script")
    if not isinstance(data.get("characters"), list) or not data["characters"]:
        raise ValueError("故事 JSON 必须包含至少一个角色")
    if not isinstance(data.get("beats"), list) or not data["beats"]:
        raise ValueError("故事 JSON 必须包含剧情节拍")
    return data


def _fit_speech_text(value: object, seconds: str) -> str:
    cleaned = re.sub(r"\s+", "", str(value or "").strip())
    limit = max(8, int(seconds) * 4)
    if len(cleaned) <= limit:
        return cleaned
    candidate = cleaned[:limit]
    cut = max(candidate.rfind(mark) for mark in "。！？；，")
    return candidate[: cut + 1] if cut >= limit // 2 else candidate


def _character_for_spec(
    spec: dict[str, object], continuity: dict[str, object]
) -> dict[str, object] | None:
    speaker = str(spec.get("speaker") or "").strip()
    characters = continuity.get("characters")
    if not isinstance(characters, list):
        return None
    for character in characters:
        if isinstance(character, dict) and str(character.get("name") or "").strip() == speaker:
            return character
    return None


def _voice_id_for_spec(spec: dict[str, object], continuity: dict[str, object]) -> str | None:
    explicit = str(spec.get("voice_id") or "").strip()
    if explicit:
        return explicit[:200]
    character = _character_for_spec(spec, continuity)
    if character is not None:
        voice_id = str(character.get("voice_id") or "").strip()
        if voice_id:
            return voice_id[:200]
    return None


def _infer_voice_role(description: str, speaker: str = "") -> str:
    combined = f"{speaker} {description}".casefold()
    if speaker in {"旁白", "画外音", "解说"} or any(
        token in combined for token in ("旁白", "解说", "播音", "narrator")
    ):
        return "narrator"
    is_female = any(
        token in combined for token in ("女", "女性", "女孩", "奶奶", "婆婆", "母亲", "妈妈")
    )
    is_elder = any(
        token in combined for token in ("老人", "老年", "爷爷", "奶奶", "外公", "外婆", "elder")
    )
    is_child = any(
        token in combined for token in ("儿童", "小孩", "孩子", "男孩", "女孩", "少年", "少女")
    )
    if is_elder:
        return "elder_female" if is_female else "elder_male"
    if is_child:
        return "girl" if is_female else "boy"
    return "adult_female" if is_female else "adult_male"


def _voice_role_for_spec(spec: dict[str, object], continuity: dict[str, object]) -> str:
    explicit = str(spec.get("voice_role") or "").strip()
    if explicit in SPEECH_VOICE_ROLES:
        return explicit
    speaker = str(spec.get("speaker") or "旁白").strip()
    character = _character_for_spec(spec, continuity)
    if character is None:
        return _infer_voice_role("", speaker)
    role = str(character.get("voice_role") or "").strip()
    if role in SPEECH_VOICE_ROLES:
        return role
    description = " ".join(
        str(character.get(key) or "") for key in ("role", "voice_profile", "appearance")
    )
    return _infer_voice_role(description, speaker)


def _locked_voice_ids(notes: str | None) -> set[str]:
    return {
        match.strip()
        for match in re.findall(r"voice_id\s*[:=：]\s*([^;；,，\n]+)", notes or "", re.I)
        if match.strip()
    }


def _sanitize_character_voices(
    characters: list[object], continuity_notes: str | None
) -> None:
    locked_ids = _locked_voice_ids(continuity_notes)
    for item in characters:
        if not isinstance(item, dict):
            continue
        voice_id = str(item.get("voice_id") or "").strip()
        if voice_id not in locked_ids:
            item.pop("voice_id", None)
        role = str(item.get("voice_role") or "").strip()
        if role not in SPEECH_VOICE_ROLES:
            item["voice_role"] = _infer_voice_role(
                " ".join(
                    str(item.get(key) or "")
                    for key in ("role", "voice_profile", "appearance")
                ),
                str(item.get("name") or ""),
            )


def _default_continuity_bible(project: DirectorProject) -> dict[str, object]:
    return {
        "version": 1,
        "lock_mode": "text",
        "characters": [],
        "relationships": [],
        "visual_rules": [
            project.visual_style,
            f"固定画幅 {project.aspect_ratio}",
            f"目标清晰度 {project.resolution or '768P'}",
        ],
        "continuity_notes": project.continuity_notes or "",
        "reference_capability": (
            "已登记定妆照时可供兼容主体参考的模型使用；当前 H3 文生视频仅执行文字连续性约束"
        ),
    }


def _extract_continuity_bible(content: str, project: DirectorProject) -> dict[str, object]:
    marker = "【连续性JSON】"
    fallback = _default_continuity_bible(project)
    if marker not in content:
        return fallback
    tail = content.split(marker, 1)[1].strip().replace("```json", "").replace("```", "")
    start = tail.find("{")
    if start < 0:
        return fallback
    try:
        parsed, _ = json.JSONDecoder().raw_decode(tail[start:])
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    parsed.setdefault("version", 1)
    parsed.setdefault("lock_mode", "text")
    parsed.setdefault("characters", [])
    parsed.setdefault("relationships", [])
    parsed.setdefault("visual_rules", [project.visual_style])
    parsed["continuity_notes"] = project.continuity_notes or parsed.get("continuity_notes", "")
    parsed["reference_capability"] = fallback["reference_capability"]
    return parsed


def _continuity_prompt(project: DirectorProject) -> str:
    bible = project.continuity_bible or _default_continuity_bible(project)
    return json.dumps(bible, ensure_ascii=False, separators=(",", ":"))[:6000]


def _shot_durations(target_seconds: int) -> list[str]:
    remaining = target_seconds
    result: list[str] = []
    while remaining > 0:
        if remaining > 8:
            duration = 12
        elif remaining > 4:
            duration = 8
        else:
            duration = 4
        result.append(str(duration))
        remaining -= duration
    return result


def _shot_phase(sequence: int, total: int) -> str:
    position = sequence / max(total, 1)
    if position <= 0.2:
        return "开场建立人物、环境与钩子"
    if position <= 0.55:
        return "推动行动升级并揭示人物关系"
    if position <= 0.8:
        return "进入转折与冲突高潮"
    return "完成高潮、情绪落点与结尾回收"


def _fallback_shot_spec(sequence: int, total: int, premise: str) -> dict[str, object]:
    beat_index = ((sequence - 1) * (len(SHOT_BEATS) - 1)) // max(total - 1, 1)
    beat = SHOT_BEATS[beat_index]
    phase = _shot_phase(sequence, total)
    return {
        "sequence": sequence,
        "title": f"第 {sequence} 镜 · {beat}",
        "story_beat": beat,
        "instruction": (
            f"全片故事背景：{premise}\n"
            f"当前剧情阶段：{phase}\n"
            f"本镜唯一微节拍：{beat}\n"
            "本镜必须用一个可见的新动作和一个明确的新构图推进剧情，"
            "不得重复上一镜的站位、动作、景别和机位。"
        ),
        "speaker": "旁白",
        "speech_text": f"{beat}。",
        "subtitle_text": f"{beat}。",
        "voice_id": None,
        "voice_role": "narrator",
        "emotion": "calm",
        "speech_speed": 1.0,
    }


def _shot_spec_instruction(raw: dict[str, object]) -> str:
    fields = (
        ("叙事任务", "story_beat"),
        ("出镜人物", "characters"),
        ("地点", "location"),
        ("核心动作", "action"),
        ("景别", "shot_size"),
        ("机位与运镜", "camera"),
        ("光线与色彩", "lighting"),
        ("转场衔接", "transition"),
        ("正向提示词", "positive_prompt"),
        ("负向提示词", "negative_prompt"),
    )
    lines: list[str] = []
    explicit = str(raw.get("instruction") or "").strip()
    if explicit:
        lines.append(explicit)
    for label, key in fields:
        value = raw.get(key)
        if isinstance(value, list):
            value = "、".join(str(item) for item in value if str(item).strip())
        text_value = str(value or "").strip()
        if text_value:
            lines.append(f"{label}：{text_value}")
    return "\n".join(lines).strip()


def _normalize_shot_spec(
    raw: dict[str, object],
    sequence: int,
    total: int,
    premise: str,
) -> dict[str, object]:
    fallback = _fallback_shot_spec(sequence, total, premise)
    title = str(raw.get("title") or fallback["title"]).strip()
    instruction = _shot_spec_instruction(raw)
    if not instruction:
        return fallback
    speech_text = str(raw.get("speech_text") or raw.get("dialogue") or "").strip()
    if not speech_text:
        speech_text = str(fallback["speech_text"])
    subtitle_text = str(raw.get("subtitle_text") or speech_text).strip()
    try:
        speech_speed = max(0.5, min(float(raw.get("speech_speed") or 1.0), 2.0))
    except (TypeError, ValueError):
        speech_speed = 1.0
    voice_role = str(raw.get("voice_role") or "").strip()
    emotion = str(raw.get("emotion") or "calm").strip()
    return {
        "sequence": sequence,
        "title": title[:200],
        "story_beat": str(raw.get("story_beat") or title).strip(),
        "instruction": instruction[:6_000],
        "speaker": str(raw.get("speaker") or "旁白").strip()[:100],
        "speech_text": speech_text,
        "subtitle_text": subtitle_text,
        "voice_id": str(raw.get("voice_id") or "").strip()[:200] or None,
        "voice_role": voice_role if voice_role in SPEECH_VOICE_ROLES else None,
        "emotion": emotion if emotion in SPEECH_EMOTIONS else "calm",
        "speech_speed": speech_speed,
    }


def _markdown_shot_specs(content: str) -> dict[int, dict[str, object]]:
    pattern = re.compile(
        r"(?ms)^#{1,6}\s*镜头\s*0*(\d+)\s*[：:]?\s*(.*?)"
        r"(?=^#{1,6}\s*镜头\s*0*\d+|\Z)"
    )
    specs: dict[int, dict[str, object]] = {}
    for match in pattern.finditer(content):
        sequence = int(match.group(1))
        block = match.group(2).strip()
        heading, _, body = block.partition("\n")
        title = re.sub(r"^\d{1,3}\s*[-–—]\s*\d{1,3}s?\s*", "", heading).strip()
        specs[sequence] = {
            "sequence": sequence,
            "title": title or f"第 {sequence} 镜",
            "story_beat": title,
            "instruction": body.strip() or heading,
        }
    return specs


def _markdown_table_shot_specs(content: str) -> dict[int, dict[str, object]]:
    specs: dict[int, dict[str, object]] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 6:
            continue
        sequence_match = re.fullmatch(r"0*(\d+)", cells[0])
        if sequence_match is None:
            continue
        sequence = int(sequence_match.group(1))
        visual_content = cells[5]
        title_match = re.search(r"\*\*(.+?)\*\*", visual_content)
        title = title_match.group(1).strip() if title_match else f"第 {sequence} 镜"
        sound = cells[6] if len(cells) > 6 else ""
        transition = cells[7] if len(cells) > 7 else ""
        specs[sequence] = {
            "sequence": sequence,
            "title": title,
            "story_beat": re.sub(r"\*+", "", visual_content),
            "instruction": (
                f"原分镜时间轴：{cells[1]}\n"
                f"景别：{cells[2]}\n"
                f"机位/视角：{cells[3]}\n"
                f"摄像机运动：{cells[4]}\n"
                f"画面内容与核心动作：{visual_content}\n"
                f"声音设计：{sound}\n"
                f"转场方式：{transition}"
            ),
        }
    return specs


def _extract_storyboard_plan(content: str, total: int, premise: str) -> list[dict[str, object]]:
    raw_specs: dict[int, dict[str, object]] = {}
    marker = "【分镜JSON】"
    if marker in content:
        tail = content.split(marker, 1)[1].strip().replace("```json", "").replace("```", "")
        start = tail.find("[")
        if start >= 0:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(tail[start:])
            except (json.JSONDecodeError, TypeError):
                parsed = []
            if isinstance(parsed, list):
                for index, item in enumerate(parsed, start=1):
                    if not isinstance(item, dict):
                        continue
                    try:
                        sequence = int(item.get("sequence") or index)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= sequence <= total:
                        raw_specs[sequence] = item
    for sequence, item in _markdown_shot_specs(content).items():
        raw_specs.setdefault(sequence, item)
    for sequence, item in _markdown_table_shot_specs(content).items():
        raw_specs.setdefault(sequence, item)

    plan: list[dict[str, object]] = []
    seen: set[str] = set()
    for sequence in range(1, total + 1):
        spec = _normalize_shot_spec(
            raw_specs.get(sequence, {}),
            sequence,
            total,
            premise,
        )
        uniqueness_key = re.sub(r"\s+", "", str(spec["instruction"])).casefold()
        if uniqueness_key in seen:
            fallback = _fallback_shot_spec(sequence, total, premise)
            fallback["instruction"] = (
                f"{fallback['instruction']}\n原分镜补充：{spec['instruction']}"
            )[:6_000]
            spec = fallback
            uniqueness_key = re.sub(r"\s+", "", str(spec["instruction"])).casefold()
        seen.add(uniqueness_key)
        plan.append(spec)
    return plan


def _validate_visual_data(
    data: dict[str, object],
    project: DirectorProject,
    durations: list[str],
) -> dict[str, object]:
    continuity = data.get("continuity")
    if not isinstance(continuity, dict):
        raise ValueError("视觉 JSON 缺少 continuity 对象")
    characters = continuity.get("characters")
    if not isinstance(characters, list) or not characters:
        raise ValueError("连续性圣经必须包含至少一个角色")
    _sanitize_character_voices(characters, project.continuity_notes)
    raw_shots = data.get("shots")
    if not isinstance(raw_shots, list) or len(raw_shots) != len(durations):
        raise ValueError(f"视觉 JSON 必须包含正好 {len(durations)} 个镜头")

    normalized: list[dict[str, object]] = []
    for index, (raw, seconds) in enumerate(zip(raw_shots, durations, strict=True), start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 镜不是 JSON 对象")
        if not str(raw.get("speech_text") or "").strip():
            raise ValueError(f"第 {index} 镜缺少 speech_text，无法生成对白和字幕")
        spec = _normalize_shot_spec(raw, index, len(durations), project.premise)
        spoken = _fit_speech_text(spec["speech_text"], seconds)
        if not spoken:
            raise ValueError(f"第 {index} 镜没有可配音文本")
        spec["speech_text"] = spoken
        # 字幕必须与实际送入 TTS 的文本一致，避免声画内容不一致。
        spec["subtitle_text"] = spoken
        normalized.append(spec)

    continuity.setdefault("version", 1)
    continuity.setdefault("lock_mode", "text")
    continuity.setdefault("relationships", [])
    continuity.setdefault("visual_rules", [project.visual_style])
    continuity["continuity_notes"] = project.continuity_notes or continuity.get(
        "continuity_notes", ""
    )
    continuity["reference_capability"] = _default_continuity_bible(project)[
        "reference_capability"
    ]
    return {"continuity": continuity, "shots": normalized}


def _shot_prompt(
    project: DirectorProject,
    sequence: int,
    total: int,
    seconds: str,
    spec: dict[str, object],
) -> str:
    continuity = _continuity_prompt(project)
    speaker = str(spec.get("speaker") or "旁白").strip()
    speech_text = str(spec.get("speech_text") or "").strip()
    performance = (
        f"画面中的{speaker}正在说台词“{speech_text}”，必须有自然、连续、与说话节奏一致的"
        "口部动作和表情变化；视频模型不要生成字幕或画面文字。"
        if speaker not in {"旁白", "画外音", "解说"}
        else f"本镜使用画外旁白“{speech_text}”，画面人物不要做无意义的说话口型。"
    )
    return (
        f"{project.visual_style}，{project.aspect_ratio} AI 短剧，第 {sequence}/{total} 镜，"
        f"时长 {seconds} 秒。\n"
        f"【本镜唯一分镜方案】\n{spec['instruction']}\n"
        f"【对白表演要求】{performance}\n"
        "只表现本镜的叙事任务、地点、动作和机位；不得复用其他镜头的构图或动作。\n"
        f"【全片故事背景】{project.premise}\n"
        f"【跨镜连续性圣经】{continuity}\n"
        "人物脸型、五官、发型、年龄感、服装配色、标志物、声线及人物关系必须固定；"
        "不得擅自换装、换脸或新增人物。动作自然，镜头衔接清楚，无字幕、无文字、无水印。"
    )[:8_000]


def agent_run_payload(run: DirectorAgentRun) -> dict[str, object]:
    return {
        "id": str(run.id),
        "agent": run.agent_key,
        "agent_name": run.agent_name,
        "sequence": run.sequence,
        "model": run.model_name,
        "status": run.status,
        "decision_summary": run.decision_summary,
        "deliverable": run.deliverable,
        "result_data": run.result_data or {},
        "error_message": run.error_message if run.status == "failed" else None,
    }


def shot_payload(shot: DirectorShot, job: VideoJob | None = None) -> dict[str, object]:
    return {
        "id": str(shot.id),
        "sequence": shot.sequence,
        "title": shot.title,
        "prompt": shot.prompt,
        "seconds": shot.seconds,
        "status": shot.status,
        "continuity_snapshot": shot.continuity_snapshot,
        "speaker": shot.speaker,
        "speech_text": shot.speech_text,
        "subtitle_text": shot.subtitle_text,
        "speech_job_id": str(shot.speech_job_id) if shot.speech_job_id else None,
        "has_burned_subtitles": bool(shot.rendered_path and shot.subtitle_text),
        "error_message": shot.error_message if shot.status == "failed" else None,
        "video": video_job_payload(job) if job else None,
    }


async def project_payload(
    runtime: RuntimeDependencies,
    project: DirectorProject,
) -> dict[str, object]:
    async with runtime.sessions() as session:
        runs = (
            await session.scalars(
                select(DirectorAgentRun)
                .where(DirectorAgentRun.project_id == project.id)
                .order_by(DirectorAgentRun.sequence)
            )
        ).all()
        preview = (
            await session.get(VideoJob, project.preview_video_job_id)
            if project.preview_video_job_id
            else None
        )
        shots = (
            await session.scalars(
                select(DirectorShot)
                .where(DirectorShot.project_id == project.id)
                .order_by(DirectorShot.sequence)
            )
        ).all()
        shot_jobs = {
            job.id: job
            for job in (
                await session.scalars(
                    select(VideoJob).where(
                        VideoJob.id.in_([shot.video_job_id for shot in shots if shot.video_job_id])
                    )
                )
            ).all()
        }
    return {
        "id": str(project.id),
        "title": project.title,
        "premise": project.premise,
        "target_seconds": project.target_seconds,
        "aspect_ratio": project.aspect_ratio,
        "resolution": project.resolution or "768P",
        "visual_style": project.visual_style,
        "continuity_notes": project.continuity_notes,
        "continuity_bible": project.continuity_bible or {},
        "one_click": project.one_click,
        "planned_shots": project.planned_shots,
        "completed_shots": project.completed_shots,
        "status": project.status,
        "current_stage": project.current_stage,
        "progress": project.progress,
        "final_summary": project.final_summary,
        "quality_report": project.quality_report or {},
        "error_message": project.error_message if project.status == "failed" else None,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "agents": [agent_run_payload(run) for run in runs],
        "preview_video": video_job_payload(preview) if preview else None,
        "shots": [shot_payload(shot, shot_jobs.get(shot.video_job_id)) for shot in shots],
        "final_video": (
            {
                "preview_url": f"/api/v1/director/projects/{project.id}/preview",
                "download_url": f"/api/v1/director/projects/{project.id}/download",
            }
            if project.final_video_path
            else None
        ),
    }


async def create_director_project(
    runtime: RuntimeDependencies,
    settings: Settings,
    user_id: UUID,
    premise: str,
    target_seconds: int = 60,
    aspect_ratio: str = "9:16",
    resolution: str = "768P",
    visual_style: str = "电影感写实",
    continuity_notes: str = "",
    one_click: bool = False,
) -> DirectorProject:
    _channel_name, models = await list_available_models(runtime, settings)
    assignments = {item["agent"]: item for item in route_agent_models(models)}
    unavailable = [
        profile.name for profile in AGENT_MODEL_PROFILES if not assignments[profile.key]["model"]
    ]
    if unavailable:
        raise ValueError(f"以下 Agent 暂无可用模型：{'、'.join(unavailable)}")

    project = DirectorProject(
        id=uuid4(),
        user_id=user_id,
        title=_project_title(premise),
        premise=premise[:8_000],
        target_seconds=max(30, min(target_seconds, 300)),
        aspect_ratio=aspect_ratio if aspect_ratio in {"9:16", "16:9"} else "9:16",
        resolution=resolution if resolution in DIRECTOR_RESOLUTIONS else "768P",
        visual_style=visual_style[:100],
        continuity_notes=continuity_notes[:8_000] or None,
        continuity_bible={},
        one_click=one_click,
        planned_shots=len(_shot_durations(target_seconds)) if one_click else 1,
        status="queued",
        progress=0,
    )
    runs = [
        DirectorAgentRun(
            id=uuid4(),
            project_id=project.id,
            user_id=user_id,
            agent_key=profile.key,
            agent_name=profile.name,
            sequence=index,
            model_name=str(assignments[profile.key]["model"]),
            status="pending",
        )
        for index, profile in enumerate(AGENT_MODEL_PROFILES)
    ]
    async with runtime.sessions() as session, session.begin():
        session.add(project)
        # The agent rows reference the project by its UUID, but no ORM
        # relationship connects the independently constructed objects. Flush
        # the parent explicitly so SQLAlchemy cannot batch the child inserts
        # before the project insert on PostgreSQL.
        await session.flush()
        session.add_all(runs)
    return project


async def get_director_project(
    runtime: RuntimeDependencies,
    user_id: UUID,
    project_id: UUID,
) -> DirectorProject:
    async with runtime.sessions() as session:
        project = await session.scalar(
            select(DirectorProject).where(
                DirectorProject.id == project_id,
                DirectorProject.user_id == user_id,
            )
        )
    if project is None:
        raise DirectorProjectNotFoundError("导演项目不存在或无权访问")
    return project


async def list_director_projects(
    runtime: RuntimeDependencies,
    user_id: UUID,
) -> list[DirectorProject]:
    async with runtime.sessions() as session:
        return list(
            (
                await session.scalars(
                    select(DirectorProject)
                    .where(DirectorProject.user_id == user_id)
                    .order_by(desc(DirectorProject.created_at))
                    .limit(20)
                )
            ).all()
        )


async def _update_project(
    runtime: RuntimeDependencies,
    project_id: UUID,
    **values: object,
) -> None:
    async with runtime.sessions() as session, session.begin():
        project = await session.get(DirectorProject, project_id, with_for_update=True)
        if project is None:
            return
        for key, value in values.items():
            setattr(project, key, value)
        project.updated_at = datetime.now(UTC)


async def _update_run(
    runtime: RuntimeDependencies,
    run_id: UUID,
    **values: object,
) -> None:
    async with runtime.sessions() as session, session.begin():
        run = await session.get(DirectorAgentRun, run_id, with_for_update=True)
        if run is None:
            return
        for key, value in values.items():
            setattr(run, key, value)
        run.updated_at = datetime.now(UTC)


async def _completed_context(runtime: RuntimeDependencies, project_id: UUID) -> str:
    async with runtime.sessions() as session:
        project = await session.get(DirectorProject, project_id)
        rows = (
            await session.scalars(
                select(DirectorAgentRun)
                .where(
                    DirectorAgentRun.project_id == project_id,
                    DirectorAgentRun.status == "completed",
                )
                .order_by(DirectorAgentRun.sequence)
            )
        ).all()
        shots = (
            await session.scalars(
                select(DirectorShot)
                .where(DirectorShot.project_id == project_id)
                .order_by(DirectorShot.sequence)
            )
        ).all()
    completed_parts: list[str] = []
    for row in rows:
        if row.result_data:
            payload = json.dumps(row.result_data, ensure_ascii=False, separators=(",", ":"))
            body = payload[:10_000]
        else:
            body = (row.deliverable or "")[:1800]
        completed_parts.append(f"### {row.agent_name}\n{body}")
    completed = "\n\n".join(completed_parts)[-16_000:]
    continuity = _continuity_prompt(project) if project and project.continuity_bible else ""
    if continuity:
        shot_context = "\n".join(
            f"镜头 {shot.sequence}：{shot.title} · {shot.seconds}s · {shot.status}"
            for shot in shots
        )
        final_context = (
            f"\n最终合片：{'已生成' if project and project.final_video_path else '尚未生成'}"
            if shots
            else ""
        )
        return (
            f"### 已锁定连续性圣经（所有下游 Agent 必须遵守）\n{continuity}"
            f"\n\n{completed}\n\n### 已生成镜头\n{shot_context or '尚未生成'}{final_context}"
        )[-20_000:]
    return completed


async def _update_shot(
    runtime: RuntimeDependencies,
    shot_id: UUID,
    **values: object,
) -> None:
    async with runtime.sessions() as session, session.begin():
        shot = await session.get(DirectorShot, shot_id, with_for_update=True)
        if shot is None:
            return
        for key, value in values.items():
            setattr(shot, key, value)
        shot.updated_at = datetime.now(UTC)


async def _load_storyboard_plan(
    runtime: RuntimeDependencies,
    project: DirectorProject,
    total: int,
) -> list[dict[str, object]]:
    async with runtime.sessions() as session:
        result_data = await session.scalar(
            select(DirectorAgentRun.result_data).where(
                DirectorAgentRun.project_id == project.id,
                DirectorAgentRun.agent_key == "visual",
                DirectorAgentRun.status == "completed",
            )
        )
    if isinstance(result_data, dict) and isinstance(result_data.get("shots"), list):
        shots = result_data["shots"]
        if len(shots) == total:
            return [dict(item) for item in shots if isinstance(item, dict)]
    raise RuntimeError("视觉 Agent 没有提供可执行的结构化分镜")


async def _run_media_command(*command: str) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    decoded_out = stdout.decode(errors="ignore")
    decoded_err = stderr.decode(errors="ignore")
    if process.returncode != 0:
        raise RuntimeError(f"媒体处理失败：{decoded_err[-500:]}")
    return decoded_out, decoded_err


async def _probe_media(path: str | Path) -> dict[str, object]:
    stdout, _ = await _run_media_command(
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        os.fspath(path),
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe 没有返回有效 JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def _subtitle_filter_path(path: Path) -> str:
    return path.as_posix().replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


async def _render_dialogue_shot(
    shot: DirectorShot,
    video_job: VideoJob,
    speech_job: SpeechJob,
) -> str:
    video_available = video_job.storage_path and await asyncio.to_thread(
        Path(video_job.storage_path).is_file
    )
    if not video_available:
        raise RuntimeError("视频渠道没有留下可合成的文件")
    speech_available = speech_job.storage_path and await asyncio.to_thread(
        Path(speech_job.storage_path).is_file
    )
    if not speech_available:
        raise RuntimeError("语音渠道没有留下可合成的文件")

    await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
    subtitle_path = GENERATED_ROOT / f"director-shot-{shot.id}.srt"
    rendered_path = GENERATED_ROOT / f"director-shot-{shot.id}.mp4"
    duration = float(shot.seconds)
    subtitle = re.sub(r"[\r\n]+", " ", shot.subtitle_text or shot.speech_text or "").strip()
    srt = f"1\n00:00:00,000 --> {_srt_timestamp(max(0.5, duration - 0.05))}\n{subtitle}\n"
    await asyncio.to_thread(subtitle_path.write_text, srt, encoding="utf-8")

    source_info = await _probe_media(video_job.storage_path)
    streams = source_info.get("streams", [])
    has_native_audio = isinstance(streams, list) and any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )
    subtitle_filter = (
        f"subtitles=filename='{_subtitle_filter_path(subtitle_path)}':"
        "force_style='FontName=Noto Sans CJK SC,FontSize=18,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=72'"
    )
    voice_chain = f"[1:a]aresample=48000,apad,atrim=0:{duration:.3f},volume=1.35[voice]"
    if has_native_audio:
        audio_filter = (
            f"[0:a]aresample=48000,volume=0.20[bed];{voice_chain};"
            f"[bed][voice]amix=inputs=2:duration=longest:dropout_transition=1,"
            f"apad,atrim=0:{duration:.3f}[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_filter = voice_chain
        audio_map = "[voice]"

    await _run_media_command(
        "ffmpeg",
        "-y",
        "-i",
        video_job.storage_path,
        "-i",
        speech_job.storage_path,
        "-vf",
        subtitle_filter,
        "-filter_complex",
        audio_filter,
        "-map",
        "0:v:0",
        "-map",
        audio_map,
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        os.fspath(rendered_path),
    )
    return os.fspath(rendered_path)


async def _create_and_run_shot(
    runtime: RuntimeDependencies,
    settings: Settings,
    project: DirectorProject,
    sequence: int,
    total: int,
    seconds: str,
    spec: dict[str, object],
) -> tuple[DirectorShot, VideoJob]:
    continuity = project.continuity_bible or _default_continuity_bible(project)
    prompt = _shot_prompt(project, sequence, total, seconds, spec)
    shot = DirectorShot(
        id=uuid4(),
        project_id=project.id,
        user_id=project.user_id,
        sequence=sequence,
        title=str(spec["title"])[:200],
        prompt=prompt,
        seconds=seconds,
        status="processing",
        continuity_snapshot=continuity,
        speaker=str(spec.get("speaker") or "旁白")[:100],
        speech_text=_fit_speech_text(spec.get("speech_text"), seconds),
        subtitle_text=_fit_speech_text(spec.get("speech_text"), seconds),
    )
    async with runtime.sessions() as session, session.begin():
        session.add(shot)
    size = _director_video_size(project.aspect_ratio, project.resolution)
    job = await create_video_job(
        runtime,
        project.user_id,
        shot.prompt,
        seconds,
        size,
        project.resolution,
    )
    await _update_shot(runtime, shot.id, video_job_id=job.id)
    await run_video_job(runtime, settings, job.id)
    async with runtime.sessions() as session:
        completed_job = await session.get(VideoJob, job.id)
    if completed_job is None or completed_job.status != "completed":
        message = completed_job.error_message if completed_job else "视频任务不存在"
        await _update_shot(runtime, shot.id, status="failed", error_message=message)
        raise RuntimeError(message or "镜头生成失败")

    locked_voice_id = _voice_id_for_spec(spec, continuity)
    speech_job = await create_speech_job(
        runtime,
        project.user_id,
        shot.speech_text or "",
        locked_voice_id,
        float(spec.get("speech_speed") or 1.0),
        speaker=shot.speaker,
        voice_role=None if locked_voice_id else _voice_role_for_spec(spec, continuity),
        emotion=str(spec.get("emotion") or "calm"),
    )
    await _update_shot(runtime, shot.id, speech_job_id=speech_job.id)
    await run_speech_job(runtime, settings, speech_job.id)
    async with runtime.sessions() as session:
        completed_speech = await session.get(SpeechJob, speech_job.id)
    if completed_speech is None or completed_speech.status != "completed":
        message = completed_speech.error_message if completed_speech else "语音任务不存在"
        await _update_shot(runtime, shot.id, status="failed", error_message=message)
        raise RuntimeError(message or "语音生成失败")

    rendered_path = await _render_dialogue_shot(shot, completed_job, completed_speech)
    async with runtime.sessions() as session, session.begin():
        stored_job = await session.get(VideoJob, completed_job.id, with_for_update=True)
        if stored_job is not None:
            stored_job.storage_path = rendered_path
            stored_job.updated_at = datetime.now(UTC)
    completed_job.storage_path = rendered_path
    shot.speech_job_id = speech_job.id
    shot.rendered_path = rendered_path
    await _update_shot(
        runtime,
        shot.id,
        status="completed",
        rendered_path=rendered_path,
        error_message=None,
    )
    return shot, completed_job


async def _concat_shots(project: DirectorProject, shots: list[DirectorShot]) -> str:
    await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
    list_path = GENERATED_ROOT / f"director-{project.id}.concat.txt"
    output_path = GENERATED_ROOT / f"director-{project.id}.mp4"
    lines = [f"file '{Path(str(shot.rendered_path)).as_posix()}'" for shot in shots]
    await asyncio.to_thread(list_path.write_text, "\n".join(lines), encoding="utf-8")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-t",
            str(project.target_seconds),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"合片失败：{stderr.decode(errors='ignore')[-300:]}")
    finally:
        await asyncio.to_thread(list_path.unlink, missing_ok=True)
    return os.fspath(output_path)


async def _execute_agent_run(
    runtime: RuntimeDependencies,
    settings: Settings,
    project: DirectorProject,
    run: DirectorAgentRun,
    progress: int,
) -> None:
    if run.agent_key not in {"story", "visual"}:
        raise ValueError(f"{run.agent_name}不是文本规划 Agent")
    await _update_project(
        runtime,
        project.id,
        current_stage=run.agent_key,
        progress=progress,
    )
    await _update_run(runtime, run.id, status="processing", error_message=None)
    context = await _completed_context(runtime, project.id)
    system_prompt = (
        f"你是 AI 短剧制作团队中的{run.agent_name}。"
        f"你的职责是：{AGENT_BRIEFS[run.agent_key]}。"
        "请给出可展示、可审计的专业判断摘要和具体交付物；不要输出隐藏思维链或逐步内心推理。"
        "必须使用简体中文，并严格使用两个标题：【判断摘要】与【交付物】。"
    )
    marker = "【故事JSON】" if run.agent_key == "story" else "【视觉JSON】"
    durations = _shot_durations(project.target_seconds) if project.one_click else ["4"]
    if run.agent_key == "story":
        system_prompt += (
            "交付物末尾必须输出【故事JSON】，后接严格合法的 JSON 对象，至少包含："
            "logline、audience、theme、characters、beats、script。characters 每项包含 name、"
            "role、appearance、wardrobe、voice_profile、voice_role。voice_role 只能从 narrator、"
            "adult_male、adult_female、elder_male、elder_female、boy、girl 中选择；禁止编造"
            "voice_id；beats 是按时间顺序排列的"
            "剧情节拍；script 必须包含可表演对白，而不是只有梗概。"
        )
    else:
        system_prompt += (
            f"你必须规划正好 {len(durations)} 个可独立生成的视频镜头，对应时长依次为"
            f" {durations} 秒。交付物末尾必须输出【视觉JSON】，后接严格合法 JSON 对象。"
            "对象必须包含 continuity 和 shots。continuity 包含 characters、relationships、"
            "visual_rules；每个角色包含稳定的 appearance、wardrobe、voice_profile、voice_role，"
            "voice_role 只能从 narrator、adult_male、adult_female、elder_male、elder_female、"
            "boy、girl 中选择，禁止编造 voice_id。"
            "shots 每项必须包含 sequence、title、story_beat、characters、location、action、"
            "shot_size、camera、lighting、transition、positive_prompt、negative_prompt、speaker、"
            "speech_text、subtitle_text、voice_role、emotion、speech_speed。emotion 只能从 calm、"
            "happy、surprised、disappointed、sad、devastated、angry、fearful 中选择。同一人物"
            "跨镜保持 voice_role 不变，但 emotion 应根据当前表演变化。每一镜都必须有非空 "
            "speech_text，"
            "用于真实语音生成，字幕将与 speech_text 保持完全一致。中文对白长度不得超过该镜"
            "秒数乘以 4 个汉字。不得使用“同上”省略字段。"
        )
    user_prompt = (
        f"项目：{project.title}\n故事创意：{project.premise}\n目标时长："
        f"{project.target_seconds} 秒\n画幅：{project.aspect_ratio}\n视觉风格："
        f"{project.visual_style}\n\n上游已确认内容：\n"
        f"{context or '这是第一道任务，请建立全局基线。'}\n\n"
        "用户提供的角色/定妆/声线锁定信息："
        f"{project.continuity_notes or '暂无，需由资产 Agent 建立'}"
    )
    result_data: dict[str, object] | None = None
    content = ""
    validation_error = ""
    for _attempt in range(2):
        correction = (
            f"\n\n上一次结构化交付校验失败：{validation_error}。请完整重写，并确保 {marker} "
            "后的 JSON 严格合法。"
            if validation_error
            else ""
        )
        result = await agent_text_completion(
            runtime,
            settings,
            run.model_name,
            system_prompt,
            user_prompt + correction,
        )
        content = str(result["content"])
        try:
            parsed = _extract_tagged_json(content, marker)
            result_data = (
                _validate_story_data(parsed)
                if run.agent_key == "story"
                else _validate_visual_data(parsed, project, durations)
            )
            break
        except ValueError as exc:
            validation_error = str(exc)
    if result_data is None:
        raise ValueError(f"{run.agent_name}连续两次未通过结构化校验：{validation_error}")

    summary, deliverable = _split_agent_output(content)
    if run.agent_key == "visual":
        bible = dict(result_data["continuity"])
        await _update_project(runtime, project.id, continuity_bible=bible)
        project.continuity_bible = bible
    await _update_run(
        runtime,
        run.id,
        status="completed",
        decision_summary=summary,
        deliverable=deliverable,
        result_data=result_data,
    )


async def _build_quality_report(
    project: DirectorProject,
    shots: list[DirectorShot],
    final_path: str | None,
) -> dict[str, object]:
    issues: list[str] = []
    shot_checks: list[dict[str, object]] = []
    for shot in shots:
        rendered = Path(shot.rendered_path or "")
        if not shot.rendered_path or not await asyncio.to_thread(rendered.is_file):
            issues.append(f"第 {shot.sequence} 镜缺少合成文件")
            continue
        info = await _probe_media(rendered)
        streams = info.get("streams", [])
        has_video = isinstance(streams, list) and any(
            isinstance(item, dict) and item.get("codec_type") == "video" for item in streams
        )
        has_audio = isinstance(streams, list) and any(
            isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
        )
        try:
            duration = float(dict(info.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        has_subtitle = bool(shot.subtitle_text and shot.speech_text)
        if not has_video:
            issues.append(f"第 {shot.sequence} 镜没有视频轨")
        if not has_audio:
            issues.append(f"第 {shot.sequence} 镜没有语音轨")
        if not shot.speech_job_id:
            issues.append(f"第 {shot.sequence} 镜没有独立语音任务")
        if not has_subtitle:
            issues.append(f"第 {shot.sequence} 镜没有字幕文本")
        if abs(duration - float(shot.seconds)) > 1.0:
            issues.append(f"第 {shot.sequence} 镜时长异常：{duration:.2f}s")
        shot_checks.append(
            {
                "sequence": shot.sequence,
                "video": has_video,
                "audio": has_audio,
                "burned_subtitles": has_subtitle,
                "duration_seconds": round(duration, 3),
            }
        )

    final_check: dict[str, object] | None = None
    if project.one_click:
        if not final_path or not await asyncio.to_thread(Path(final_path).is_file):
            issues.append("最终合片文件不存在")
        else:
            info = await _probe_media(final_path)
            streams = info.get("streams", [])
            has_video = isinstance(streams, list) and any(
                isinstance(item, dict) and item.get("codec_type") == "video" for item in streams
            )
            has_audio = isinstance(streams, list) and any(
                isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
            )
            try:
                duration = float(dict(info.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if not has_video or not has_audio:
                issues.append("最终合片缺少视频轨或语音轨")
            if abs(duration - project.target_seconds) > 1.5:
                issues.append(f"最终合片时长异常：{duration:.2f}s")
            final_check = {
                "video": has_video,
                "audio": has_audio,
                "duration_seconds": round(duration, 3),
            }
    return {
        "passed": not issues,
        "shots": shot_checks,
        "final": final_check,
        "issues": issues,
        "checked_at": datetime.now(UTC).isoformat(),
    }


async def run_director_project(
    runtime: RuntimeDependencies,
    settings: Settings,
    project_id: UUID,
) -> None:
    async with runtime.sessions() as session:
        project = await session.get(DirectorProject, project_id)
        runs = list(
            (
                await session.scalars(
                    select(DirectorAgentRun)
                    .where(DirectorAgentRun.project_id == project_id)
                    .order_by(DirectorAgentRun.sequence)
                )
            ).all()
        )
    if project is None:
        return

    try:
        await _update_project(
            runtime,
            project_id,
            status="processing",
            current_stage="director",
            progress=2,
            error_message=None,
        )
        story_run = next(run for run in runs if run.agent_key == "story")
        visual_run = next(run for run in runs if run.agent_key == "visual")
        media_run = next(run for run in runs if run.agent_key == "media")
        quality_run = next(run for run in runs if run.agent_key == "quality")
        await _execute_agent_run(runtime, settings, project, story_run, 8)
        await _execute_agent_run(runtime, settings, project, visual_run, 25)

        await _update_project(runtime, project_id, current_stage="media", progress=40)
        await _update_run(runtime, media_run.id, status="processing", error_message=None)

        completed_shots: list[DirectorShot] = []
        if project.one_click:
            durations = _shot_durations(project.target_seconds)
            shot_plan = await _load_storyboard_plan(runtime, project, len(durations))
            await _update_project(
                runtime,
                project_id,
                current_stage="media",
                progress=42,
                planned_shots=len(durations),
            )
            completed_jobs: list[VideoJob] = []
            for sequence, seconds in enumerate(durations, start=1):
                completed_shot, completed_job = await _create_and_run_shot(
                    runtime,
                    settings,
                    project,
                    sequence,
                    len(durations),
                    seconds,
                    shot_plan[sequence - 1],
                )
                completed_shots.append(completed_shot)
                completed_jobs.append(completed_job)
                await _update_project(
                    runtime,
                    project_id,
                    preview_video_job_id=completed_jobs[0].id,
                    completed_shots=len(completed_jobs),
                    progress=42 + round((len(completed_jobs) / len(durations)) * 43),
                )
        else:
            shot_plan = await _load_storyboard_plan(runtime, project, 1)
            await _update_project(runtime, project_id, current_stage="media", progress=45)
            completed_shot, completed_preview = await _create_and_run_shot(
                runtime,
                settings,
                project,
                1,
                1,
                "4",
                shot_plan[0],
            )
            completed_shots.append(completed_shot)
            await _update_project(
                runtime,
                project_id,
                preview_video_job_id=completed_preview.id,
                completed_shots=1,
                progress=85,
            )

        final_path: str | None = None
        if project.one_click:
            await _update_project(runtime, project_id, current_stage="media", progress=88)
            final_path = await _concat_shots(project, completed_shots)
            project.final_video_path = final_path
            await _update_project(runtime, project_id, final_video_path=final_path, progress=91)
            preview_note = (
                f"已生成 {len(completed_shots)} 个带配音和烧录字幕的镜头，并合成为约 "
                f"{project.target_seconds} 秒影片。"
            )
        else:
            preview_note = "首个带真实配音和烧录字幕的预览镜头已生成。"
        await _update_run(
            runtime,
            media_run.id,
            status="completed",
            decision_summary="视频、语音、混音和字幕烧录均已执行。",
            deliverable=preview_note,
            result_data={
                "completed_shots": len(completed_shots),
                "final_video_path": final_path,
                "speech": True,
                "burned_subtitles": True,
            },
        )

        await _update_project(runtime, project_id, current_stage="quality", progress=95)
        await _update_run(runtime, quality_run.id, status="processing", error_message=None)
        report = await _build_quality_report(project, completed_shots, final_path)
        await _update_project(runtime, project_id, quality_report=report)
        if not report["passed"]:
            issues = "；".join(str(item) for item in report["issues"])
            await _update_run(
                runtime,
                quality_run.id,
                status="failed",
                decision_summary="真实媒体质检未通过。",
                deliverable=json.dumps(report, ensure_ascii=False),
                result_data=report,
                error_message=issues[:360],
            )
            raise RuntimeError(f"质检未通过：{issues}")
        await _update_run(
            runtime,
            quality_run.id,
            status="completed",
            decision_summary="真实媒体质检通过：画面、语音、字幕和时长均可交付。",
            deliverable=json.dumps(report, ensure_ascii=False),
            result_data=report,
        )
        await _update_project(
            runtime,
            project_id,
            status="completed",
            current_stage="completed",
            progress=100,
            final_summary=f"总导演编排器与 4 位执行 Agent 已完成制作。{preview_note}",
            error_message=None,
        )
    except Exception as exc:
        async with runtime.sessions() as session:
            active_run = await session.scalar(
                select(DirectorAgentRun).where(
                    DirectorAgentRun.project_id == project_id,
                    DirectorAgentRun.status == "processing",
                )
            )
        if active_run:
            await _update_run(
                runtime,
                active_run.id,
                status="failed",
                error_message=f"{type(exc).__name__}: {str(exc)[:360]}",
            )
        await _update_project(
            runtime,
            project_id,
            status="failed",
            error_message=f"{type(exc).__name__}: {str(exc)[:420]}",
        )
