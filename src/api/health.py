"""Health and readiness probe endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models.database import get_db
from src.services.container import get_circuit_breaker

router = APIRouter(tags=["Health"])
_START_TIME = time.monotonic()


@router.get("/health", summary="Liveness & dependency health check")
async def health_check(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return service health, uptime, and circuit-breaker status for upstreams."""
    settings = get_settings()
    cb = get_circuit_breaker()

    db_status = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        db_status = f"error: {exc}"

    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "version": settings.app_version,
        "uptime_seconds": round(time.monotonic() - _START_TIME, 2),
        "checks": {
            "database": db_status,
            "case_service": cb.state("case-system").value,
            "entitlement_service": cb.state("entitlement-system").value,
            "usage_service": cb.state("usage-warehouse").value,
        },
    }


@router.get("/ready", summary="Kubernetes readiness probe")
async def readiness_check(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Verify database connectivity before accepting traffic."""
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready"}
