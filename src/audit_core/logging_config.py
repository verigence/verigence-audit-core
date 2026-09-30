"""Structured logging pipeline for Audit Core.

Audit Core writes one safe local structured stream and, when Phase-1 observability is enabled,
queues the same controlled event metadata through OpenTelemetry. Remote telemetry is never a
business-path dependency.

Every line -- structlog or stdlib, API or P2 worker -- is rendered by the same pipeline: JSON on
Railway (console only for ``APP_ENV=local`` or ``AUDIT_CORE_LOG_FORMAT=console``), with the
request/work context (``correlation_id`` etc.), ``service``/``environment``/``version`` and, for
failures, a redacted exception summary (type, sanitised message, stack frames) instead of the raw
exception text.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, ClassVar

import structlog
from structlog.types import EventDict, WrappedLogger

from audit_core.config import Settings
from audit_core.otel import emit_otel_log

_CONSOLE_ENVIRONMENTS = {"local"}
# Libraries whose INFO lines are noise or carry URLs with signatures/query strings.
_QUIET_LIBRARIES = ("httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3", "sqlalchemy.engine")
# The local telemetry fallback sink logs every metric point at INFO; keep it silent as before.
_QUIET_AUDIT_CORE_LOGGERS = ("audit_core.telemetry",)
_MAX_EXC_MESSAGE = 300
_MESSAGE_SAFE_MODULES = {"sqlalchemy", "psycopg", "psycopg2"}
_MAX_STACK_FRAMES = 12

# Exception text can carry SQL, bound parameters, signed URLs or document values.
_REDACTIONS = (
    (re.compile(r"\[SQL:.*?\](?=\s*(\[|\(|$))", re.DOTALL), "[SQL: <redacted>]"),
    (re.compile(r"\[parameters:.*?\](?=\s*(\[|\(|$))", re.DOTALL), "[parameters: <redacted>]"),
    (re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*"), r"\1?<redacted>"),
    (re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"), r"\1<redacted>"),
    (re.compile(r"(?i)\b((password|secret|token|api[_-]?key)s?\b[\s=:]+)[^\s,;'\"]+"), r"\1<redacted>"),
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "<pan>"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "<id-number>"),
    (re.compile(r"(?<!\d)(\+?91[\s-]?)?[6-9]\d{9}(?!\d)"), "<mobile>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
)


def release_version() -> str:
    """The deployed commit: from the environment, else the BUILD_SHA file CI writes into the
    upload (Railway builds from an upload without git metadata)."""
    value = os.getenv("VERIGENCE_GIT_SHA") or os.getenv("RAILWAY_GIT_COMMIT_SHA") or ""
    if not value:
        for candidate in (Path(__file__).resolve().parents[2] / "BUILD_SHA", Path.cwd() / "BUILD_SHA"):
            try:
                value = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                break
    return value[:12] or "unknown"


def redact_text(value: str, limit: int = _MAX_EXC_MESSAGE) -> str:
    """Exception/dependency text made safe to log or store."""
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def exception_summary(exc: BaseException) -> dict[str, Any]:
    """Type, sanitised message, the innermost stack frames and the causal chain -- enough to find
    the failing line without logging raw values. ``pgcode`` is the Postgres SQLSTATE, if any."""
    frames = traceback.extract_tb(exc.__traceback__)[-_MAX_STACK_FRAMES:]
    summary: dict[str, Any] = {
        "exc_type": type(exc).__name__,
        "exc_stack": [f"{_short_path(f.filename)}:{f.lineno} in {f.name}" for f in frames],
    }
    # Free-text messages can name people or quote document values, which no pattern can
    # reliably remove. Database errors are the exception: their text ("column ... does not
    # exist", constraint names) is what diagnoses them, and SQL/parameters are stripped.
    if type(exc).__module__.split(".")[0] in _MESSAGE_SAFE_MODULES:
        summary["exc_message"] = redact_text(str(exc))
    original = getattr(exc, "orig", None)
    pgcode = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if pgcode:
        summary["pgcode"] = str(pgcode)
    chain: list[str] = []
    cause = exc.__cause__ or exc.__context__
    while cause is not None and len(chain) < 4:
        chain.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
    if chain:
        summary["exc_chain"] = chain
    return summary


def _short_path(filename: str) -> str:
    marker = f"{os.sep}audit_core{os.sep}"
    if marker in filename:
        return "audit_core/" + filename.split(marker, 1)[1].replace(os.sep, "/")
    parts = filename.replace(os.sep, "/").split("/")
    return "/".join(parts[-2:])


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


class _ServiceFields:
    """service / environment / version on every line, so logs from all replicas and the worker
    can be told apart and tied to a release."""

    def __init__(self, settings: Settings, process: str) -> None:
        self._fields = {
            "service": settings.service_name,
            "environment": settings.environment,
            "version": release_version(),
            "process": process,
        }

    def __call__(self, logger: WrappedLogger, method: str, event_dict: EventDict) -> EventDict:
        for key, value in self._fields.items():
            event_dict.setdefault(key, value)
        return event_dict


def _safe_exception(logger: WrappedLogger, method: str, event_dict: EventDict) -> EventDict:
    """Replace ``exc_info`` with a redacted summary. Raw tracebacks would print exception
    messages (SQL parameters, document values, signed URLs); the frames alone locate the bug."""
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict
    if isinstance(exc_info, BaseException):
        exc = exc_info
    elif isinstance(exc_info, tuple):
        exc = exc_info[1]
    else:
        exc = sys.exc_info()[1]
    if exc is not None:
        for key, value in exception_summary(exc).items():
            event_dict.setdefault(key, value)
    return event_dict


class _OtelLogQueue:
    """Copy an allow-listed event into the SDK's bounded background log processor."""

    def __call__(
        self, logger: WrappedLogger, method: str, event_dict: EventDict
    ) -> EventDict:
        emit_otel_log(event_dict)
        return event_dict


