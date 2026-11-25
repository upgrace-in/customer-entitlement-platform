"""Webhook subscription management and local receiver endpoint."""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.middleware.auth import CurrentUser, require_scopes
from src.models.database import get_db
from src.models.schemas import (
    WebhookSubscriptionRequest,
    WebhookSubscriptionResponse,
)
from src.services.container import get_webhook_service
from src.services.webhook_service import WebhookService
from src.utils.security import verify_hmac_signature

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["Webhooks"])


@router.post(
    "",
    response_model=WebhookSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook subscription",
)
async def register_webhook(
    body: WebhookSubscriptionRequest,
    user: Annotated[CurrentUser, require_scopes("cases:write")],
    service: Annotated[WebhookService, Depends(get_webhook_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Create a webhook subscription and return its HMAC signing secret once."""
    return await service.register_webhook(
        url=str(body.url),
        events=body.events,
        secret=body.secret,
        db=db,
        user_id=user.sub,
    )


@router.get(
    "",
    summary="List registered webhook subscriptions",
)
async def list_webhooks(
    user: Annotated[CurrentUser, require_scopes("cases:read")],
    service: Annotated[WebhookService, Depends(get_webhook_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Return active and inactive webhook subscriptions (secrets omitted)."""
    _ = user
    subs = await service.list_webhooks(db)
    return {"data": subs, "total_count": len(subs)}


@router.get(
    "/deliveries",
    summary="Inspect recent outbound webhook deliveries (dev/demo diagnostics)",
)
async def list_deliveries(
    user: Annotated[CurrentUser, require_scopes("cases:read")],
    service: Annotated[WebhookService, Depends(get_webhook_service)],
) -> dict[str, Any]:
    """Return recent signed webhook delivery attempts recorded by the server."""
    _ = user
    return {"data": service.delivery_log, "total_count": len(service.delivery_log)}


@router.delete(
    "/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a webhook subscription",
)
async def delete_webhook(
    webhook_id: str,
    user: Annotated[CurrentUser, require_scopes("cases:write")],
    service: Annotated[WebhookService, Depends(get_webhook_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Remove a webhook subscription."""
    await service.delete_webhook(webhook_id, db=db, user_id=user.sub)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/test",
    summary="Test webhook receiver that verifies HMAC-SHA256 signatures",
)
async def test_webhook_receiver(
    request: Request,
    x_signature_256: Annotated[str | None, Header(alias="X-Signature-256")] = None,
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
) -> dict[str, Any]:
    """Reference webhook receiver verifying ``X-Signature-256`` HMAC-SHA256 signatures."""
    raw_body = await request.body()
    verified = False
    if x_signature_256 and x_webhook_secret:
        hex_sig = x_signature_256.removeprefix("sha256=")
        verified = verify_hmac_signature(raw_body, hex_sig, x_webhook_secret)

    logger.info(
        "Test webhook receiver invoked",
        signature_present=bool(x_signature_256),
        verified=verified,
        payload_bytes=len(raw_body),
    )
    return {
        "received": True,
        "signature_verified": verified,
        "bytes": len(raw_body),
    }
