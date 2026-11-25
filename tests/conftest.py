"""Shared pytest fixtures for unit, contract, and integration tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from mock_services.case_service import CaseMockService
from mock_services.entitlement_service import EntitlementMockService
from mock_services.usage_service import UsageMockService
from src.main import create_app
from src.middleware.auth import create_mock_token
from src.models import database as db_mod
from src.services.container import (
    get_case_mock,
    get_case_service,
    get_circuit_breaker,
    get_customer_service,
    get_entitlement_mock,
    get_usage_mock,
    get_webhook_service,
)


@pytest.fixture(autouse=True)
def _disable_random_mock_faults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep random simulated faults disabled unless a test explicitly injects them."""
    monkeypatch.setattr(CaseMockService, "_maybe_timeout", staticmethod(lambda: None))
    monkeypatch.setattr(CaseMockService, "_simulate_latency", staticmethod(lambda: None))
    monkeypatch.setattr(EntitlementMockService, "_maybe_timeout", staticmethod(lambda: None))
    monkeypatch.setattr(EntitlementMockService, "_simulate_latency", staticmethod(lambda: None))
    monkeypatch.setattr(UsageMockService, "_maybe_unavailable", staticmethod(lambda: None))
    monkeypatch.setattr(UsageMockService, "_simulate_latency", staticmethod(lambda: None))


@pytest.fixture()
def test_client() -> Generator[TestClient, None, None]:
    """Provide an isolated FastAPI TestClient backed by a fresh temporary SQLite DB."""
    tmp_dir = tempfile.mkdtemp(prefix="assign_test_")
    db_path = os.path.join(tmp_dir, "test.db")

    # Reset DB singletons & container caches for strict test isolation
    db_mod.DATABASE_URL = f"sqlite+aiosqlite:///{db_path}"
    db_mod._engine = None
    db_mod._async_session_factory = None
    for cache_fn in (
        get_circuit_breaker,
        get_case_mock,
        get_entitlement_mock,
        get_usage_mock,
        get_webhook_service,
        get_case_service,
        get_customer_service,
    ):
        cache_fn.cache_clear()

    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def make_token():
    """Factory fixture to generate scoped mock OAuth JWTs."""

    def _factory(
        scopes: list[str] | None = None,
        sub: str = "test-user-1",
        email: str = "test.user@example.com",
    ) -> str:
        return create_mock_token(sub=sub, email=email, name="Test User", scopes=scopes or [])

    return _factory
