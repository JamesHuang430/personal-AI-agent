from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from assistant_app.db.models import MemoryItem
from assistant_app.services.conversations import (
    ConversationNotFoundError,
    delete_conversation,
)


class FakeSession:
    def __init__(self, conversation: object | None) -> None:
        self.conversation = conversation
        self.deleted: object | None = None

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> FakeSession:
        return self

    async def scalar(self, _statement: object) -> object | None:
        return self.conversation

    async def delete(self, conversation: object) -> None:
        self.deleted = conversation


@pytest.mark.asyncio
async def test_delete_conversation_removes_owned_conversation() -> None:
    conversation = object()
    session = FakeSession(conversation)
    runtime = SimpleNamespace(sessions=lambda: session)

    await delete_conversation(runtime, uuid4(), uuid4())

    assert session.deleted is conversation


@pytest.mark.asyncio
async def test_delete_conversation_rejects_missing_or_unowned_conversation() -> None:
    session = FakeSession(None)
    runtime = SimpleNamespace(sessions=lambda: session)

    with pytest.raises(ConversationNotFoundError, match="无权访问"):
        await delete_conversation(runtime, uuid4(), uuid4())

    assert session.deleted is None


def test_deleting_source_message_preserves_organized_memory() -> None:
    source_message_fk = next(iter(MemoryItem.__table__.c.source_message_id.foreign_keys))

    assert source_message_fk.ondelete == "SET NULL"
