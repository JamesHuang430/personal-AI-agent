from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import ChatRun, User
from assistant_app.services.agent_model_router import route_agent_models
from assistant_app.services.chat_runs import run_chat_request, update_chat_run
from assistant_app.services.chat_tools import execute_tools
from assistant_app.services.conversations import (
    ConversationNotFoundError,
    delete_conversation,
    get_conversation_messages,
    list_conversations,
    prepare_conversation,
    record_assistant_message,
)
from assistant_app.services.creative_preferences import personalization_prompt
from assistant_app.services.document_skill import (
    document_skill_payload,
    extract_document_context,
    get_owned_documents,
    uploaded_document_payload,
)
from assistant_app.services.mcp_runtime import list_mcp_tools
from assistant_app.services.memory import (
    organize_conversation_session,
    retrieve_memory_context,
)
from assistant_app.services.model_gateway import (
    ModelChannelUnavailableError,
    ModelRateLimitError,
    list_available_models,
)
from assistant_app.services.music_gateway import (
    MusicChannelUnavailableError,
)
from assistant_app.services.pi_runtime import PiRuntimeError, routed_chat_completion
from assistant_app.services.speech_gateway import (
    SpeechChannelUnavailableError,
)
from assistant_app.services.video_gateway import (
    VideoChannelUnavailableError,
)

router = APIRouter()


@router.get("/runs/{run_id}")
async def chat_run_status(
    run_id: UUID, request: Request, user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    async with request.app.state.runtime.sessions() as session:
        run = await session.scalar(select(ChatRun).where(
            ChatRun.id == run_id, ChatRun.user_id == user.id,
        ))
    if run is None:
        raise HTTPException(404, "请求记录不存在")
    return {
        "run_id": str(run.id), "status": run.status, "error": run.error,
        "conversation_id": str(run.conversation_id) if run.conversation_id else None,
        "result": run.response,
    }


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


async def _execute_chat(
    payload: ChatPayload,
    request: Request,
    user: User,
    run,
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
            artifacts={"documents": [uploaded_document_payload(d) for d in documents]},
        )
        await update_chat_run(
            request.app.state.runtime, run.id, conversation_id=prepared.conversation.id,
        )
        memory_context = await retrieve_memory_context(
            request.app.state.runtime,
            request.app.state.settings,
            user.id,
            payload.message.strip(),
        )
        result = await routed_chat_completion(
            request.app.state.runtime,
            request.app.state.settings,
            payload.model,
            payload.message.strip(),
            prepared.history,
            memory_context.text + personalization_prompt({
                "preferences": getattr(user, "creative_preferences", None) or {},
            }),
            document_context,
        )
        async def checkpoint(artifacts):
            owned = await update_chat_run(request.app.state.runtime, run.id, response=artifacts)
            if not owned:
                raise HTTPException(409, "请求已失效，已停止后续工具执行")

        calls = result.pop("tool_calls", [])
        for call in calls:
            arguments = call.get("arguments")
            if call.get("name") == "start_director_production" and isinstance(arguments, dict):
                arguments["one_click"] = director_full_production_requested(
                    payload.message, arguments,
                )
        artifacts, notices = await execute_tools(
            request.app.state.runtime, request.app.state.settings, user.id,
            calls, checkpoint,
        )
        result.update(artifacts)
        if notices:
            result["content"] = "\n".join(filter(None, [result.get("content", ""), *notices]))
        elif not result.get("content"):
            result["content"] = "模型没有返回文本内容。"
        result["documents"] = [uploaded_document_payload(record) for record in documents]
        assistant_message = await record_assistant_message(
            request.app.state.runtime,
            user.id,
            prepared.conversation.id,
            str(result["content"]),
            str(result["channel"]),
            str(result["model"]),
            result.get("usage", {}),
            artifacts={k: v for k, v in result.items() if k != "content"},
            learning={
                "user_id": str(user.id),
                "source_message_id": str(prepared.user_message.id),
                "user_text": payload.message.strip(),
                "assistant_text": str(result["content"]),
                "model_name": payload.model,
            },
        )
        result["conversation_id"] = str(prepared.conversation.id)
        result["message_id"] = str(assistant_message.id)
        result["memory"] = {
            "items_used": memory_context.memory_count,
            "graph_edges_used": memory_context.graph_edge_count,
        }
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
    except PiRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"大模型渠道返回错误（HTTP {exc.status_code}）",
        ) from exc


@router.post("")
async def chat(
    payload: ChatPayload, request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    return await run_chat_request(
        request.app.state.runtime, user.id,
        request.headers.get("Idempotency-Key"), payload.model_dump(mode="json"),
        lambda run: _execute_chat(payload, request, user, run),
    )
