"""MCP Server wrapping the Customer Entitlement & Support Case REST API.

Exposes deliberate, LLM-optimized tools with strict JSON schemas, enums over
free text, token-efficient responses, OBO auth propagation, HITL write
confirmations, prompt-injection scanning, PII/ARR redaction, rate limits,
and full audit logging.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

# Prevent local `/assign/mcp/` directory from shadowing the installed `mcp` SDK package.
_THIS_DIR = str(Path(__file__).resolve().parent)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_cleaned_path = [p for p in sys.path if p not in ("", _THIS_DIR, _PROJECT_ROOT)]
_orig_path = sys.path[:]
try:
    sys.path = _cleaned_path
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
finally:
    # Ensure project root is available for `src.*` and `mcp_*` imports while keeping loaded `mcp` SDK cached.
    sys.path = [_PROJECT_ROOT, *_orig_path]

# Import local sibling modules via importlib by file path so local `mcp/` never collides with `mcp` SDK in sys.modules.
import importlib.util


def _load_local(module_name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, Path(_THIS_DIR) / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


_auth_mod = _load_local("assign_mcp_auth", "auth.py")
_guardrails_mod = _load_local("assign_mcp_guardrails", "guardrails.py")

AgentAuthContext = _auth_mod.AgentAuthContext
default_env_auth_context = _auth_mod.default_env_auth_context
GuardrailSuite = _guardrails_mod.GuardrailSuite


class SupportCopilotToolkit:
    """Core MCP tool implementation decoupled from transport so it works over stdio, HTTP, or in-process ASGI tests."""

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        guardrails: GuardrailSuite | None = None,
    ) -> None:
        self.api_base_url = (api_base_url or os.environ.get("API_BASE_URL", "http://localhost:8000")).rstrip("/")
        self._external_client = http_client
        self.guardrails = guardrails or GuardrailSuite()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: AgentAuthContext,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        req_headers = {
            "Authorization": f"Bearer {auth.bearer_token}",
            "X-Correlation-ID": f"mcp-{uuid.uuid4().hex[:12]}",
            **(headers or {}),
        }
        if self._external_client is not None:
            return await self._external_client.request(
                method, path, params=params, json=json_body, headers=req_headers
            )
        async with httpx.AsyncClient(base_url=self.api_base_url, timeout=10.0) as client:
            return await client.request(
                method, path, params=params, json=json_body, headers=req_headers
            )

    def _scrub_text(self, text: str) -> tuple[str, bool]:
        scan = self.guardrails.injection_detector.scan(text)
        redacted = self.guardrails.pii_redactor.redact(scan.sanitized_text)
        return redacted, scan.detected

    async def search_cases(
        self,
        *,
        auth: AgentAuthContext,
        status: Literal["open", "in_progress", "escalated", "resolved", "closed"] | None = None,
        priority: Literal["P1", "P2", "P3", "P4"] | None = None,
        account_id: str | None = None,
        opened_after: str | None = None,
        search: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search support cases and return token-efficient, injection-scrubbed summaries."""
        self.guardrails.rate_limiter.check_and_record(auth.agent_id)
        params: dict[str, Any] = {"limit": max(1, min(limit, 50))}
        if status:
            params["status"] = status
        if priority:
            params["priority"] = priority
        if account_id:
            params["account_id"] = account_id
        if opened_after:
            params["opened_after"] = opened_after
        if search:
            params["search"] = self.guardrails.sanitizer.clean(search)

        resp = await self._request("GET", "/v1/cases", auth=auth, params=params)
        if resp.status_code >= 400:
            err = resp.json()
            self.guardrails.audit_logger.record(
                agent_id=auth.agent_id,
                user_id=auth.user_id,
                tool_name="search_cases",
                params=params,
                status="error",
                result_summary=err.get("detail", str(resp.status_code)),
            )
            return {"error": err.get("title", "API Error"), "detail": err.get("detail", "")}

        raw = resp.json()
        now = datetime.now(timezone.utc)
        injection_warnings: list[str] = []
        compact_cases: list[dict[str, Any]] = []

        for item in raw.get("data", []):
            opened_dt = datetime.fromisoformat(item["opened_at"].replace("Z", "+00:00"))
            age_hours = round((now - opened_dt).total_seconds() / 3600.0, 1)

            subj, subj_inj = self._scrub_text(item["subject"])
            if subj_inj:
                injection_warnings.append(f"{item['id']}:subject")

            for idx, cmt in enumerate(item.get("comments", [])):
                _, cmt_inj = self._scrub_text(cmt.get("body", ""))
                if cmt_inj:
                    injection_warnings.append(f"{item['id']}:comment[{idx}]")

            compact_cases.append(
                {
                    "id": item["id"],
                    "account_id": item["account_id"],
                    "priority": item["priority"],
                    "status": item["status"],
                    "subject": subj,
                    "age_hours": age_hours,
                    "comment_count": len(item.get("comments", [])),
                    "etag": item["etag"],
                }
            )

        result: dict[str, Any] = {
            "cases": compact_cases,
            "total_count": raw.get("pagination", {}).get("total_count", len(compact_cases)),
            "has_more": raw.get("pagination", {}).get("has_more", False),
        }
        if injection_warnings:
            result["security_warning"] = (
                f"Potential prompt injection detected and neutralized in: {', '.join(injection_warnings)}"
            )

        limited = self.guardrails.size_limiter.enforce(result)
        self.guardrails.audit_logger.record(
            agent_id=auth.agent_id,
            user_id=auth.user_id,
            tool_name="search_cases",
            params=params,
            status="ok",
            result_summary=f"returned {len(compact_cases)} cases",
        )
        return limited

    async def get_customer_summary(
        self,
        *,
        auth: AgentAuthContext,
        customer_id: str,
    ) -> dict[str, Any]:
        """Fetch an aggregated customer summary without leaking raw payloads or unauthorized ARR."""
        self.guardrails.rate_limiter.check_and_record(auth.agent_id)
        resp = await self._request("GET", f"/v1/customers/{customer_id}", auth=auth)
        if resp.status_code >= 400:
            err = resp.json()
            self.guardrails.audit_logger.record(
                agent_id=auth.agent_id,
                user_id=auth.user_id,
                tool_name="get_customer_summary",
                params={"customer_id": customer_id},
                status="error",
                result_summary=err.get("detail", str(resp.status_code)),
            )
            return {"error": err.get("title", "API Error"), "detail": err.get("detail", "")}

        data = resp.json()
        contract_end = date.fromisoformat(data["contract_end"])
        days_until_renewal = (contract_end - date.today()).days

        usage_rows = data.get("usage", [])
        latest_usage = usage_rows[0] if usage_rows else None
        usage_trend = "unavailable"
        if len(usage_rows) >= 2:
            delta = usage_rows[0]["api_calls"] - usage_rows[1]["api_calls"]
            usage_trend = "growing" if delta > 0 else ("declining" if delta < 0 else "flat")
        elif latest_usage:
            usage_trend = "steady"

        summary: dict[str, Any] = {
            "customer_id": data["id"],
            "name": data["name"],
            "tier": data["support_tier"],
            "renewal_date": data["contract_end"],
            "days_until_renewal": days_until_renewal,
            "open_cases": data["open_cases_count"],
            "usage_trend": usage_trend,
            "latest_usage": latest_usage,
            "degraded_sources": data.get("degraded_sources", []),
        }
        # Defense in depth: even if the API token had finance:read, only expose ARR if explicitly present.
        if "arr_usd" in data and auth.has_scope("finance:read"):
            summary["arr_usd"] = data["arr_usd"]
        else:
            summary["arr_redacted"] = True

        limited = self.guardrails.size_limiter.enforce(summary)
        self.guardrails.audit_logger.record(
            agent_id=auth.agent_id,
            user_id=auth.user_id,
            tool_name="get_customer_summary",
            params={"customer_id": customer_id},
            status="ok",
            result_summary=f"tier={data['support_tier']} open_cases={data['open_cases_count']}",
        )
        return limited

    async def add_case_comment(
        self,
        *,
        auth: AgentAuthContext,
        case_id: str,
        comment: str,
        confirmed: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add a comment to a support case (requires human confirmation)."""
        self.guardrails.rate_limiter.check_and_record(auth.agent_id)

        scan = self.guardrails.injection_detector.scan(comment)
        if scan.detected:
            self.guardrails.audit_logger.record(
                agent_id=auth.agent_id,
                user_id=auth.user_id,
                tool_name="add_case_comment",
                params={"case_id": case_id},
                status="blocked_injection",
                result_summary=f"patterns={scan.matched_patterns}",
            )
            return {
                "error": "Blocked by Security Guardrail",
                "detail": f"Comment rejected due to prompt-injection patterns: {scan.matched_patterns}",
            }

        gate = self.guardrails.confirmation.require_confirmation(
            action_name="add_case_comment",
            target_id=case_id,
            risk_level="medium",
            summary=f"Append comment ({len(comment)} chars) to case {case_id}",
            confirmed=confirmed,
        )
        if gate is not None:
            self.guardrails.audit_logger.record(
                agent_id=auth.agent_id,
                user_id=auth.user_id,
                tool_name="add_case_comment",
                params={"case_id": case_id, "confirmed": False},
                status="awaiting_confirmation",
                result_summary="HITL gate triggered",
            )
            return gate

        clean_comment = self.guardrails.sanitizer.clean(comment)[:2000]
        idem_key = idempotency_key or str(uuid.uuid4())
        resp = await self._request(
            "POST",
            f"/v1/cases/{case_id}/comments",
            auth=auth,
            json_body={"body": clean_comment},
            headers={"Idempotency-Key": idem_key},
        )
        if resp.status_code >= 400:
            err = resp.json()
            return {"error": err.get("title", "API Error"), "detail": err.get("detail", "")}

        data = resp.json()
        result = {
            "case_id": case_id,
            "comment_added": True,
            "timestamp": data["comment"]["at"],
            "etag": data["etag"],
        }
        self.guardrails.audit_logger.record(
            agent_id=auth.agent_id,
            user_id=auth.user_id,
            tool_name="add_case_comment",
            params={"case_id": case_id, "confirmed": True},
            status="ok",
            result_summary=f"comment added at {result['timestamp']}",
        )
        return result

    async def escalate_case(
        self,
        *,
        auth: AgentAuthContext,
        case_id: str,
        reason: str,
        notify_customer: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """High-risk write: escalate a case (requires explicit confirmation and fetches ETag)."""
        self.guardrails.rate_limiter.check_and_record(auth.agent_id)

        scan = self.guardrails.injection_detector.scan(reason)
        if scan.detected:
            self.guardrails.audit_logger.record(
                agent_id=auth.agent_id,
                user_id=auth.user_id,
                tool_name="escalate_case",
                params={"case_id": case_id},
                status="blocked_injection",
                result_summary=f"patterns={scan.matched_patterns}",
            )
            return {
                "error": "Blocked by Security Guardrail",
                "detail": f"Escalation blocked due to prompt-injection patterns: {scan.matched_patterns}",
            }

        gate = self.guardrails.confirmation.require_confirmation(
            action_name="escalate_case",
            target_id=case_id,
            risk_level="high",
            summary=f"Escalate case {case_id} (notify_customer={notify_customer})",
            confirmed=confirmed,
        )
        if gate is not None:
            self.guardrails.audit_logger.record(
                agent_id=auth.agent_id,
                user_id=auth.user_id,
                tool_name="escalate_case",
                params={"case_id": case_id, "confirmed": False},
                status="awaiting_confirmation",
                result_summary="High-risk HITL gate triggered",
            )
            return gate

        # Fetch current ETag for optimistic concurrency
        get_resp = await self._request("GET", f"/v1/cases/{case_id}", auth=auth)
        if get_resp.status_code >= 400:
            err = get_resp.json()
            return {"error": err.get("title", "API Error"), "detail": err.get("detail", "")}
        current_etag = get_resp.json()["etag"]

        clean_reason = self.guardrails.sanitizer.clean(reason)[:2000]
        resp = await self._request(
            "POST",
            f"/v1/cases/{case_id}/escalate",
            auth=auth,
            json_body={"reason": clean_reason, "notify_customer": notify_customer},
            headers={"If-Match": current_etag},
        )
        if resp.status_code >= 400:
            err = resp.json()
            return {"error": err.get("title", "API Error"), "detail": err.get("detail", "")}

        data = resp.json()
        result = {
            "case_id": data["id"],
            "new_status": data["status"],
            "escalated_at": data["escalated_at"],
            "assignee": data["assignee"],
            "notifications_sent": notify_customer or data["priority"] == "P1",
        }
        self.guardrails.audit_logger.record(
            agent_id=auth.agent_id,
            user_id=auth.user_id,
            tool_name="escalate_case",
            params={"case_id": case_id, "confirmed": True},
            status="ok",
            result_summary=f"escalated to {data['assignee']}",
        )
        return result


def build_mcp_server(toolkit: SupportCopilotToolkit | None = None) -> MCPServer:
    """Create the MCPServer instance wired with all 4 deliberate LLM tools."""
    tk = toolkit or SupportCopilotToolkit()
    mcp_server = MCPServer(
        name="customer-support-platform",
        instructions=(
            "Internal Support Copilot tools for searching support cases, viewing "
            "aggregated customer health, adding comments, and escalating urgent cases. "
            "Always request user confirmation before passing confirmed=true on write tools."
        ),
    )

    @mcp_server.tool(
        name="search_cases",
        description=(
            "Search support cases with filters. Returns compact case summaries with "
            "priority, status, subject, age_hours, and comment_count. Use this to find "
            "cases by status, priority level, customer account, or date range."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def search_cases_tool(
        status: Literal["open", "in_progress", "escalated", "resolved", "closed"] | None = None,
        priority: Literal["P1", "P2", "P3", "P4"] | None = None,
        account_id: str | None = None,
        opened_after: str | None = None,
        search: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return await tk.search_cases(
            auth=default_env_auth_context(),
            status=status,
            priority=priority,
            account_id=account_id,
            opened_after=opened_after,
            search=search,
            limit=limit,
        )

    @mcp_server.tool(
        name="get_customer_summary",
        description=(
            "Get a customer's complete profile including support tier, renewal date, "
            "days_until_renewal, usage_trend, and open_cases count. ARR is automatically "
            "redacted unless the caller holds the finance:read scope."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def get_customer_summary_tool(customer_id: str) -> dict[str, Any]:
        return await tk.get_customer_summary(
            auth=default_env_auth_context(),
            customer_id=customer_id,
        )

    @mcp_server.tool(
        name="add_case_comment",
        description=(
            "Add a comment to a support case (WRITE operation). First call with "
            "confirmed=false to preview; pass confirmed=true only after user approval."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    async def add_case_comment_tool(
        case_id: str,
        comment: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return await tk.add_case_comment(
            auth=default_env_auth_context(),
            case_id=case_id,
            comment=comment,
            confirmed=confirmed,
        )

    @mcp_server.tool(
        name="escalate_case",
        description=(
            "HIGH RISK WRITE: Escalate a support case to on-call engineering and fire "
            "case.escalated webhooks. Must pass confirmed=true ONLY after explicit human approval."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    async def escalate_case_tool(
        case_id: str,
        reason: str,
        notify_customer: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return await tk.escalate_case(
            auth=default_env_auth_context(),
            case_id=case_id,
            reason=reason,
            notify_customer=notify_customer,
            confirmed=confirmed,
        )

    return mcp_server


if __name__ == "__main__":
    transport: Literal["stdio", "sse", "streamable-http"] = (
        "streamable-http" if "--http" in sys.argv else "stdio"
    )
    server = build_mcp_server()
    server.run(transport=transport)
