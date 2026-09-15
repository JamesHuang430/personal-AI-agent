"""User-owned director sound controls. Auditions are explicit queued media jobs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import (
    DirectorAgentRun,
    DirectorProject,
    DirectorShot,
    SpeechChannel,
    User,
)
from assistant_app.services.director import (
    _fit_speech_text,
    _project_durations,
    project_payload,
    storyboard_hash,
)
from assistant_app.services.director_audio import (
    EDGE_VOICES,
    AudioSettings,
    audio_settings,
    reserve_speech,
    speech_options,
    validate_audio_assets,
)
from assistant_app.services.speech_gateway import speech_job_payload

router = APIRouter()


class SoundUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: AudioSettings
    storyboard_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AuditionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storyboard_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get("/audio-options")
async def audio_options(request: Request, user: Annotated[User, Depends(current_user)]):
    async with request.app.state.runtime.sessions() as session:
        channel = await session.scalar(
            select(SpeechChannel).where(SpeechChannel.is_active.is_(True))
        )
    voices = [{"id": key, "name": label, "mode": "edge"} for key, label in EDGE_VOICES.items()]
    if channel:
        voices.append(
            {
                "id": channel.default_voice_id,
                "name": f"渠道默认音色 · {channel.name}",
                "mode": "minimax",
            }
        )
    return {"voices": voices, "note": "试听由你点击后才提交；MiniMax 试听可能收费，Edge 需要联网。"}


async def editable(session, user_id, project_id, digest):
    project = await session.scalar(
        select(DirectorProject)
        .where(DirectorProject.id == project_id, DirectorProject.user_id == user_id)
        .with_for_update()
    )
    if not project:
        raise HTTPException(404, "项目不存在")
    visual = await session.scalar(
        select(DirectorAgentRun).where(
            DirectorAgentRun.project_id == project_id, DirectorAgentRun.agent_key == "visual"
        )
    )
    if storyboard_hash(project, visual.result_data if visual else {}) != digest:
        raise HTTPException(409, "制作方案已变化，请关闭面板并刷新后重试")
    if project.status not in {"awaiting_confirmation", "awaiting_storyboard"}:
        raise HTTPException(409, "只能在制作前设置声音；已启动的作品请新建一版")
    prepared = await session.scalar(
        select(DirectorShot.id).where(
            DirectorShot.project_id == project_id, DirectorShot.speech_job_id.is_not(None)
        )
    )
    if prepared or project.current_stage == "whiteboard_annotation_review":
        raise HTTPException(409, "配音已用于媒体制作，不能覆盖；请新建一版")
    return project, visual


@router.put("/projects/{project_id}/sound")
async def save_sound(
    project_id: UUID,
    payload: SoundUpdate,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    runtime = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        project, _ = await editable(session, user.id, project_id, payload.storyboard_hash)
        try:
            await validate_audio_assets(session, user.id, payload.settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        private = {k: v for k, v in (project.postproduction or {}).items() if k.startswith("_")}
        project.postproduction = payload.settings.model_dump(mode="json") | private
        project.storyboard_approved = False
    return await project_payload(runtime, project)


@router.post("/projects/{project_id}/auditions/{sequence}", status_code=202)
async def audition(
    project_id: UUID,
    sequence: int,
    payload: AuditionRequest,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    runtime = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        project, visual = await editable(session, user.id, project_id, payload.storyboard_hash)
        if project.status != "awaiting_storyboard" or not visual:
            raise HTTPException(409, "分镜完成后才能试听实际台词")
        plan = (visual.result_data or {}).get("shots", [])
        if not 1 <= sequence <= len(plan):
            raise HTTPException(404, "分镜不存在")
        if audio_settings(project).voice_mode == "auto":
            raise HTTPException(422, "请先选择明确音色并保存；自动模式可能使用视频原声")
        # Rate limit explicit generation actions; no provider call is made by this endpoint.
        key = f"director:audition:{user.id}"
        count = await runtime.redis.incr(key)
        if count == 1:
            await runtime.redis.expire(key, 60)
        if count > 10:
            raise HTTPException(429, "试听请求过多，请一分钟后重试")
        spec = plan[sequence - 1]
        text = str(spec.get("speech_text") or "")
        if project.production_mode != "whiteboard":
            text = _fit_speech_text(text, _project_durations(project)[sequence - 1])
        if not text.strip() or len(text) > 10000:
            raise HTTPException(422, "台词为空或过长")
        try:
            job = await reserve_speech(
                session, project, sequence, text, speech_options(project, spec), schedule=True
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return speech_job_payload(job)
