"""Director's low-cost branch: images + TTS + local rendering, never video generation."""

import asyncio
import json
import math
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from assistant_app.db.models import DirectorShot, ImageChannel, SpeechChannel, SpeechJob
from assistant_app.services.activity import emit_activity
from assistant_app.services.director_media import (
    _concat_shots,
    _probe_media,
    _run_media_command,
    _srt_timestamp,
    _subtitle_filter_path,
)
from assistant_app.services.director_quality import _build_quality_report
from assistant_app.services.generated_files import GENERATED_ROOT
from assistant_app.services.image_gateway import generate_shot_image
from assistant_app.services.speech_gateway import create_speech_job, run_speech_job

WHITEBOARD_INSTRUCTIONS = (
    "\n本项目制作方式为白板讲解，不是动态电影。以下要求覆盖前述动态表演要求："
    "每镜是一幅静态插画，在白板上描线后上色，旁白推动叙事。不要求口型、运动、"
    "原生音效或配乐。positive_prompt 必须描述一幅白底、清晰深色线稿、少量平涂色、"
    "主体明确且适合描线的图，不要图片内文字或复杂纹理。保持角色外貌跨镜一致。"
    "speech_text 是解说词，speaker 和 voice_role 使用 narrator，保持同一旁白声线；"
    "按给定镜头时长规划，每秒不超过 4 个汉字，字幕使用相同文本。"
    "continuity.characters 可包含被讲解的主体，无人物时可用旁白作为角色。"
    "保持原有 JSON 字段结构。不要承诺逐对象语义绘制或真实角色运动。"
)


def whiteboard_durations(target_seconds, one_click):
    if not one_click:
        return ["4"]
    count = max(1, math.ceil(target_seconds / 35))
    base, extra = divmod(target_seconds, count)
    return [str(base + (index < extra)) for index in range(count)]


async def ensure_shot(session, project, sequence, spec, seconds):
    shot = await session.scalar(
        select(DirectorShot).where(
            DirectorShot.project_id == project.id, DirectorShot.sequence == sequence
        )
    )
    if shot:
        return shot
    prompt = (
        "Whiteboard illustration, white background, clear dark outlines, sparse flat colors, "
        "no text, no watermark. "
        + str(spec.get("positive_prompt") or spec.get("action"))[:850]
        + "\nConsistent character design: "
        + json.dumps((project.continuity_bible or {}).get("characters", []), ensure_ascii=False)[
            :400
        ]
    )
    shot = DirectorShot(
        id=uuid4(),
        project_id=project.id,
        user_id=project.user_id,
        sequence=sequence,
        title=str(spec.get("title") or f"白板第 {sequence} 镜")[:200],
        prompt=prompt,
        seconds=seconds,
        status="pending",
        speaker="narrator",
        speech_text=str(spec["speech_text"]),
        subtitle_text=str(spec["speech_text"]),
        continuity_snapshot=dict(spec),
    )
    session.add(shot)
    await session.flush()
    return shot


