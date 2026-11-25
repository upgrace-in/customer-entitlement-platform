"""Structured JSON logging with correlation-ID injection.

Uses `structlog <https://www.structlog.org/>`_ for structured, JSON-formatted
log output.  Every log entry automatically includes:

* ``correlation_id`` — from the per-request :mod:`src.utils.correlation`
  context variable.
* ``timestamp`` — ISO-8601 UTC.
* ``level`` — standard Python level name.

Call :func:`setup_logging` once at application startup (e.g. in a FastAPI
lifespan handler).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from src.utils.correlation import get_correlation_id


# ---------------------------------------------------------------------------
# Custom structlog processors
# ---------------------------------------------------------------------------

def _add_correlation_id(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Inject the current correlation ID into every log event."""
    cid = get_correlation_id()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def _add_log_level(
    _logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Normalise the log level to upper-case ``level`` key."""
    event_dict["level"] = method_name.upper()
    return event_dict


# ---------------------------------------------------------------------------
# Public setup function
# ---------------------------------------------------------------------------

def setup_logging(
    *,
    level: int = logging.INFO,
    json_output: bool = True,
) -> None:
    """Configure structured logging for the application.

    Parameters
    ----------
    level:
        Root log level (e.g. ``logging.DEBUG``).
    json_output:
        When ``True`` (the default), render log entries as single-line JSON.
        Set to ``False`` during local development for coloured, human-readable
        output.
    """

    # Shared processor chain.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_id,
        _add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_logger_name,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Formatter that feeds stdlib log records through structlog processors.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    # Root handler — single stream to stdout for container/cloud-native setups.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Quieten noisy third-party loggers.
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.get_logger().info("Structured logging initialised", level=logging.getLevelName(level))
