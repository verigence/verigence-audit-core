"""Journey list views: which journeys a PC or a TL / PM sees on each tab.

Product owner 2026-10-20: a Project brings about 1,200 journeys a month, so
the list is split into tabs, filtered and paged on the server (50 at a time)
with every tab's count, rather than loading every journey into the browser.

PC (their own outlets):
  needs_action  open, with an open PC task (the oldest due first)
  processing    open, pages still being read or retried, nothing for the PC
  with_tl       open, waiting on a TL / PM decision, nothing for the PC
  closed        delivered or cancelled
  all

TL / PM (exceptions first):
  needs_tl      open, with an open TL / PM task (the oldest due first)
  high_risk     open, with an open Critical / High finding
  overdue       open, with an open task past its due time
  stuck         open, with a page that failed to process or a rejected scan
  all, closed

Every flag comes from a small set (open tasks, pages in flight or failed,
open serious findings, closed stages) joined to the journeys in scope -- no
per-journey tenant-wide aggregation.

``month`` narrows the list to a month. By default it is the month the journey
was started. With ``month_basis`` = ``business`` (the Team Lead dashboard's
calendar month, product owner 2026-11-07: "all bookings/deliveries needs to be
handled on calendar month") a journey is in the month when its booking date or
its delivery date falls in it (India time):

  booking date   auditcore.bookings.booking_date; a date before the programme's
                 date floor is a misread and, like a missing one, gives way to
                 the day the journey was started
  delivery date  the delivery date on the vehicle's gate pass (the date the
                 list shows), else the recorded delivery date, else the day the
                 delivery was started; none while the journey is at booking

``bookings_in_month`` / ``deliveries_in_month`` count the journeys whose
booking / delivery date is in the month.

``dealer_id``, ``outlet_id`` and ``pc_actor_id`` narrow the journeys in scope
(an Executive or Project Manager sees every Dealership of the Project and picks
one, 2026-11-09).

The search box matches the customer's name, the Dealership, the Outlet, the
vehicle model and the Journey reference / id as text, and a mobile number by
its digits (product owner 2026-11-09: "search by customer name, mobile
number"). A mobile search ignores spaces, dashes, a +91 country code and a
leading 0; four digits match the number's last four (as before), five or more
match anywhere in the number -- on the customer record or on a customer phone
read from one of the Journey's own documents (mobile_search_pattern).
"""
from __future__ import annotations

import re
from typing import Any

from audit_core.uc03_p2_runtime import swept_pending_sql

PC_VIEWS = ("needs_action", "processing", "with_tl", "closed", "all")
TL_VIEWS = ("needs_tl", "high_risk", "overdue", "stuck", "all", "closed")
ALL_VIEWS = frozenset((*PC_VIEWS, *TL_VIEWS, "open"))

_IN_FLIGHT = ("QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING",
              "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT")
_IN_FLIGHT_SQL = ", ".join(f"'{s}'" for s in _IN_FLIGHT)

