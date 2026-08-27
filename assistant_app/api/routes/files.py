from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import desc, select

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import GeneratedFile, User
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.document_skill import (
    create_uploaded_document,
    uploaded_document_payload,
)
from assistant_app.services.generated_files import file_payload

router = APIRouter()


@router.post("/upload")
async def upload_file(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    upload: Annotated[UploadFile, File(...)],
) -> dict[str, object]:
    settings = request.app.state.settings
    chunks: list[bytes] = []
    size = 0
    try:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > settings.document_max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"单个附件不能超过 {settings.document_max_bytes // 1_000_000} MB",
                )
            chunks.append(chunk)
        record = await create_uploaded_document(
            request.app.state.runtime,
            user.id,
            upload.filename or "document",
            b"".join(chunks),
            settings.document_max_bytes,
        )
        return uploaded_document_payload(record)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await upload.close()


@router.get("")
async def list_files(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(GeneratedFile)
                .where(GeneratedFile.user_id == user.id)
                .order_by(desc(GeneratedFile.created_at))
                .limit(50)
            )
        ).all()
    return [file_payload(row) for row in rows]


@router.get("/{file_id}/download", response_class=FileResponse)
async def download_file(
    file_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> FileResponse:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        record = await session.scalar(
            select(GeneratedFile).where(
                GeneratedFile.id == file_id,
                GeneratedFile.user_id == user.id,
            )
        )
    if record is None or not await asyncio.to_thread(Path(record.storage_path).is_file):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        record.storage_path,
        media_type=record.media_type,
        filename=record.filename,
    )
