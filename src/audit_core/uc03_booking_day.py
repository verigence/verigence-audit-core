"""uc03_booking_day.py -- the day a Journey was booked, for the DMS reconciliation.

Product owner 2026-11-22 (replacing the upload-day fallback of 2026-11-07): the booking form's
date; without one, the first money receipt's date -- never the day the PC uploaded the papers,
which "will always give wrong results". Matching a DMS booking row, either date may be within
two days of the DMS's date.

FIRST_RECEIPT_SQL: a CTE body giving each Journey of tenant ``:t`` its first money receipt date:
the earliest of its payments read from an active receipt (the receipts the delivery date rule
reads too, uc03_p2_delivery_dates). A date more than a year back or in the future is a misread
(an "2006" for 2026) and is left out.
"""
from __future__ import annotations

BOOKING_MATCH_DAYS = 2

FIRST_RECEIPT_SQL = """
    SELECT p.journey_id, min(COALESCE(p.receipt_date, p.payment_reference_date)) AS first_receipt
    FROM auditcore.payments p
    JOIN auditcore.evidence e
      ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id AND e.di_document_id = p.source_di_document_id
     AND e.association_status = 'ACTIVE'
    WHERE p.tenant_id = :t AND p.status_source = 'EVIDENCE' AND p.amount > 0
      AND COALESCE(p.receipt_date, p.payment_reference_date) BETWEEN CURRENT_DATE - 366 AND CURRENT_DATE + 1
    GROUP BY p.journey_id
"""

__all__ = ["BOOKING_MATCH_DAYS", "FIRST_RECEIPT_SQL"]
