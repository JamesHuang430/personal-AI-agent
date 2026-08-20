from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import desc, select

from assistant_app.api.dependencies import current_user
from assistant_app.core.time import local_today
from assistant_app.db.models import DailyCheckin, PointLedger, User
from assistant_app.db.runtime import RuntimeDependencies

router = APIRouter()
CHECKIN_REWARD = 100


@router.get("/me")
async def me(user: Annotated[User, Depends(current_user)]) -> dict[str, object]:
    return {
        "id": str(user.id),
        "email": user.email,
        "points": user.points,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }


@router.post("/check-in")
async def check_in(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    today = local_today()

    async with runtime.sessions() as session, session.begin():
        locked_user = await session.scalar(select(User).where(User.id == user.id).with_for_update())
        existing = await session.scalar(
            select(DailyCheckin).where(
                DailyCheckin.user_id == user.id,
                DailyCheckin.checkin_date == today,
            )
        )
        if existing is not None:
            return {
                "awarded": 0,
                "already_checked_in": True,
                "points": locked_user.points,
            }

        locked_user.points += CHECKIN_REWARD
        session.add(
            DailyCheckin(
                user_id=user.id,
                checkin_date=today,
                points_awarded=CHECKIN_REWARD,
            )
        )
        session.add(
            PointLedger(
                user_id=user.id,
                delta=CHECKIN_REWARD,
                balance_after=locked_user.points,
                reason="daily_checkin",
                note="每日签到奖励",
            )
        )
        points = locked_user.points

    return {"awarded": CHECKIN_REWARD, "already_checked_in": False, "points": points}


@router.get("/point-ledger")
async def point_ledger(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        entries = (
            await session.scalars(
                select(PointLedger)
                .where(PointLedger.user_id == user.id)
                .order_by(desc(PointLedger.created_at))
                .limit(30)
            )
        ).all()
    return [
        {
            "id": entry.id,
            "delta": entry.delta,
            "balance_after": entry.balance_after,
            "reason": entry.reason,
            "note": entry.note,
            "created_at": entry.created_at.isoformat(),
        }
        for entry in entries
    ]