_HEALTH_PATHS = ("/health", "/healthz", "/ready")


class _AccessLogFields(logging.Filter):
    """uvicorn access lines as fields (method, path without query string, status); health
    probes dropped. The query string can carry identifiers or signatures."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        _client, method, full_path, _http_version, status = args[:5]
        path = str(full_path).split("?", 1)[0]
        if path in _HEALTH_PATHS:
            return False
        record.msg, record.args = "http_access", ()
        record.method, record.path, record.status_code = method, path, status
        return True


def _use_console(settings: Settings) -> bool:
    chosen = os.getenv("AUDIT_CORE_LOG_FORMAT", "").strip().lower()
    if chosen in {"json", "console"}:
        return chosen == "console"
    return settings.environment.strip().lower() in _CONSOLE_ENVIRONMENTS


def configure_logging(settings: Settings, *, process: str = "api") -> None:
    """Configure concise structured logging without synchronous remote I/O."""
    stream = sys.stdout if settings.log_stdout else sys.stderr
    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if _use_console(settings)
        else structlog.processors.JSONRenderer(default=str)
    )
    context_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _ServiceFields(settings, process),
        _safe_exception,
    ]

    structlog.configure(
        processors=context_processors + [_LevelFilter(settings.log_level), _OtelLogQueue(), renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        # No caching: a module logger first used under one configuration would otherwise keep it
        # forever (tests, the worker re-configuring). The per-call cost is negligible here.
        cache_logger_on_first_use=False,
    )

    # stdlib loggers (our older modules, alembic, libraries) go through the same processors, so
    # their ``extra=`` fields, correlation_id and redacted exceptions appear like everyone else's.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_logger_name,
            structlog.stdlib.ExtraAdder(),
            *context_processors,
        ],
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    for name in _QUIET_LIBRARIES + _QUIET_AUDIT_CORE_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # uvicorn installs its own plain-text handlers before the app is imported; send its lines
    # through this pipeline instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers = []
        server_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessLogFields) for f in access.filters):
        access.addFilter(_AccessLogFields())
