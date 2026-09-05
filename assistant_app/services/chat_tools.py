"""Validated side-effect tool registry shared by both agent runtimes."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assistant_app.services.director import create_director_project, project_payload
from assistant_app.services.generated_files import create_generated_file, file_payload
from assistant_app.services.music_gateway import create_music_job, music_job_payload
from assistant_app.services.speech_gateway import create_speech_job, speech_job_payload
from assistant_app.services.video_gateway import create_video_job, video_job_payload


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FileArguments(Arguments):
    filename: str = Field(min_length=1, max_length=255)
    content: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1,
        max_length=1_000_000,
    )


class VideoArguments(Arguments):
    prompt: str = Field(min_length=1, max_length=8000)
    seconds: Literal["4", "8", "12"] | None = None
    size: Literal["720x1280", "1280x720", "1024x1792", "1792x1024"] | None = None
    resolution: Literal["768P", "2K"] | None = None


class MusicArguments(Arguments):
    prompt: str = Field(min_length=1, max_length=2000)
    lyrics: str | None = Field(default=None, max_length=6000)
    is_instrumental: bool = True


class SpeechArguments(Arguments):
    text: str = Field(min_length=1, max_length=10_000)
    voice_id: str | None = Field(default=None, max_length=200)
    speed: float = Field(default=1, ge=0.5, le=2, allow_inf_nan=False)
    speaker: str | None = Field(default=None, max_length=100)
    voice_role: str | None = Field(default=None, max_length=32)
    emotion: str = Field(default="calm", max_length=32)


class DirectorArguments(Arguments):
    premise: str = Field(min_length=4, max_length=8000)
    target_seconds: Literal[4, 30, 60, 180, 300] = 60
    aspect_ratio: Literal["9:16", "16:9"] = "9:16"
    resolution: Literal["768P", "2K"] = "768P"
    visual_style: str = Field(default="", max_length=100)
    continuity_notes: str = Field(default="", max_length=8000)
    one_click: bool = False
    use_memory: bool = True


async def file_tool(runtime, settings, user_id, args):
    record = await create_generated_file(runtime, user_id, **args.model_dump())
    return "files", file_payload(record), f"已生成文件：{record.filename}"


async def video_tool(runtime, settings, user_id, args):
    job = await create_video_job(
        runtime,
        user_id,
        **args.model_dump(),
        awaiting_confirmation=True,
    )
    return (
        "video_jobs",
        video_job_payload(job),
        "视频草稿已保存，请核对提示词和参数后点击确认生成。",
    )


async def music_tool(runtime, settings, user_id, args):
    job = await create_music_job(runtime, user_id, **args.model_dump())
    return "music_jobs", music_job_payload(job), "音乐生成任务已提交。"


async def speech_tool(runtime, settings, user_id, args):
    values = args.model_dump()
    values["speech_text"] = values.pop("text")
    job = await create_speech_job(runtime, user_id, **values)
    return "speech_jobs", speech_job_payload(job), "语音配音任务已提交。"


async def director_tool(runtime, settings, user_id, args):
    project = await create_director_project(runtime, settings, user_id, **args.model_dump())
    return (
        "director_projects",
        await project_payload(runtime, project),
        "导演故事草案已保存，请到导演工作室核对并确认故事后开始制作。",
    )


TOOL_REGISTRY = {
    "create_file": (FileArguments, file_tool),
    "generate_video": (VideoArguments, video_tool),
    "generate_music": (MusicArguments, music_tool),
    "generate_speech": (SpeechArguments, speech_tool),
    "start_director_production": (DirectorArguments, director_tool),
}


async def execute_tools(runtime, settings, user_id, calls, checkpoint):
    artifacts = {
        name: []
        for name in (
            "files",
            "video_jobs",
            "music_jobs",
            "speech_jobs",
            "director_projects",
            "tool_results",
        )
    }
    notices = []
    for index, call in enumerate(calls[:3]):
        name = call.get("name", "")
        outcome = {"name": name, "index": index, "status": "processing"}
        artifacts["tool_results"].append(outcome)
        # Persist intent before performing a side effect; failed runs are never replayed.
        await checkpoint(artifacts)
        try:
            schema, handler = TOOL_REGISTRY[name]
            arguments = schema.model_validate(call.get("arguments", {}))
            async with asyncio.timeout(120):
                key, resource, notice = await handler(runtime, settings, user_id, arguments)
            artifacts[key].append(resource)
            outcome.update(status="completed", resource_id=resource["id"])
            notices.append(notice)
        except Exception as exc:
            outcome.update(status="failed", error=type(exc).__name__)
            notices.append(f"工具 {name} 未完成（{type(exc).__name__}），请检查参数或渠道配置。")
        await checkpoint(artifacts)
    return artifacts, notices
