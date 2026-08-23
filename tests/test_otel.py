from fastapi import FastAPI

from audit_core.config import Settings
from audit_core.otel import configure_otlp, emit_otel_log


def test_observability_is_disabled_by_default() -> None:
    settings = Settings(
        service_name="verigence-audit-core",
        environment="test",
    )

    assert configure_otlp(FastAPI(), settings) is False


def test_enabled_observability_without_endpoints_fails_open(monkeypatch) -> None:
    for name in (
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(
        service_name="verigence-audit-core",
        environment="test",
        observability_enabled=True,
    )

    assert configure_otlp(FastAPI(), settings) is False
    # Remote log export being unavailable must remain a no-op, never an application failure.
    emit_otel_log(
        {
            "event": "safe_test_event",
            "correlation_id": "corr-test",
            "secret": "must-never-be-copied",
        }
    )
