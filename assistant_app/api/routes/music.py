from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import MusicJob, User
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.music_gateway import (
    MusicChannelUnavailableError,
    create_music_job,
    music_job_payload,
)

router = APIRouter()


class MusicCreatePayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    lyrics: str | None = Field(default=None, max_length=6000)
    is_instrumental: bool = True


async def _owned_job(runtime: RuntimeDependencies, user_id: UUID, job_id: UUID) -> MusicJob:
    async with runtime.sessions() as session:
        job = await session.scalar(
            select(MusicJob).where(MusicJob.id == job_id, MusicJob.user_id == user_id)
        )
    if job is None:
        raise HTTPException(status_code=404, detail="音乐任务不存在")
    return job


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_music(
    payload: MusicCreatePayload,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        job = await create_music_job(
            request.app.state.runtime,
            user.id,
            payload.prompt.strip(),
            payload.lyrics.strip() if payload.lyrics else None,
            payload.is_instrumental,
        )
    except MusicChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return music_job_payload(job)


@router.get("")
async def list_music_jobs(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> list[dict[str, object]]:
    async with request.app.state.runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(MusicJob)
                .where(MusicJob.user_id == user.id)
                .order_by(desc(MusicJob.created_at))
                .limit(30)
            )
        ).all()
    return [music_job_payload(row) for row in rows]


@router.get("/{job_id}")
async def music_job(
    job_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    return music_job_payload(await _owned_job(request.app.state.runtime, user.id, job_id))


@router.get("/{job_id}/download", response_class=FileResponse)
async def download_music(
    job_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> FileResponse:
    job = await _owned_job(request.app.state.runtime, user.id, job_id)
    available = job.storage_path and await asyncio.to_thread(Path(job.storage_path).is_file)
    if job.status != "completed" or not available:
        raise HTTPException(status_code=409, detail="音乐尚未生成完成")
    media_type = "audio/mpeg" if job.audio_format == "mp3" else f"audio/{job.audio_format}"
    return FileResponse(
        job.storage_path,
        media_type=media_type,
        filename=f"music-{job.id}.{job.audio_format}",
    )
