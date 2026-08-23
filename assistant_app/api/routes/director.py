from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import User
from assistant_app.services.director import (
    DirectorProjectNotFoundError,
    create_director_project,
    get_director_project,
    list_director_projects,
    project_payload,
    run_director_project,
)
from assistant_app.services.model_gateway import ModelChannelUnavailableError

router = APIRouter()


class DirectorProjectCreatePayload(BaseModel):
    premise: str = Field(min_length=4, max_length=8_000)
    target_seconds: Literal[30, 60, 180, 300] = 60
    aspect_ratio: Literal["9:16", "16:9"] = "9:16"
    visual_style: str = Field(default="电影感写实", min_length=2, max_length=100)
    continuity_notes: str = Field(default="", max_length=8_000)
    one_click: bool = False


@router.post("/projects", status_code=status.HTTP_202_ACCEPTED)
async def start_director_project(
    payload: DirectorProjectCreatePayload,
    request: Request,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        project = await create_director_project(
            request.app.state.runtime,
            request.app.state.settings,
            user.id,
            payload.premise.strip(),
            payload.target_seconds,
            payload.aspect_ratio,
            payload.visual_style.strip(),
            payload.continuity_notes.strip(),
            payload.one_click,
        )
    except ModelChannelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (APIConnectionError, APITimeoutError) as exc:
        raise HTTPException(status_code=502, detail="无法获取导演 Agent 可用模型") from exc
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"文本模型渠道返回错误（HTTP {exc.status_code}）",
        ) from exc
    background_tasks.add_task(
        run_director_project,
        request.app.state.runtime,
        request.app.state.settings,
        project.id,
    )
    return await project_payload(request.app.state.runtime, project)


async def _owned_project(project_id: UUID, request: Request, user: User):
    try:
        return await get_director_project(request.app.state.runtime, user.id, project_id)
    except DirectorProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/projects/{project_id}/download", response_class=FileResponse)
async def download_director_movie(
    project_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> FileResponse:
    project = await _owned_project(project_id, request, user)
    available = project.final_video_path and await asyncio.to_thread(
        Path(project.final_video_path).is_file
    )
    if project.status != "completed" or not available:
        raise HTTPException(status_code=409, detail="一键成片尚未生成完成")
    return FileResponse(
        project.final_video_path,
        media_type="video/mp4",
        filename=f"director-{project.id}.mp4",
    )


@router.get("/projects/{project_id}/preview", response_class=FileResponse)
async def preview_director_movie(
    project_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> FileResponse:
    project = await _owned_project(project_id, request, user)
    available = project.final_video_path and await asyncio.to_thread(
        Path(project.final_video_path).is_file
    )
    if project.status != "completed" or not available:
        raise HTTPException(status_code=409, detail="一键成片尚未生成完成")
    return FileResponse(project.final_video_path, media_type="video/mp4")


@router.get("/projects")
async def director_projects(
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> list[dict[str, object]]:
    projects = await list_director_projects(request.app.state.runtime, user.id)
    return [await project_payload(request.app.state.runtime, project) for project in projects]


@router.get("/projects/{project_id}")
async def director_project(
    project_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        project = await get_director_project(
            request.app.state.runtime,
            user.id,
            project_id,
        )
    except DirectorProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await project_payload(request.app.state.runtime, project)
