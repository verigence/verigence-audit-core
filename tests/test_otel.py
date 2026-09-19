from typing import Any

from fastapi import FastAPI

from audit_core import otel
from audit_core.config import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "service_name": "verigence-audit-core",
        "environment": "test",
    }
    values.update(overrides)
    return Settings(**values)


def test_observability_is_disabled_by_default() -> None:
    assert otel.configure_otlp(FastAPI(), _settings()) is False


def test_enabled_log_signal_without_endpoint_fails_open(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", raising=False)

    settings = _settings(observability_logs_enabled=True)

    assert otel.configure_otlp(FastAPI(), settings) is False
    otel.emit_otel_log(
        {
            "event": "safe_test_event",
            "correlation_id": "corr-test",
            "secret": "must-never-be-copied",
        }
    )


def test_trace_switch_off_does_not_create_trace_provider(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://example.invalid/v1/traces")
    settings = _settings(observability_traces_enabled=False)

    assert otel._configure_traces(FastAPI(), settings, otel._resource(settings)) is None


def test_errors_only_exports_only_error_events(monkeypatch) -> None:
    emitted: list[dict[str, Any]] = []

    class _Logger:
        def emit(self, **kwargs: Any) -> None:
            emitted.append(kwargs)

    monkeypatch.setattr(otel, "_OTEL_LOGGER", _Logger())
    monkeypatch.setattr(otel, "_EXPORT_ALL_LOGS", False)
    monkeypatch.setattr(otel, "_EXPORT_ERRORS", True)

    otel.emit_otel_log({"event": "request_complete", "level": "info"})
    otel.emit_otel_log(
        {
            "event": "dependency_failed",
            "level": "warning",
            "error_code": "VAC-DEP-001",
            "correlation_id": "corr-1",
            "raw_response": "must-not-be-exported",
        }
    )

    assert len(emitted) == 1
    assert emitted[0]["body"] == "dependency_failed"
    assert emitted[0]["attributes"]["error_code"] == "VAC-DEP-001"
    assert "raw_response" not in emitted[0]["attributes"]
