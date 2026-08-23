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
from assistant_app.db.models import DirectorAgentRun, DirectorProject, DirectorShot, VideoJob
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.agent_model_router import (
    AGENT_MODEL_PROFILES,
    route_agent_models,
)
from assistant_app.services.generated_files import GENERATED_ROOT
from assistant_app.services.model_gateway import agent_text_completion, list_available_models
from assistant_app.services.video_gateway import create_video_job, run_video_job, video_job_payload


class DirectorProjectNotFoundError(LookupError):
    pass


AGENT_BRIEFS = {
    "director": "明确创作目标、生产边界、整体调度顺序和各 Agent 的验收标准",
    "concept": "判断目标受众、核心情绪、开场钩子、差异化卖点与追看动力",
    "script": "完成角色动机、三幕或节拍结构、主要场次、对白原则和结尾回收",
    "assets": "建立角色、场景、服装、道具、色彩与跨镜头连续性资产圣经",
    "storyboard": "拆分可执行镜头，说明景别、机位、运动、时长、转场和轴线关系",
    "video": "把关键镜头转为视频模型提示词，约束人物稳定、动作、画幅与负面条件",
    "audio": "规划对白、旁白、环境声、拟音、声场和需要额外语音模型介入的部分",
    "edit": "给出粗剪顺序、节奏、声画同步、转场、字幕安全区与输出规范",
    "quality": "按叙事、连续性、画面、声音、技术、版权与平台合规执行终审",
}

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