# The journeys in the caller's scope with every flag a tab is decided on.
# Parameters: tenant_id, actor_id, search, mobile_pattern, dealer_id, outlet_id, pc_actor_id, month,
# month_basis, date_floor, timezone (scope_params builds the filter ones).
# No literal braces below: BREAKDOWN_SQL is filled in with str.format.
SCOPED_SQL = f"""
    open_tasks AS (
        SELECT t.journey_id,
               bool_or(t.assigned_role_code = 'PC') AS pc_task,
               bool_or(t.assigned_role_code IN ('TL', 'PM')) AS tl_task,
               bool_or(t.due_at_utc < now()) AS overdue,
               MIN(t.due_at_utc) FILTER (WHERE t.assigned_role_code = 'PC') AS pc_due,
               MIN(t.due_at_utc) FILTER (WHERE t.assigned_role_code IN ('TL', 'PM')) AS tl_due
        FROM auditcore.p2_tasks t
        WHERE t.tenant_id = :tenant_id
          AND t.task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
        GROUP BY t.journey_id
    ),
    pages AS (
        SELECT q.journey_id,
               bool_or(q.queue_status IN ({_IN_FLIGHT_SQL})) AS processing,
               bool_or(q.queue_status IN ('FAILED', 'DEAD_LETTER') OR {swept_pending_sql("q")}) AS stuck
        FROM auditcore.p2_document_queue q
        WHERE q.tenant_id = :tenant_id
          AND (q.queue_status IN ({_IN_FLIGHT_SQL}, 'FAILED', 'DEAD_LETTER') OR {swept_pending_sql("q")})
        GROUP BY q.journey_id
        UNION ALL
        SELECT b.journey_id, false, true
        FROM auditcore.p2_upload_batches b
        WHERE b.tenant_id = :tenant_id AND b.batch_status = 'FAILED'
    ),
    risky AS (
        SELECT DISTINCT f.journey_id
        FROM auditcore.audit_findings f
        WHERE f.tenant_id = :tenant_id AND f.finding_status IN ('OPEN', 'ACKNOWLEDGED')
          AND f.severity IN ('CRITICAL', 'HIGH')
    ),
    finished AS (
        SELECT DISTINCT st.journey_id
        FROM auditcore.journey_stage_states st
        WHERE st.tenant_id = :tenant_id
          AND ((st.stage_code = 'DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
               OR (st.stage_code = 'BOOKING'
                   AND (st.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
                        OR st.closure_disposition = 'NO_DELIVERY')))
    ),
    -- Journeys with a customer phone read from one of their own documents that
    -- matches the searched mobile number; nothing is read without one.
    phone_hits AS (
        SELECT DISTINCT f.journey_id
        FROM auditcore.journey_document_extracted_fields f
        JOIN auditcore.evidence e
          ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
         AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
        WHERE CAST(:mobile_pattern AS varchar) IS NOT NULL
          AND f.tenant_id = :tenant_id AND f.field_key = 'customer_phone'
          AND regexp_replace(COALESCE(f.effective_value::text, ''), '[^0-9]', '', 'g')
              LIKE CAST(:mobile_pattern AS varchar)
    ),
    scoped AS (
        SELECT j.tenant_id, j.journey_id, j.journey_reference, j.customer_id, j.dealer_id,
               j.outlet_id, j.created_at_utc, j.updated_at_utc, j.created_by_actor_id,
               COALESCE(c.legal_name, c.display_name) AS customer_name, c.mobile_last4,
               d.dealer_name, o.outlet_name, o.outlet_code,
               NULLIF(concat_ws(' · ',
                 NULLIF(jp.model_name_snapshot,''),
                 NULLIF(jp.variant_name_snapshot,''),
                 NULLIF(jp.colour_name_snapshot,'')
               ), '') AS vehicle,
               j.created_by_display_name AS pc_name,
               bm.booking_month, bm.delivery_month,
               (fin.journey_id IS NOT NULL) AS is_closed,
               COALESCE(ot.pc_task, false) AS pc_task,
               COALESCE(ot.tl_task, false) AS tl_task,
               COALESCE(ot.overdue, false) AS overdue,
               ot.pc_due, ot.tl_due,
               COALESCE(pg.processing, false) AS processing,
               COALESCE(pg.stuck, false) AS stuck,
               (rk.journey_id IS NOT NULL) AS high_risk,
               -- In delivery as the row's own Delivery chip and the Booking &
               -- Delivery headline count it (journeys:summary): a Phase 2
               -- journey by its stage record, a Phase 1 one by a delivery row.
               CASE WHEN pr.journey_id IS NOT NULL
                    THEN (pr.current_stage = 'BOOKING_COMPLETE' OR pr.current_stage LIKE 'DELIVERY%')
                    ELSE EXISTS (SELECT 1 FROM auditcore.deliveries dl
                                 WHERE dl.tenant_id = j.tenant_id AND dl.journey_id = j.journey_id)
                         OR EXISTS (SELECT 1 FROM auditcore.journey_stage_states ds
                                    WHERE ds.tenant_id = j.tenant_id AND ds.journey_id = j.journey_id
                                      AND ds.stage_code = 'DELIVERY') END AS in_delivery
        FROM auditcore.journeys j
        JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
        JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        JOIN auditcore.dealer_outlets o
          ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
        LEFT JOIN auditcore.p2_journey_runtime pr ON pr.tenant_id=j.tenant_id AND pr.journey_id=j.journey_id
        LEFT JOIN finished fin ON fin.journey_id = j.journey_id
        LEFT JOIN open_tasks ot ON ot.journey_id = j.journey_id
        LEFT JOIN (SELECT journey_id, bool_or(processing) AS processing, bool_or(stuck) AS stuck
                   FROM pages GROUP BY journey_id) pg ON pg.journey_id = j.journey_id
        LEFT JOIN risky rk ON rk.journey_id = j.journey_id
        -- The calendar month of the booking and of the delivery; only worked out
        -- when the month is read on that basis (the WHERE is a one-time filter).
        LEFT JOIN LATERAL (
            SELECT to_char(COALESCE(
                     CASE WHEN bk.booking_date >= CAST(:date_floor AS date) THEN bk.booking_date END,
                     CAST(j.created_at_utc AT TIME ZONE :timezone AS date)), 'YYYY-MM') AS booking_month,
                   to_char(COALESCE(
                     (SELECT CAST(btrim(f.effective_value::text, '"') AS date)
                      FROM auditcore.evidence e
                      JOIN auditcore.journey_document_extracted_fields f
                        ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
                       AND f.di_document_id = e.di_document_id
                      WHERE e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                        AND e.association_status = 'ACTIVE' AND e.document_type_key = 'gate_pass'
                        AND f.field_key = 'delivery_date'
                        AND btrim(f.effective_value::text, '"') ~ '^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$'
                        AND CAST(btrim(f.effective_value::text, '"') AS date) >= CAST(:date_floor AS date)
                      ORDER BY e.linked_at_utc DESC LIMIT 1),
                     CASE WHEN CAST(dl.actual_delivered_at AS date) >= CAST(:date_floor AS date)
                          THEN CAST(dl.actual_delivered_at AS date) END,
                     CAST(dst.first_started_at_utc AT TIME ZONE :timezone AS date)), 'YYYY-MM') AS delivery_month
            FROM (SELECT 1) one
            LEFT JOIN auditcore.bookings bk ON bk.tenant_id = j.tenant_id AND bk.journey_id = j.journey_id
            LEFT JOIN auditcore.deliveries dl ON dl.tenant_id = j.tenant_id AND dl.journey_id = j.journey_id
            LEFT JOIN auditcore.journey_stage_states dst
              ON dst.tenant_id = j.tenant_id AND dst.journey_id = j.journey_id AND dst.stage_code = 'DELIVERY'
            WHERE CAST(:month_basis AS varchar) = 'business'
            LIMIT 1
        ) bm ON true
        WHERE j.tenant_id = :tenant_id
          AND EXISTS (
            SELECT 1 FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = j.tenant_id AND ba.security_actor_id = :actor_id
              AND ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
              AND (ba.dealer_id IS NULL
                   OR (ba.dealer_id = j.dealer_id AND (ba.outlet_id IS NULL OR ba.outlet_id = j.outlet_id)))
          )
          AND (CAST(:dealer_id AS uuid) IS NULL OR j.dealer_id = CAST(:dealer_id AS uuid))
          AND (CAST(:outlet_id AS uuid) IS NULL OR j.outlet_id = CAST(:outlet_id AS uuid))
          AND (CAST(:pc_actor_id AS varchar) IS NULL OR j.created_by_actor_id = CAST(:pc_actor_id AS varchar))
          AND (CAST(:month AS varchar) IS NULL
               OR (COALESCE(CAST(:month_basis AS varchar), '') <> 'business'
                   AND to_char(j.created_at_utc AT TIME ZONE :timezone, 'YYYY-MM') = CAST(:month AS varchar))
               OR (CAST(:month_basis AS varchar) = 'business'
                   AND (bm.booking_month = CAST(:month AS varchar) OR bm.delivery_month = CAST(:month AS varchar))))
          AND (
            :search = ''
            OR c.display_name ILIKE '%' || :search || '%'
            OR COALESCE(c.legal_name,'') ILIKE '%' || :search || '%'
            OR d.dealer_name ILIKE '%' || :search || '%'
            OR o.outlet_name ILIKE '%' || :search || '%'
            OR COALESCE(jp.model_name_snapshot,'') ILIKE '%' || :search || '%'
            OR COALESCE(j.journey_reference,'') ILIKE '%' || :search || '%'
            OR j.journey_id::text ILIKE '%' || :search || '%'
            OR COALESCE(c.mobile_last4,'') = :search
            OR (CAST(:mobile_pattern AS varchar) IS NOT NULL
                AND (regexp_replace(COALESCE(c.mobile_number,''), '[^0-9]', '', 'g')
                       LIKE CAST(:mobile_pattern AS varchar)
                     OR (char_length(CAST(:mobile_pattern AS varchar)) = 5
                         AND COALESCE(c.mobile_last4,'') = substr(CAST(:mobile_pattern AS varchar), 2))
                     OR j.journey_id IN (SELECT ph.journey_id FROM phone_hits ph)))
          )
    )
"""

