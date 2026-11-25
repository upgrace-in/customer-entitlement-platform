"""Token-bucket rate limiter middleware for FastAPI.

Implements a per-client token-bucket algorithm with:

* Configurable bucket capacity and refill rate.
* Standard ``X-RateLimit-*`` response headers on every response.
* ``429 Too Many Requests`` with ``Retry-After`` when the bucket is
  exhausted.

Client identity is derived from the ``Authorization`` header (if present)
or the remote IP address, so authenticated and anonymous callers are
tracked independently.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# ---------------------------------------------------------------------------
# Token bucket implementation
# ---------------------------------------------------------------------------

@dataclass
class _TokenBucket:
    """Single client's token bucket."""

    capacity: float
    refill_rate: float  # tokens per second
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        # Start with a full bucket.
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, tokens: float = 1.0) -> bool:
        """Try to consume *tokens*.  Returns ``True`` on success."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    @property
    def remaining(self) -> int:
        """Tokens available right now (after a silent refill)."""
        self._refill()
        return int(self.tokens)

    @property
    def reset_after(self) -> float:
        """Seconds until at least one token is available."""
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        deficit = 1.0 - self.tokens
        return deficit / self.refill_rate


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client token-bucket rate-limiting middleware.

    Parameters
    ----------
    app:
        The ASGI application.
    capacity:
        Maximum burst size (bucket capacity).
    refill_rate:
        Tokens added per second.

    Usage::

        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, capacity=100, refill_rate=10)
    """

    def __init__(
        self,
        app: Any,
        *,
        capacity: float = 100.0,
        refill_rate: float = 10.0,
    ) -> None:
        super().__init__(app)
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._buckets: dict[str, _TokenBucket] = {}

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _client_key(request: Request) -> str:
        """Derive a rate-limit key from the request.

        Uses the ``Authorization`` header when present so each authenticated
        client gets its own bucket; otherwise falls back to the remote IP.
        """
        auth = request.headers.get("authorization", "")
        if auth:
            return f"auth:{auth}"
        # request.client can be None in test/proxy scenarios.
        host = request.client.host if request.client else "unknown"
        return f"ip:{host}"

    def _get_bucket(self, key: str) -> _TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = _TokenBucket(
                capacity=self.capacity,
                refill_rate=self.refill_rate,
            )
        return self._buckets[key]

    # -- middleware entry point ----------------------------------------------

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        key = self._client_key(request)
        bucket = self._get_bucket(key)

        if not bucket.consume():
            retry_after = max(1.0, bucket.reset_after)
            return JSONResponse(
                status_code=429,
                content={
                    "type": "https://httpproblems.com/http-status/429",
                    "title": "Too Many Requests",
                    "status": 429,
                    "detail": (
                        "Rate limit exceeded. Please wait before retrying."
                    ),
                },
                headers={
                    "Retry-After": str(int(retry_after)),
                    "X-RateLimit-Limit": str(int(self.capacity)),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time() + retry_after)),
                },
            )

        response: Response = await call_next(request)

        # Attach rate-limit headers to every successful response.
        response.headers["X-RateLimit-Limit"] = str(int(self.capacity))
        response.headers["X-RateLimit-Remaining"] = str(bucket.remaining)
        reset_after = bucket.reset_after
        response.headers["X-RateLimit-Reset"] = str(
            int(time.time() + reset_after) if reset_after > 0 else int(time.time())
        )

        return response
