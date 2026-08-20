from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import User
from assistant_app.services.generated_files import create_generated_file, file_payload
from assistant_app.services.model_gateway import (
    ModelChannelUnavailableError,
    ModelRateLimitError,
    chat_completion,
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
    message: str = Field(min_length=1, max_length=8_000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=20)


@router.post("")
async def chat(
    payload: ChatPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        result = await chat_completion(
            request.app.state.runtime,
            request.app.state.settings,
            payload.message.strip(),
            [item.model_dump() for item in payload.history],
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
        result["files"] = files
        result["video_jobs"] = video_jobs
        return result
    except ModelRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ModelChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VideoChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise HTTPException(status_code=502, detail="无法连接当前大模型渠道") from exc
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"大模型渠道返回错误（HTTP {exc.status_code}）",
        ) from exc
