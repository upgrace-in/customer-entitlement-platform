"""Domain exceptions mapped cleanly to RFC 9457 ProblemDetail HTTP responses."""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    """Base exception for domain and platform errors."""

    status_code: int = 500
    problem_type: str = "https://api.example.com/problems/internal-error"
    title: str = "Internal Server Error"

    def __init__(
        self,
        detail: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.detail = detail
        self.extra = extra or {}
        super().__init__(detail)


class ResourceNotFoundError(PlatformError):
    status_code = 404
    problem_type = "https://api.example.com/problems/not-found"
    title = "Resource Not Found"


class IdempotencyConflictError(PlatformError):
    status_code = 409
    problem_type = "https://api.example.com/problems/idempotency-conflict"
    title = "Idempotency Key Conflict"


class PreconditionFailedError(PlatformError):
    status_code = 412
    problem_type = "https://api.example.com/problems/precondition-failed"
    title = "Precondition Failed"


class BusinessRuleViolationError(PlatformError):
    status_code = 422
    problem_type = "https://api.example.com/problems/invalid-state-transition"
    title = "Business Rule Violation"


class UpstreamServiceError(PlatformError):
    status_code = 502
    problem_type = "https://api.example.com/problems/upstream-failure"
    title = "Upstream Service Failure"


class ServiceUnavailableError(PlatformError):
    status_code = 503
    problem_type = "https://api.example.com/problems/service-unavailable"
    title = "Service Unavailable"
