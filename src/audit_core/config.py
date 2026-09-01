import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEV_WEB_ORIGIN = "https://verigence-web-dev.jbrconsulting-it.workers.dev"
_DEV_ENVIRONMENTS = {"dev", "development"}


class SettingsError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    service_name: str
    environment: str
    log_level: str = field(default="INFO")
    log_stdout: bool = field(default=True)
    # Legacy direct-Axiom fields remain parseable for configuration compatibility but are no
    # longer used. Phase-1 remote export uses standard OTEL_EXPORTER_OTLP_* variables only.
    log_axiom: bool = field(default=False)
    axiom_token: str = field(default="")
    axiom_dataset: str = field(default="")
    observability_enabled: bool = field(default=False)
    success_events_enabled: bool = field(default=True)
    trace_spans_enabled: bool = field(default=False)
    observability_export_timeout_seconds: float = field(default=2.0)
    observability_batch_delay_ms: int = field(default=1000)
    observability_max_queue_size: int = field(default=2048)
    observability_max_export_batch_size: int = field(default=512)
    observability_metric_export_interval_ms: int = field(default=60000)
    cors_allowed_origins: tuple[str, ...] = field(default_factory=tuple)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise SettingsError(f"Missing required runtime setting: {name}")
    return value


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"Invalid runtime setting: {name}") from exc
    if value <= 0:
        raise SettingsError(f"Invalid runtime setting: {name}")
    return value


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"Invalid runtime setting: {name}") from exc
    if value <= 0:
        raise SettingsError(f"Invalid runtime setting: {name}")
    return value


def _cors_allowed_origins(environ: Mapping[str, str], environment: str) -> tuple[str, ...]:
    configured = tuple(
        origin.strip()
        for origin in environ.get("AUDIT_CORE_CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )
    if configured:
        return configured
    if environment.strip().lower() in _DEV_ENVIRONMENTS:
        return (DEV_WEB_ORIGIN,)
    return ()


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if environ is None else environ
    environment = _required(source, "APP_ENV")
    max_queue_size = _positive_int(source, "OBSERVABILITY_MAX_QUEUE_SIZE", 2048)
    max_export_batch_size = _positive_int(
        source,
        "OBSERVABILITY_MAX_EXPORT_BATCH_SIZE",
        512,
    )
    if max_export_batch_size > max_queue_size:
        raise SettingsError(
            "Invalid runtime setting: OBSERVABILITY_MAX_EXPORT_BATCH_SIZE exceeds queue size"
        )
    return Settings(
        service_name=source.get("SERVICE_NAME", "verigence-audit-core").strip()
        or "verigence-audit-core",
        environment=environment,
        log_level=source.get("AUDIT_CORE_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        log_stdout=(source.get("AUDIT_CORE_LOG_STDOUT", "true").strip().lower() != "false"),
        log_axiom=(source.get("AUDIT_CORE_LOG_AXIOM", "false").strip().lower() == "true"),
        axiom_token=source.get("AUDIT_CORE_AXIOM_TOKEN", "").strip(),
        axiom_dataset=source.get("AUDIT_CORE_AXIOM_DATASET", "").strip(),
        observability_enabled=(
            source.get("OBSERVABILITY_ENABLED", "false").strip().lower() == "true"
        ),
        success_events_enabled=(
            source.get("AUDIT_CORE_SUCCESS_EVENTS_ENABLED", "true").strip().lower() != "false"
        ),
        trace_spans_enabled=(
            source.get("AUDIT_CORE_TRACE_SPANS_ENABLED", "false").strip().lower() == "true"
        ),
        observability_export_timeout_seconds=_positive_float(
            source,
            "OBSERVABILITY_EXPORT_TIMEOUT_SECONDS",
            2.0,
        ),
        observability_batch_delay_ms=_positive_int(
            source,
            "OBSERVABILITY_BATCH_DELAY_MS",
            1000,
        ),
        observability_max_queue_size=max_queue_size,
        observability_max_export_batch_size=max_export_batch_size,
        observability_metric_export_interval_ms=_positive_int(
            source,
            "OBSERVABILITY_METRIC_EXPORT_INTERVAL_MS",
            60000,
        ),
        cors_allowed_origins=_cors_allowed_origins(source, environment),
    )
