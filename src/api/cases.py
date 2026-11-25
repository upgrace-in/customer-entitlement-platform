"""Support case endpoints — list, idempotent comment, validated escalation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.middleware.auth import CurrentUser, require_scopes
from src.models.database import get_db
from src.models.schemas import (
    CaseFilter,
    CaseListResponse,
    CaseResponse,
    CaseSort,
    CommentCreatedResponse,
    CreateCommentRequest,
    EscalateRequest,
)
from src.services.case_service import CaseService
from src.services.container import get_case_service
from src.services.errors import BusinessRuleViolationError, PreconditionFailedError

router = APIRouter(prefix="/v1/cases", tags=["Cases"])


@router.get(
    "",
    response_model=CaseListResponse,
    summary="List support cases with filtering, sorting, and cursor pagination",
)
async def list_cases(
    user: Annotated[CurrentUser, require_scopes("cases:read")],
    service: Annotated[CaseService, Depends(get_case_service)],
    status_filter: Annotated[
        Literal["open", "in_progress", "escalated", "resolved", "closed"] | None,
        Query(alias="status"),
    ] = None,
    priority: Annotated[Literal["P1", "P2", "P3", "P4"] | None, Query()] = None,
    account_id: Annotated[str | None, Query(min_length=1)] = None,
    opened_after: Annotated[datetime | None, Query()] = None,
    opened_before: Annotated[datetime | None, Query()] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    sort_by: Annotated[Literal["opened_at", "priority", "status"], Query()] = "opened_at",
    sort_order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> CaseListResponse:
    """List support cases with filtering, sorting, and opaque cursor pagination."""
    _ = user
    filters = CaseFilter(
        status=status_filter,
        priority=priority,
        account_id=account_id,
        opened_after=opened_after,
        opened_before=opened_before,
        search=search,
    )
    sort = CaseSort(sort_by=sort_by, sort_order=sort_order)
    return await service.list_cases(
        filters=filters,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/{case_id}",
    response_model=CaseResponse,
    summary="Get a single support case by ID",
)
async def get_case(
    case_id: str,
    response: Response,
    user: Annotated[CurrentUser, require_scopes("cases:read")],
    service: Annotated[CaseService, Depends(get_case_service)],
) -> CaseResponse:
    """Fetch a single normalized support case and attach its current ETag."""
    _ = user
    case = await service.get_case(case_id)
    response.headers["ETag"] = case.etag
    return case


@router.post(
    "/{case_id}/comments",
    response_model=CommentCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Idempotently add a comment to a support case",
)
async def add_case_comment(
    case_id: str,
    body: CreateCommentRequest,
    response: Response,
    user: Annotated[CurrentUser, require_scopes("cases:write")],
    service: Annotated[CaseService, Depends(get_case_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CommentCreatedResponse:
    """Append a comment using an ``Idempotency-Key`` header.

    * First execution returns ``201 Created``.
    * Duplicate execution with same key + same body replays ``200 OK``.
    * Reusing key with a different body returns ``409 Conflict``.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise BusinessRuleViolationError(
            "Missing required 'Idempotency-Key' header."
        )

    comment, new_etag, is_replay = await service.add_comment(
        case_id=case_id,
        body=body.body,
        idempotency_key=idempotency_key.strip(),
        user=user,
        db=db,
        if_match=if_match,
    )
    response.headers["ETag"] = new_etag
    if is_replay:
        response.status_code = status.HTTP_200_OK

    return CommentCreatedResponse(
        case_id=case_id,
        comment=comment,
        etag=new_etag,
        idempotent_replay=is_replay,
    )


@router.post(
    "/{case_id}/escalate",
    response_model=CaseResponse,
    summary="Escalate a support case with state-machine & ETag checks",
)
async def escalate_case(
    case_id: str,
    body: EscalateRequest,
    response: Response,
    user: Annotated[CurrentUser, require_scopes("cases:write")],
    service: Annotated[CaseService, Depends(get_case_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CaseResponse:
    """Escalate an ``open`` or ``in_progress`` case and emit ``case.escalated`` webhook."""
    if not if_match or not if_match.strip():
        raise PreconditionFailedError(
            "Missing required 'If-Match' header with current case ETag."
        )

    updated = await service.escalate_case(
        case_id=case_id,
        req=body,
        user=user,
        if_match=if_match,
        db=db,
    )
    response.headers["ETag"] = updated.etag
    return updated
