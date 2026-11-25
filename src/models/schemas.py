"""Pydantic v2 schemas for Customer Entitlement & Support Case API.

Defines request/response models, filters, and RFC 9457 error responses.
All models use strict validation and are immutable where appropriate.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class CursorPagination(BaseModel):
    """Cursor-based pagination metadata.

    Cursor pagination is preferred over offset pagination for large datasets
    because it provides stable results even when rows are inserted/deleted
    between pages, and performs better on indexed columns.
    """

    model_config = ConfigDict(frozen=True)

    next_cursor: Optional[str] = Field(
        default=None,
        description="Opaque cursor pointing to the next page. ``None`` when no more pages exist.",
    )
    has_more: bool = Field(
        description="Whether additional pages are available after this one.",
    )
    total_count: int = Field(
        ge=0,
        description="Total number of records matching the current filters.",
    )


# ---------------------------------------------------------------------------
# Case models
# ---------------------------------------------------------------------------

class CaseComment(BaseModel):
    """A single comment attached to a support case."""

    model_config = ConfigDict(frozen=True)

    author: str = Field(
        min_length=1,
        max_length=256,
        description="Display name or ID of the comment author.",
    )
    body: str = Field(
        min_length=1,
        max_length=10_000,
        description="Markdown-formatted comment body.",
    )
    at: datetime = Field(
        description="UTC timestamp when the comment was created.",
    )


class Case(BaseModel):
    """Core support-case representation.

    Priority uses a P1–P4 scale (P1 = critical production outage).
    Status follows a linear lifecycle: open → in_progress → escalated → resolved → closed.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(
        min_length=1,
        description="Unique case identifier (e.g. ``CASE-00042``).",
    )
    account_id: str = Field(
        min_length=1,
        description="Account that owns this case.",
    )
    priority: Literal["P1", "P2", "P3", "P4"] = Field(
        description="Severity level — P1 is highest.",
    )
    status: Literal["open", "in_progress", "escalated", "resolved", "closed"] = Field(
        description="Current lifecycle status.",
    )
    opened_at: datetime = Field(
        description="UTC timestamp when the case was opened.",
    )
    subject: str = Field(
        min_length=1,
        max_length=512,
        description="One-line subject/title.",
    )
    assignee: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Support engineer currently assigned.",
    )
    comments: list[CaseComment] = Field(
        default_factory=list,
        description="Chronologically ordered comments.",
    )
    escalated_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the case was escalated, if applicable.",
    )
    resolved_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the case was resolved, if applicable.",
    )


class CaseResponse(Case):
    """Case payload returned to clients, including an ETag for optimistic concurrency."""

    etag: str = Field(
        min_length=1,
        description="Entity tag for conditional requests (``If-Match`` / ``If-None-Match``).",
    )


class CaseListResponse(BaseModel):
    """Paginated list of cases."""

    model_config = ConfigDict(frozen=True)

    data: list[CaseResponse] = Field(
        description="Cases on this page.",
    )
    pagination: CursorPagination


# ---------------------------------------------------------------------------
# Customer / usage models
# ---------------------------------------------------------------------------

class UsageSummary(BaseModel):
    """Monthly usage snapshot for a customer account."""

    model_config = ConfigDict(frozen=True)

    month: str = Field(
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Year-month in ``YYYY-MM`` format.",
    )
    protected_tb: float = Field(
        ge=0.0,
        description="Terabytes of data protected this month.",
    )
    api_calls: int = Field(
        ge=0,
        description="Total API calls made this month.",
    )
    active_users: int = Field(
        ge=0,
        description="Distinct active users this month.",
    )


class CustomerSummary(BaseModel):
    """Lightweight customer overview shown in dashboards and case side-panels."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=512)
    support_tier: str = Field(
        min_length=1,
        description="E.g. ``standard``, ``premium``, ``enterprise``.",
    )
    contract_end: date = Field(
        description="Date the current contract expires.",
    )
    arr_usd: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Annual recurring revenue in USD — requires ``finance:read`` scope.",
    )
    usage: list[UsageSummary] = Field(
        default_factory=list,
        description="Recent monthly usage snapshots.",
    )
    open_cases_count: int = Field(
        ge=0,
        description="Number of currently open support cases.",
    )
    degraded_sources: list[str] = Field(
        default_factory=list,
        description="Data-source names whose enrichment is currently degraded.",
    )


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CreateCommentRequest(BaseModel):
    """Request body for adding a comment to a case."""

    body: str = Field(
        min_length=1,
        max_length=10_000,
        description="Markdown-formatted comment body.",
    )

    @field_validator("body")
    @classmethod
    def body_must_not_be_blank(cls, v: str) -> str:
        """Reject strings that are only whitespace."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Comment body must contain non-whitespace characters.")
        return stripped


