"""One structured stream for structlog and stdlib loggers, with safe exception summaries,
and the correlation id sent on outbound calls."""
from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import httpx
import pytest
import structlog

from audit_core import logging_config
from audit_core.config import load_settings
from audit_core.logging_config import configure_logging, exception_summary, redact_text
from audit_core.otel import install_correlation_propagation


@pytest.fixture
def json_stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    stream = io.StringIO()
    monkeypatch.setattr(logging_config.sys, "stdout", stream)
    monkeypatch.setenv("AUDIT_CORE_LOG_FORMAT", "json")
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    configure_logging(load_settings({"APP_ENV": "dev"}), process="test")
    yield stream
    root.handlers, root.level = saved
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_structlog_and_stdlib_lines_are_json_with_context_and_service(json_stream: io.StringIO) -> None:
    structlog.contextvars.bind_contextvars(correlation_id="c-42")
    structlog.get_logger("audit_core.x").info("structured_event", journey_id="j1")
    logging.getLogger("audit_core.legacy").info("legacy event", extra={"tenant_id": "t1"})
    structured, legacy = _lines(json_stream)
    assert structured["event"] == "structured_event" and structured["correlation_id"] == "c-42"
    assert structured["service"] == "verigence-audit-core" and structured["process"] == "test"
    assert structured["environment"] == "dev" and "timestamp" in structured
    # stdlib INFO is no longer dropped, and keeps its extra fields and the request context.
    assert legacy["event"] == "legacy event" and legacy["tenant_id"] == "t1"
    assert legacy["correlation_id"] == "c-42" and legacy["level"] == "info"


def test_exceptions_are_summarised_without_their_message(json_stream: io.StringIO) -> None:
    try:
        raise ValueError("customer Ravi Kumar PAN ABCDE1234F")
    except ValueError:
        structlog.get_logger("audit_core.x").exception("failed_event")
        logging.getLogger("audit_core.legacy").warning("legacy failure", exc_info=True)
    for line in _lines(json_stream):
        assert line["exc_type"] == "ValueError"
        assert any("test_logging_config.py" in frame for frame in line["exc_stack"])
        assert "exc_info" not in line and "exception" not in line
    assert "Ravi" not in json_stream.getvalue() and "ABCDE1234F" not in json_stream.getvalue()


def test_quiet_libraries_do_not_print_info(json_stream: io.StringIO) -> None:
    logging.getLogger("httpx").info("HTTP Request: PUT https://bucket/key?X-Amz-Signature=abc")
    assert json_stream.getvalue() == ""


def test_database_error_messages_are_kept_but_sql_and_parameters_removed() -> None:
    from sqlalchemy.exc import ProgrammingError

    exc = ProgrammingError("SELECT name FROM t WHERE pan=%(pan)s", {"pan": "ABCDE1234F"}, Exception("column x missing"))
    summary = exception_summary(exc)
    assert "column x missing" in summary["exc_message"]
    assert "ABCDE1234F" not in summary["exc_message"] and "SELECT" not in summary["exc_message"]


@pytest.mark.parametrize(
    ("raw", "hidden"),
    [
        ("PUT https://r2.example/key?X-Amz-Signature=deadbeef failed", "deadbeef"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("password=hunter2 rejected", "hunter2"),
        ("call 9876543210 now", "9876543210"),
        ("mail someone@example.com", "someone@example.com"),
        ("aadhaar 1234 5678 9012", "1234 5678 9012"),
    ],
)
def test_redact_text_hides_secrets_and_identifiers(raw: str, hidden: str) -> None:
    assert hidden not in redact_text(raw)


def test_outbound_calls_carry_the_correlation_id() -> None:
    install_correlation_propagation()
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200))
    structlog.contextvars.bind_contextvars(correlation_id="c-out")
    try:
        with httpx.Client(transport=transport) as client:
            client.get("https://di.example/v2/x")
            client.put("https://bucket.example/k?X-Amz-Signature=abc", content=b"x")
            client.get("https://di.example/v2/y", headers={"X-Correlation-ID": "explicit"})
    finally:
        structlog.contextvars.clear_contextvars()
    assert seen[0].headers["X-Correlation-ID"] == "c-out"
    assert "X-Correlation-ID" not in seen[1].headers  # presigned storage URL left untouched
    assert seen[2].headers["X-Correlation-ID"] == "explicit"
