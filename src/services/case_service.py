"""Case management service with Anti-Corruption Layer, resilience, and idempotency."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mock_services.case_service import (
    CaseMockService,
    CaseNotFoundError,
    CaseServiceTimeoutError,
)
from mock_services.entitlement_service import (
    AccountNotFoundError,
    EntitlementMockService,
    EntitlementServiceTimeoutError,
)
from src.middleware.auth import CurrentUser
from src.models.database import AuditLog, IdempotencyRecord
from src.models.schemas import (
    CaseComment,
    CaseFilter,
    CaseListResponse,
    CaseResponse,
    CaseSort,
    CursorPagination,
    EscalateRequest,
)
from src.services.errors import (
    BusinessRuleViolationError,
    IdempotencyConflictError,
    PreconditionFailedError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    UpstreamServiceError,
)
from src.services.webhook_service import WebhookService
from src.utils.correlation import get_correlation_id
from src.utils.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    retry_with_backoff,
    timeout_wrapper,
)
from src.utils.security import hash_idempotency_key, sanitize_input

logger = structlog.get_logger(__name__)

# Upstream Salesforce status -> canonical API status
_STATUS_MAP: dict[str, str] = {
    "open": "open",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "waiting on customer": "in_progress",
    "escalated": "escalated",
    "resolved": "resolved",
    "closed": "closed",
}

_PRIORITY_RANK: dict[str, int] = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
_STATUS_RANK: dict[str, int] = {
    "open": 1,
    "in_progress": 2,
    "escalated": 3,
    "resolved": 4,
    "closed": 5,
}
_ESCALATABLE_STATUSES: set[str] = {"open", "in_progress"}


def _compute_etag(case_dict: dict[str, Any]) -> str:
    """Compute a deterministic quoted ETag from canonical case fields."""
    fingerprint_payload = {
        "id": case_dict["id"],
        "status": case_dict["status"],
        "priority": case_dict["priority"],
        "assignee": case_dict.get("assignee"),
        "comments_count": len(case_dict.get("comments", [])),
        "escalated_at": case_dict.get("escalated_at"),
        "resolved_at": case_dict.get("resolved_at"),
        "updated_at": case_dict.get("updated_at"),
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f'"{digest}"'


def _normalize_etag(etag: str) -> str:
    """Normalize weak/unquoted ETags for comparison."""
    cleaned = etag.strip()
    if cleaned.startswith("W/"):
        cleaned = cleaned[2:].strip()
    if not (cleaned.startswith('"') and cleaned.endswith('"')):
        cleaned = f'"{cleaned.strip(chr(34))}"'
    return cleaned


def _encode_cursor(offset: int) -> str:
    payload = json.dumps({"offset": offset}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        offset = int(data["offset"])
        if offset < 0:
            raise ValueError("Negative offset")
        return offset
    except Exception as exc:
        raise BusinessRuleViolationError(f"Invalid pagination cursor: {exc}") from exc


class CaseService:
    """Orchestrates case reads, idempotent comments, and validated escalations."""

    def __init__(
        self,
        case_mock: CaseMockService,
        entitlement_mock: EntitlementMockService,
        webhook_service: WebhookService,
        circuit_breaker: CircuitBreaker | None = None,
        timeout_seconds: float = 3.0,
        max_retries: int = 2,
    ) -> None:
        self._case_mock = case_mock
        self._entitlement_mock = entitlement_mock
        self._webhook_service = webhook_service
        self._cb = circuit_breaker or CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
        )
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    async def _call_case_upstream(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute a synchronous mock case-service method with resilience."""

        async def _op() -> Any:
            async with self._cb.protect("case-system"):
                return await asyncio.to_thread(fn, *args, **kwargs)

        timed_op = timeout_wrapper(_op, timeout_seconds=self._timeout_seconds)
        try:
            return await retry_with_backoff(
                timed_op,
                max_retries=self._max_retries,
                base_delay=0.1,
                max_delay=1.0,
                retryable_exceptions=(CaseServiceTimeoutError, asyncio.TimeoutError),
            )
        except CircuitOpenError as exc:
            raise ServiceUnavailableError(
                str(exc),
                extra={"retryAfter": max(1, int(exc.retry_after))},
            ) from exc
        except CaseNotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        except (CaseServiceTimeoutError, asyncio.TimeoutError) as exc:
            raise UpstreamServiceError(
                "Case system (Salesforce) timed out after retries.",
                extra={"upstreamService": "case-system", "retryAfter": 5},
            ) from exc

    @staticmethod
    def _normalize_case(raw: dict[str, Any]) -> CaseResponse:
        """Anti-Corruption Layer: translate raw upstream Salesforce schema to canonical API schema."""
        raw_status = str(raw.get("status", "open")).strip().lower()
        canonical_status = _STATUS_MAP.get(raw_status, "open")

        comments: list[CaseComment] = []
        for c in raw.get("comments", []):
            comments.append(
                CaseComment(
                    author=c.get("author", "unknown"),
                    body=c.get("body", ""),
                    at=c.get("at") or c.get("timestamp"),
                )
            )

        canonical_dict = {
            "id": raw.get("id") or raw["case_id"],
            "account_id": raw["account_id"],
            "priority": raw["priority"],
            "status": canonical_status,
            "opened_at": raw.get("opened_at") or raw["created_at"],
            "subject": raw["subject"],
            "assignee": raw.get("assignee") or raw.get("assigned_to"),
            "comments": comments,
            "escalated_at": raw.get("escalated_at"),
            "resolved_at": raw.get("resolved_at"),
            "updated_at": raw.get("updated_at"),
        }
        etag = _compute_etag(canonical_dict)
        return CaseResponse(
            id=canonical_dict["id"],
            account_id=canonical_dict["account_id"],
            priority=canonical_dict["priority"],
            status=canonical_dict["status"],  # type: ignore[arg-type]
            opened_at=canonical_dict["opened_at"],
            subject=canonical_dict["subject"],
            assignee=canonical_dict["assignee"],
            comments=comments,
            escalated_at=canonical_dict["escalated_at"],
            resolved_at=canonical_dict["resolved_at"],
            etag=etag,
        )

    async def list_cases(
        self,
        *,
        filters: CaseFilter,
        sort: CaseSort,
        cursor: str | None = None,
        limit: int = 25,
    ) -> CaseListResponse:
        """List cases with filtering, deterministic sorting, and cursor pagination."""
        raw_cases: list[dict[str, Any]] = await self._call_case_upstream(
            self._case_mock.list_cases
        )
        normalized = [self._normalize_case(c) for c in raw_cases]

        # Apply filters
        if filters.status:
            normalized = [c for c in normalized if c.status == filters.status]
        if filters.priority:
            normalized = [c for c in normalized if c.priority == filters.priority]
        if filters.account_id:
            normalized = [c for c in normalized if c.account_id == filters.account_id]
        if filters.opened_after:
            after = (
                filters.opened_after
                if filters.opened_after.tzinfo
                else filters.opened_after.replace(tzinfo=timezone.utc)
            )
            normalized = [c for c in normalized if c.opened_at >= after]
        if filters.opened_before:
            before = (
                filters.opened_before
                if filters.opened_before.tzinfo
                else filters.opened_before.replace(tzinfo=timezone.utc)
            )
            normalized = [c for c in normalized if c.opened_at <= before]
        if filters.search:
            needle = filters.search.lower()
            normalized = [c for c in normalized if needle in c.subject.lower()]

        # Deterministic sorting (tie-broken by id)
        reverse = sort.sort_order == "desc"
        if sort.sort_by == "priority":
            normalized.sort(
                key=lambda c: (_PRIORITY_RANK.get(c.priority, 99), c.id),
                reverse=reverse,
            )
        elif sort.sort_by == "status":
            normalized.sort(
                key=lambda c: (_STATUS_RANK.get(c.status, 99), c.id),
                reverse=reverse,
            )
        else:
            normalized.sort(key=lambda c: (c.opened_at, c.id), reverse=reverse)

        total_count = len(normalized)
        offset = _decode_cursor(cursor) if cursor else 0
        page = normalized[offset : offset + limit]
        has_more = (offset + limit) < total_count
        next_cursor = _encode_cursor(offset + limit) if has_more else None

        return CaseListResponse(
            data=page,
            pagination=CursorPagination(
                next_cursor=next_cursor,
                has_more=has_more,
                total_count=total_count,
            ),
        )

    async def get_case(self, case_id: str) -> CaseResponse:
        """Retrieve a single normalized case with ETag."""
        raw = await self._call_case_upstream(self._case_mock.get_case, case_id)
        return self._normalize_case(raw)

    async def add_comment(
        self,
        *,
        case_id: str,
        body: str,
        idempotency_key: str,
        user: CurrentUser,
        db: AsyncSession,
        if_match: str | None = None,
    ) -> tuple[CaseComment, str, bool]:
        """Idempotently add a comment to a case.

        Returns ``(comment, new_etag, is_replay)``.
        Raises 409 if the same Idempotency-Key is reused with a different body.
        Raises 412 if ``if_match`` is supplied and does not match the current ETag.
        """
        scoped_key = hash_idempotency_key(f"{user.sub}:comment:{case_id}:{idempotency_key}")
        sanitized_body = sanitize_input(body)
        request_hash = hashlib.sha256(sanitized_body.encode("utf-8")).hexdigest()

        now = datetime.now(timezone.utc)
        existing_res = await db.execute(
            select(IdempotencyRecord).where(IdempotencyRecord.key == scoped_key)
        )
        existing = existing_res.scalar_one_or_none()
        if existing is not None:
            expires_at = (
                existing.expires_at
                if existing.expires_at.tzinfo
                else existing.expires_at.replace(tzinfo=timezone.utc)
            )
            if expires_at > now:
                stored = json.loads(existing.response)
                if stored.get("request_hash") != request_hash:
                    raise IdempotencyConflictError(
                        f"Idempotency-Key '{idempotency_key}' was already used with a different request body."
                    )
                comment = CaseComment.model_validate(stored["comment"])
                return comment, stored["etag"], True
            # Expired key: remove and proceed
            await db.delete(existing)
            await db.flush()

        current_case = await self.get_case(case_id)
        if current_case.status == "closed":
            raise BusinessRuleViolationError(
                f"Case '{case_id}' is closed and no longer accepts comments."
            )
        if if_match is not None and _normalize_etag(if_match) != current_case.etag:
            raise PreconditionFailedError(
                f"ETag mismatch: supplied {if_match}, current is {current_case.etag}."
            )

        author = user.email or user.name or user.sub
        raw_comment = await self._call_case_upstream(
            self._case_mock.add_comment,
            case_id,
            sanitized_body,
            author,
        )
        comment = CaseComment(
            author=raw_comment["author"],
            body=raw_comment["body"],
            at=raw_comment.get("at") or raw_comment["timestamp"],
        )
        updated_case = await self.get_case(case_id)

        record_payload = {
            "request_hash": request_hash,
            "comment": comment.model_dump(mode="json"),
            "etag": updated_case.etag,
        }
        db.add(
            IdempotencyRecord(
                key=scoped_key,
                response=json.dumps(record_payload),
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )
        db.add(
            AuditLog(
                correlation_id=get_correlation_id() or None,
                user_id=user.sub,
                action="case.comment_added",
                resource_type="case",
                resource_id=case_id,
                details=json.dumps({"author": author, "length": len(sanitized_body)}),
            )
        )
        await db.flush()
        return comment, updated_case.etag, False

    async def escalate_case(
        self,
        *,
        case_id: str,
        req: EscalateRequest,
        user: CurrentUser,
        if_match: str,
        db: AsyncSession,
    ) -> CaseResponse:
        """Escalate a support case with state-machine rules, ETag guard, and webhook dispatch."""
        current = await self.get_case(case_id)

        if _normalize_etag(if_match) != current.etag:
            raise PreconditionFailedError(
                f"ETag mismatch for case '{case_id}': supplied {if_match}, current is {current.etag}."
            )

        if current.status not in _ESCALATABLE_STATUSES:
            raise BusinessRuleViolationError(
                f"Case '{case_id}' is in status '{current.status}' and cannot be escalated. "
                "Only cases in 'open' or 'in_progress' status can be escalated.",
                extra={
                    "allowedTransitions": [
                        {"from": current.status, "to": ["resolved", "closed"]}
                        if current.status == "escalated"
                        else {"from": current.status, "to": []}
                    ]
                },
            )

        # Look up customer tier to apply SLA routing / auto-notification rules
        support_tier = "Unknown"
        try:
            acct = await asyncio.to_thread(
                self._entitlement_mock.get_account, current.account_id
            )
            support_tier = acct.get("support_tier", "Unknown")
        except (AccountNotFoundError, EntitlementServiceTimeoutError):
            logger.warning(
                "Could not resolve customer tier during escalation; using default routing",
                account_id=current.account_id,
            )

        notify_customer = req.notify_customer or (current.priority == "P1")
        new_assignee = (
            "oncall-platinum@platform.internal"
            if support_tier == "Platinum"
            else "oncall-escalation@platform.internal"
        )
        escalated_at = datetime.now(timezone.utc).isoformat()
        sanitized_reason = sanitize_input(req.reason)

        await self._call_case_upstream(
            self._case_mock.update_case,
            case_id,
            {
                "status": "Escalated",
                "assigned_to": new_assignee,
                "escalated_at": escalated_at,
            },
        )
        await self._call_case_upstream(
            self._case_mock.add_comment,
            case_id,
            f"[ESCALATED by {user.email or user.sub}] Reason: {sanitized_reason} (notify_customer={notify_customer})",
            "system",
        )

        updated = await self.get_case(case_id)

        db.add(
            AuditLog(
                correlation_id=get_correlation_id() or None,
                user_id=user.sub,
                action="case.escalated",
                resource_type="case",
                resource_id=case_id,
                details=json.dumps(
                    {
                        "previous_status": current.status,
                        "new_status": updated.status,
                        "priority": updated.priority,
                        "support_tier": support_tier,
                        "notify_customer": notify_customer,
                        "assignee": new_assignee,
                        "reason": sanitized_reason,
                    }
                ),
            )
        )
        await db.flush()

        await self._webhook_service.dispatch_event(
            event_type="case.escalated",
            payload={
                "case_id": updated.id,
                "account_id": updated.account_id,
                "priority": updated.priority,
                "previous_status": current.status,
                "new_status": updated.status,
                "escalated_by": user.email or user.sub,
                "reason": sanitized_reason,
                "assignee": new_assignee,
                "support_tier": support_tier,
                "notify_customer": notify_customer,
                "subject": updated.subject,
                "escalated_at": escalated_at,
            },
            db=db,
        )

        return updated
