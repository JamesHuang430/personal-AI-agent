from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from assistant_app import __version__
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.pi_runtime import pi_runtime_readiness

router = APIRouter()


@router.get("/live", summary="Process liveness")
async def liveness() -> dict[str, str]:
    return {
        "status": "ok",
        "version": __version__,
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/ready", summary="Dependency readiness")
async def readiness(request: Request) -> JSONResponse:
    runtime: RuntimeDependencies = request.app.state.runtime
    checks = await runtime.readiness()
    if request.app.state.settings.agent_runtime == "pi":
        checks["pi_runtime"] = await pi_runtime_readiness(request.app.state.settings)
    ready = all(check.status == "ok" for check in checks.values())
    body = {
        "status": "ok" if ready else "degraded",
        "checks": {
            name: {"status": check.status, "detail": check.detail} for name, check in checks.items()
        },
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)
