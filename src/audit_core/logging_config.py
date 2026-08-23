"""Structured logging pipeline for Audit Core.

Audit Core writes one safe local structured stream and, when Phase-1 observability is enabled,
queues the same controlled event metadata through OpenTelemetry. Remote telemetry is never a
business-path dependency.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, ClassVar

import structlog
from structlog.types import EventDict, WrappedLogger

from audit_core.config import Settings
from audit_core.otel import emit_otel_log


class _LevelFilter:
    """Drop log records below *min_level*."""

    _LEVELS: ClassVar[dict[str, int]] = {
        "DEBUG": 10,
        "INFO": 20,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }

    def __init__(self, min_level: str) -> None:
        self._min = self._LEVELS.get(min_level.upper(), 20)

    def __call__(
        self, logger: WrappedLogger, method: str, event_dict: EventDict
    ) -> EventDict:
        level_str = event_dict.get("level", "info").upper()
        if self._LEVELS.get(level_str, 20) < self._min:
            raise structlog.DropEvent()
        return event_dict


class _OtelLogQueue:
    """Copy an allow-listed event into the SDK's bounded background log processor."""

    def __call__(
        self, logger: WrappedLogger, method: str, event_dict: EventDict
    ) -> EventDict:
        emit_otel_log(event_dict)
        return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure concise structured logging without synchronous remote I/O."""
    is_dev = settings.environment.lower() in {"local", "dev", "development"}

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _LevelFilter(settings.log_level),
        _OtelLogQueue(),
    ]

    if is_dev:
        renderer: Any = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(
            file=sys.stdout if settings.log_stdout else sys.stderr
        ),
        cache_logger_on_first_use=True,
    )

    # Keep stdlib at WARNING to suppress framework/internal success noise.
    logging.basicConfig(
        stream=sys.stdout if settings.log_stdout else sys.stderr,
        level=logging.WARNING,
        format="%(message)s",
    )
