from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field, field_validator

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import User
from assistant_app.services.agent_model_router import route_agent_models
from assistant_app.services.conversations import (
    ConversationNotFoundError,
    get_conversation_messages,
    list_conversations,
    prepare_conversation,
    record_assistant_message,
)
from assistant_app.services.generated_files import create_generated_file, file_payload
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
from assistant_app.services.video_gateway import (
    VideoChannelUnavailableError,
    create_video_job,
    run_video_job,
    video_job_payload,
)

router = APIRouter()


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatPayload(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8_000)
    conversation_id: UUID | None = None
    history: list[HistoryMessage] = Field(default_factory=list, max_length=20)

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
        )
        files: list[dict[str, object]] = []
        video_jobs: list[dict[str, object]] = []
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
                job = await create_video_job(
                    request.app.state.runtime,
                    user.id,
                    str(arguments.get("prompt", payload.message)),
                    str(arguments.get("seconds")) if arguments.get("seconds") else None,
                    str(arguments.get("size")) if arguments.get("size") else None,
                )
                background_tasks.add_task(
                    run_video_job,
                    request.app.state.runtime,
                    request.app.state.settings,
                    job.id,
                )
                video_jobs.append(video_job_payload(job))
                notices.append("视频生成任务已提交，可在对话中查看进度。")
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
        return result
    except ModelRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ModelChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VideoChannelUnavailableError as exc:
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
