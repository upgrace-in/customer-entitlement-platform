"""Customer aggregation endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response, status

from src.middleware.auth import CurrentUser, require_scopes
from src.services.container import get_customer_service
from src.services.customer_service import CustomerService

router = APIRouter(prefix="/v1/customers", tags=["Customers"])


@router.get(
    "/{customer_id}",
    summary="Get aggregated customer view (entitlement + usage + open cases)",
)
async def get_customer(
    customer_id: str,
    response: Response,
    user: Annotated[CurrentUser, require_scopes("customers:read")],
    service: Annotated[CustomerService, Depends(get_customer_service)],
    usage_months: Annotated[int, Query(ge=1, le=24)] = 3,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Any:
    """Return aggregated customer view with partial-failure degradation and field-level auth.

    * Requires ``customers:read`` scope.
    * ``arr_usd`` is omitted unless ``finance:read`` is also present in caller scopes.
    * Supports conditional GET via ``If-None-Match`` / ``ETag``.
    """
    payload, etag = await service.get_customer_summary(
        customer_id,
        scopes=user.scopes,
        usage_months=usage_months,
    )

    response.headers["ETag"] = etag
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    return payload
