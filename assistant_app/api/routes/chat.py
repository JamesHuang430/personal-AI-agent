from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field, field_validator

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import User
from assistant_app.services.agent_model_router import route_agent_models
from assistant_app.services.conversations import (
    ConversationNotFoundError,
    delete_conversation,
    get_conversation_messages,
    list_conversations,
    prepare_conversation,
    record_assistant_message,
)
from assistant_app.services.director import (
    create_director_project,
    project_payload,
)
from assistant_app.services.document_skill import (
    document_skill_payload,
    extract_document_context,
    get_owned_documents,
    uploaded_document_payload,
)
from assistant_app.services.generated_files import create_generated_file, file_payload
from assistant_app.services.mcp_runtime import list_mcp_tools
from assistant_app.services.memory import (
    learn_from_exchange,
    organize_conversation_session,
    retrieve_memory_context,
)
from assistant_app.services.model_gateway import (
    ModelChannelUnavailableError,
    ModelRateLimitError,
    chat_completion,
    list_available_models,
)
from assistant_app.services.music_gateway import (
    MusicChannelUnavailableError,
    create_music_job,
    music_job_payload,
    run_music_job,
)
from assistant_app.services.speech_gateway import (
    SpeechChannelUnavailableError,
    create_speech_job,
    run_speech_job,
    speech_job_payload,
)
from assistant_app.services.video_gateway import (
    VideoChannelUnavailableError,
    create_video_job,
    run_video_job,
    video_job_payload,
)

router = APIRouter()


_DIRECTOR_PREVIEW_PATTERNS = (
    r"(?:先|只)生成(?:一个|首个)?(?:预览|测试)?镜头",
    r"(?:先看|生成)(?:一个)?预览",
    r"逐镜确认",
)
_DIRECTOR_FULL_PRODUCTION_PATTERNS = (
    r"一键成片",
    r"(?:完整|最终)(?:成片|视频|短剧|电影)",
    r"自动合片|合成为",
    r"制作(?:一部|一个|一段)?.{0,12}(?:短剧|电影|视频)",
    r"生成(?:一部|一个|一段)?.{0,12}(?:短剧|电影|视频)",
    r"视频(?:共计|总计|总时长|时长).{0,8}(?:30|60|180|300)\s*秒",
)
_VIDEO_CONFIRMATION_PATTERNS = (
    r"(?:提示词|分镜|方案).{0,8}(?:已确认|确认通过)",
    r"(?:确认|同意)(?:并|后)?(?:立即|现在)?生成",
    r"(?:立即|现在)提交视频生成",
)


def director_full_production_requested(
    message: str,
    arguments: dict[str, object],
) -> bool:
    """Resolve explicit full-video intent without relying on one UI phrase."""

    if arguments.get("one_click") is True:
        return True
    normalized = re.sub(r"\s+", "", message)
    if any(re.search(pattern, normalized) for pattern in _DIRECTOR_PREVIEW_PATTERNS):
        return False
    return any(
        re.search(pattern, normalized) for pattern in _DIRECTOR_FULL_PRODUCTION_PATTERNS
    )


def video_generation_confirmed(message: str) -> bool:
    normalized = re.sub(r"\s+", "", message)
    return any(re.search(pattern, normalized) for pattern in _VIDEO_CONFIRMATION_PATTERNS)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatPayload(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8_000)
    conversation_id: UUID | None = None
    history: list[HistoryMessage] = Field(default_factory=list, max_length=20)
    file_ids: list[UUID] = Field(default_factory=list, max_length=8)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("请选择或输入模型 ID")
        return normalized


@router.get("/models")
async def available_models(
    request: Request,
    _user: Annotated[User, Depends(current_user)],
) -> list[dict[str, str]]:
    """Discover safe model metadata from the single active LLM channel."""

    try:
        channel_name, model_names = await list_available_models(
            request.app.state.runtime,
            request.app.state.settings,
        )
        return [{"channel": channel_name, "model": name} for name in model_names]
    except ModelChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="大模型渠道密钥无法解密") from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise HTTPException(status_code=502, detail="无法获取当前渠道的模型列表") from exc
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"获取模型列表失败（HTTP {exc.status_code}）",
        ) from exc


