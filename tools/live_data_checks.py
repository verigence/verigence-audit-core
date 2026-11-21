"""Live data checks after every DEV deploy (product owner 2026-11-20, after Utkal's delivery
sheets of 19-20 Nov lost every booking date on upload and August bookings showed Inscope: "how
come we are introducing so many bugs").

Each check is a rule the stored data must keep, read only, on the live database. A broken rule
fails the job with what broke and a few examples; nothing is changed. Tests prove the code on the
rows they are given; these prove it on the files the dealerships actually upload.

    python tools/live_data_checks.py            # DATABASE_URL or LIVE_CHECKS_DATABASE_URL
"""
from __future__ import annotations

import os
import sys

import psycopg

# Rows of dumps still in use: a superseded upload is history, not what the screens show.
_ACTIVE = """
    FROM auditcore.p2_dms_dump_rows r
    JOIN auditcore.p2_dms_dumps u ON u.tenant_id = r.tenant_id AND u.dump_id = r.dump_id
    WHERE u.status = 'ACTIVE'
"""

# (name, what must hold, SQL selecting the rows that break it: file, row, detail)
CHECKS: list[tuple[str, str, str]] = [
    (
        "delivery-booking-date-read",
        "A delivery row whose sheet prints a BOOKING DATE has it stored (it decides Inscope / Outscope).",
        f"""SELECT u.file_name, r.row_no, r.raw->>'BOOKING DATE' {_ACTIVE}
            AND r.dump_kind = 'DELIVERY' AND r.booking_date IS NULL
            AND coalesce(r.raw->>'BOOKING DATE', '') ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'""",
    ),
    (
        "delivery-booked-before-sep-not-inscope",
        "A delivery booked before 1 Sep 2026 is not Inscope (product owner 2026-11-11 / 2026-11-19).",
        f"""SELECT u.file_name, r.row_no, r.booking_date::text {_ACTIVE}
            AND r.dump_kind = 'DELIVERY' AND r.deal_type = 'INSCOPE' AND r.booking_date < DATE '2026-09-01'""",
    ),
    (
        "delivery-chassis-read",
        "A delivery row whose sheet prints a chassis number has it stored (deliveries match on it).",
        f"""SELECT u.file_name, r.row_no, coalesce(r.raw->>'CHASSIS NO', r.raw->>'CHASSIS NUMBER', r.raw->>'VIN') {_ACTIVE}
            AND r.dump_kind = 'DELIVERY' AND coalesce(r.chassis, '') = ''
            AND coalesce(r.raw->>'CHASSIS NO', r.raw->>'CHASSIS NUMBER', r.raw->>'VIN', '') <> ''""",
    ),
    (
        "dump-row-date-read",
        "Every dump row has its booking / delivery date (the DMS date decides the report's day).",
        f"""SELECT u.file_name, r.row_no, r.dump_kind {_ACTIVE} AND r.event_date IS NULL""",
    ),
    (
        "dump-row-customer-read",
        "Every dump row has its customer name (PCs search and claim intimations by it).",
        f"""SELECT u.file_name, r.row_no, r.dump_kind {_ACTIVE} AND coalesce(r.customer_name, '') = ''
            AND coalesce(r.raw->>'CUSTOMER NAME', '') <> ''""",
    ),
]


def run(url: str) -> int:
    broken = 0
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only = on")
        for name, rule, sql in CHECKS:
            count = connection.execute(f"SELECT count(*) FROM ({sql}) x").fetchone()[0]
            if not count:
                print(f"PASS {name}")
                continue
            broken += 1
            examples = connection.execute(f"{sql} LIMIT 5").fetchall()
            shown = "; ".join(f"{f} row {n}: {d}" for f, n, d in examples)
            print(f"::error title=Live data check {name}::{count} row(s) break it. {rule} e.g. {shown}")
    return 1 if broken else 0


if __name__ == "__main__":
    target = os.environ.get("LIVE_CHECKS_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not target:
        sys.exit("LIVE_CHECKS_DATABASE_URL or DATABASE_URL is required")
    sys.exit(run(target.replace("postgresql+psycopg://", "postgresql://", 1)))