class EscalateRequest(BaseModel):
    """Request body for escalating a support case."""

    reason: str = Field(
        min_length=1,
        max_length=2_000,
        description="Human-readable reason for the escalation.",
    )
    notify_customer: bool = Field(
        default=False,
        description="Whether to send an email notification to the customer.",
    )

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Escalation reason must contain non-whitespace characters.")
        return stripped


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

class CommentCreatedResponse(BaseModel):
    """Response returned when a comment is added to a case."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    comment: CaseComment
    etag: str
    idempotent_replay: bool = False


class WebhookSubscriptionRequest(BaseModel):
    """Request body to register a webhook subscription."""

    url: str = Field(min_length=8, max_length=2048, pattern=r"^https?://")
    events: list[str] = Field(min_length=1)
    secret: Optional[str] = Field(default=None, min_length=16, max_length=256)


class WebhookSubscriptionResponse(BaseModel):
    """Public representation of a registered webhook subscription."""

    model_config = ConfigDict(frozen=True)

    id: str
    url: str
    events: list[str]
    secret: Optional[str] = None
    active: bool
    created_at: str


class WebhookEvent(BaseModel):
    """Inbound webhook event envelope."""

    model_config = ConfigDict(frozen=True)

    event_type: str = Field(
        min_length=1,
        description="Dot-namespaced event type, e.g. ``case.created``.",
    )
    timestamp: datetime = Field(
        description="UTC timestamp when the event was produced.",
    )
    payload: dict[str, Any] = Field(
        description="Event-specific payload.",
    )
    signature: str = Field(
        min_length=1,
        description="HMAC-SHA256 hex digest used to verify authenticity.",
    )


# ---------------------------------------------------------------------------
# Error response (RFC 9457 — Problem Details for HTTP APIs)
# ---------------------------------------------------------------------------

class ProblemDetail(BaseModel):
    """RFC 9457 *Problem Details* error response.

    Provides a machine-readable error format with an optional ``errors``
    list for field-level validation failures.
    """

    model_config = ConfigDict(frozen=True)

    type: str = Field(
        default="about:blank",
        description="URI reference identifying the problem type.",
    )
    title: str = Field(
        description="Short, human-readable summary.",
    )
    status: int = Field(
        ge=100,
        le=599,
        description="HTTP status code.",
    )
    detail: str = Field(
        description="Human-readable explanation specific to this occurrence.",
    )
    instance: Optional[str] = Field(
        default=None,
        description="URI reference identifying this specific occurrence.",
    )
    errors: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Field-level validation errors, if any.",
    )


# ---------------------------------------------------------------------------
# Query / filter helpers
# ---------------------------------------------------------------------------

class CaseFilter(BaseModel):
    """Query-parameter model for filtering support cases.

    All fields are optional — omitted fields are not applied as filters.
    """

    status: Optional[Literal["open", "in_progress", "escalated", "resolved", "closed"]] = Field(
        default=None,
        description="Filter by case status.",
    )
    priority: Optional[Literal["P1", "P2", "P3", "P4"]] = Field(
        default=None,
        description="Filter by priority level.",
    )
    account_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Filter by owning account.",
    )
    opened_after: Optional[datetime] = Field(
        default=None,
        description="Return cases opened after this UTC timestamp.",
    )
    opened_before: Optional[datetime] = Field(
        default=None,
        description="Return cases opened before this UTC timestamp.",
    )
    search: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Free-text search matched against the case subject.",
    )

    @field_validator("search")
    @classmethod
    def sanitize_search(cls, v: Optional[str]) -> Optional[str]:
        """Strip leading/trailing whitespace and collapse internal runs."""
        if v is None:
            return v
        cleaned = re.sub(r"\s+", " ", v.strip())
        return cleaned or None


class CaseSort(BaseModel):
    """Sorting parameters for case listings."""

    sort_by: Literal["opened_at", "priority", "status"] = Field(
        default="opened_at",
        description="Field to sort by.",
    )
    sort_order: Literal["asc", "desc"] = Field(
        default="desc",
        description="Sort direction.",
    )
