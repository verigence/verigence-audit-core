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
    log_axiom: bool = field(default=False)
    axiom_token: str = field(default="")
    axiom_dataset: str = field(default="")
    cors_allowed_origins: tuple[str, ...] = field(default_factory=tuple)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise SettingsError(f"Missing required runtime setting: {name}")
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
    return Settings(
        service_name=source.get("SERVICE_NAME", "verigence-audit-core").strip()
        or "verigence-audit-core",
        environment=environment,
        log_level=source.get("AUDIT_CORE_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        log_stdout=(source.get("AUDIT_CORE_LOG_STDOUT", "true").strip().lower() != "false"),
        log_axiom=(source.get("AUDIT_CORE_LOG_AXIOM", "false").strip().lower() == "true"),
        axiom_token=source.get("AUDIT_CORE_AXIOM_TOKEN", "").strip(),
        axiom_dataset=source.get("AUDIT_CORE_AXIOM_DATASET", "").strip(),
        cors_allowed_origins=_cors_allowed_origins(source, environment),
    )
