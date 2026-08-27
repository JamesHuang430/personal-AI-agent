from __future__ import annotations

import json
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


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "start_director_production",
            "description": (
                "用户明确要求启动导演工作室、调用总导演和各专业 Agent、制作短剧或电影时，"
                "创建一个持久化导演项目。普通模式生成一个带独立配音和烧录字幕的预览镜头；"
                "用户明确说一键成片时才逐镜生成视频与语音、烧录字幕并自动合片。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premise": {"type": "string", "description": "故事创意、人物与核心冲突"},
                    "target_seconds": {
                        "type": "integer",
                        "enum": [30, 60, 180, 300],
                        "description": "目标总时长，默认 60 秒",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["9:16", "16:9"],
                        "description": "竖屏或横屏画幅",
                    },
                    "visual_style": {
                        "type": "string",
                        "description": "视觉风格，默认电影感写实",
                    },
                    "continuity_notes": {
                        "type": "string",
                        "description": "角色关系、外貌、服装、voice_id、定妆照 URL 等锁定信息",
                    },
                    "one_click": {
                        "type": "boolean",
                        "description": "是否直接逐镜生成并合片；只有用户明确要求一键成片时为 true",
                    },
                },
                "required": ["premise"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "根据用户要求生成可下载的文本类文件。用户明确要求生成、导出或保存文件时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名，扩展名限 md、txt、csv、json、html",
                    },
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["filename", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "用户明确要求生成视频时，提交异步视频生成任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "适合视频模型的详细画面提示词"},
                    "seconds": {"type": "string", "enum": ["4", "8", "12"]},
                    "size": {
                        "type": "string",
                        "enum": ["720x1280", "1280x720", "1024x1792", "1792x1024"],
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_speech",
            "description": (
                "用户明确要求旁白、对白配音、固定声线或为视频补配音时生成语音。"
                "普通 H3 视频优先保留原生音轨，不要自动重复配音。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "需要朗读的对白或旁白正文"},
                    "voice_id": {
                        "type": "string",
                        "description": "可选 MiniMax voice_id；省略时使用运营后台默认声线",
                    },
                    "speed": {
                        "type": "number",
                        "minimum": 0.5,
                        "maximum": 2.0,
                        "description": "语速，默认 1.0",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_music",
            "description": "用户明确要求生成主题曲、配乐、背景音乐或氛围音乐时提交任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "音乐风格、情绪和使用场景"},
                    "lyrics": {"type": "string", "description": "可选歌词"},
                    "is_instrumental": {
                        "type": "boolean",
                        "description": "纯配乐为 true，带人声歌曲为 false",
                    },
                },
                "required": ["prompt", "is_instrumental"],
                "additionalProperties": False,
            },
        },
    },
]


async def _enforce_qps(runtime: RuntimeDependencies, channel: ModelChannel) -> None:
    window = int(time.time())
    key = f"model:qps:{channel.id}:{window}"
    async with runtime.redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 2)
        count, _ = await pipe.execute()
    if count > channel.qps_limit:
        raise ModelRateLimitError("当前模型请求较多，请稍后重试")


async def list_available_models(
    runtime: RuntimeDependencies,
    settings: Settings,
) -> tuple[str, list[str]]:
    """Discover models from the active OpenAI-compatible channel."""

    async with runtime.sessions() as session:
        channel = await session.scalar(select(ModelChannel).where(ModelChannel.is_active.is_(True)))
    if channel is None:
        raise ModelChannelUnavailableError("运营后台尚未启用文本模型渠道")

    api_key = decrypt_secret(channel.encrypted_api_key, settings.secret_key)
    async with AsyncOpenAI(api_key=api_key, base_url=channel.base_url, timeout=20) as client:
        page = await client.models.list()
    model_names = sorted(
        {
            item.id.strip()
            for item in page.data
            if isinstance(item.id, str) and 0 < len(item.id.strip()) <= 200
        }
    )
    return channel.name, model_names


async def chat_completion(
    runtime: RuntimeDependencies,
    settings: Settings,
    model_name: str,
    message: str,
    history: list[dict[str, str]],
    memory_context: str = "",
) -> dict[str, Any]:
    async with runtime.sessions() as session:
        channel = await session.scalar(select(ModelChannel).where(ModelChannel.is_active.is_(True)))
    if channel is None:
        raise ModelChannelUnavailableError("运营后台尚未启用文本模型渠道")
    await _enforce_qps(runtime, channel)
    api_key = decrypt_secret(channel.encrypted_api_key, settings.secret_key)
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个可靠、友善的私人 AI 助理。使用简体中文回答，"
                "不知道的信息要明确说明，不要虚构机票、火车票或实时数据。"
                "用户明确要求生成或导出文件时调用 create_file；明确要求生成视频时调用"
                " generate_video；明确要求主题曲、配乐或背景音乐时调用 generate_music。"
                "用户要求启动导演工作室、调用各个 Agent、制作短剧或电影时，必须调用"
                " start_director_production，不要只写一篇故事或口头描述流程；导演项目会自行"
                "生成带独立配音和烧录字幕的首个预览镜头，此时不要再重复调用 generate_video"
                " 或 generate_speech。用户明确说‘一键成片’"
                "时，将 one_click 设为 true；否则必须为 false，避免未经确认批量消耗视频额度。"
                "明确要求旁白、对白、固定声线或补配音时调用 generate_speech；普通 H3 视频"
                "优先保留原生音轨，不要重复配音。不要声称已经生成文件、视频、语音或音乐，"
                "必须实际调用对应工具。"
            ),
        },
    ]
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    messages.extend([*history[-20:], {"role": "user", "content": message}])
    async with AsyncOpenAI(api_key=api_key, base_url=channel.base_url, timeout=60) as client:
        completion = await client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=AGENT_TOOLS,
            tool_choice="auto",
        )
    response_message = completion.choices[0].message
    tool_calls: list[dict[str, Any]] = []
    for call in response_message.tool_calls or []:
        if call.type != "function":
            continue
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            continue
        tool_calls.append({"name": call.function.name, "arguments": arguments})

    content = response_message.content or ""
    usage = completion.usage
    return {
        "content": content,
        "channel": channel.name,
        "model": model_name,
        "tool_calls": tool_calls[:3],
        "usage": {
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
        },
    }


async def agent_text_completion(
    runtime: RuntimeDependencies,
    settings: Settings,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    """Run one visible director-agent pass without exposing hidden chain of thought."""

    async with runtime.sessions() as session:
        channel = await session.scalar(select(ModelChannel).where(ModelChannel.is_active.is_(True)))
    if channel is None:
        raise ModelChannelUnavailableError("运营后台尚未启用文本模型渠道")
    await _enforce_qps(runtime, channel)
    api_key = decrypt_secret(channel.encrypted_api_key, settings.secret_key)
    async with AsyncOpenAI(api_key=api_key, base_url=channel.base_url, timeout=120) as client:
        completion = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    content = completion.choices[0].message.content or ""
    if not content.strip():
        raise ValueError("Agent 没有返回可用交付物")
    return {"content": content.strip(), "channel": channel.name, "model": model_name}