# Which scoped rows belong to a tab, and how the tab is ordered.
VIEW_FILTER: dict[str, str] = {
    "all": "true",
    "open": "NOT s.is_closed",
    "closed": "s.is_closed",
    "needs_action": "NOT s.is_closed AND s.pc_task",
    "processing": "NOT s.is_closed AND s.processing AND NOT s.pc_task",
    "with_tl": "NOT s.is_closed AND s.tl_task AND NOT s.pc_task",
    "needs_tl": "NOT s.is_closed AND s.tl_task",
    "high_risk": "NOT s.is_closed AND s.high_risk",
    "overdue": "NOT s.is_closed AND s.overdue",
    "stuck": "NOT s.is_closed AND s.stuck",
}
VIEW_ORDER: dict[str, str] = {
    "needs_action": "s.pc_due ASC NULLS LAST, s.updated_at_utc DESC, s.journey_id DESC",
    "needs_tl": "s.tl_due ASC NULLS LAST, s.updated_at_utc DESC, s.journey_id DESC",
    "overdue": "LEAST(s.pc_due, s.tl_due) ASC NULLS LAST, s.updated_at_utc DESC, s.journey_id DESC",
}
DEFAULT_ORDER = "s.updated_at_utc DESC, s.journey_id DESC"


# "outlet" lists a Dealer's Outlets one after another and keeps the tab's own
# order inside each Outlet; anything else is the tab's own order.
OUTLET_ORDER_PREFIX = "lower(s.dealer_name), lower(s.outlet_name), s.outlet_id, "


