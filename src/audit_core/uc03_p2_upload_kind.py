"""What a Journey's upload day counts as (product owner 2026-11-23).

"The count should be for new journey for booking or new deliveries.. any document additional
should be considered under journey's updated."

Every tracker, dashboard and My Tracker figure of bookings and deliveries uploaded reads this
one rule, so they cannot drift apart again. Before it, a Journey counted as a booking or a
delivery on every day a page was added to it: on 23 Nov 2026 one PC showed 9 bookings that
were all single pages topped up on Journeys started between 28 Oct and 19 Nov, and 130
deliveries in November for 76 Journeys whose delivery first came in that month.

For each Journey and day it was uploaded to (in the Project's time zone), under the person
who uploaded:

    delivery   the first day a delivery page came for the Journey (a new delivery), whether
               the Journey is new that day or was booked earlier
    booking    the Journey's first day (a new Journey) when no delivery page came that day
    updated    any other day: documents added to a Journey already booked or delivered

A Journey's first day is the earlier of the day it was created and its first upload, so a
Journey opened before uploads were recorded is not read as new. An upload is a file that
came in: one still awaiting its bytes, or cancelled on arrival as the same file again, is not.

Each Journey-day also carries the channel its first file came in on (direct, bulk upload or
Google Drive), so the Tracker's channel table counts each Journey once a day, as new or as an
update (product owner 2026-11-23: "new journey and updates to existing journey"). Pages carried
from a file read on a Journey of its own to the customer's earlier booking (a Drive or bulk file
joined to it, uc03_p2_same_file.carry_documents) count under the person who put the file in and
by that file's channel (product owner 2026-11-24: a Drive file joined to an older booking was
missing from the PC's day, counted as the booking creator's direct upload).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

BOOKING, DELIVERY, UPDATED = "booking", "delivery", "updated"
KINDS = (BOOKING, DELIVERY, UPDATED)

# An upload that counts: the file came in (not awaiting it, not a copy cancelled on arrival).
_UPLOADED = """ub.batch_status <> 'AWAITING_UPLOAD'
               AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)"""

_SQL = f"""
    WITH up AS (
        SELECT ub.journey_id, ub.uploaded_by_actor_id AS person,
               CAST(ub.created_at_utc AT TIME ZONE :tz AS date) AS day,
               count(*) AS files,
               (array_agg(ub.batch_id ORDER BY ub.created_at_utc, ub.batch_id))[1] AS first_batch
        FROM auditcore.p2_upload_batches ub
        WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
          AND (CAST(:me AS text) IS NULL OR CAST(ub.uploaded_by_actor_id AS text) = CAST(:me AS text))
          AND {_UPLOADED}
        GROUP BY 1, 2, 3
    ),
    -- Each of those Journeys' first upload day and first delivery-page day, ever (not only in
    -- the window: a Journey booked last month and added to today is updated, not new).
    firsts AS (
        SELECT ub.journey_id,
               min(CAST(ub.created_at_utc AT TIME ZONE :tz AS date)) AS first_upload,
               min(CAST(ub.created_at_utc AT TIME ZONE :tz AS date)) FILTER (WHERE EXISTS (
                   SELECT 1 FROM auditcore.p2_document_queue q
                   WHERE q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
                     AND q.business_stage = 'DELIVERY')) AS first_delivery
        FROM auditcore.p2_upload_batches ub
        WHERE ub.tenant_id = :t AND ub.journey_id IN (SELECT journey_id FROM up) AND {_UPLOADED}
        GROUP BY ub.journey_id
    )
    SELECT CAST(up.journey_id AS text) AS journey_id, CAST(j.outlet_id AS text) AS outlet_id,
           CAST(up.person AS text) AS person, up.day,
           CASE WHEN up.day = f.first_delivery THEN 'delivery'
                WHEN up.day = LEAST(f.first_upload, CAST(j.created_at_utc AT TIME ZONE :tz AS date)) THEN 'booking'
                ELSE 'updated' END AS kind,
           up.files, COALESCE(c.channel, 'direct') AS channel
    FROM up
    JOIN firsts f ON f.journey_id = up.journey_id
    JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = up.journey_id
    -- The channel of the day's first file to the Journey: a bulk or Drive file read on it, else
    -- direct (by the bulk files' index on the upload, 0258). A page carried from a file read on a
    -- Journey of its own (the copy's activity names it: job "same-file:<that journey>") goes by
    -- the bulk or Drive file read there.
    LEFT JOIN LATERAL (
        SELECT CASE WHEN bool_or(bu.source = 'DRIVE') THEN 'drive' ELSE 'bulk' END AS channel
        FROM auditcore.p2_bulk_files bf
        JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
        WHERE bf.tenant_id = :t
          AND (bf.batch_id = up.first_batch
               OR bf.journey_id IN (
                   SELECT CAST(substr(a.details->>'job', 11) AS uuid)
                   FROM auditcore.p2_activity_events a
                   WHERE a.tenant_id = :t AND a.journey_id = up.journey_id
                     AND a.subject_id = CAST(up.first_batch AS text)
                     AND a.event_type = 'DOCUMENT_COPIED_FROM_DUPLICATE'
                     AND a.details->>'job' ~ '^same-file:[0-9a-fA-F-]{{36}}$'))
        HAVING count(*) > 0
    ) c ON true
"""


def upload_days(connection: Connection, *, tenant_id: str, start: datetime, end: datetime, tz: str,
                actor_id: str | None = None) -> list[dict[str, Any]]:
    """Each Journey, person and day uploaded to in [start, end), with what that day counts as
    (``kind``: booking, delivery or updated), the ``files`` that came in and the ``channel``
    (direct, bulk, drive) of the first of them. ``actor_id``: only that person's uploads."""
    return [dict(r) for r in connection.execute(
        text(_SQL), {"t": tenant_id, "s": start, "e": end, "tz": tz, "me": actor_id},
    ).mappings()]
