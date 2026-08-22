from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select

from assistant_app.db.models import Conversation, ConversationMessage
from assistant_app.db.runtime import RuntimeDependencies


class ConversationNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class PreparedConversation:
    conversation: Conversation
    user_message: ConversationMessage
    history: list[dict[str, str]]


def _title_from_message(message: str) -> str:
    title = " ".join(message.split()).strip()
    return (title[:60] + "…") if len(title) > 60 else (title or "新的对话")


async def prepare_conversation(
    runtime: RuntimeDependencies,
    user_id: UUID,
    conversation_id: UUID | None,
    message: str,
    model_name: str,
) -> PreparedConversation:
    now = datetime.now(UTC)
    async with runtime.sessions() as session, session.begin():
        conversation: Conversation | None = None
        if conversation_id is not None:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
            if conversation is None:
                raise ConversationNotFoundError("对话不存在或无权访问")
        else:
            conversation = Conversation(
                user_id=user_id,
                title=_title_from_message(message),
                last_message_at=now,
            )
            session.add(conversation)
            await session.flush()

        rows = (
            await session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.user_id == user_id,
                )
                .order_by(desc(ConversationMessage.created_at))
                .limit(20)
            )
        ).all()
        history = [
            {"role": item.role, "content": item.content}
            for item in reversed(rows)
            if item.role in {"user", "assistant"}
        ]
        user_message = ConversationMessage(
            conversation_id=conversation.id,
            user_id=user_id,
            role="user",
            content=message,
            model_name=model_name,
        )
        session.add(user_message)
        conversation.last_message_at = now
        conversation.updated_at = now
        await session.flush()

    return PreparedConversation(
        conversation=conversation,
        user_message=user_message,
        history=history,
    )


async def record_assistant_message(
    runtime: RuntimeDependencies,
    user_id: UUID,
    conversation_id: UUID,
    content: str,
    channel_name: str,
    model_name: str,
    usage: dict[str, int | None],
) -> ConversationMessage:
    now = datetime.now(UTC)
    async with runtime.sessions() as session, session.begin():
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        if conversation is None:
            raise ConversationNotFoundError("对话不存在或无权访问")
        record = ConversationMessage(
            conversation_id=conversation_id,
            user_id=user_id,
            role="assistant",
            content=content,
            channel_name=channel_name,
            model_name=model_name,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        session.add(record)
        conversation.last_message_at = now
        conversation.updated_at = now
        await session.flush()
    return record


def conversation_payload(item: Conversation) -> dict[str, object]:
    return {
        "id": str(item.id),
        "title": item.title,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "last_message_at": item.last_message_at.isoformat(),
    }


def message_payload(item: ConversationMessage) -> dict[str, object]:
    return {
        "id": str(item.id),
        "conversation_id": str(item.conversation_id),
        "role": item.role,
        "content": item.content,
        "channel": item.channel_name,
        "model": item.model_name,
        "usage": {
            "prompt_tokens": item.prompt_tokens,
            "completion_tokens": item.completion_tokens,
            "total_tokens": item.total_tokens,
        },
        "created_at": item.created_at.isoformat(),
    }


async def list_conversations(
    runtime: RuntimeDependencies,
    user_id: UUID,
    limit: int = 50,
) -> list[dict[str, object]]:
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(desc(Conversation.last_message_at))
                .limit(max(1, min(limit, 100)))
            )
        ).all()
    return [conversation_payload(item) for item in rows]


async def get_conversation_messages(
    runtime: RuntimeDependencies,
    user_id: UUID,
    conversation_id: UUID,
) -> dict[str, object]:
    async with runtime.sessions() as session:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        if conversation is None:
            raise ConversationNotFoundError("对话不存在或无权访问")
        messages = (
            await session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.user_id == user_id,
                )
                .order_by(ConversationMessage.created_at)
            )
        ).all()
    return {
        "conversation": conversation_payload(conversation),
        "messages": [message_payload(item) for item in messages],
    }


async def delete_conversation(
    runtime: RuntimeDependencies,
    user_id: UUID,
    conversation_id: UUID,
) -> None:
    """Delete one user's conversation while keeping organized long-term memories."""

    async with runtime.sessions() as session, session.begin():
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        if conversation is None:
            raise ConversationNotFoundError("对话不存在或无权访问")
        await session.delete(conversation)
