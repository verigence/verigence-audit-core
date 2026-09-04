from __future__ import annotations
from functools import lru_cache
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class WhatsAppSettings(BaseSettings):
    wa_app_secret: SecretStr = Field(...)
    wa_access_token: SecretStr = Field(...)
    wa_verify_token: SecretStr = Field(...)
    wa_media_concurrent_per_contact: int = Field(default=3, ge=1, le=10)
    wa_media_concurrent_global: int = Field(default=20, ge=1, le=100)
    wa_debounce_seconds: int = Field(default=90, ge=10, le=600)
    wa_di_poll_interval_seconds: int = Field(default=30, ge=5, le=300)
    wa_di_sla_minutes: int = Field(default=15, ge=1, le=120)
    evidence_service_base_url: str = Field(default="http://localhost:8000")
    wa_flush_scheduler_interval_seconds: int = Field(default=60)
    wa_redaction_enabled: bool = Field(default=False)
    wa_default_locale: str = Field(default="en")

    class Config:
        env_prefix = ""
        case_sensitive = False

    @model_validator(mode="after")
    def _check_secrets_non_empty(self) -> "WhatsAppSettings":
        for field_name in ("wa_app_secret", "wa_access_token", "wa_verify_token"):
            if not getattr(self, field_name).get_secret_value().strip():
                raise ValueError(f"{field_name} must not be empty")
        return self


@lru_cache(maxsize=1)
def get_wa_settings() -> WhatsAppSettings:
    return WhatsAppSettings()  # type: ignore[call-arg]
