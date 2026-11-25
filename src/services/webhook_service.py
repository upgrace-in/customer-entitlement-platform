"""Webhook management and HMAC-SHA256 signed event delivery service."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import AuditLog, WebhookSubscription
from src.services.errors import ResourceNotFoundError
from src.utils.correlation import get_correlation_id
from src.utils.security import generate_hmac_signature

logger = structlog.get_logger(__name__)


class WebhookService:
    """Manages webhook subscriptions and delivers HMAC-SHA256 signed events.

    Records every delivery attempt in ``delivery_log`` (in-memory ring buffer)
    so automated tests and local demos can inspect signed payloads without
    requiring an external callback receiver.
    """

    def __init__(self) -> None:
        self.delivery_log: list[dict[str, Any]] = []

    async def register_webhook(
        self,
        *,
        url: str,
        events: list[str],
        secret: str | None = None,
        db: AsyncSession,
        user_id: str = "system",
    ) -> dict[str, Any]:
        """Persist a new webhook subscription and return its representation."""
        webhook_id = f"whk_{uuid.uuid4().hex[:16]}"
        signing_secret = secret or f"whsec_{secrets.token_urlsafe(24)}"
        now = datetime.now(timezone.utc)

        sub = WebhookSubscription(
            id=webhook_id,
            url=url,
            events=json.dumps(events),
            secret=signing_secret,
            active=True,
            created_at=now,
        )
        db.add(sub)
        db.add(
            AuditLog(
                correlation_id=get_correlation_id() or None,
                user_id=user_id,
                action="webhook.registered",
                resource_type="webhook_subscription",
                resource_id=webhook_id,
                details=json.dumps({"url": url, "events": events}),
            )
        )
        await db.flush()

        return {
            "id": sub.id,
            "url": sub.url,
            "events": events,
            "secret": signing_secret,
            "active": sub.active,
            "created_at": now.isoformat(),
        }

    async def list_webhooks(self, db: AsyncSession) -> list[dict[str, Any]]:
        """Return all registered webhook subscriptions (secrets masked)."""
        result = await db.execute(
            select(WebhookSubscription).order_by(WebhookSubscription.created_at.desc())
        )
        subs = result.scalars().all()
        return [
            {
                "id": sub.id,
                "url": sub.url,
                "events": json.loads(sub.events),
                "active": sub.active,
                "created_at": sub.created_at.isoformat(),
            }
            for sub in subs
        ]

    async def delete_webhook(
        self,
        webhook_id: str,
        db: AsyncSession,
        user_id: str = "system",
    ) -> None:
        """Delete a webhook subscription by ID."""
        result = await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == webhook_id)
        )
        sub = result.scalar_one_or_none()
        if sub is None:
            raise ResourceNotFoundError(
                f"Webhook subscription '{webhook_id}' not found."
            )
        await db.delete(sub)
        db.add(
            AuditLog(
                correlation_id=get_correlation_id() or None,
                user_id=user_id,
                action="webhook.deleted",
                resource_type="webhook_subscription",
                resource_id=webhook_id,
                details=json.dumps({"url": sub.url}),
            )
        )
        await db.flush()

    async def dispatch_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        db: AsyncSession,
    ) -> list[dict[str, Any]]:
        """Deliver an event to all active subscriptions matching *event_type*.

        Each payload is signed with the subscription's HMAC-SHA256 secret and
        sent with ``X-Signature-256``, ``X-Webhook-ID``, and ``X-Webhook-Timestamp``.
        """
        result = await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.active.is_(True))
        )
        subs = result.scalars().all()

        deliveries: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        epoch_ts = str(int(time.time()))

        for sub in subs:
            subscribed_events = json.loads(sub.events)
            if event_type not in subscribed_events and "*" not in subscribed_events:
                continue

            event_id = f"evt_{uuid.uuid4().hex[:16]}"
            envelope = {
                "event_id": event_id,
                "event_type": event_type,
                "timestamp": now.isoformat(),
                "webhook_id": sub.id,
                "payload": payload,
            }
            raw_body = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
            signature = generate_hmac_signature(raw_body, sub.secret)
            envelope["signature"] = signature

            headers = {
                "Content-Type": "application/json",
                "X-Signature-256": f"sha256={signature}",
                "X-Webhook-ID": event_id,
                "X-Webhook-Timestamp": epoch_ts,
                "X-Correlation-ID": get_correlation_id() or uuid.uuid4().hex,
                "User-Agent": "EntitlementAPI-Webhook/1.0",
            }

            delivery_status = "delivered"
            http_status: int | None = None
            error_msg: str | None = None

            # Best-effort delivery with short timeout; failures are logged, not fatal.
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.post(sub.url, content=raw_body, headers=headers)
                    http_status = resp.status_code
                    if resp.status_code >= 400:
                        delivery_status = "failed"
                        error_msg = f"HTTP {resp.status_code}"
            except Exception as exc:
                delivery_status = "failed"
                error_msg = str(exc)

            record = {
                "event_id": event_id,
                "webhook_id": sub.id,
                "url": sub.url,
                "event_type": event_type,
                "signature": f"sha256={signature}",
                "status": delivery_status,
                "http_status": http_status,
                "error": error_msg,
                "timestamp": now.isoformat(),
                "envelope": envelope,
            }
            self.delivery_log.append(record)
            if len(self.delivery_log) > 200:
                self.delivery_log.pop(0)

            logger.info(
                "Webhook event dispatched",
                event_id=event_id,
                event_type=event_type,
                webhook_id=sub.id,
                delivery_status=delivery_status,
                http_status=http_status,
            )
            deliveries.append(record)

        return deliveries
