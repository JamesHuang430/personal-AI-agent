"""User-facing execution events, deliberately excluding model prompts and reasoning."""

from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

logger = logging.getLogger(__name__)
activity_run: ContextVar[str | None] = ContextVar("activity_run", default=None)
MAX_EVENTS = 200


async def emit_activity(runtime, name, status="completed", *, kind="stage", detail="",
                        duration_ms=None, run_id=None):
    target = run_id or activity_run.get()
    if not target:
        return
    event = {
        "id": str(uuid4()), "time": datetime.now(UTC).isoformat(),
        "name": str(name)[:200], "status": status, "kind": kind,
        "detail": str(detail)[:500], "duration_ms": duration_ms,
    }
    try:
        async with asyncio.timeout(1):
            async with runtime.redis.pipeline(transaction=True) as pipe:
                key = f"chat:activity:{target}"
                pipe.rpush(key, json.dumps(event, ensure_ascii=False))
                pipe.ltrim(key, -MAX_EVENTS, -1)
                pipe.expire(key, 86400)
                await pipe.execute()
    except Exception:
        logger.warning("activity_event_unavailable")


async def read_activity(runtime, run_id=None):
    target = run_id or activity_run.get()
    if not target:
        return []
    try:
        async with asyncio.timeout(1):
            rows = await runtime.redis.lrange(f"chat:activity:{target}", 0, -1)
        return [json.loads(row) for row in rows]
    except Exception:
        return []


async def pi_activity_target(runtime, run_id):
    try:
        async with asyncio.timeout(1):
            return await runtime.redis.get(f"pi-runtime:activity:{run_id}")
    except Exception:
        return None
