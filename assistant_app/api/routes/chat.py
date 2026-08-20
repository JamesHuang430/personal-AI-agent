from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import User
from assistant_app.services.model_gateway import (
    ModelChannelUnavailableError,
    ModelRateLimitError,
    chat_completion,
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
    _user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        return await chat_completion(
            request.app.state.runtime,
            request.app.state.settings,
            payload.message.strip(),
            [item.model_dump() for item in payload.history],
        )
    except ModelRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ModelChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise HTTPException(status_code=502, detail="无法连接当前大模型渠道") from exc
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"大模型渠道返回错误（HTTP {exc.status_code}）",
        ) from exc
