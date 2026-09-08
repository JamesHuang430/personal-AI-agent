from __future__ import annotations

import hmac
import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from assistant_app.db.models import ModelChannel
from assistant_app.services.activity import emit_activity, pi_activity_target
from assistant_app.services.model_gateway import ModelRateLimitError, _enforce_qps
from assistant_app.services.web_search import WebSearchError, fetch_webpage, search_web

router = APIRouter()


class PiToolExecutionPayload(BaseModel):
    run_id: UUID
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("/tools/execute", include_in_schema=False)
async def execute_pi_tool(
    payload: PiToolExecutionPayload,
    request: Request,
    runtime_secret: Annotated[
        str | None, Header(alias="X-Pi-Runtime-Secret")
    ] = None,
) -> dict[str, object]:
    settings = request.app.state.settings
    expected = settings.pi_runtime_shared_secret
    if not expected or not runtime_secret or not hmac.compare_digest(expected, runtime_secret):
        raise HTTPException(status_code=401, detail="Pi Runtime authentication failed")

    allowed_key = f"pi-runtime:web-urls:{payload.run_id}"
    runtime = request.app.state.runtime
    target = await pi_activity_target(runtime, payload.run_id)
    started = time.perf_counter()

    async def event(state, detail):
        if target:
            await emit_activity(
                runtime,
                "模型请求许可" if payload.name == "model_request_permit" else payload.name,
                state, kind="model" if payload.name == "model_request_permit" else "tool",
                detail=detail, run_id=target,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )

    await event("processing", "Pi 正在请求执行")
    try:
        if payload.name == "model_request_permit":
            channel_id = await request.app.state.runtime.redis.get(
                f"pi-runtime:channel:{payload.run_id}"
            )
            if not channel_id:
                raise HTTPException(403, "Pi run expired or unknown")
            async with request.app.state.runtime.sessions() as session:
                channel = await session.scalar(select(ModelChannel).where(
                    ModelChannel.id == UUID(channel_id),
                ))
            if channel is None:
                raise HTTPException(503, "Model channel unavailable")
            await _enforce_qps(request.app.state.runtime, channel)
            result = {"allowed": True}
        elif payload.name == "web_search":
            result = await search_web(
                settings,
                str(payload.arguments.get("query", "")),
                topic=str(payload.arguments.get("topic", "general")),
                time_range=str(payload.arguments.get("time_range", "all")),
                max_results=(
                    int(payload.arguments["max_results"])
                    if payload.arguments.get("max_results") is not None
                    else None
                ),
            )
            urls = [
                str(item["url"])
                for item in result["results"]
                if isinstance(item, dict) and item.get("url")
            ]
            if urls:
                await request.app.state.runtime.redis.sadd(allowed_key, *urls)
                await request.app.state.runtime.redis.expire(allowed_key, 600)
        elif payload.name == "fetch_webpage":
            url = str(payload.arguments.get("url", "")).strip()
            if not url or not await request.app.state.runtime.redis.sismember(allowed_key, url):
                raise WebSearchError("只能读取本轮搜索结果中已经返回的链接")
            result = await fetch_webpage(settings, url)
        else:
            raise HTTPException(status_code=404, detail="Tool is not allowed through Pi Runtime")
    except (ValueError, WebSearchError, ModelRateLimitError) as exc:
        await event("failed", f"执行失败：{type(exc).__name__}")
        return {"is_error": True, "message": str(exc)}
    except TimeoutError:
        await event("failed", "执行超时")
        return {"is_error": True, "message": "联网检索超时"}
    except Exception:
        await event("failed", "执行失败，请检查渠道或工具配置")
        raise
    await event("completed", "请求许可已发放" if payload.name == "model_request_permit"
                else "工具结果已返回主 Agent")
    return {"is_error": False, "data": result}
