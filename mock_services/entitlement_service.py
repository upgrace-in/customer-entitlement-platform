"""
Mock NetSuite-like Entitlement Service.

Loads account / entitlement data from both CSV and XML sources and exposes
query methods with simulated network latency.
"""

import csv
import os
import random
import time
import copy
import xml.etree.ElementTree as ET
from typing import Any


class EntitlementServiceTimeoutError(Exception):
    """Raised when the mock service simulates a timeout."""
    pass


class AccountNotFoundError(Exception):
    """Raised when a requested account does not exist."""
    pass


CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "entitlements.csv")
XML_PATH = os.path.join(os.path.dirname(__file__), "data", "entitlements.xml")


class EntitlementMockService:
    """Mock NetSuite-like entitlement / account service.

    Loads account data from *both* CSV and XML files (demonstrating
    multi-format ingestion).  The canonical in-memory representation is a
    list of dicts.  Simulated latency is 50-200 ms per call with a 5 %
    timeout probability.
    """

    def __init__(
        self,
        csv_path: str = CSV_PATH,
        xml_path: str = XML_PATH,
    ):
        self._accounts: list[dict[str, Any]] = []
        self._load_csv(csv_path)
        self._reconcile_xml(xml_path)
        # Index by account_id
        self._index: dict[str, dict[str, Any]] = {
            a["account_id"]: a for a in self._accounts
        }

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    def _load_csv(self, path: str) -> None:
        """Primary load from CSV."""
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                self._accounts.append({
                    "account_id": row["account_id"],
                    "name": row["name"],
                    "support_tier": row["support_tier"],
                    "contract_end": row["contract_end"],
                    "arr_usd": int(row["arr_usd"]),
                })

    def _reconcile_xml(self, path: str) -> None:
        """Cross-check / enrich from XML source.

        In a real integration you might reconcile discrepancies between
        NetSuite and an ERP feed.  Here we simply verify that every account
        in the XML exists in the CSV-loaded set and log a warning otherwise.
        """
        tree = ET.parse(path)
        root = tree.getroot()
        csv_ids = {a["account_id"] for a in self._accounts}

        for account_el in root.findall("account"):
            xml_id = account_el.get("id")
            if xml_id and xml_id not in csv_ids:
                # In production this would trigger an alert / reconciliation
                # workflow.  For the mock we silently add it.
                self._accounts.append({
                    "account_id": xml_id,
                    "name": account_el.findtext("name", ""),
                    "support_tier": account_el.findtext("tier", ""),
                    "contract_end": account_el.findtext("renewal", ""),
                    "arr_usd": int(account_el.findtext("arr_usd", "0")),
                })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _simulate_latency() -> None:
        time.sleep(random.uniform(0.05, 0.2))

    @staticmethod
    def _maybe_timeout() -> None:
        if random.random() < 0.05:
            raise EntitlementServiceTimeoutError(
                "EntitlementService request timed out — upstream NetSuite API "
                "did not respond within the configured deadline (30 s)."
            )

    def _apply_filters(
        self, accounts: list[dict[str, Any]], filters: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if not filters:
            return accounts
        results = accounts
        for key, value in filters.items():
            if isinstance(value, list):
                results = [a for a in results if a.get(key) in value]
            else:
                results = [a for a in results if a.get(key) == value]
        return results

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_account(self, account_id: str) -> dict[str, Any]:
        """Return a single account by ID.

        Parameters
        ----------
        account_id : str
            e.g. ``"ACC-007"``

        Raises
        ------
        AccountNotFoundError
            If the account ID does not exist.
        EntitlementServiceTimeoutError
            5 % chance per call.
        """
        self._maybe_timeout()
        self._simulate_latency()
        account = self._index.get(account_id)
        if account is None:
            raise AccountNotFoundError(
                f"Account '{account_id}' not found."
            )
        return copy.deepcopy(account)

    def list_accounts(
        self, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Return accounts, optionally filtered.

        Parameters
        ----------
        filters : dict, optional
            Key-value filter pairs.  Values may be scalars or lists.
            Example: ``{"support_tier": ["Platinum", "Gold"]}``

        Returns
        -------
        list[dict]
            Matching account records.

        Raises
        ------
        EntitlementServiceTimeoutError
            5 % chance per call.
        """
        self._maybe_timeout()
        self._simulate_latency()
        matched = self._apply_filters(self._accounts, filters)
        return copy.deepcopy(matched)
