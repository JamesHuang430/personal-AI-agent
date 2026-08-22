from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import SpeechJob, User
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.speech_gateway import (
    SpeechChannelUnavailableError,
    create_speech_job,
    run_speech_job,
    speech_job_payload,
)

router = APIRouter()


class SpeechCreatePayload(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    voice_id: str | None = Field(default=None, max_length=200)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


async def _owned_job(runtime: RuntimeDependencies, user_id: UUID, job_id: UUID) -> SpeechJob:
    async with runtime.sessions() as session:
        job = await session.scalar(
            select(SpeechJob).where(SpeechJob.id == job_id, SpeechJob.user_id == user_id)
        )
    if job is None:
        raise HTTPException(status_code=404, detail="语音任务不存在")
    return job


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_speech(
    payload: SpeechCreatePayload,
    request: Request,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        job = await create_speech_job(
            request.app.state.runtime,
            user.id,
            payload.text.strip(),
            payload.voice_id.strip() if payload.voice_id else None,
            payload.speed,
        )
    except SpeechChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    background_tasks.add_task(
        run_speech_job,
        request.app.state.runtime,
        request.app.state.settings,
        job.id,
    )
    return speech_job_payload(job)


@router.get("")
async def list_speech_jobs(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> list[dict[str, object]]:
    async with request.app.state.runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(SpeechJob)
                .where(SpeechJob.user_id == user.id)
                .order_by(desc(SpeechJob.created_at))
                .limit(30)
            )
        ).all()
    return [speech_job_payload(row) for row in rows]


@router.get("/{job_id}")
async def speech_job(
    job_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    return speech_job_payload(await _owned_job(request.app.state.runtime, user.id, job_id))


@router.get("/{job_id}/download", response_class=FileResponse)
async def download_speech(
    job_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> FileResponse:
    job = await _owned_job(request.app.state.runtime, user.id, job_id)
    available = job.storage_path and await asyncio.to_thread(Path(job.storage_path).is_file)
    if job.status != "completed" or not available:
        raise HTTPException(status_code=409, detail="语音尚未生成完成")
    media_type = "audio/mpeg" if job.audio_format == "mp3" else f"audio/{job.audio_format}"
    return FileResponse(
        job.storage_path,
        media_type=media_type,
        filename=f"speech-{job.id}.{job.audio_format}",
    )
