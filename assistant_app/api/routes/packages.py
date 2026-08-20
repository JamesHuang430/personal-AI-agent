from fastapi import APIRouter, Request
from sqlalchemy import select

from assistant_app.db.models import Package
from assistant_app.db.runtime import RuntimeDependencies

router = APIRouter()


@router.get("")
async def list_packages(request: Request) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        packages = (
            await session.scalars(
                select(Package)
                .where(Package.is_active.is_(True))
                .order_by(Package.sort_order, Package.price_cents)
            )
        ).all()
    return [
        {
            "id": str(item.id),
            "name": item.name,
            "price_yuan": item.price_cents / 100,
            "points": item.points,
            "is_active": item.is_active,
        }
        for item in packages
    ]
