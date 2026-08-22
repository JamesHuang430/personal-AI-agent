from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import text

from assistant_app.core.config import get_settings
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.age_graph import (
    GraphEntity,
    GraphRelation,
    load_memory_graph,
    upsert_memory_graph,
)


async def validate() -> None:
    settings = get_settings()
    runtime = RuntimeDependencies(settings)
    try:
        async with runtime.database.connect() as connection:
            distance = await connection.scalar(
                text("SELECT '[1,0]'::vector <=> '[1,0]'::vector")
            )
            tables = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class "
                    "WHERE relname IN ('conversations', 'conversation_messages', "
                    "'memory_items', 'memory_embeddings')"
                )
            )
        assert float(distance) == 0.0
        assert int(tables or 0) == 4

        user_id = uuid4()
        source_message_id = uuid4()
        await upsert_memory_graph(
            runtime,
            user_id,
            source_message_id,
            [
                GraphEntity("user", "Person", "测试用户"),
                GraphEntity("place", "Place", "杭州"),
            ],
            [GraphRelation("user", "LIKES", "place", 0.9)],
        )
        graph = await load_memory_graph(runtime, user_id)
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1
        assert graph["edges"][0]["label"] == "LIKES"
        print("memory-stack-ok: vector, relational tables, AGE nodes and edges")
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(validate())
