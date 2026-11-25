"""Unit tests for retry, circuit breaker, timeout, and partial-failure primitives."""

from __future__ import annotations

import asyncio

import pytest

from src.utils.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    retry_with_backoff,
    timeout_wrapper,
)


async def test_retry_with_backoff_recovers_after_transient_errors() -> None:
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient error")
        return "ok"

    result = await retry_with_backoff(flaky, max_retries=3, base_delay=0.01, max_delay=0.02)
    assert result == "ok"
    assert attempts == 3


async def test_circuit_breaker_opens_and_recovers() -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            async with cb.protect("svc"):
                raise RuntimeError("boom")

    assert cb.state("svc") is CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        async with cb.protect("svc"):
            pass

    await asyncio.sleep(0.06)

    async with cb.protect("svc"):
        pass

    assert cb.state("svc") is CircuitState.CLOSED


async def test_timeout_wrapper_raises_timeout_error() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.2)
        return "done"

    wrapped = timeout_wrapper(slow, timeout_seconds=0.02)
    with pytest.raises(asyncio.TimeoutError):
        await wrapped()
