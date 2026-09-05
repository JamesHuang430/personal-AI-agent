"""Run with python -m assistant_app.worker; shares the API database and media volume."""

from __future__ import annotations

import asyncio
import logging

from assistant_app.core.config import get_settings
from assistant_app.core.logging import configure_logging
from assistant_app.db.models import DirectorProject, MusicJob, SpeechJob, VideoJob, WorkItem
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services import work_queue

logger = logging.getLogger(__name__)


async def execute(runtime, settings, item) -> None:
    from assistant_app.services.director import run_director_project, run_director_remaster
    from assistant_app.services.memory import learn_from_exchange
    from assistant_app.services.music_gateway import run_music_job
    from assistant_app.services.speech_gateway import run_speech_job
    from assistant_app.services.video_gateway import run_video_job

    handlers = {
        "video": run_video_job,
        "music": run_music_job,
        "speech": run_speech_job,
        "director": run_director_project,
        "remaster": run_director_remaster,
    }
    if item.kind == "memory":
        from uuid import UUID

        values = dict(item.payload)
        values["user_id"] = UUID(values["user_id"])
        values["source_message_id"] = UUID(values["source_message_id"])
        await learn_from_exchange(runtime, settings, **values)
    else:
        await handlers[item.kind](runtime, settings, item.resource_id, **item.payload)


async def fail_resource(runtime, item, reason: str) -> None:
    model = {
        "video": VideoJob,
        "music": MusicJob,
        "speech": SpeechJob,
        "director": DirectorProject,
        "remaster": DirectorProject,
    }.get(item.kind)
    if model is not None:
        async with runtime.sessions() as session, session.begin():
            record = await session.get(model, item.resource_id, with_for_update=True)
            lease = await session.get(WorkItem, item.id, with_for_update=True)
            if lease is None or lease.owner != item.owner:
                return
            if record is not None and record.status != "completed":
                record.status = "failed"
                record.error_message = reason[:500]


async def process(runtime, settings, item) -> None:
    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            async with asyncio.timeout(10):
                if not await work_queue.renew(runtime, item):
                    raise RuntimeError("Worker lease lost")

    if item.attempts > work_queue.MAX_ATTEMPTS:
        await fail_resource(runtime, item, "任务多次中断，请检查 worker 后继续制作")
        await work_queue.finish(runtime, item, "Recovery limit reached")
        return
    try:
        async with asyncio.TaskGroup() as group:
            monitor = group.create_task(heartbeat())
            # Overall deadline, including polling and rendering.
            async with asyncio.timeout(7200):
                await execute(runtime, settings, item)
            monitor.cancel()
    except Exception as exc:
        logger.exception("worker_task_failed", extra={"job_id": str(item.id)})
        await fail_resource(runtime, item, f"任务执行失败：{type(exc).__name__}")
        await work_queue.finish(runtime, item, type(exc).__name__)
    else:
        model = {
            "video": VideoJob,
            "music": MusicJob,
            "speech": SpeechJob,
            "director": DirectorProject,
            "remaster": DirectorProject,
        }.get(item.kind)
        error = None
        if model:
            async with runtime.sessions() as session:
                record = await session.get(model, item.resource_id)
                if record and record.status == "failed":
                    error = record.error_message or "Business task failed"
        await work_queue.finish(runtime, item, error)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    runtime = RuntimeDependencies(settings)

    async def consume():
        while True:
            try:
                item = await work_queue.claim(runtime)
                if item is not None:
                    await process(runtime, settings, item)
                else:
                    await asyncio.sleep(2)
            except Exception:
                logger.exception("worker_poll_failed")
                await asyncio.sleep(5)

    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(2):
                group.create_task(consume())
    finally:
        await runtime.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
