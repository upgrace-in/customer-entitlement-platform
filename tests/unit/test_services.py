"""Unit tests for CaseService and CustomerService domain logic & partial failures."""

from __future__ import annotations

import os
import tempfile

import pytest

from mock_services.case_service import CaseMockService
from mock_services.entitlement_service import EntitlementMockService
from mock_services.usage_service import UsageMockService, WarehouseUnavailableError
from src.middleware.auth import CurrentUser
from src.models import database as db_mod
from src.models.schemas import CaseFilter, CaseSort, EscalateRequest
from src.services.case_service import CaseService
from src.services.customer_service import CustomerService
from src.services.errors import (
    BusinessRuleViolationError,
    IdempotencyConflictError,
    PreconditionFailedError,
)
from src.services.webhook_service import WebhookService


@pytest.fixture()
async def fresh_db():
    tmp = tempfile.mkdtemp(prefix="svc_test_")
    db_mod.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(tmp, 'svc.db')}"
    db_mod._engine = None
    db_mod._async_session_factory = None
    await db_mod.init_db()
    async with db_mod.get_db_context() as session:
        yield session


async def test_case_service_cursor_pagination() -> None:
    svc = CaseService(CaseMockService(), EntitlementMockService(), WebhookService())
    page1 = await svc.list_cases(
        filters=CaseFilter(), sort=CaseSort(sort_by="opened_at", sort_order="desc"), limit=4
    )
    assert len(page1.data) == 4
    assert page1.pagination.has_more is True
    assert page1.pagination.next_cursor is not None

    page2 = await svc.list_cases(
        filters=CaseFilter(),
        sort=CaseSort(sort_by="opened_at", sort_order="desc"),
        cursor=page1.pagination.next_cursor,
        limit=4,
    )
    ids_1 = {c.id for c in page1.data}
    ids_2 = {c.id for c in page2.data}
    assert ids_1.isdisjoint(ids_2)


async def test_case_service_idempotency_and_conflict(fresh_db) -> None:
    svc = CaseService(CaseMockService(), EntitlementMockService(), WebhookService())
    user = CurrentUser(sub="u1", email="u1@example.com", scopes=["cases:write"])

    c1, _, replay1 = await svc.add_comment(
        case_id="CS-10231",
        body="First update",
        idempotency_key="idem-123",
        user=user,
        db=fresh_db,
    )
    assert replay1 is False

    c2, _, replay2 = await svc.add_comment(
        case_id="CS-10231",
        body="First update",
        idempotency_key="idem-123",
        user=user,
        db=fresh_db,
    )
    assert replay2 is True
    assert c2.body == c1.body

    with pytest.raises(IdempotencyConflictError):
        await svc.add_comment(
            case_id="CS-10231",
            body="Different body",
            idempotency_key="idem-123",
            user=user,
            db=fresh_db,
        )


async def test_case_service_escalation_rules_and_etag(fresh_db) -> None:
    wh = WebhookService()
    svc = CaseService(CaseMockService(), EntitlementMockService(), wh)
    user = CurrentUser(sub="u1", email="u1@example.com", scopes=["cases:write"])

    case = await svc.get_case("CS-10231")

    with pytest.raises(PreconditionFailedError):
        await svc.escalate_case(
            case_id="CS-10231",
            req=EscalateRequest(reason="SLA breach"),
            user=user,
            if_match='"wrong-etag"',
            db=fresh_db,
        )

    escalated = await svc.escalate_case(
        case_id="CS-10231",
        req=EscalateRequest(reason="SLA breach"),
        user=user,
        if_match=case.etag,
        db=fresh_db,
    )
    assert escalated.status == "escalated"
    assert escalated.assignee == "oncall-platinum@platform.internal"

    # Cannot escalate twice
    with pytest.raises(BusinessRuleViolationError):
        await svc.escalate_case(
            case_id="CS-10231",
            req=EscalateRequest(reason="Second attempt"),
            user=user,
            if_match=escalated.etag,
            db=fresh_db,
        )


async def test_customer_service_partial_failure_degradation(monkeypatch: pytest.MonkeyPatch) -> None:
    usage_mock = UsageMockService()

    def broken_usage(*_args, **_kwargs):
        raise WarehouseUnavailableError("Snowflake suspended")

    monkeypatch.setattr(usage_mock, "get_usage", broken_usage)

    svc = CustomerService(EntitlementMockService(), usage_mock, CaseMockService())
    payload, etag = await svc.get_customer_summary(
        "ACC-007", scopes=["customers:read"], usage_months=3
    )
    assert payload["id"] == "ACC-007"
    assert "arr_usd" not in payload
    assert payload["usage"] == []
    assert "usage-warehouse" in payload["degraded_sources"]
    assert etag.startswith('"')
