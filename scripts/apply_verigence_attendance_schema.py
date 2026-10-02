from __future__ import annotations

import os
from pathlib import Path

import psycopg


def _database_url() -> str:
    value = os.environ.get("VERIGENCE_ATTENDANCE_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("VERIGENCE_ATTENDANCE_DATABASE_URL is required")
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        if value.startswith(prefix):
            return "postgresql://" + value[len(prefix):]
    return value


def main() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "database" / "VERIGENCE_ATTENDANCE_SCHEMA_v1.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")
    with psycopg.connect(_database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(schema_sql)
        connection.commit()
    print("VERIGENCE_ATTENDANCE_SCHEMA=APPLIED")


if __name__ == "__main__":
    main()
