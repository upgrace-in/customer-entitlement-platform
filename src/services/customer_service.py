"""Customer aggregation service with partial-failure resilience and scope-based redaction."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from typing import Any

import structlog

from mock_services.case_service import CaseMockService, CaseServiceTimeoutError
from mock_services.entitlement_service import (
    AccountNotFoundError,
    EntitlementMockService,
    EntitlementServiceTimeoutError,
)
from mock_services.usage_service import (
    UsageDataNotFoundError,
    UsageMockService,
    WarehouseUnavailableError,
)
from src.models.schemas import CustomerSummary, UsageSummary
from src.services.errors import (
    ResourceNotFoundError,
    ServiceUnavailableError,
    UpstreamServiceError,
)
from src.utils.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    retry_with_backoff,
    timeout_wrapper,
)
from src.utils.security import redact_arr

logger = structlog.get_logger(__name__)


class CustomerService:
    """Aggregates Entitlement (NetSuite), Usage (Snowflake), and Case (Salesforce) data.

    Demonstrates resilient fan-out:
    * Entitlement is the primary source of truth — if missing, 404; if down, 502/503.
    * Usage warehouse and case counts are non-fatal enrichments — if they fail or
      their circuit breaker is open, the API degrades gracefully and reports the
      failed source in ``degraded_sources``.
    * ``arr_usd`` is redacted server-side unless ``finance:read`` is in caller scopes.
    """

    def __init__(
        self,
        entitlement_mock: EntitlementMockService,
        usage_mock: UsageMockService,
        case_mock: CaseMockService,
        circuit_breaker: CircuitBreaker | None = None,
        timeout_seconds: float = 3.0,
        max_retries: int = 2,
    ) -> None:
        self._entitlement_mock = entitlement_mock
        self._usage_mock = usage_mock
        self._case_mock = case_mock
        self._cb = circuit_breaker or CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
        )
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    async def _fetch_entitlement(self, customer_id: str) -> dict[str, Any]:
        async def _op() -> dict[str, Any]:
            async with self._cb.protect("entitlement-system"):
                return await asyncio.to_thread(
                    self._entitlement_mock.get_account, customer_id
                )

        timed = timeout_wrapper(_op, timeout_seconds=self._timeout_seconds)
        try:
            return await retry_with_backoff(
                timed,
                max_retries=self._max_retries,
                base_delay=0.1,
                max_delay=1.0,
                retryable_exceptions=(EntitlementServiceTimeoutError, asyncio.TimeoutError),
            )
        except CircuitOpenError as exc:
            raise ServiceUnavailableError(
                str(exc), extra={"retryAfter": max(1, int(exc.retry_after))}
            ) from exc
        except AccountNotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        except (EntitlementServiceTimeoutError, asyncio.TimeoutError) as exc:
            raise UpstreamServiceError(
                "Entitlement system (NetSuite) timed out.",
                extra={"upstreamService": "entitlement-system", "retryAfter": 5},
            ) from exc

    async def _fetch_usage_graceful(
        self, customer_id: str, months: int
    ) -> tuple[list[UsageSummary], bool]:
        async def _op() -> list[dict[str, Any]]:
            async with self._cb.protect("usage-warehouse"):
                return await asyncio.to_thread(
                    self._usage_mock.get_usage, customer_id, months
                )

        timed = timeout_wrapper(_op, timeout_seconds=self._timeout_seconds)
        try:
            rows = await retry_with_backoff(
                timed,
                max_retries=self._max_retries,
                base_delay=0.1,
                max_delay=0.8,
                retryable_exceptions=(WarehouseUnavailableError, asyncio.TimeoutError),
            )
            return [UsageSummary(**row) for row in rows], False
        except UsageDataNotFoundError:
            return [], False
        except (WarehouseUnavailableError, CircuitOpenError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Usage warehouse degraded during customer aggregation",
                customer_id=customer_id,
                error=str(exc),
            )
            return [], True

    async def _fetch_open_cases_count_graceful(self, customer_id: str) -> tuple[int, bool]:
        async def _op() -> list[dict[str, Any]]:
            async with self._cb.protect("case-system"):
                return await asyncio.to_thread(
                    self._case_mock.list_cases, {"account_id": customer_id}
                )

        timed = timeout_wrapper(_op, timeout_seconds=self._timeout_seconds)
        try:
            cases = await retry_with_backoff(
                timed,
                max_retries=self._max_retries,
                base_delay=0.1,
                max_delay=0.8,
                retryable_exceptions=(CaseServiceTimeoutError, asyncio.TimeoutError),
            )
            open_count = sum(
                1
                for c in cases
                if str(c.get("status", "")).lower() not in {"resolved", "closed"}
            )
            return open_count, False
        except (CaseServiceTimeoutError, CircuitOpenError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Case system degraded during customer aggregation",
                customer_id=customer_id,
                error=str(exc),
            )
            return 0, True

    async def get_customer_summary(
        self,
        customer_id: str,
        *,
        scopes: list[str],
        usage_months: int = 3,
    ) -> tuple[dict[str, Any], str]:
        """Return ``(serialized_customer_dict, etag)`` with scope redaction & graceful degradation."""
        entitlement = await self._fetch_entitlement(customer_id)

        # Fan out usage & case counts concurrently
        (usage, usage_degraded), (open_cases_count, cases_degraded) = await asyncio.gather(
            self._fetch_usage_graceful(customer_id, usage_months),
            self._fetch_open_cases_count_graceful(customer_id),
        )

        degraded_sources: list[str] = []
        if usage_degraded:
            degraded_sources.append("usage-warehouse")
        if cases_degraded:
            degraded_sources.append("case-system")

        contract_end_val = entitlement["contract_end"]
        if isinstance(contract_end_val, str):
            contract_end_val = date.fromisoformat(contract_end_val)

        summary = CustomerSummary(
            id=entitlement["account_id"],
            name=entitlement["name"],
            support_tier=entitlement["support_tier"],
            contract_end=contract_end_val,
            arr_usd=float(entitlement["arr_usd"]),
            usage=usage,
            open_cases_count=open_cases_count,
            degraded_sources=degraded_sources,
        )

        payload = redact_arr(summary.model_dump(mode="json"), scopes)
        etag_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
        return payload, f'"{etag_digest}"'
