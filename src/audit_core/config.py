import os
from collections.abc import Mapping
from dataclasses import dataclass


class SettingsError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    service_name: str
    environment: str


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise SettingsError(f"Missing required runtime setting: {name}")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if environ is None else environ
    return Settings(
        service_name=source.get("SERVICE_NAME", "verigence-audit-core").strip()
        or "verigence-audit-core",
        environment=_required(source, "APP_ENV"),
    )
