from __future__ import annotations

import asyncio
import re
from pathlib import Path
from uuid import UUID, uuid4

from assistant_app.db.models import GeneratedFile
from assistant_app.db.runtime import RuntimeDependencies

GENERATED_ROOT = Path("/data/generated")
MAX_FILE_BYTES = 1_000_000
MEDIA_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
}


def safe_filename(value: str) -> str:
    raw_name = Path(value.strip()).name
    stem = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", Path(raw_name).stem).strip("-_")
    suffix = Path(raw_name).suffix.lower()
    if suffix not in MEDIA_TYPES:
        suffix = ".md"
    return f"{(stem or 'assistant-file')[:180]}{suffix}"


async def create_generated_file(
    runtime: RuntimeDependencies,
    user_id: UUID,
    filename: str,
    content: str,
) -> GeneratedFile:
    encoded = content.encode("utf-8")
    if not encoded:
        raise ValueError("不能生成空文件")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("单个生成文件不能超过 1 MB")

    clean_name = safe_filename(filename)
    file_id = uuid4()
    storage_path = GENERATED_ROOT / f"{file_id}{Path(clean_name).suffix}"
    await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(storage_path.write_bytes, encoded)

    record = GeneratedFile(
        id=file_id,
        user_id=user_id,
        filename=clean_name,
        media_type=MEDIA_TYPES[Path(clean_name).suffix],
        storage_path=str(storage_path),
        size_bytes=len(encoded),
    )
    try:
        async with runtime.sessions() as session, session.begin():
            session.add(record)
    except Exception:
        await asyncio.to_thread(storage_path.unlink, missing_ok=True)
        raise
    return record


def file_payload(record: GeneratedFile) -> dict[str, object]:
    return {
        "id": str(record.id),
        "filename": record.filename,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "download_url": f"/api/v1/files/{record.id}/download",
    }
