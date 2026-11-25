"""
Mock Snowflake-like Usage Warehouse Service.

Loads product usage metrics from usage.json and exposes query methods with
simulated latency and a 10 % "warehouse unavailable" error rate.
"""

import json
import os
import random
import time
import copy
from typing import Any


class WarehouseUnavailableError(Exception):
    """Raised when the mock warehouse simulates an availability failure."""
    pass


class UsageDataNotFoundError(Exception):
    """Raised when no usage data exists for the given account."""
    pass


DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "usage.json")


class UsageMockService:
    """Mock Snowflake-like usage data warehouse.

    Loads per-account monthly usage metrics from usage.json.  Simulated
    latency is 50-200 ms per call, with a **10 %** chance of raising a
    ``WarehouseUnavailableError`` to test retry / circuit-breaker logic.
    """

    def __init__(self, data_path: str = DATA_PATH):
        with open(data_path, "r", encoding="utf-8") as f:
            self._usage_records: list[dict[str, Any]] = json.load(f)
        # Group by account_id for fast lookup
        self._by_account: dict[str, list[dict[str, Any]]] = {}
        for rec in self._usage_records:
            self._by_account.setdefault(rec["account_id"], []).append(rec)
        # Sort each account's records by month descending (most recent first)
        for records in self._by_account.values():
            records.sort(key=lambda r: r["month"], reverse=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _simulate_latency() -> None:
        """Sleep for a random duration between 50 and 200 ms."""
        time.sleep(random.uniform(0.05, 0.2))

    @staticmethod
    def _maybe_unavailable() -> None:
        """Raise WarehouseUnavailableError with a 10 % probability."""
        if random.random() < 0.10:
            raise WarehouseUnavailableError(
                "Snowflake warehouse 'ANALYTICS_WH' is currently suspended "
                "or unavailable.  Please retry after the warehouse resumes.  "
                "(Simulated transient failure.)"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_usage(
        self, account_id: str, months: int = 3
    ) -> list[dict[str, Any]]:
        """Return the most recent *months* of usage data for an account.

        Parameters
        ----------
        account_id : str
            e.g. ``"ACC-007"``
        months : int, optional
            Number of most-recent months to return (default 3).

        Returns
        -------
        list[dict]
            Usage records sorted most-recent-first, each containing:
            ``account_id``, ``month``, ``protected_tb``, ``api_calls``,
            ``active_users``.

        Raises
        ------
        UsageDataNotFoundError
            If no usage records exist for the account.
        WarehouseUnavailableError
            10 % chance on every call.
        """
        self._maybe_unavailable()
        self._simulate_latency()

        records = self._by_account.get(account_id)
        if records is None:
            raise UsageDataNotFoundError(
                f"No usage data found for account '{account_id}'."
            )
        return copy.deepcopy(records[:months])

    def get_all_usage(self) -> list[dict[str, Any]]:
        """Return all usage records across all accounts.

        Raises
        ------
        WarehouseUnavailableError
            10 % chance on every call.
        """
        self._maybe_unavailable()
        self._simulate_latency()
        return copy.deepcopy(self._usage_records)
