from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class AttendanceSettings:
    database_url: str
    allowed_origins: tuple[str, ...]
    security_jwks_url: str
    security_issuer: str
    security_audience: str
    security_base_url: str
    security_client_id: str
    security_client_secret: str
    storage_endpoint: str
    storage_access_key_id: str
    storage_secret_access_key: str
    storage_bucket: str
    storage_region: str
    timezone_iana: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for Verigence Attendance")
    return value


@lru_cache
def get_settings() -> AttendanceSettings:
    origins = tuple(
        item.strip()
        for item in os.environ.get("VERIGENCE_ATTENDANCE_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    )
    return AttendanceSettings(
        database_url=_required("VERIGENCE_ATTENDANCE_DATABASE_URL"),
        allowed_origins=origins,
        security_jwks_url=_required("SECURITY_JWKS_URL"),
        security_issuer=_required("SECURITY_ISSUER"),
        security_audience=_required("SECURITY_AUDIENCE"),
        security_base_url=_required("SECURITY_BASE_URL"),
        security_client_id=_required("VERIGENCE_ATTENDANCE_SECURITY_CLIENT_ID"),
        security_client_secret=_required("VERIGENCE_ATTENDANCE_SECURITY_CLIENT_SECRET"),
        storage_endpoint=_required("VERIGENCE_ATTENDANCE_STORAGE_ENDPOINT"),
        storage_access_key_id=_required("VERIGENCE_ATTENDANCE_STORAGE_ACCESS_KEY_ID"),
        storage_secret_access_key=_required("VERIGENCE_ATTENDANCE_STORAGE_SECRET_ACCESS_KEY"),
        storage_bucket=_required("VERIGENCE_ATTENDANCE_STORAGE_BUCKET"),
        storage_region=os.environ.get("VERIGENCE_ATTENDANCE_STORAGE_REGION", "auto").strip()
        or "auto",
        timezone_iana=os.environ.get("VERIGENCE_ATTENDANCE_TIMEZONE", "Asia/Kolkata").strip()
        or "Asia/Kolkata",
    )