@router.get("/capabilities")
async def chat_capabilities(
    request: Request,
    _user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    settings = request.app.state.settings
    tools = await list_mcp_tools(settings, settings.mcp_markitdown_url)
    ready = "convert_to_markdown" in tools
    return {
        "skills": [document_skill_payload(ready=ready)],
        "mcp": {
            "enabled": settings.mcp_enabled,
            "servers": [
                {
                    "id": "markitdown",
                    "name": "Microsoft MarkItDown",
                    "transport": "streamable-http",
                    "ready": ready,
                    "tools": [name for name in tools if name == "convert_to_markdown"],
                }
            ],
        },
    }


@router.get("/agent-model-routing")
async def agent_model_routing(
    request: Request,
    _user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    """Match every production agent to the best currently available model."""

    try:
        channel_name, model_names = await list_available_models(
            request.app.state.runtime,
            request.app.state.settings,
        )
        return {
            "channel": channel_name,
            "available_models": model_names,
            "assignments": route_agent_models(model_names),
        }
    except ModelChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="大模型渠道密钥无法解密") from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise HTTPException(status_code=502, detail="无法匹配 Agent 模型") from exc
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"匹配 Agent 模型失败（HTTP {exc.status_code}）",
        ) from exc


@router.get("/conversations")
async def conversations(
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> list[dict[str, object]]:
    return await list_conversations(request.app.state.runtime, user.id)


@router.get("/conversations/{conversation_id}")
async def conversation_messages(
    conversation_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        return await get_conversation_messages(
            request.app.state.runtime,
            user.id,
            conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_conversation(
    conversation_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> None:
    try:
        await delete_conversation(
            request.app.state.runtime,
            user.id,
            conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/organize")
async def organize_conversation(
    conversation_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        await get_conversation_messages(
            request.app.state.runtime,
            user.id,
            conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = await organize_conversation_session(
        request.app.state.runtime,
        request.app.state.settings,
        user.id,
        conversation_id,
    )
    messages = {
        "completed": "本次会话已整理并同步到个人知识库",
        "disabled": "当前未启用长期记忆，未保存本次会话",
        "failed": "本次会话整理失败，原始对话仍已安全保存",
    }
    return {
        "status": result.status,
        "message": messages[result.status],
        "memories": result.memory_count,
        "entities": result.entity_count,
        "relations": result.relation_count,
    }


@router.post("")
async def chat(
    payload: ChatPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        if len(payload.file_ids) > request.app.state.settings.document_max_files_per_message:
            raise ValueError(
                f"每次最多使用 {request.app.state.settings.document_max_files_per_message} 个附件"
            )
        documents = await get_owned_documents(
            request.app.state.runtime,
            user.id,
            payload.file_ids,
        )
        document_context, mcp_calls = await extract_document_context(
            request.app.state.settings,
            documents,
        )
        prepared = await prepare_conversation(
            request.app.state.runtime,
            user.id,
            payload.conversation_id,
            payload.message.strip(),
            payload.model,
        )
        memory_context = await retrieve_memory_context(
            request.app.state.runtime,
            request.app.state.settings,
            user.id,
            payload.message.strip(),
        )
        result = await chat_completion(
            request.app.state.runtime,
            request.app.state.settings,
            payload.model,
            payload.message.strip(),
            prepared.history,
            memory_context.text,
            document_context,
        )
        files: list[dict[str, object]] = []
        video_jobs: list[dict[str, object]] = []
        music_jobs: list[dict[str, object]] = []
        speech_jobs: list[dict[str, object]] = []
        director_projects: list[dict[str, object]] = []
        notices: list[str] = []
        for tool_call in result.pop("tool_calls", []):
            arguments = tool_call.get("arguments", {})
            if tool_call.get("name") == "create_file":
                record = await create_generated_file(
                    request.app.state.runtime,
                    user.id,
                    str(arguments.get("filename", "assistant-file.md")),
                    str(arguments.get("content", "")),
                )
                files.append(file_payload(record))
                notices.append(f"已生成文件：{record.filename}")
            elif tool_call.get("name") == "generate_video":
                prompt = str(arguments.get("prompt", payload.message)).strip()
                if not video_generation_confirmed(payload.message):
                    notices.append(
                        "为避免误耗视频额度，本次只完成提示词准备，尚未提交视频模型。"
                        f"待确认提示词：{prompt[:1200]}\n"
                        "确认无误后请明确回复“提示词已确认，立即生成”。"
                    )
                    continue
                job = await create_video_job(
                    request.app.state.runtime,
                    user.id,
                    prompt,
                    str(arguments.get("seconds")) if arguments.get("seconds") else None,
                    str(arguments.get("size")) if arguments.get("size") else None,
                    (
                        str(arguments.get("resolution"))
                        if arguments.get("resolution")
                        else None
                    ),
                )
                background_tasks.add_task(
                    run_video_job,
                    request.app.state.runtime,
                    request.app.state.settings,
                    job.id,
                )
                video_jobs.append(video_job_payload(job))
                notices.append("视频生成任务已提交，可在对话中查看进度。")
            elif tool_call.get("name") == "generate_music":
                lyrics = str(arguments.get("lyrics", "")).strip() or None
                job = await create_music_job(
                    request.app.state.runtime,
                    user.id,
                    str(arguments.get("prompt", payload.message)),
                    lyrics,
                    bool(arguments.get("is_instrumental", True)),
                )
                background_tasks.add_task(
                    run_music_job,
                    request.app.state.runtime,
                    request.app.state.settings,
                    job.id,
                )
                music_jobs.append(music_job_payload(job))
                notices.append("音乐生成任务已提交，可在对话中查看进度。")
            elif tool_call.get("name") == "generate_speech":
                voice_id = str(arguments.get("voice_id", "")).strip() or None
                job = await create_speech_job(
                    request.app.state.runtime,
                    user.id,
                    str(arguments.get("text", payload.message)),
                    voice_id,
                    float(arguments.get("speed", 1.0)),
                    speaker=str(arguments.get("speaker", "")).strip() or None,
                    voice_role=str(arguments.get("voice_role", "")).strip() or None,
                    emotion=str(arguments.get("emotion", "calm")),
                )
                background_tasks.add_task(
                    run_speech_job,
                    request.app.state.runtime,
                    request.app.state.settings,
                    job.id,
                )
                speech_jobs.append(speech_job_payload(job))
                notices.append("语音配音任务已提交，可在对话中查看进度。")
            elif tool_call.get("name") == "start_director_production":
                full_production = director_full_production_requested(
                    payload.message,
                    arguments,
                )
                target_seconds = int(arguments.get("target_seconds", 60))
                if target_seconds not in {30, 60, 180, 300}:
                    target_seconds = 60
                aspect_ratio = str(arguments.get("aspect_ratio", "9:16"))
                if aspect_ratio not in {"9:16", "16:9"}:
                    aspect_ratio = "9:16"
                resolution = str(arguments.get("resolution", "768P"))
                if resolution not in {"768P", "2K"}:
                    resolution = "768P"
                project = await create_director_project(
                    request.app.state.runtime,
                    request.app.state.settings,
                    user.id,
                    premise=str(arguments.get("premise", payload.message)),
                    target_seconds=target_seconds,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    visual_style=str(arguments.get("visual_style", "电影感写实")),
                    continuity_notes=str(arguments.get("continuity_notes", "")),
                    one_click=full_production,
                    story_confirmed=False,
                )
                director_projects.append(
                    await project_payload(request.app.state.runtime, project)
                )
                notices.append(
                    "导演故事草案已创建，但尚未调用任何视频模型。请到导演工作室核对故事、"
                    "时长、画幅与风格，确认后总导演会先进行至少两轮文本预演；只有评分达到"
                    " 90 分才会进入视频生成。"
                )
        if notices:
            result["content"] = "\n".join(filter(None, [result.get("content", ""), *notices]))
        elif not result.get("content"):
            result["content"] = "模型没有返回文本内容。"
        assistant_message = await record_assistant_message(
            request.app.state.runtime,
            user.id,
            prepared.conversation.id,
            str(result["content"]),
            str(result["channel"]),
            str(result["model"]),
            result.get("usage", {}),
        )
        background_tasks.add_task(
            learn_from_exchange,
            request.app.state.runtime,
            request.app.state.settings,
            user.id,
            prepared.user_message.id,
            payload.message.strip(),
            str(result["content"]),
            payload.model,
        )
        result["conversation_id"] = str(prepared.conversation.id)
        result["message_id"] = str(assistant_message.id)
        result["memory"] = {
            "items_used": memory_context.memory_count,
            "graph_edges_used": memory_context.graph_edge_count,
        }
        result["files"] = files
        result["video_jobs"] = video_jobs
        result["music_jobs"] = music_jobs
        result["speech_jobs"] = speech_jobs
        result["director_projects"] = director_projects
        result["documents"] = [uploaded_document_payload(record) for record in documents]
        result["skills_used"] = ["document-understanding"] if documents else []
        result["mcp_calls"] = mcp_calls
        return result
    except ModelRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ModelChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VideoChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MusicChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SpeechChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise HTTPException(status_code=502, detail="无法连接当前大模型渠道") from exc
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"大模型渠道返回错误（HTTP {exc.status_code}）",
        ) from exc
