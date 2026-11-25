"""Dependency providers shared across FastAPI routers."""

from __future__ import annotations

from functools import lru_cache

from mock_services.case_service import CaseMockService
from mock_services.entitlement_service import EntitlementMockService
from mock_services.usage_service import UsageMockService
from src.config import get_settings
from src.services.case_service import CaseService
from src.services.customer_service import CustomerService
from src.services.webhook_service import WebhookService
from src.utils.resilience import CircuitBreaker


@lru_cache
def get_circuit_breaker() -> CircuitBreaker:
    settings = get_settings()
    return CircuitBreaker(
        failure_threshold=settings.circuit_breaker_threshold,
        recovery_timeout=settings.circuit_breaker_recovery_seconds,
    )


@lru_cache
def get_case_mock() -> CaseMockService:
    return CaseMockService()


@lru_cache
def get_entitlement_mock() -> EntitlementMockService:
    return EntitlementMockService()


@lru_cache
def get_usage_mock() -> UsageMockService:
    return UsageMockService()


@lru_cache
def get_webhook_service() -> WebhookService:
    return WebhookService()


@lru_cache
def get_case_service() -> CaseService:
    settings = get_settings()
    return CaseService(
        case_mock=get_case_mock(),
        entitlement_mock=get_entitlement_mock(),
        webhook_service=get_webhook_service(),
        circuit_breaker=get_circuit_breaker(),
        timeout_seconds=settings.upstream_timeout_seconds,
        max_retries=settings.upstream_max_retries,
    )


@lru_cache
def get_customer_service() -> CustomerService:
    settings = get_settings()
    return CustomerService(
        entitlement_mock=get_entitlement_mock(),
        usage_mock=get_usage_mock(),
        case_mock=get_case_mock(),
        circuit_breaker=get_circuit_breaker(),
        timeout_seconds=settings.upstream_timeout_seconds,
        max_retries=settings.upstream_max_retries,
    )
