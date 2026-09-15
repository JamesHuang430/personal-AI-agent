from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import (
    DirectorAgentRun,
    DirectorProject,
    DirectorShot,
    ImageChannel,
    SpeechChannel,
    User,
)
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
from assistant_app.services.director_audio import AudioSettings
from assistant_app.services.generated_files import GENERATED_ROOT
from assistant_app.services.model_gateway import ModelChannelUnavailableError
from assistant_app.services.speech_gateway import EDGE_FEMALE_VOICE_ID

router = APIRouter()


@router.get("/whiteboard-readiness")
async def whiteboard_readiness(request: Request, user: Annotated[User, Depends(current_user)]):
    async with request.app.state.runtime.sessions() as session:
        image = await session.scalar(select(ImageChannel).where(ImageChannel.is_active.is_(True)))
        speech = await session.scalar(
            select(SpeechChannel).where(SpeechChannel.is_active.is_(True))
        )
    return {
        "image_configured": image is not None,
        "speech_configured": speech is not None,
        "edge_available": True,
        "image_model": image.model_name if image else None,
        "note": "配置存在不代表权限或余额有效。生图和配音按各渠道计费，本地合成不调用视频模型。",
    }


@router.put("/projects/{project_id}/images/{sequence}")
async def upload_storyboard_image(
    project_id: UUID,
    sequence: int,
    file: UploadFile,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    import hashlib
    from uuid import uuid4

    from assistant_app.services.image_gateway import MAX_IMAGE_BYTES, save_image
    from assistant_app.services.whiteboard import ensure_shot, whiteboard_durations

    try:
        data = await file.read(MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "图片不能超过 12 MB")
    async with request.app.state.runtime.sessions() as session, session.begin():
        project = await session.scalar(
            select(DirectorProject)
            .where(DirectorProject.id == project_id, DirectorProject.user_id == user.id)
            .with_for_update()
        )
        if project is None:
            raise HTTPException(404, "项目不存在")
        if project.production_mode != "whiteboard" or project.status not in {
            "awaiting_storyboard",
            "failed",
        }:
            raise HTTPException(409, "只能在白板分镜待确认或失败后上传替代图片")
        visual = await session.scalar(
            select(DirectorAgentRun).where(
                DirectorAgentRun.project_id == project_id, DirectorAgentRun.agent_key == "visual"
            )
        )
        plan = (visual.result_data or {}).get("shots", []) if visual else []
        durations = whiteboard_durations(project.target_seconds, project.one_click)
        if (
            not 1 <= sequence <= len(plan)
            or len(plan) != len(durations)
            or visual.status != "completed"
        ):
            raise HTTPException(409, "尚无有效的已规划分镜")
        shot = await ensure_shot(
            session, project, sequence, plan[sequence - 1], durations[sequence - 1]
        )
        if shot.status == "completed":
            raise HTTPException(409, "已完成的镜头不能覆盖；请新建一版")
        path = GENERATED_ROOT / f"director-upload-{uuid4()}.png"
        try:
            await asyncio.to_thread(save_image, data, path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, "图片无效，仅支持尺寸合理的 PNG、JPEG、WebP") from exc
        shot.image_path = str(path)
        snapshot = dict(shot.continuity_snapshot or {})
        snapshot.pop("_whiteboard", None)
        shot.continuity_snapshot = snapshot
        shot.image_source = "uploaded"
        shot.status = "pending"
        shot.error_message = None
        result = dict(visual.result_data)
        uploads = dict(result.get("uploaded_images") or {})
        uploads[str(sequence)] = hashlib.sha256(data).hexdigest()
        visual.result_data = result | {"uploaded_images": uploads}
        project.storyboard_approved = False
        project.status = "awaiting_storyboard"
        project.current_stage = "storyboard_review"
        project.error_message = None
    return await project_payload(request.app.state.runtime, project)


class WhiteboardAnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    annotation: dict
    storyboard_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


async def annotation_target(session, user_id, project_id, shot_id):
    project = await session.scalar(
        select(DirectorProject)
        .where(DirectorProject.id == project_id, DirectorProject.user_id == user_id)
        .with_for_update()
    )
    if project is None:
        raise HTTPException(404, "项目不存在")
    shot = await session.scalar(
        select(DirectorShot).where(
            DirectorShot.id == shot_id,
            DirectorShot.project_id == project_id,
            DirectorShot.user_id == user_id,
        )
    )
    if shot is None or project.production_mode != "whiteboard":
        raise HTTPException(404, "白板镜头不存在")
    if project.status not in {"awaiting_storyboard", "failed"} or shot.status == "completed":
        raise HTTPException(409, "只能编辑等待确认或失败的未完成镜头")
    if not shot.image_path or not (shot.continuity_snapshot or {}).get("_whiteboard"):
        raise HTTPException(409, "请先确认分镜并准备图片和旁白")
    return project, shot


@router.put("/projects/{project_id}/shots/{shot_id}/annotation")
async def save_whiteboard_annotation(
    project_id: UUID,
    shot_id: UUID,
    payload: WhiteboardAnnotationUpdate,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    import re

    from assistant_app.services.director import storyboard_hash
    from assistant_app.services.whiteboard_annotations import annotation_digest, validate_annotation

    async with request.app.state.runtime.sessions() as session, session.begin():
        project, shot = await annotation_target(session, user.id, project_id, shot_id)
        visual = await session.scalar(
            select(DirectorAgentRun).where(
                DirectorAgentRun.project_id == project_id, DirectorAgentRun.agent_key == "visual"
            )
        )
        if storyboard_hash(project, visual.result_data or {}) != payload.storyboard_hash:
            raise HTTPException(409, "分镜已变化，请刷新后再保存，避免覆盖其他修改")
        try:
            data = await asyncio.to_thread(validate_annotation, payload.annotation, shot.image_path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        state = dict(shot.continuity_snapshot["_whiteboard"])
        if data["cues"] == state["annotation"]["cues"]:
            data["subtitleAlignment"] = state["annotation"]["subtitleAlignment"]
        elif data["subtitleAlignment"] != "srt":
            data["subtitleAlignment"] = "manual"
        if data["sceneDurationMs"] != state["annotation"]["sceneDurationMs"]:
            raise HTTPException(422, "不能改变已准备好的场景时长")

        def clean(value):
            return re.sub(r"\s+", "", value)

        if clean("".join(c["text"] for c in data["cues"])) != clean(shot.speech_text):
            raise HTTPException(422, "字幕必须与已生成的旁白一致；可修改时间，不可改写台词")
        if any(c["endMs"] > state["audioDurationMs"] + 100 for c in data["cues"]):
            raise HTTPException(422, "字幕不能超出旁白音频时长")
        shot.continuity_snapshot = dict(shot.continuity_snapshot) | {
            "_whiteboard": state | {"annotation": data, "saved": True, "approved": False}
        }
        result = dict(visual.result_data or {})
        revisions = dict(result.get("whiteboard_annotations") or {})
        revisions[str(shot.sequence)] = annotation_digest(data)
        visual.result_data = result | {"whiteboard_annotations": revisions}
        project.storyboard_approved = False
        project.status = "awaiting_storyboard"
        project.current_stage = "whiteboard_annotation_review"
        project.error_message = None
    return await project_payload(request.app.state.runtime, project)


class WhiteboardSrtInput(BaseModel):
    text: str = Field(min_length=1, max_length=64000)


@router.post("/whiteboard/parse-srt")
async def parse_whiteboard_srt(
    payload: WhiteboardSrtInput,
    user: Annotated[User, Depends(current_user)],
):
    from assistant_app.services.whiteboard_annotations import parse_srt

    try:
        return {"cues": parse_srt(payload.text)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class WhiteboardPreviewInput(BaseModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.post("/projects/{project_id}/shots/{shot_id}/annotation-preview")
async def create_whiteboard_preview(
    project_id: UUID,
    shot_id: UUID,
    payload: WhiteboardPreviewInput,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    import json
    import sys

    from assistant_app.services.director_media import _run_media_command
    from assistant_app.services.whiteboard_annotations import annotation_digest, validate_annotation

    runtime = request.app.state.runtime
    async with runtime.sessions() as session:
        _project, shot = await annotation_target(session, user.id, project_id, shot_id)
        state = shot.continuity_snapshot["_whiteboard"]
        if not state.get("saved") or annotation_digest(state["annotation"]) != payload.digest:
            raise HTTPException(409, "请先保存当前标注再预览")
        data = await asyncio.to_thread(validate_annotation, state["annotation"], shot.image_path)
    output = GENERATED_ROOT / f"wb-preview-{shot.id}-{payload.digest}.mp4"
    key = f"whiteboard:preview:{user.id}"
    from uuid import uuid4

    lease = str(uuid4())
    if not await runtime.redis.set(key, lease, nx=True, ex=1250):
        raise HTTPException(409, "已有白板预览正在渲染，请稍候")
    try:
        if not await asyncio.to_thread(output.is_file):
            annotation_file = output.with_suffix(".json")
            await asyncio.to_thread(annotation_file.write_text, json.dumps(data), encoding="utf-8")
            raw = output.with_suffix(".raw.mp4")
            # Match the production aspect ratio; do not infer it from the input artwork.
            width, height = (360, 640) if _project.aspect_ratio == "9:16" else (640, 360)
            temporary = output.with_suffix(".pending.mp4")
            try:
                await _run_media_command(
                    sys.executable,
                    "-m",
                    "assistant_app.services.whiteboard_stream",
                    shot.image_path,
                    str(annotation_file),
                    str(raw),
                    str(width),
                    str(height),
                )
                await _run_media_command(
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(raw),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(temporary),
                )
                await asyncio.to_thread(temporary.replace, output)
            finally:
                await asyncio.to_thread(raw.unlink, missing_ok=True)
                await asyncio.to_thread(temporary.unlink, missing_ok=True)
    finally:
        await runtime.redis.eval(
            "if redis.call('get',KEYS[1]) == ARGV[1] then "
            "return redis.call('del',KEYS[1]) else return 0 end",
            1,
            key,
            lease,
        )
    return {
        "url": (
            f"/api/v1/director/projects/{project_id}/shots/{shot_id}"
            f"/annotation-preview/{payload.digest}"
        )
    }


@router.get("/projects/{project_id}/shots/{shot_id}/annotation-preview/{digest}")
async def get_whiteboard_preview(
    project_id: UUID,
    shot_id: UUID,
    digest: str,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    import re

    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise HTTPException(404, "预览不存在")
    async with request.app.state.runtime.sessions() as session:
        shot = await session.scalar(
            select(DirectorShot).where(
                DirectorShot.id == shot_id,
                DirectorShot.project_id == project_id,
                DirectorShot.user_id == user.id,
            )
        )
    path = GENERATED_ROOT / f"wb-preview-{shot_id}-{digest}.mp4"
    if shot is None or not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "预览不存在")
    return FileResponse(
        path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"}
    )


@router.get("/projects/{project_id}/shots/{shot_id}/image", response_class=FileResponse)
async def preview_shot_image(
    project_id: UUID,
    shot_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
):
    async with request.app.state.runtime.sessions() as session:
        shot = await session.scalar(
            select(DirectorShot).where(
                DirectorShot.id == shot_id,
                DirectorShot.project_id == project_id,
                DirectorShot.user_id == user.id,
            )
        )
    if shot is None or not shot.image_path:
        raise HTTPException(404, "图片不存在")
    path = await asyncio.to_thread(Path(shot.image_path).resolve)
    root = await asyncio.to_thread(GENERATED_ROOT.resolve)
    if not path.is_relative_to(root) or not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "图片不存在")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, no-store"}
    )


class DirectorProjectCreatePayload(BaseModel):
    postproduction: AudioSettings = Field(default_factory=AudioSettings)
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
    production_mode: Literal["whiteboard", "video"] = "whiteboard"


class DirectorDraftUpdate(BaseModel):
    production_mode: Literal["whiteboard", "video"] | None = None
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
            payload.model_dump(exclude_none=True),
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
            production_mode=payload.production_mode,
            postproduction=payload.postproduction.model_dump(mode="json"),
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
