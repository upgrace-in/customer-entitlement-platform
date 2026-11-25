"""End-to-end HTTP integration tests via FastAPI TestClient."""

from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient

from src.main import create_app
from src.middleware.rate_limit import RateLimitMiddleware
from src.models import database as db_mod
from src.utils.security import generate_hmac_signature


def test_customer_aggregation_and_arr_scope_gating(test_client: TestClient, make_token) -> None:
    no_fin = make_token(["customers:read"])
    r1 = test_client.get(
        "/v1/customers/ACC-007",
        headers={"Authorization": f"Bearer {no_fin}", "X-Correlation-ID": "cid-123"},
    )
    assert r1.status_code == 200
    assert r1.headers["X-Correlation-ID"] == "cid-123"
    assert "ETag" in r1.headers
    assert "X-RateLimit-Limit" in r1.headers
    body1 = r1.json()
    assert body1["support_tier"] == "Platinum"
    assert "arr_usd" not in body1

    # Conditional GET -> 304 Not Modified
    r_304 = test_client.get(
        "/v1/customers/ACC-007",
        headers={"Authorization": f"Bearer {no_fin}", "If-None-Match": r1.headers["ETag"]},
    )
    assert r_304.status_code == 304

    with_fin = make_token(["customers:read", "finance:read"])
    r2 = test_client.get(
        "/v1/customers/ACC-007",
        headers={"Authorization": f"Bearer {with_fin}"},
    )
    assert r2.status_code == 200
    assert r2.json()["arr_usd"] == 480000.0


def test_cases_filtering_sorting_and_pagination(test_client: TestClient, make_token) -> None:
    token = make_token(["cases:read"])
    resp = test_client.get(
        "/v1/cases?priority=P1&status=open&limit=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["data"]) == 2
    assert all(c["priority"] == "P1" and c["status"] == "open" for c in data["data"])


def test_comment_idempotency_end_to_end(test_client: TestClient, make_token) -> None:
    token = make_token(["cases:write"])
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "e2e-idem-1",
    }
    r1 = test_client.post("/v1/cases/CS-10231/comments", json={"body": "Checking logs"}, headers=headers)
    assert r1.status_code == 201
    assert r1.json()["idempotent_replay"] is False

    r2 = test_client.post("/v1/cases/CS-10231/comments", json={"body": "Checking logs"}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True

    r3 = test_client.post("/v1/cases/CS-10231/comments", json={"body": "Modified body"}, headers=headers)
    assert r3.status_code == 409


def test_escalation_and_webhook_dispatch_and_hmac(test_client: TestClient, make_token) -> None:
    rw_token = make_token(["cases:read", "cases:write"])
    headers = {"Authorization": f"Bearer {rw_token}"}

    # Register webhook subscription
    secret = "test_webhook_secret_123456"
    sub_resp = test_client.post(
        "/v1/webhooks",
        json={"url": "http://localhost:9999/hook", "events": ["case.escalated"], "secret": secret},
        headers=headers,
    )
    assert sub_resp.status_code == 201

    # Fetch case ETag and escalate
    case_resp = test_client.get("/v1/cases/CS-10231", headers=headers)
    etag = case_resp.headers["ETag"]

    esc_resp = test_client.post(
        "/v1/cases/CS-10231/escalate",
        json={"reason": "Customer SLA at risk", "notify_customer": False},
        headers={**headers, "If-Match": etag},
    )
    assert esc_resp.status_code == 200
    assert esc_resp.json()["status"] == "escalated"

    # Verify outbound delivery was signed & logged
    deliveries = test_client.get("/v1/webhooks/deliveries", headers=headers).json()["data"]
    assert len(deliveries) == 1
    assert deliveries[0]["event_type"] == "case.escalated"
    assert deliveries[0]["signature"].startswith("sha256=")

    # Verify reference receiver endpoint validates HMAC signatures
    payload = b'{"event_type":"case.escalated"}'
    sig = generate_hmac_signature(payload, secret)
    rx = test_client.post(
        "/v1/webhooks/test",
        content=payload,
        headers={
            "X-Signature-256": f"sha256={sig}",
            "X-Webhook-Secret": secret,
        },
    )
    assert rx.status_code == 200
    assert rx.json()["signature_verified"] is True


def test_rate_limiting_returns_429(make_token) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="rl_test_")
    db_mod.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(tmp_dir, 'rl.db')}"
    db_mod._engine = None
    db_mod._async_session_factory = None

    app = create_app()
    # Tighten rate limit middleware for deterministic 429 verification
    for mw in app.user_middleware:
        if mw.cls is RateLimitMiddleware:
            mw.kwargs["capacity"] = 2.0
            mw.kwargs["refill_rate"] = 0.01

    token = make_token(["cases:read"])
    with TestClient(app) as client:
        for _ in range(2):
            assert client.get("/v1/cases", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        blocked = client.get("/v1/cases", headers={"Authorization": f"Bearer {token}"})
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers


def test_health_and_readiness(test_client: TestClient) -> None:
    h = test_client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "healthy"

    r = test_client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
