from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine

from audit_core.verigence_attendance.settings import get_settings


@lru_cache
def attendance_engine() -> Engine:
    """Dedicated low-footprint pool; never shares Audit Core's runtime engine."""
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=1,
        pool_timeout=3,
        pool_recycle=600,
    )


def get_connection() -> Iterator[Connection]:
    with attendance_engine().begin() as connection:
        yield connection
