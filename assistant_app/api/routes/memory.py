from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from assistant_app.api.dependencies import current_user
from assistant_app.db.models import User
from assistant_app.services.age_graph import load_memory_graph
from assistant_app.services.memory import forget_memory_item, list_memory_items

router = APIRouter()


@router.get("/items")
async def memory_items(
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> list[dict[str, object]]:
    return await list_memory_items(request.app.state.runtime, user.id)


@router.delete("/items/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory_item(
    memory_id: UUID,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> None:
    deleted = await forget_memory_item(request.app.state.runtime, user.id, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="记忆不存在")


@router.get("/graph")
async def memory_graph(
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> dict[str, list[dict[str, object]]]:
    return await load_memory_graph(
        request.app.state.runtime,
        user.id,
        request.app.state.settings.memory_graph_limit,
    )
