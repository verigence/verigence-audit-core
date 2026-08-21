"""Structured logging pipeline for Audit Core.

Call ``configure_logging()`` once, as the very first statement in ``create_app()``.
Everywhere else, obtain a logger at module level::

    import structlog
    logger = structlog.get_logger(__name__)

Never pass the logger as a function argument.  Never use f-strings or string
concatenation inside log calls — use keyword arguments only.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, ClassVar

import structlog
from structlog.types import EventDict, WrappedLogger

from audit_core.config import Settings

# ---------------------------------------------------------------------------
# Internal processors
# ---------------------------------------------------------------------------

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


class _AxiomDrain:
    """Best-effort background drain to Axiom's ingest API."""

    _INGEST_URL: ClassVar[str] = "https://api.axiom.co/v1/datasets/{dataset}/ingest"

    def __init__(self, *, token: str, dataset: str) -> None:
        self._token = token
        self._dataset = dataset
        self._lock = threading.Lock()
        self._stdlib_log = logging.getLogger("audit_core.axiom_drain")

    def __call__(
        self, logger: WrappedLogger, method: str, event_dict: EventDict
    ) -> EventDict:
        import json
        import urllib.request

        payload = json.dumps([event_dict]).encode()
        url = self._INGEST_URL.format(dataset=self._dataset)
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        t = threading.Thread(target=self._send, args=(req,), daemon=True)
        t.start()
        return event_dict

    def _send(self, req: Any) -> None:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(req, timeout=3):
                pass
        except Exception as exc:  # noqa: BLE001
            self._stdlib_log.warning("axiom_drain_failed", exc_info=exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger.

    Must be called **before** any logger is obtained.
    """
    is_dev = settings.environment.lower() in {"local", "dev", "development"}

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _LevelFilter(settings.log_level),
    ]

    if settings.log_axiom and settings.axiom_token and settings.axiom_dataset:
        shared_processors.append(
            _AxiomDrain(token=settings.axiom_token, dataset=settings.axiom_dataset)
        )

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

    # Keep stdlib at WARNING to suppress noisy framework internals
    logging.basicConfig(
        stream=sys.stdout if settings.log_stdout else sys.stderr,
        level=logging.WARNING,
        format="%(message)s",
    )
