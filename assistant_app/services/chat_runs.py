from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update

from assistant_app.db.models import ChatRun, User
from assistant_app.services.activity import activity_run, emit_activity, read_activity


async def update_chat_run(runtime, run_id, **values) -> bool:
    async with runtime.sessions() as session, session.begin():
        result = await session.execute(
            update(ChatRun)
            .where(
                ChatRun.id == run_id,
                ChatRun.status == "processing",
            )
            .values(**values, heartbeat_at=datetime.now(UTC))
        )
        return result.rowcount == 1


async def reserve_chat_run(runtime, user_id, key, payload):
    key = key or str(uuid4())
    if not 1 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
        raise HTTPException(422, "Idempotency-Key 必须是 1–128 位可打印 ASCII 字符")
    fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    now = datetime.now(UTC)
    async with runtime.sessions() as session, session.begin():
        # Serializes reservations across processes without holding a connection during LLM work.
        await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        active = await session.scalar(
            select(ChatRun).where(
                ChatRun.user_id == user_id,
                ChatRun.status == "processing",
            )
        )
        if active and active.heartbeat_at < now - timedelta(seconds=90):
            active.status = "failed"
            active.error = "上次请求中断，已停止自动重试；请查看历史会话和已创建资源"
            active.error_status = 409
            await session.flush()
        existing = await session.scalar(
            select(ChatRun).where(
                ChatRun.user_id == user_id,
                ChatRun.idempotency_key == key,
            )
        )
        if existing:
            if existing.request_hash != fingerprint:
                raise HTTPException(409, "同一个 Idempotency-Key 不能用于不同请求")
            return existing, False
        if active and active.status == "processing":
            raise HTTPException(409, "当前账号有请求正在处理，请等待完成后再发送")
        run = ChatRun(
            id=uuid4(),
            user_id=user_id,
            idempotency_key=key,
            request_hash=fingerprint,
            status="processing",
            heartbeat_at=now,
        )
        session.add(run)
        return run, True


async def run_chat_request(runtime, user_id, key, payload, execute):
    run, created = await reserve_chat_run(runtime, user_id, key, payload)
    if not created:
        if run.status == "completed":
            return run.response
        raise HTTPException(
            run.error_status or 409,
            {
                "message": run.error or "该请求正在处理，请使用相同请求重试以获取结果",
                "run_id": str(run.id),
                "status": run.status,
                "conversation_id": str(run.conversation_id) if run.conversation_id else None,
                "artifacts": run.response or {},
            },
        )

    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            async with asyncio.timeout(10):
                if not await update_chat_run(runtime, run.id):
                    raise RuntimeError("Chat run ownership lost")

    # Explicit tasks preserve HTTPException rather than wrapping it in ExceptionGroup.
    token = activity_run.set(str(run.id))
    await emit_activity(runtime, "请求已接收", detail="主 Agent 开始处理本轮对话")
    work = asyncio.create_task(execute(run))
    monitor = asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(600):
            done, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            result = await work
        result["run_id"] = str(run.id)
        result["activity"] = await read_activity(runtime)
        await update_chat_run(runtime, run.id, status="completed", response=result)
        return result
    except BaseException as exc:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        message = str(exc.detail) if isinstance(exc, HTTPException) else "请求中断，请查看历史会话"
        await emit_activity(runtime, "请求未完成", "failed", detail="请检查错误提示后重试")
        async with runtime.sessions() as session:
            latest = await session.get(ChatRun, run.id)
            saved_response = dict(latest.response or {}) if latest else {}
        saved_response["activity"] = await read_activity(runtime)
        await update_chat_run(
            runtime,
            run.id,
            status="failed",
            response=saved_response,
            error=message,
            error_status=exc.status_code if isinstance(exc, HTTPException) else 409,
        )
        raise
    finally:
        monitor.cancel()
        work.cancel()
        await asyncio.gather(work, monitor, return_exceptions=True)
        activity_run.reset(token)
