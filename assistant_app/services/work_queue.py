from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update

from assistant_app.db.models import WorkItem

LEASE_SECONDS = 90
MAX_ATTEMPTS = 3


async def enqueue(
    session,
    kind: str,
    resource_id: UUID,
    payload: dict | None = None,
    *,
    restart: bool = False,
) -> None:
    """Commit this row in the SAME transaction as its business resource."""
    existing = await session.scalar(
        select(WorkItem)
        .where(
            WorkItem.kind == kind,
            WorkItem.resource_id == resource_id,
        )
        .with_for_update()
    )
    if existing is None:
        session.add(WorkItem(kind=kind, resource_id=resource_id, payload=payload or {}))
    elif restart or existing.status in {"completed", "failed"}:
        existing.status = "queued"
        existing.owner = None
        existing.attempts = 0
        existing.error = None
        existing.payload = payload or {}


async def claim(runtime) -> WorkItem | None:
    now = datetime.now(UTC)
    async with runtime.sessions() as session, session.begin():
        item = await session.scalar(
            select(WorkItem)
            .where(
                or_(
                    WorkItem.status == "queued",
                    (WorkItem.status == "processing") & (WorkItem.lease_until < now),
                )
            )
            .order_by(WorkItem.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            return None
        item.status = "processing"
        item.owner = uuid4()
        item.attempts += 1
        item.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return item


async def renew(runtime, item: WorkItem) -> bool:
    async with runtime.sessions() as session, session.begin():
        result = await session.execute(
            update(WorkItem)
            .where(
                WorkItem.id == item.id,
                WorkItem.owner == item.owner,
                WorkItem.status == "processing",
            )
            .values(lease_until=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
        )
        return result.rowcount == 1


async def finish(runtime, item: WorkItem, error: str | None = None) -> None:
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            update(WorkItem)
            .where(
                WorkItem.id == item.id,
                WorkItem.owner == item.owner,
            )
            .values(status="failed" if error else "completed", error=error, lease_until=None)
        )
