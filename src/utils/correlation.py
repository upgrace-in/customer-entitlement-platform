"""Correlation ID middleware and context-variable accessor.

Every inbound HTTP request is assigned a unique *correlation ID* — either
extracted from the ``X-Correlation-ID`` request header or generated as a
UUID-4.  The ID is:

* stored in a :class:`contextvars.ContextVar` so any downstream code
  (logging, DB audit rows, outgoing HTTP calls) can retrieve it without
  passing it through every function signature;
* injected into every response as the ``X-Correlation-ID`` header so
  clients can quote it in bug reports or follow-up requests.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Module-level context variable — importable by logging config, DAL, etc.
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")

HEADER_NAME: str = "X-Correlation-ID"


def get_correlation_id() -> str:
    """Return the correlation ID for the current async context.

    Returns an empty string if called outside a request context.
    """
    return correlation_id_ctx.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that manages per-request correlation IDs.

    Usage::

        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Prefer a client-supplied value; fall back to a fresh UUID-4.
        cid: str = request.headers.get(HEADER_NAME) or uuid.uuid4().hex
        token = correlation_id_ctx.set(cid)
        try:
            response: Response = await call_next(request)
            response.headers[HEADER_NAME] = cid
            return response
        finally:
            correlation_id_ctx.reset(token)
