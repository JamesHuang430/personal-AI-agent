from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import DirectorShot, User
from assistant_app.services.creative_preferences import (
    CreativeFeedback,
    CreativePreferences,
    get_preferences,
    save_feedback,
    save_preferences,
)
from assistant_app.services.director import (
    DirectorProjectNotApprovableError,
    DirectorProjectNotFoundError,
    DirectorProjectNotRemasterableError,
    DirectorProjectNotResumableError,
    approve_storyboard,
    create_director_project,
    get_director_project,
    list_director_summaries,
    prepare_director_approval,
    prepare_director_remaster,
    prepare_director_resume,
    project_payload,
    update_director_draft,
)
from assistant_app.services.generated_files import GENERATED_ROOT
from assistant_app.services.model_gateway import ModelChannelUnavailableError
from assistant_app.services.speech_gateway import EDGE_FEMALE_VOICE_ID

router = APIRouter()


class DirectorProjectCreatePayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    premise: str = Field(min_length=4, max_length=8_000)
    target_seconds: Literal[4, 30, 60, 180, 300] = 60
    aspect_ratio: Literal["9:16", "16:9"] = "9:16"
    resolution: Literal["768P", "2K"] = "768P"
    visual_style: str = Field(default="", max_length=100)
    continuity_notes: str = Field(default="", max_length=8_000)
    one_click: bool = False
    story_confirmed: bool = False
    use_memory: bool = True


class DirectorDraftUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    premise: str = Field(min_length=4, max_length=8000)
    target_seconds: Literal[4, 30, 60, 180, 300]
    aspect_ratio: Literal["9:16", "16:9"]
    resolution: Literal["768P", "2K"]
    visual_style: str = Field(min_length=2, max_length=100)
    continuity_notes: str = Field(default="", max_length=8000)


class StoryboardApproval(BaseModel):
    storyboard_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get("/preferences")
async def read_creative_preferences(request: Request, user: Annotated[User, Depends(current_user)]):
    return (await get_preferences(request.app.state.runtime, user.id)).model_dump()


@router.put("/preferences")
async def write_creative_preferences(
    payload: CreativePreferences, request: Request, user: Annotated[User, Depends(current_user)]
):
    return (await save_preferences(request.app.state.runtime, user.id, payload)).model_dump()


@router.put("/projects/{project_id}/feedback")
async def review_project(
    project_id: UUID,
    payload: CreativeFeedback,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    try:
        project = await save_feedback(request.app.state.runtime, user.id, project_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await project_payload(request.app.state.runtime, project)


@router.patch("/projects/{project_id}")
async def edit_project(
    project_id: UUID,
    payload: DirectorDraftUpdate,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    try:
        project = await update_director_draft(
            request.app.state.runtime,
            user.id,
            project_id,
            payload.model_dump(),
        )
    except DirectorProjectNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except DirectorProjectNotApprovableError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await project_payload(request.app.state.runtime, project)


@router.post("/projects/{project_id}/approve-storyboard", status_code=202)
async def confirm_storyboard(
    project_id: UUID,
    payload: StoryboardApproval,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    try:
        project = await approve_storyboard(
            request.app.state.runtime,
            user.id,
            project_id,
            payload.storyboard_hash,
        )
    except DirectorProjectNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except DirectorProjectNotApprovableError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await project_payload(request.app.state.runtime, project)


class DirectorProjectRemasterPayload(BaseModel):
    voice_id: Literal["edge:zh-CN-XiaoxiaoNeural"] = EDGE_FEMALE_VOICE_ID


@router.post("/projects", status_code=status.HTTP_202_ACCEPTED)
async def start_director_project(
    payload: DirectorProjectCreatePayload,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        project = await create_director_project(
            request.app.state.runtime,
            request.app.state.settings,
            user.id,
            premise=payload.premise.strip(),
            target_seconds=payload.target_seconds,
            aspect_ratio=payload.aspect_ratio,
            resolution=payload.resolution,
            visual_style=payload.visual_style.strip(),
            continuity_notes=payload.continuity_notes.strip(),
            one_click=payload.one_click,
            story_confirmed=payload.story_confirmed,
            use_memory=payload.use_memory,
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


async def _rendered_shot_response(project_id, shot_id, request, user, *, download=False):
    await _owned_project(project_id, request, user)
    async with request.app.state.runtime.sessions() as session:
        shot = await session.scalar(
            select(DirectorShot).where(
                DirectorShot.id == shot_id,
                DirectorShot.project_id == project_id,
                DirectorShot.user_id == user.id,
            )
        )
    if shot is None:
        raise HTTPException(404, "镜头不存在")
    if shot.status != "completed" or not shot.rendered_path:
        raise HTTPException(409, "镜头尚未完成声音与字幕制作")
    path = await asyncio.to_thread(Path(shot.rendered_path).resolve)
    root = await asyncio.to_thread(GENERATED_ROOT.resolve)
    if root not in path.parents or not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "镜头文件不存在")
    return FileResponse(
        path, media_type="video/mp4", filename=f"shot-{shot.id}.mp4" if download else None
    )


@router.get("/projects/{project_id}/shots/{shot_id}/preview", response_class=FileResponse)
async def preview_rendered_shot(
    project_id: UUID, shot_id: UUID, request: Request, user: Annotated[User, Depends(current_user)]
):
    return await _rendered_shot_response(project_id, shot_id, request, user)


@router.get("/projects/{project_id}/shots/{shot_id}/download", response_class=FileResponse)
async def download_rendered_shot(
    project_id: UUID, shot_id: UUID, request: Request, user: Annotated[User, Depends(current_user)]
):
    return await _rendered_shot_response(project_id, shot_id, request, user, download=True)


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
    return await list_director_summaries(request.app.state.runtime, user.id)


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


@router.post("/projects/{project_id}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve_director_story(
    project_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        project = await prepare_director_approval(
            request.app.state.runtime,
            user.id,
            project_id,
        )
    except DirectorProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DirectorProjectNotApprovableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await project_payload(request.app.state.runtime, project)


@router.post("/projects/{project_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_director_project(
    project_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        project = await prepare_director_resume(
            request.app.state.runtime,
            user.id,
            project_id,
        )
    except DirectorProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DirectorProjectNotResumableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await project_payload(request.app.state.runtime, project)


@router.post("/projects/{project_id}/remaster", status_code=status.HTTP_202_ACCEPTED)
async def remaster_director_project(
    project_id: UUID,
    payload: DirectorProjectRemasterPayload,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, object]:
    try:
        project = await prepare_director_remaster(
            request.app.state.runtime,
            user.id,
            project_id,
        )
    except DirectorProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DirectorProjectNotRemasterableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await project_payload(request.app.state.runtime, project)