def view_sql(view: str, sort: str | None = None) -> tuple[str, str]:
    """(WHERE fragment over ``s``, ORDER BY) for a tab; unknown tabs are 'all'.

    ``sort`` "outlet" groups the list by Outlet (the TL's list, 2026-10-23); any
    other value keeps the tab's own order (most urgent first).
    """
    key = view if view in VIEW_FILTER else "all"
    order = VIEW_ORDER.get(key, DEFAULT_ORDER)
    if sort == "outlet":
        order = OUTLET_ORDER_PREFIX + order
    return VIEW_FILTER[key], order


COUNTS_SQL = "WITH " + SCOPED_SQL + """
    SELECT
      COUNT(*) AS all_count,
      COUNT(*) FILTER (WHERE s.is_closed) AS closed,
      COUNT(*) FILTER (WHERE NOT s.is_closed) AS open,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND NOT s.in_delivery) AS booking_open,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.in_delivery) AS delivery_open,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.pc_task) AS needs_action,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.processing AND NOT s.pc_task) AS processing,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.tl_task AND NOT s.pc_task) AS with_tl,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.tl_task) AS needs_tl,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.high_risk) AS high_risk,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.overdue) AS overdue,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.stuck) AS stuck,
      COUNT(*) FILTER (WHERE s.booking_month = CAST(:month AS varchar)) AS bookings_in_month,
      COUNT(*) FILTER (WHERE s.delivery_month = CAST(:month AS varchar)) AS deliveries_in_month
    FROM scoped s
"""

BREAKDOWN_SQL = "WITH " + SCOPED_SQL + """
    SELECT {key_columns},
      COUNT(*) AS total,
      COUNT(*) FILTER (WHERE NOT s.is_closed) AS open,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND NOT s.in_delivery) AS booking_open,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.in_delivery) AS delivery_open,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.tl_task) AS needs_tl,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.overdue) AS overdue,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.high_risk) AS high_risk,
      COUNT(*) FILTER (WHERE NOT s.is_closed AND s.stuck) AS stuck,
      COUNT(*) FILTER (WHERE s.is_closed) AS closed,
      COUNT(*) FILTER (WHERE s.booking_month = CAST(:month AS varchar)) AS bookings_in_month,
      COUNT(*) FILTER (WHERE s.delivery_month = CAST(:month AS varchar)) AS deliveries_in_month
    FROM scoped s
    GROUP BY {group_by}
    ORDER BY COUNT(*) FILTER (WHERE NOT s.is_closed AND (s.overdue OR s.high_risk)) DESC,
             COUNT(*) FILTER (WHERE NOT s.is_closed) DESC
"""


_MOBILE_CHARACTERS = re.compile(r"^[0-9+\-\s().]+$")


def mobile_search_pattern(search: str | None) -> str | None:
    """The LIKE pattern a search text matches a mobile number's digits with,
    or None when the text is not a mobile number (letters, or under four digits).

    Spaces, dashes, brackets and dots are ignored; a +91 / 91 country code and a
    leading 0 are dropped. Four digits are the number's last four ("%1234"),
    five or more may be any part of it ("%98765%")."""
    text_ = (search or "").strip()
    if not text_ or not _MOBILE_CHARACTERS.match(text_):
        return None
    digits = "".join(ch for ch in text_ if ch.isdigit())
    # +91 typed with the number, or a full 12-digit number beginning 91.
    country_code = text_.startswith("+") and len(digits) > 6 or len(digits) == 12
    if country_code and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) >= 6 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) < 4:
        return None
    return f"%{digits}" if len(digits) == 4 else f"%{digits}%"


def scope_params(
    *,
    search: str | None,
    dealer_id: Any = None,
    outlet_id: Any = None,
    pc_actor_id: str | None = None,
) -> dict[str, Any]:
    """The search and filter parameters SCOPED_SQL reads, from the request's own."""
    text_ = (search or "").strip()
    return {
        "search": text_,
        "mobile_pattern": mobile_search_pattern(text_),
        "dealer_id": str(dealer_id) if dealer_id else None,
        "outlet_id": str(outlet_id) if outlet_id else None,
        "pc_actor_id": (pc_actor_id or "").strip() or None,
    }


def counts_from_row(row: Any) -> dict[str, int]:
    data = dict(row)
    data["all"] = data.pop("all_count")
    return {key: int(value or 0) for key, value in data.items()}
