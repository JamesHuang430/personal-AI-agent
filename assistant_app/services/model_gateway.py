from __future__ import annotations

import time
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select

from assistant_app.core.config import Settings
from assistant_app.core.encryption import decrypt_secret
from assistant_app.db.models import ModelChannel
from assistant_app.db.runtime import RuntimeDependencies


class ModelChannelUnavailableError(RuntimeError):
    pass


class ModelRateLimitError(RuntimeError):
    pass


async def _enforce_qps(runtime: RuntimeDependencies, channel: ModelChannel) -> None:
    window = int(time.time())
    key = f"model:qps:{channel.id}:{window}"
    async with runtime.redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 2)
        count, _ = await pipe.execute()
    if count > channel.qps_limit:
        raise ModelRateLimitError("当前模型请求较多，请稍后重试")


async def chat_completion(
    runtime: RuntimeDependencies,
    settings: Settings,
    message: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    async with runtime.sessions() as session:
        channel = await session.scalar(select(ModelChannel).where(ModelChannel.is_active.is_(True)))
    if channel is None:
        raise ModelChannelUnavailableError("运营后台尚未启用大模型渠道")

    await _enforce_qps(runtime, channel)
    api_key = decrypt_secret(channel.encrypted_api_key, settings.secret_key)
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个可靠、友善的私人 AI 助理。使用简体中文回答，"
                "不知道的信息要明确说明，不要虚构机票、火车票或实时数据。"
            ),
        },
        *history[-20:],
        {"role": "user", "content": message},
    ]
    async with AsyncOpenAI(api_key=api_key, base_url=channel.base_url, timeout=60) as client:
        completion = await client.chat.completions.create(
            model=channel.model_name,
            messages=messages,
        )
    content = completion.choices[0].message.content or "模型没有返回文本内容。"
    usage = completion.usage
    return {
        "content": content,
        "channel": channel.name,
        "model": channel.model_name,
        "usage": {
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
        },
    }