def subtitle_cues(text, duration):
    # Estimated phrase timing, NOT forced alignment. Escape subtitle markup from model text.
    clean = text.replace("\r", " ").replace("\n", " ").replace("{", "（").replace("}", "）")
    clean = clean.replace("<", "〈").replace(">", "〉").replace("\\", "＼")
    chunks = [clean[index : index + 22] for index in range(0, len(clean), 22)]
    total = max(1, len(clean))
    elapsed = 0
    cues = []
    for index, chunk in enumerate(chunks, 1):
        start = duration * elapsed / total
        elapsed += len(chunk)
        end = duration * elapsed / total
        cues.append(f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{chunk}\n")
    return "\n".join(cues)


async def render_shot(project, shot, speech):
    from assistant_app.services.director import _director_video_size

    info = await _probe_media(speech.storage_path)
    audio_seconds = float((info.get("format") or {}).get("duration") or 0)
    if not 0.1 < audio_seconds <= 120:
        raise ValueError("配音时长无效或超过单镜 120 秒限制")
    # Keep the planned pacing, but never cut off a longer spoken sentence.
    duration = math.ceil(max(float(shot.seconds), audio_seconds + 0.3) * 24) / 24
    width, height = map(
        int, _director_video_size(project.aspect_ratio, project.resolution).split("x")
    )
    raw = GENERATED_ROOT / f"whiteboard-raw-{shot.id}.mp4"
    output = GENERATED_ROOT / f"director-shot-{shot.id}.mp4"
    subtitle = GENERATED_ROOT / f"director-shot-{shot.id}.srt"
    await asyncio.to_thread(
        subtitle.write_text, subtitle_cues(shot.speech_text, audio_seconds), encoding="utf-8"
    )
    # Persist the actual timings and input mapping for reproducibility, not invented object boxes.
    annotation = {
        "version": 1,
        "ordering": "geometric_contours",
        "scope": "whole_scene",
        "image": Path(shot.image_path).name,
        "speech_job_id": str(speech.id),
        "regions": [
            {
                "id": "scene",
                "bbox": [0, 0, width, height],
                "start_ms": 0,
                "end_ms": round(duration * 1000),
                "subtitle_text": shot.speech_text,
            }
        ],
        "duration_seconds": duration,
        "subtitle_alignment": "estimated_by_text_length",
    }
    await asyncio.to_thread(
        (GENERATED_ROOT / f"director-shot-{shot.id}.annotation.json").write_text,
        json.dumps(annotation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        await _run_media_command(
            sys.executable,
            "-m",
            "assistant_app.services.whiteboard_renderer",
            shot.image_path,
            str(raw),
            str(duration),
            str(width),
            str(height),
        )
        await _run_media_command(
            "ffmpeg",
            "-y",
            "-i",
            str(raw),
            "-i",
            speech.storage_path,
            "-vf",
            f"subtitles=filename='{_subtitle_filter_path(subtitle)}':"
            "force_style='FontName=Noto Sans CJK SC,FontSize=16,Outline=1,MarginV=24'",
            "-af",
            "aresample=48000,apad",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
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
            str(output),
        )
    finally:
        await asyncio.to_thread(raw.unlink, missing_ok=True)
    return str(output), duration, audio_seconds


async def run_whiteboard_media(runtime, settings, project, media_run, quality_run):
    from assistant_app.services.director import (
        _load_storyboard_plan,
        _update_project,
        _update_run,
        _update_shot,
    )

    await _update_run(
        runtime,
        media_run.id,
        status="processing",
        error_message=None,
        model_name="image channel + speech channel + local whiteboard/ffmpeg",
    )
    await _update_project(runtime, project.id, current_stage="media", progress=40)
    # Fail before any paid submission when local dependencies or TTS configuration are absent.
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("白板渲染需要服务器安装 FFmpeg 与 ffprobe，尚未提交付费媒体请求")
    await _run_media_command(sys.executable, "-c", "import cv2, numpy, PIL")
    durations = whiteboard_durations(project.target_seconds, project.one_click)
    plan = await _load_storyboard_plan(runtime, project, len(durations))
    async with runtime.sessions() as session, session.begin():
        speech_channel = await session.scalar(
            select(SpeechChannel).where(SpeechChannel.is_active.is_(True))
        )
        if speech_channel is None:
            raise ValueError("尚未启用语音渠道，未提交生图请求")
        shots = [
            await ensure_shot(session, project, i, spec, seconds)
            for i, (spec, seconds) in enumerate(zip(plan, durations, strict=True), 1)
        ]
        image_channel = await session.scalar(
            select(ImageChannel).where(ImageChannel.is_active.is_(True))
        )
        if not image_channel and any(not shot.image_path for shot in shots):
            raise ValueError("请先启用图片渠道，或为每个分镜上传图片；不会调用视频渠道兜底")
    await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
    completed = []
    for shot in shots:
        if (shot.status == "completed" and shot.rendered_path
                and await asyncio.to_thread(Path(shot.rendered_path).is_file)):
            completed.append(shot)
            continue
        try:
            await _update_shot(runtime, shot.id, status="processing", error_message=None)
            shot.image_path = await generate_shot_image(
                runtime, settings, shot.id, project.aspect_ratio
            )
            await emit_activity(
                runtime, f"第 {shot.sequence} 镜 · 旁白配音", "processing", kind="tool"
            )
            if not shot.speech_job_id:
                speech = await create_speech_job(
                    runtime,
                    project.user_id,
                    shot.speech_text,
                    speaker="narrator",
                    schedule=False,
                )
                shot.speech_job_id = speech.id
                # The job is linked before submission so crashes cannot trigger a second charge.
                await _update_shot(runtime, shot.id, speech_job_id=speech.id)
            await run_speech_job(runtime, settings, shot.speech_job_id)
            async with runtime.sessions() as session:
                speech = await session.get(SpeechJob, shot.speech_job_id)
            if speech.status != "completed" or not speech.storage_path:
                raise RuntimeError(speech.error_message or "旁白尚未生成，未开始渲染")
            await emit_activity(
                runtime, f"第 {shot.sequence} 镜 · 旁白完成", "completed", kind="tool"
            )
            await emit_activity(
                runtime, f"第 {shot.sequence} 镜 · 本地描线、上色与合成", "processing", kind="tool"
            )
            path, duration, audio_seconds = await render_shot(project, shot, speech)
            shot.seconds = f"{duration:.3f}"
            shot.rendered_path = path
            shot.status = "completed"
            shot.continuity_snapshot = dict(shot.continuity_snapshot or {}) | {
                "_media": {
                    "audio_source": "whiteboard_tts",
                    "subtitle_start_seconds": 0,
                    "subtitle_end_seconds": audio_seconds,
                    "subtitle_alignment": "estimated",
                    "single_speaker": True,
                    "image_path": Path(shot.image_path).name,
                }
            }
            await _update_shot(
                runtime,
                shot.id,
                seconds=shot.seconds,
                rendered_path=path,
                status="completed",
                continuity_snapshot=shot.continuity_snapshot,
            )
            completed.append(shot)
            await emit_activity(
                runtime, f"第 {shot.sequence} 镜 · 白板合成完成", "completed", kind="tool"
            )
            await _update_project(
                runtime,
                project.id,
                completed_shots=len(completed),
                progress=42 + round(len(completed) / len(shots) * 45),
            )
        except Exception as exc:
            await _update_shot(runtime, shot.id, status="failed", error_message=str(exc)[:360])
            raise
    final_path = (
        await _concat_shots(project, completed) if project.one_click else completed[0].rendered_path
    )
    await _update_project(
        runtime,
        project.id,
        final_video_path=final_path,
        completed_shots=len(completed),
        current_stage="quality",
        progress=95,
    )
    await _update_run(
        runtime,
        media_run.id,
        status="completed",
        decision_summary="白板产线完成：图片 + 旁白 + 本地描线和上色，无视频模型调用。",
        deliverable=f"已合成 {len(completed)} 幅白板场景，字幕为估算时序。",
        result_data={
            "production_mode": "whiteboard",
            "video_model_calls": 0,
            "completed_shots": len(completed),
            "final_video_path": final_path,
        },
    )
    await _update_run(runtime, quality_run.id, status="processing", error_message=None)
    report = await _build_quality_report(project, completed, final_path)
    report["not_checked"] += ["semantic_drawing_order", "image_story_consistency"]
    await _update_project(runtime, project.id, quality_report=report)
    await _update_run(
        runtime,
        quality_run.id,
        status="completed" if report["passed"] else "failed",
        decision_summary="检查音视频轨和时长；绘图语义、内容及字幕对齐需人工验收。",
        deliverable=json.dumps(report, ensure_ascii=False),
        result_data=report,
    )
    if not report["passed"]:
        raise RuntimeError("白板技术检查失败：" + "；".join(report["issues"]))
    await _update_project(
        runtime,
        project.id,
        status="completed",
        current_stage="completed",
        progress=100,
        error_message=None,
        final_summary=(
            f"白板成片完成：{len(completed)} 幅图片、旁白和字幕；"
            "未调用视频模型。为完整保留旁白，实际时长可能略长于目标。"
        ),
    )
