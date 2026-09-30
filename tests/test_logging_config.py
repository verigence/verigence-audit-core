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


def test_outbound_calls_carry_the_correlation_id_and_are_logged_once() -> None:
    """Every outbound call is logged once with its response time (2026-09-30),
    path only: a presigned signature in the query never reaches the log."""
    from structlog.testing import capture_logs

    install_correlation_propagation()
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(503 if request.url.path == "/v2/y" else 200)
    )
    structlog.contextvars.bind_contextvars(correlation_id="c-out")
    try:
        with capture_logs() as logs, httpx.Client(transport=transport) as client:
            client.get("https://verigence-di-dev.example/v2/x")
            client.put("https://bucket.storage.example/k?X-Amz-Signature=abc", content=b"x")
            client.get("https://verigence-di-dev.example/v2/y", headers={"X-Correlation-ID": "explicit"})
    finally:
        structlog.contextvars.clear_contextvars()
    assert seen[0].headers["X-Correlation-ID"] == "c-out"
    assert "X-Correlation-ID" not in seen[1].headers  # presigned storage URL left untouched
    assert seen[2].headers["X-Correlation-ID"] == "explicit"
    lines = [line for line in logs if line["event"] == "outbound_request"]
    assert [(line["dependency"], line["method"], line["path"], line["status_code"], line["log_level"]) for line in lines] == [
        ("DI", "GET", "/v2/x", 200, "info"), ("STORAGE", "PUT", "/k", 200, "info"), ("DI", "GET", "/v2/y", 503, "warning"),
    ]
    assert all(line["duration_ms"] >= 0 for line in lines)
    assert "abc" not in repr(logs)


def test_uvicorn_access_lines_are_dropped_in_favour_of_the_request_line(json_stream: io.StringIO) -> None:
    """The middleware logs every request once with its response time
    (http_request); uvicorn's access line would only repeat it."""
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/health", "1.1", 200)
    access.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/v1/projects?token=abc", "1.1", 200)
    assert _lines(json_stream) == []
    assert "abc" not in json_stream.getvalue()


def test_release_version_comes_from_the_build_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("VERIGENCE_GIT_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "BUILD_SHA").write_text("0123456789abcdef\n")
    monkeypatch.setattr(logging_config, "__file__", str(tmp_path / "x" / "y" / "z.py"))
    assert logging_config.release_version() == "0123456789ab"
