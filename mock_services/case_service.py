"""
Mock Salesforce-like Case Service.

Simulates a CRM case management API with realistic latency and intermittent
timeout errors to test client resilience.
"""

import json
import os
import random
import time
import copy
from datetime import datetime, timezone
from typing import Any


class CaseServiceTimeoutError(Exception):
    """Raised when the mock service simulates a timeout."""
    pass


class CaseNotFoundError(Exception):
    """Raised when a requested case does not exist."""
    pass


DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "cases.json")


class CaseMockService:
    """Mock Salesforce-like case management service.

    Loads case data from cases.json and provides CRUD-style operations with
    simulated network latency (50-200 ms) and a 5 % chance of timeout errors.
    """

    def __init__(self, data_path: str = DATA_PATH):
        with open(data_path, "r", encoding="utf-8") as f:
            self._cases: list[dict[str, Any]] = json.load(f)
        # Index by case_id for O(1) lookups
        self._index: dict[str, dict[str, Any]] = {
            c["case_id"]: c for c in self._cases
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _simulate_latency() -> None:
        """Sleep for a random duration between 50 and 200 ms."""
        time.sleep(random.uniform(0.05, 0.2))

    @staticmethod
    def _maybe_timeout() -> None:
        """Raise a timeout error with a 5 % probability."""
        if random.random() < 0.05:
            raise CaseServiceTimeoutError(
                "CaseService request timed out — upstream Salesforce API "
                "did not respond within the configured deadline (30 s)."
            )

    def _apply_filters(
        self, cases: list[dict[str, Any]], filters: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Return cases matching all supplied filter criteria."""
        if not filters:
            return cases

        results = cases
        for key, value in filters.items():
            if isinstance(value, list):
                results = [c for c in results if c.get(key) in value]
            else:
                results = [c for c in results if c.get(key) == value]
        return results

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_cases(
        self, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Return cases, optionally filtered by field values.

        Parameters
        ----------
        filters : dict, optional
            Key-value pairs to match against case fields.  Values may be
            scalars (exact match) or lists (membership match).
            Example: ``{"priority": "P1", "status": ["Open", "In Progress"]}``

        Returns
        -------
        list[dict]
            Matching cases (deep copies so callers cannot mutate internal state).

        Raises
        ------
        CaseServiceTimeoutError
            5 % chance on every call.
        """
        self._maybe_timeout()
        self._simulate_latency()
        matched = self._apply_filters(self._cases, filters)
        return copy.deepcopy(matched)

    def get_case(self, case_id: str) -> dict[str, Any]:
        """Return a single case by its ID.

        Parameters
        ----------
        case_id : str
            e.g. ``"CS-10231"``

        Raises
        ------
        CaseNotFoundError
            If the case ID does not exist.
        CaseServiceTimeoutError
            5 % chance on every call.
        """
        self._maybe_timeout()
        self._simulate_latency()
        case = self._index.get(case_id)
        if case is None:
            raise CaseNotFoundError(f"Case '{case_id}' not found.")
        return copy.deepcopy(case)

    def add_comment(self, case_id: str, comment: str, author: str = "system") -> dict[str, Any]:
        """Append a comment to an existing case.

        Parameters
        ----------
        case_id : str
            Target case ID.
        comment : str
            Comment body text.
        author : str, optional
            Comment author (defaults to ``"system"``).

        Returns
        -------
        dict
            The newly created comment record.

        Raises
        ------
        CaseNotFoundError
            If the case ID does not exist.
        CaseServiceTimeoutError
            5 % chance on every call.
        """
        self._maybe_timeout()
        self._simulate_latency()

        case = self._index.get(case_id)
        if case is None:
            raise CaseNotFoundError(f"Case '{case_id}' not found.")

        new_comment = {
            "author": author,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "body": comment,
        }
        case["comments"].append(new_comment)
        case["updated_at"] = new_comment["timestamp"]
        return copy.deepcopy(new_comment)

    def update_case(
        self, case_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Update fields on an existing case.

        Parameters
        ----------
        case_id : str
            Target case ID.
        updates : dict
            Field-value pairs to update. Only top-level scalar fields are
            supported (not ``comments``).

        Returns
        -------
        dict
            The full updated case record.

        Raises
        ------
        CaseNotFoundError
            If the case ID does not exist.
        CaseServiceTimeoutError
            5 % chance on every call.
        ValueError
            If ``updates`` attempts to modify ``case_id`` or ``comments``.
        """
        self._maybe_timeout()
        self._simulate_latency()

        case = self._index.get(case_id)
        if case is None:
            raise CaseNotFoundError(f"Case '{case_id}' not found.")

        disallowed = {"case_id", "comments"}
        bad_keys = disallowed & set(updates.keys())
        if bad_keys:
            raise ValueError(
                f"Cannot update protected fields: {bad_keys}"
            )

        case.update(updates)
        case["updated_at"] = datetime.now(timezone.utc).isoformat()
        return copy.deepcopy(case)
