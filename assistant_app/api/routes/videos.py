from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import desc, select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import User, VideoJob
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.video_gateway import video_job_payload

router = APIRouter()


async def _owned_job(runtime: RuntimeDependencies, user_id: UUID, job_id: UUID) -> VideoJob:
    async with runtime.sessions() as session:
        job = await session.scalar(
            select(VideoJob).where(VideoJob.id == job_id, VideoJob.user_id == user_id)
        )
    if job is None:
        raise HTTPException(status_code=404, detail="视频任务不存在")
    return job


@router.get("")
async def list_video_jobs(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(VideoJob)
                .where(VideoJob.user_id == user.id)
                .order_by(desc(VideoJob.created_at))
                .limit(30)
            )
        ).all()
    return [video_job_payload(row) for row in rows]


@router.get("/{job_id}")
async def video_job(
    job_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    job = await _owned_job(request.app.state.runtime, user.id, job_id)
    return video_job_payload(job)


@router.get("/{job_id}/download", response_class=FileResponse)
async def download_video(
    job_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> FileResponse:
    job = await _owned_job(request.app.state.runtime, user.id, job_id)
    available = job.storage_path and await asyncio.to_thread(Path(job.storage_path).is_file)
    if job.status != "completed" or not available:
        raise HTTPException(status_code=409, detail="视频尚未生成完成")
    return FileResponse(job.storage_path, media_type="video/mp4", filename=f"video-{job.id}.mp4")
