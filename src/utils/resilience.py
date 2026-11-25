"""Resilience utilities — retry, circuit-breaker, timeout, partial failure.

These primitives are intentionally *library-agnostic*: they wrap plain
``async`` callables and compose via decoration or direct invocation, so
they can be applied consistently across HTTP clients, DB calls, and
third-party SDK calls without coupling to any single framework.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


# ---------------------------------------------------------------------------
# Retry with exponential back-off + jitter
# ---------------------------------------------------------------------------

async def retry_with_backoff(
    func: Callable[P, Awaitable[T]],
    *args: P.args,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
    **kwargs: P.kwargs,
) -> T:
    """Execute *func* with exponential back-off and **full jitter**.

    Parameters
    ----------
    func:
        An async callable to retry.
    max_retries:
        Maximum number of retry attempts (excluding the initial call).
    base_delay:
        Base delay in seconds for the first retry.
    max_delay:
        Upper bound on the computed delay (capped before jitter).
    retryable_exceptions:
        Exception types that are eligible for retry.

    Returns
    -------
    T
        The return value of *func* on success.

    Raises
    ------
    Exception
        The last exception if all retries are exhausted.
    """
    last_exc: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_retries:
                logger.error(
                    "All %d retries exhausted for %s: %s",
                    max_retries,
                    func.__qualname__,
                    exc,
                )
                raise

            # Exponential back-off: base * 2^attempt, capped at max_delay.
            delay = min(base_delay * (2 ** attempt), max_delay)
            # Full jitter: uniform [0, delay].
            jittered = random.uniform(0, delay)  # noqa: S311
            logger.warning(
                "Attempt %d/%d for %s failed (%s). Retrying in %.2fs…",
                attempt + 1,
                max_retries + 1,
                func.__qualname__,
                exc,
                jittered,
            )
            await asyncio.sleep(jittered)

    # Should never reach here, but satisfy the type-checker.
    assert last_exc is not None  # noqa: S101
    raise last_exc


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitState(str, enum.Enum):
    """Possible states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, service: str, retry_after: float) -> None:
        self.service = service
        self.retry_after = retry_after
        super().__init__(
            f"Circuit for '{service}' is open. Retry after {retry_after:.1f}s."
        )


@dataclass
class _CircuitStats:
    """Internal per-service circuit state."""

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    success_count_in_half_open: int = 0


class CircuitBreaker:
    """Per-service circuit breaker with closed → open → half-open states.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures before the circuit opens.
    recovery_timeout:
        Seconds to wait in the *open* state before transitioning to
        *half-open*.
    half_open_max_calls:
        Successful calls required in *half-open* before closing the circuit.

    Usage::

        cb = CircuitBreaker()

        async def call_billing():
            async with cb.protect("billing-service"):
                return await http_client.get(...)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._services: dict[str, _CircuitStats] = {}

    # -- helpers -------------------------------------------------------------

    def _stats(self, service: str) -> _CircuitStats:
        if service not in self._services:
            self._services[service] = _CircuitStats()
        return self._services[service]

    def state(self, service: str) -> CircuitState:
        """Return the current state for *service*."""
        return self._stats(service).state

    # -- context manager -----------------------------------------------------

    class _ProtectContext:
        """Async context manager returned by :meth:`CircuitBreaker.protect`."""

        def __init__(self, breaker: CircuitBreaker, service: str) -> None:
            self._breaker = breaker
            self._service = service

        async def __aenter__(self) -> None:
            stats = self._breaker._stats(self._service)
            now = time.monotonic()

            if stats.state is CircuitState.OPEN:
                elapsed = now - stats.last_failure_time
                if elapsed >= self._breaker.recovery_timeout:
                    stats.state = CircuitState.HALF_OPEN
                    stats.success_count_in_half_open = 0
                    logger.info("Circuit for '%s' → half-open", self._service)
                else:
                    raise CircuitOpenError(
                        self._service,
                        self._breaker.recovery_timeout - elapsed,
                    )

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> bool:
            stats = self._breaker._stats(self._service)

            if exc_type is not None:
                # Failure path
                stats.failure_count += 1
                stats.last_failure_time = time.monotonic()

                if stats.state is CircuitState.HALF_OPEN:
                    stats.state = CircuitState.OPEN
                    logger.warning(
                        "Circuit for '%s' → open (failed in half-open)",
                        self._service,
                    )
                elif (
                    stats.state is CircuitState.CLOSED
                    and stats.failure_count >= self._breaker.failure_threshold
                ):
                    stats.state = CircuitState.OPEN
                    logger.warning(
                        "Circuit for '%s' → open (threshold=%d reached)",
                        self._service,
                        self._breaker.failure_threshold,
                    )

                return False  # re-raise the original exception

            # Success path
            if stats.state is CircuitState.HALF_OPEN:
                stats.success_count_in_half_open += 1
                if stats.success_count_in_half_open >= self._breaker.half_open_max_calls:
                    stats.state = CircuitState.CLOSED
                    stats.failure_count = 0
                    logger.info("Circuit for '%s' → closed", self._service)
            else:
                stats.failure_count = 0

            return False

    def protect(self, service: str) -> _ProtectContext:
        """Return an async context manager that guards calls to *service*.

        Raises :class:`CircuitOpenError` if the circuit is open.
        """
        return self._ProtectContext(self, service)


# ---------------------------------------------------------------------------
# Timeout wrapper
# ---------------------------------------------------------------------------

def timeout_wrapper(
    func: Callable[P, Awaitable[T]],
    timeout_seconds: float = 5.0,
) -> Callable[P, Awaitable[T]]:
    """Return a wrapped version of *func* that raises on timeout.

    Parameters
    ----------
    func:
        An async callable to wrap.
    timeout_seconds:
        Maximum wall-clock seconds before :class:`asyncio.TimeoutError` is
        raised.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.wait_for(
            func(*args, **kwargs),
            timeout=timeout_seconds,
        )

    return wrapper


# ---------------------------------------------------------------------------
# Partial-failure mixin
# ---------------------------------------------------------------------------

class PartialFailureResponse(BaseModel):
    """Mixin that tags a response as partially degraded.

    Compose via multiple inheritance::

        class MyResponse(PartialFailureResponse, BaseModel):
            items: list[Item]
    """

    degraded_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Names of upstream data sources that could not be reached. "
            "When non-empty the response may be incomplete."
        ),
    )