def _default_continuity_bible(project: DirectorProject) -> dict[str, object]:
    return {
        "version": 1,
        "lock_mode": "text",
        "characters": [],
        "relationships": [],
        "visual_rules": [project.visual_style, f"固定画幅 {project.aspect_ratio}"],
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
    return {
        "sequence": sequence,
        "title": title[:200],
        "story_beat": str(raw.get("story_beat") or title).strip(),
        "instruction": instruction[:6_000],
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


def _shot_prompt(
    project: DirectorProject,
    sequence: int,
    total: int,
    seconds: str,
    spec: dict[str, object],
) -> str:
    continuity = _continuity_prompt(project)
    return (
        f"{project.visual_style}，{project.aspect_ratio} AI 短剧，第 {sequence}/{total} 镜，"
        f"时长 {seconds} 秒。\n"
        f"【本镜唯一分镜方案】\n{spec['instruction']}\n"
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
    completed = "\n\n".join(
        f"### {row.agent_name}\n{(row.deliverable or '')[:1800]}" for row in rows
    )[-14_000:]
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
        deliverable = await session.scalar(
            select(DirectorAgentRun.deliverable).where(
                DirectorAgentRun.project_id == project.id,
                DirectorAgentRun.agent_key == "storyboard",
                DirectorAgentRun.status == "completed",
            )
        )
    return _extract_storyboard_plan(str(deliverable or ""), total, project.premise)


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
    )
    async with runtime.sessions() as session, session.begin():
        session.add(shot)
    size = "720x1280" if project.aspect_ratio == "9:16" else "1280x720"
    job = await create_video_job(runtime, project.user_id, shot.prompt, seconds, size)
    await _update_shot(runtime, shot.id, video_job_id=job.id)
    await run_video_job(runtime, settings, job.id)
    async with runtime.sessions() as session:
        completed_job = await session.get(VideoJob, job.id)
    if completed_job is None or completed_job.status != "completed":
        message = completed_job.error_message if completed_job else "视频任务不存在"
        await _update_shot(runtime, shot.id, status="failed", error_message=message)
        raise RuntimeError(message or "镜头生成失败")
    await _update_shot(runtime, shot.id, status="completed", error_message=None)
    return shot, completed_job


async def _concat_shots(project: DirectorProject, jobs: list[VideoJob]) -> str:
    await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
    list_path = GENERATED_ROOT / f"director-{project.id}.concat.txt"
    output_path = GENERATED_ROOT / f"director-{project.id}.mp4"
    lines = [f"file '{Path(str(job.storage_path)).as_posix()}'" for job in jobs]
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
    if run.agent_key == "assets":
        system_prompt += (
            "你还必须在交付物末尾输出【连续性JSON】，后接严格合法 JSON："
            '{"version":1,"lock_mode":"text","characters":['
            '{"name":"","role":"主角或配角","appearance":"五官发型年龄体态",'
            '"wardrobe":"固定服装配色材质标志物","voice_profile":"固定音色语速口音",'
            '"voice_id":null,"portrait_prompt":"定妆照提示词",'
            '"reference_image_url":null}],"relationships":['
            '{"source":"","target":"","relation":""}],"visual_rules":[]}。'
            "角色必须覆盖故事中的主角、配角和关键人物；"
            "若用户给了定妆照 URL 或 voice_id，原样登记。"
        )
    if run.agent_key == "storyboard":
        durations = _shot_durations(project.target_seconds) if project.one_click else ["4"]
        system_prompt += (
            f"你必须规划正好 {len(durations)} 个可独立生成的视频镜头，"
            f"对应时长依次为 {durations} 秒。每一镜的叙事任务、地点、核心动作、景别和运镜"
            "必须实质不同，同时保持角色连续性。交付物末尾必须输出【分镜JSON】，"
            "后接严格合法 JSON 数组；每项必须包含 sequence、title、story_beat、characters、"
            "location、action、shot_size、camera、lighting、transition、positive_prompt、"
            "negative_prompt。不得用“同上”“延续上一镜”等方式省略字段。"
        )
    user_prompt = (
        f"项目：{project.title}\n故事创意：{project.premise}\n目标时长："
        f"{project.target_seconds} 秒\n画幅：{project.aspect_ratio}\n视觉风格："
        f"{project.visual_style}\n\n上游已确认内容：\n"
        f"{context or '这是第一道任务，请建立全局基线。'}\n\n"
        "用户提供的角色/定妆/声线锁定信息："
        f"{project.continuity_notes or '暂无，需由资产 Agent 建立'}"
    )
    result = await agent_text_completion(
        runtime,
        settings,
        run.model_name,
        system_prompt,
        user_prompt,
    )
    summary, deliverable = _split_agent_output(str(result["content"]))
    if run.agent_key == "assets":
        bible = _extract_continuity_bible(str(result["content"]), project)
        await _update_project(runtime, project.id, continuity_bible=bible)
        project.continuity_bible = bible
    await _update_run(
        runtime,
        run.id,
        status="completed",
        decision_summary=summary,
        deliverable=deliverable,
    )


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
        preproduction_runs = [run for run in runs if run.agent_key not in {"edit", "quality"}]
        for index, run in enumerate(preproduction_runs):
            progress = 3 + round((index / len(preproduction_runs)) * 61)
            await _execute_agent_run(runtime, settings, project, run, progress)

        if project.one_click:
            durations = _shot_durations(project.target_seconds)
            shot_plan = await _load_storyboard_plan(runtime, project, len(durations))
            await _update_project(
                runtime,
                project_id,
                current_stage="video",
                progress=65,
                planned_shots=len(durations),
            )
            completed_jobs: list[VideoJob] = []
            for sequence, seconds in enumerate(durations, start=1):
                _shot, completed_job = await _create_and_run_shot(
                    runtime,
                    settings,
                    project,
                    sequence,
                    len(durations),
                    seconds,
                    shot_plan[sequence - 1],
                )
                completed_jobs.append(completed_job)
                await _update_project(
                    runtime,
                    project_id,
                    preview_video_job_id=completed_jobs[0].id,
                    completed_shots=len(completed_jobs),
                    progress=65 + round((len(completed_jobs) / len(durations)) * 25),
                )
        else:
            completed_jobs = []
            shot_plan = await _load_storyboard_plan(runtime, project, 1)
            await _update_project(runtime, project_id, current_stage="preview", progress=68)
            _shot, completed_preview = await _create_and_run_shot(
                runtime,
                settings,
                project,
                1,
                1,
                "4",
                shot_plan[0],
            )
            await _update_project(
                runtime,
                project_id,
                preview_video_job_id=completed_preview.id,
                completed_shots=1,
                progress=82,
            )

        edit_run = next(run for run in runs if run.agent_key == "edit")
        await _execute_agent_run(runtime, settings, project, edit_run, 92)
        if project.one_click:
            await _update_project(runtime, project_id, current_stage="edit", progress=95)
            final_path = await _concat_shots(project, completed_jobs)
            project.final_video_path = final_path
            await _update_project(runtime, project_id, final_video_path=final_path, progress=97)
            preview_note = (
                f"一键成片已生成 {len(completed_jobs)} 个连续镜头，并合成为约 "
                f"{project.target_seconds} 秒影片。"
            )
        else:
            preview_note = "首个真实预览镜头已生成，可在成片展示区播放。"

        quality_run = next(run for run in runs if run.agent_key == "quality")
        await _execute_agent_run(runtime, settings, project, quality_run, 98)
        await _update_project(
            runtime,
            project_id,
            status="completed",
            current_stage="completed",
            progress=100,
            final_summary=f"总导演与 8 位专业 Agent 已完成制作。{preview_note}",
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
