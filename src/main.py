"""FastAPI application entrypoint for Customer Entitlement & Support Case API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.cases import router as cases_router
from src.api.customers import router as customers_router
from src.api.health import router as health_router
from src.api.webhooks import router as webhooks_router
from src.config import get_settings
from src.middleware.rate_limit import RateLimitMiddleware
from src.models.database import init_db
from src.models.schemas import ProblemDetail
from src.services.container import (
    get_case_mock,
    get_entitlement_mock,
    get_usage_mock,
)
from src.services.errors import PlatformError
from src.utils.correlation import CorrelationIdMiddleware, get_correlation_id
from src.utils.logging_config import setup_logging

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize structured logging, SQLite tables, and mock upstream services."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    setup_logging(level=level, json_output=settings.log_json)
    await init_db()
    # Warm mock upstreams
    get_case_mock()
    get_entitlement_mock()
    get_usage_mock()
    logger.info("Application startup complete", env=settings.app_env, version=settings.app_version)
    yield
    logger.info("Application shutdown complete")


def _problem_response(
    *,
    status_code: int,
    problem_type: str,
    title: str,
    detail: str,
    request: Request,
    errors: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Serialize an RFC 9457 ``application/problem+json`` response."""
    problem = ProblemDetail(
        type=problem_type,
        title=title,
        status=status_code,
        detail=detail,
        instance=str(request.url.path),
        errors=errors,
    )
    body = problem.model_dump(exclude_none=True)
    cid = get_correlation_id()
    if cid:
        body["correlationId"] = cid
    if extra:
        body.update(extra)

    resp_headers = dict(headers or {})
    if cid:
        resp_headers["X-Correlation-ID"] = cid
    if extra and "retryAfter" in extra:
        resp_headers["Retry-After"] = str(extra["retryAfter"])

    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
        headers=resp_headers,
    )


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Unified Customer Entitlement, Product Usage & Support Case Platform API",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Starlette executes middlewares in LIFO order:
    # CorrelationId runs first (outermost) so RateLimit and route handlers see correlation_id.
    app.add_middleware(
        RateLimitMiddleware,
        capacity=settings.rate_limit_capacity,
        refill_rate=settings.rate_limit_refill_rate,
    )
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(PlatformError)
    async def platform_error_handler(request: Request, exc: PlatformError) -> JSONResponse:
        return _problem_response(
            status_code=exc.status_code,
            problem_type=exc.problem_type,
            title=exc.title,
            detail=exc.detail,
            request=request,
            extra=exc.extra,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        title_map = {
            401: ("https://api.example.com/problems/authentication-required", "Authentication Required"),
            403: ("https://api.example.com/problems/insufficient-scope", "Insufficient Scope"),
            404: ("https://api.example.com/problems/not-found", "Resource Not Found"),
            429: ("https://api.example.com/problems/rate-limit-exceeded", "Rate Limit Exceeded"),
        }
        ptype, title = title_map.get(
            exc.status_code,
            ("about:blank", "HTTP Error"),
        )
        return _problem_response(
            status_code=exc.status_code,
            problem_type=ptype,
            title=title,
            detail=str(exc.detail),
            request=request,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        field_errors = [
            {
                "field": "/" + "/".join(str(p) for p in err.get("loc", [])),
                "message": err.get("msg", "Invalid value"),
                "code": err.get("type", "validation_error"),
            }
            for err in exc.errors()
        ]
        return _problem_response(
            status_code=400,
            problem_type="https://api.example.com/problems/validation-error",
            title="Validation Error",
            detail="One or more request parameters or body fields failed validation.",
            request=request,
            errors=field_errors,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception during request", error=str(exc))
        return _problem_response(
            status_code=500,
            problem_type="https://api.example.com/problems/internal-error",
            title="Internal Server Error",
            detail="An unexpected internal error occurred.",
            request=request,
        )

    app.include_router(health_router)
    app.include_router(customers_router)
    app.include_router(cases_router)
    app.include_router(webhooks_router)

    return app


app = create_app()
