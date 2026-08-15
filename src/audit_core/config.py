import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str
    environment: str


def load_settings() -> Settings:
    return Settings(
        service_name=os.getenv("SERVICE_NAME", "verigence-audit-core"),
        environment=os.getenv("APP_ENV", "development"),
    )


settings = load_settings()
