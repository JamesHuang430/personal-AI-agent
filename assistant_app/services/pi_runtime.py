from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from assistant_app.core.config import Settings
from assistant_app.core.encryption import decrypt_secret
from assistant_app.core.request_context import current_request_actor, current_request_id
from assistant_app.db.models import ModelChannel
from assistant_app.db.runtime import DependencyStatus, RuntimeDependencies
from assistant_app.services.model_gateway import (
    AGENT_SYSTEM_PROMPT,
    AGENT_TOOLS,
    WEB_TOOL_NAMES,
    ModelChannelUnavailableError,
)
from assistant_app.services.request_logging import record_request_log


class PiRuntimeError(RuntimeError):
    pass


async def pi_runtime_readiness(settings: Settings) -> DependencyStatus:
    try:
        timeout = httpx.Timeout(settings.dependency_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(f"{settings.pi_runtime_url.rstrip('/')}/health")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise PiRuntimeError("misconfigured")
        return DependencyStatus(status="ok")
    except Exception as exc:  # readiness reports dependency failure without crashing
        return DependencyStatus(status="error", detail=type(exc).__name__)


async def pi_chat_completion(
    runtime: RuntimeDependencies,
    settings: Settings,
    model_name: str,
    message: str,
    history: list[dict[str, str]],
    memory_context: str = "",
    document_context: str = "",
) -> dict[str, Any]:
    """Run the generic agent loop in the isolated Pi sidecar."""

    async with runtime.sessions() as session:
        channel = await session.scalar(select(ModelChannel).where(ModelChannel.is_active.is_(True)))
    if channel is None:
        raise ModelChannelUnavailableError("运营后台尚未启用文本模型渠道")

    run_id = str(uuid4())
    await runtime.redis.set(f"pi-runtime:channel:{run_id}", str(channel.id), ex=900)
    system_parts = [AGENT_SYSTEM_PROMPT]
    if memory_context:
        system_parts.append(memory_context)
    if document_context:
        system_parts.append(document_context)
    tools = [
        tool
        for tool in AGENT_TOOLS
        if settings.web_search_enabled or tool["function"]["name"] not in WEB_TOOL_NAMES
    ]
    request_payload = {
        "run_id": run_id,
        "model": model_name,
        "base_url": channel.base_url,
        "api_key": decrypt_secret(channel.encrypted_api_key, settings.secret_key),
        "system_prompt": "\n\n".join(system_parts),
        "message": message,
        "history": history[-20:],
        "tools": tools,
        "max_turns": 8,
        "require_model_permit": True,
    }
    started = time.perf_counter()
    request_id = current_request_id() or str(uuid4())
    safe_input = {
        key: value
        for key, value in request_payload.items()
        if key not in {"api_key", "system_prompt"}
    }
    try:
        timeout = httpx.Timeout(settings.pi_runtime_timeout_seconds, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{settings.pi_runtime_url.rstrip('/')}/v1/runs",
                headers={"X-Pi-Runtime-Secret": settings.pi_runtime_shared_secret},
                json=request_payload,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise PiRuntimeError("Pi Runtime 返回了无法识别的数据")
    except (httpx.HTTPError, ValueError, PiRuntimeError) as exc:
        await record_request_log(
            runtime,
            request_id=request_id,
            category="model",
            source="pi-runtime",
            actor=current_request_actor(),
            status_code=502,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            model_name=model_name,
            input_payload=safe_input,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise PiRuntimeError("Pi Agent Runtime 暂时不可用") from exc

    result = {
        "content": payload["content"].strip(),
        "channel": channel.name,
        "model": model_name,
        "tool_calls": payload.get("tool_calls", [])[:3],
        "web_sources": payload.get("web_sources", [])[:10],
        "usage": payload.get("usage", {}),
    }
    await record_request_log(
        runtime,
        request_id=request_id,
        category="model",
        source="pi-runtime",
        actor=current_request_actor(),
        status_code=200,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        model_name=model_name,
        input_payload=safe_input,
        output_payload=result,
    )
    return result


async def routed_chat_completion(
    runtime: RuntimeDependencies,
    settings: Settings,
    model_name: str,
    message: str,
    history: list[dict[str, str]],
    memory_context: str = "",
    document_context: str = "",
) -> dict[str, Any]:
    if settings.agent_runtime == "pi":
        return await pi_chat_completion(
            runtime,
            settings,
            model_name,
            message,
            history,
            memory_context,
            document_context,
        )

    from assistant_app.services.model_gateway import chat_completion

    return await chat_completion(
        runtime,
        settings,
        model_name,
        message,
        history,
        memory_context,
        document_context,
    )
