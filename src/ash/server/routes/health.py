"""Liveness and integration-owned readiness checks."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness only; use /ready to check dependencies."""
    return {"status": "alive"}


@router.get("/ready")
async def readiness_check(request: Request) -> JSONResponse:
    """Probe actual dependencies through the integration runtime."""
    runtime = getattr(request.app.state, "integration_runtime", None)
    checks = await runtime.check_health() if runtime is not None else {"runtime": False}
    ready = bool(checks) and all(checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "degraded", "checks": checks},
        status_code=200 if ready else 503,
    )
