"""The same file uploaded again (product owner, 2026-10-31).

    "When ever a PC enters a Journey which has existing PAN, Mobile No,
    Customer Name, Model same, Pls raise an Alert across all PC telling Same
    File Being Uploaded. Give the name of customer, Model, PC Name and
    Dealership, and delete the journey completely. This alert should be seen
    by all team on their respective screens, if they are active."

What is "the same file"
    Two journeys of one Project (any dealership) that agree on ALL FOUR:
      - PAN: the journey's highest-confidence ``pan_number``, in PAN shape
        (``_valid_pan``), equal;
      - mobile: the highest-confidence ``customer_phone``, as a real Indian
        mobile (``_normalize_mobile``: the last 10 digits, starting 6-9), equal;
      - customer name: the KYC name on the customer (``customers.legal_name``,
        VERIFIED), the same person by ``same_person`` -- a masked PAN name
        ("PXXXXJ KXXXR") by ``masked_matches``, two masked names by equality;
      - model: both SKUs pinned -> the same ``product_skus.model_id``;
        otherwise the model names (the master's once pinned, the booking
        form's until then) are one family by ``generation_key``
        ("NEW THAR 2WD" is "THAR"; "THAR ROXX" is not).
    A journey missing any of the four is not compared. Closed journeys
    (delivered, booking cancelled, duplicate booking, no delivery) are never
    paired. The DUPLICATE_BOOKING rule (one basis is enough, a TL task) is
    unchanged; this is the stricter, all-four check on top of it.

Which one is the new file
    The later ``created_at_utc``, ``journey_id`` breaking a tie -- the same
    order whichever journey's check runs, so exactly one side is ever "new".
    The check runs in the NATIVE:BOOKING unit of either journey; when the
    older one is the last to have all four values, its own run flags the
    newer one.

What happens to it
    - An individual's PAN (4th character "P"): the new journey is deleted
      completely (Document Intelligence, R2, Audit Core), by the system, as
      its own SAME_FILE_DELETE work item after this check has committed --
      never inside the check's transaction. The alert is published once the
      delete is done.
    - A company, firm or any other PAN (4th character C, F, H, A, T, B, L,
      J, G ...): alert only, the journey is kept for review -- a fleet buyer
      can genuinely book two identical vehicles (product owner 2026-10-31).
    - A journey whose documents a reviewer already marked reviewed is kept
      too (the delete guard), and the alert says so.

Once per journey
    The alert row is the marker: (tenant, SAME_FILE_UPLOADED, new journey) is
    unique, so a recomputed check, or the two journeys' checks running at
    the same time, raise one alert.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.errors import ConflictError, NotFoundError
from audit_core.uc03_duplicate_booking_detection import _normalize_mobile, _valid_pan
from audit_core.uc03_p2_names import (
    is_masked,
    masked_matches,
    person_tokens,
    same_person,
)
from audit_core.uc03_p2_runtime import enqueue_work
from audit_core.uc03_sku_words import generation_key

logger = structlog.get_logger(__name__)

ALERT_KIND = "SAME_FILE_UPLOADED"
DELETE_WORK_TYPE = "SAME_FILE_DELETE"
ALERT_HOURS = 24
ALERT_TITLE = "Same file being uploaded"

# Outcomes, as the alert states them.
DELETED = "DELETED"
KEPT_NOT_INDIVIDUAL = "KEPT_NOT_INDIVIDUAL"
KEPT_REVIEWED = "KEPT_REVIEWED"
KEPT_CLOSED = "KEPT_CLOSED"
DELETE_FAILED = "DELETE_FAILED"
PENDING_DELETE = "PENDING_DELETE"

# The PAN's 4th character is the holder type; "P" is an individual.
_INDIVIDUAL_HOLDER = "P"

_CLOSED_SQL = """
    EXISTS (
      SELECT 1 FROM auditcore.journey_stage_states st
      WHERE st.tenant_id=j.tenant_id AND st.journey_id=j.journey_id
        AND ((st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
             OR (st.stage_code='BOOKING'
                 AND (st.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                      OR st.closure_disposition='NO_DELIVERY')))
    )
"""

_IDENTITY_SQL = f"""
    WITH ranked AS (
        SELECT f.journey_id, f.field_key, f.effective_value #>> '{{}}' AS value,
               ROW_NUMBER() OVER (
                   PARTITION BY f.journey_id, f.field_key
                   ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
               ) AS row_rank
        FROM auditcore.journey_document_extracted_fields f
        WHERE f.tenant_id = :tenant_id AND f.journey_id = ANY(:journey_ids)
          AND f.field_key IN ('pan_number', 'customer_phone')
          AND f.effective_value IS NOT NULL
    )
    SELECT j.journey_id, j.created_at_utc, j.journey_reference,
           j.created_by_display_name AS pc_name,
           d.dealer_name, o.outlet_name,
           (SELECT r.value FROM ranked r
             WHERE r.journey_id = j.journey_id AND r.field_key = 'pan_number' AND r.row_rank = 1) AS pan,
           (SELECT r.value FROM ranked r
             WHERE r.journey_id = j.journey_id AND r.field_key = 'customer_phone' AND r.row_rank = 1) AS phone,
           CASE WHEN c.legal_name_status = 'VERIFIED' THEN c.legal_name END AS customer_name,
           s.model_id, jp.model_name_snapshot AS model_name, jp.variant_name_snapshot AS variant_name,
           {_CLOSED_SQL} AS is_closed
    FROM auditcore.journeys j
    JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
    JOIN auditcore.dealer_outlets o
      ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
    LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
    WHERE j.tenant_id = :tenant_id AND j.journey_id = ANY(:journey_ids)
"""

# Journeys whose extracted PAN, on any document, is this PAN: a narrowing
# only -- each is then compared on its own highest-confidence PAN.
_SAME_PAN_SQL = """
    SELECT DISTINCT f.journey_id
    FROM auditcore.journey_document_extracted_fields f
    WHERE f.tenant_id = :tenant_id AND f.field_key = 'pan_number' AND f.journey_id <> :journey_id
      AND upper(regexp_replace(COALESCE(f.effective_value #>> '{}', ''), '[^A-Za-z0-9]', '', 'g')) = :pan
"""


@dataclass(frozen=True)
class FileIdentity:
    journey_id: UUID
    created_at: datetime
    reference: str | None
    pc_name: str | None
    dealer_name: str | None
    outlet_name: str | None
    pan: str | None
    mobile: str | None
    name: str | None
    model_id: UUID | None
    model_name: str | None
    variant_name: str | None
    closed: bool

    @property
    def model_key(self) -> str:
        return generation_key(self.model_name) if self.model_name and self.model_name.strip() else ""

    @property
    def complete(self) -> bool:
        return bool(self.pan and self.mobile and self.name and (self.model_id or self.model_key))

    @property
    def order_key(self) -> tuple[datetime, str]:
        return (self.created_at, str(self.journey_id))


def _identities(connection: Connection, *, tenant_id: str, journey_ids: Iterable[UUID]) -> dict[UUID, FileIdentity]:
    ids = list(journey_ids)
    if not ids:
        return {}
    rows = connection.execute(
        text(_IDENTITY_SQL), {"tenant_id": tenant_id, "journey_ids": ids},
    ).mappings().all()
    out: dict[UUID, FileIdentity] = {}
    for row in rows:
        name = " ".join(str(row["customer_name"] or "").split()) or None
        if name and not person_tokens(name):
            name = None
        out[UUID(str(row["journey_id"]))] = FileIdentity(
            journey_id=UUID(str(row["journey_id"])),
            created_at=row["created_at_utc"],
            reference=row["journey_reference"],
            pc_name=row["pc_name"],
            dealer_name=row["dealer_name"],
            outlet_name=row["outlet_name"],
            pan=_valid_pan(row["pan"]),
            mobile=_normalize_mobile(row["phone"]),
            name=name,
            model_id=UUID(str(row["model_id"])) if row["model_id"] else None,
            model_name=(str(row["model_name"]).strip() or None) if row["model_name"] else None,
            variant_name=(str(row["variant_name"]).strip() or None) if row["variant_name"] else None,
            closed=bool(row["is_closed"]),
        )
    return out


# ── the four comparisons ──────────────────────────────────────────────────────
def same_name(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_masked, right_masked = is_masked(left), is_masked(right)
    if left_masked and right_masked:
        return " ".join(left.upper().split()) == " ".join(right.upper().split())
    if left_masked:
        return masked_matches(left, right)
    if right_masked:
        return masked_matches(right, left)
    return same_person(left, right)


def same_model(left: FileIdentity, right: FileIdentity) -> bool:
    if left.model_id and right.model_id:
        return left.model_id == right.model_id
    return bool(left.model_key) and left.model_key == right.model_key


def _masked(value: str | None) -> str | None:
    if not value:
        return None
    return value if len(value) <= 4 else f"{'X' * (len(value) - 4)}{value[-4:]}"


def matched_fields(new: FileIdentity, existing: FileIdentity) -> list[dict[str, Any]] | None:
    """The four fields, as a reviewer should see them, when the two files
    agree on all four; None when any one differs or is missing."""
    if not (new.complete and existing.complete):
        return None
    if new.pan != existing.pan or new.mobile != existing.mobile:
        return None
    if not same_name(new.name, existing.name) or not same_model(new, existing):
        return None
    return [
        {"field": "pan", "label": "PAN", "newValue": _masked(new.pan), "existingValue": _masked(existing.pan),
         "exact": True},
        {"field": "mobile", "label": "Mobile", "newValue": _masked(new.mobile),
         "existingValue": _masked(existing.mobile), "exact": True},
        {"field": "name", "label": "Customer name", "newValue": new.name, "existingValue": existing.name,
         "exact": new.name == existing.name},
        {"field": "model", "label": "Model", "newValue": new.model_name, "existingValue": existing.model_name,
         "exact": bool(new.model_id and new.model_id == existing.model_id) or new.model_name == existing.model_name},
    ]


def is_individual_pan(pan: str | None) -> bool:
    return bool(pan) and len(pan) == 10 and pan[3] == _INDIVIDUAL_HOLDER


# ── the alert ─────────────────────────────────────────────────────────────────
def _place(dealer: str | None, outlet: str | None) -> str:
    parts = [p for p in (dealer, outlet) if p and p.strip()]
    if len(parts) == 2 and parts[0].strip().lower() == parts[1].strip().lower():
        parts = parts[:1]
    return "/".join(parts) or "unknown dealership"


def alert_body(payload: dict[str, Any], outcome: str) -> str:
    new = payload.get("newJourney") or {}
    existing = payload.get("existingJourney") or {}
    who = (f"{payload.get('customerName') or 'Unknown customer'}, {payload.get('model') or 'unknown model'}, "
           f"PC {new.get('pcName') or 'unknown'}, {_place(new.get('dealerName'), new.get('outletName'))}")
    ref = new.get("reference") or "the new journey"
    matches = (f"It matches {existing.get('reference') or 'an earlier journey'} "
               f"(PC {existing.get('pcName') or 'unknown'}, "
               f"{_place(existing.get('dealerName'), existing.get('outletName'))}) "
               "on PAN, mobile, customer name and model.")
    what = {
        DELETED: f"The new file {ref} was deleted.",
        PENDING_DELETE: f"The new file {ref} is being deleted.",
        KEPT_NOT_INDIVIDUAL: (f"The new file {ref} was kept for review: the PAN is not an individual's "
                              "(a company or firm may book two identical vehicles)."),
        KEPT_REVIEWED: f"The new file {ref} was kept for review: its documents are already marked reviewed.",
        KEPT_CLOSED: f"The new file {ref} was kept for review: it is already closed.",
        DELETE_FAILED: f"The new file {ref} could not be deleted; the Team Lead has been told.",
    }.get(outcome, f"The new file is {ref}.")
    return f"{ALERT_TITLE} — {who}. {what} {matches}"


def _journey_view(identity: FileIdentity) -> dict[str, Any]:
    return {
        "journeyId": str(identity.journey_id),
        "reference": identity.reference,
        "pcName": identity.pc_name,
        "dealerName": identity.dealer_name,
        "outletName": identity.outlet_name,
        "createdAtUtc": identity.created_at.isoformat() if identity.created_at else None,
    }


def publish_alert(connection: Connection, *, tenant_id: str, journey_id: UUID, outcome: str) -> bool:
    """Publish the same-file alert of a journey with its outcome (for 24 hours).
    True when a PENDING alert was published; an alert already published is
    left as it is."""
    row = connection.execute(
        text(
            "SELECT alert_id, payload FROM auditcore.p2_tenant_alerts "
            "WHERE tenant_id=:t AND alert_kind=:k AND subject_journey_id=:j AND alert_status='PENDING' "
            "FOR UPDATE"
        ),
        {"t": tenant_id, "k": ALERT_KIND, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return False
    payload = {**dict(row["payload"] or {}), "outcome": outcome}
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tenant_alerts
            SET alert_status='PUBLISHED', outcome=:outcome, body=:body, payload=CAST(:payload AS jsonb),
                published_at_utc=now(), expires_at_utc=now() + (:hours * interval '1 hour'), updated_at_utc=now()
            WHERE tenant_id=:t AND alert_id=:a
            """
        ),
        {"t": tenant_id, "a": row["alert_id"], "outcome": outcome, "body": alert_body(payload, outcome),
         "payload": json.dumps(payload, default=str), "hours": ALERT_HOURS},
    )
    logger.info("p2_same_file_alert_published", tenant_id=tenant_id, journey_id=str(journey_id), outcome=outcome)
    return True


def _reviewed(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    return connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews "
             "WHERE tenant_id=:t AND journey_id=:j)"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()


def _raise(
    connection: Connection, *, tenant_id: str, new: FileIdentity, existing: FileIdentity,
    fields: list[dict[str, Any]], correlation_id: str,
) -> str | None:
    """The alert for one new file, and its delete queued; None when this
    file was already handled (the marker)."""
    if not is_individual_pan(new.pan):
        outcome = KEPT_NOT_INDIVIDUAL
    elif _reviewed(connection, tenant_id=tenant_id, journey_id=new.journey_id):
        outcome = KEPT_REVIEWED
    else:
        outcome = PENDING_DELETE
    payload = {
        "customerName": new.name,
        "model": new.model_name,
        "variant": new.variant_name,
        "newJourney": _journey_view(new),
        "existingJourney": _journey_view(existing),
        "matchedFields": fields,
        "outcome": outcome,
    }
    publish_now = outcome != PENDING_DELETE
    alert_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_tenant_alerts (
                tenant_id, alert_kind, subject_journey_id, subject_journey_reference, related_journey_id,
                alert_status, outcome, title, body, payload, published_at_utc, expires_at_utc
            ) VALUES (
                :t, :k, :j, :ref, :related, :status, :outcome, :title, :body, CAST(:payload AS jsonb),
                CASE WHEN :publish THEN now() END,
                CASE WHEN :publish THEN now() + (:hours * interval '1 hour') END
            )
            ON CONFLICT (tenant_id, alert_kind, subject_journey_id) DO NOTHING
            RETURNING alert_id
            """
        ),
        {"t": tenant_id, "k": ALERT_KIND, "j": new.journey_id, "ref": new.reference,
         "related": existing.journey_id, "status": "PUBLISHED" if publish_now else "PENDING",
         "outcome": outcome, "title": ALERT_TITLE, "body": alert_body(payload, outcome),
         "payload": json.dumps(payload, default=str), "publish": publish_now, "hours": ALERT_HOURS},
    ).scalar_one_or_none()
    if alert_id is None:
        return None
    if outcome == PENDING_DELETE:
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=new.journey_id, work_type=DELETE_WORK_TYPE,
            work_key=f"same-file:{new.journey_id}",
            payload={"alertId": str(alert_id), "existingJourneyId": str(existing.journey_id),
                     "existingReference": existing.reference},
            correlation_id=correlation_id,
        )
    logger.info(
        "p2_same_file_detected", tenant_id=tenant_id, journey_id=str(new.journey_id),
        journey_reference=new.reference, existing_journey_id=str(existing.journey_id),
        existing_reference=existing.reference, outcome=outcome,
    )
    return outcome


def _detect(connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str) -> dict[str, Any]:
    me = _identities(connection, tenant_id=tenant_id, journey_ids=[journey_id]).get(journey_id)
    if me is None or me.closed or not me.complete:
        return {"examined": 0, "raised": 0}
    candidate_ids = [
        UUID(str(v)) for v in connection.execute(
            text(_SAME_PAN_SQL), {"tenant_id": tenant_id, "journey_id": journey_id, "pan": me.pan},
        ).scalars().all()
    ]
    others = _identities(connection, tenant_id=tenant_id, journey_ids=candidate_ids)
    matches = [o for o in others.values() if not o.closed and matched_fields(me, o) is not None]
    if not matches:
        return {"examined": len(others), "raised": 0}
    group = [me, *matches]
    original = min(group, key=lambda identity: identity.order_key)
    outcomes: list[str] = []
    for new in sorted(group, key=lambda identity: identity.order_key):
        if new is original:
            continue
        fields = matched_fields(new, original)
        if fields is None:  # matched this journey, not the oldest one
            continue
        outcome = _raise(connection, tenant_id=tenant_id, new=new, existing=original, fields=fields,
                         correlation_id=correlation_id)
        if outcome is not None:
            outcomes.append(outcome)
    return {"examined": len(others), "raised": len(outcomes), "outcomes": outcomes}


def sync_same_file_detection(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str,
) -> dict[str, Any]:
    """Look for an earlier journey that is the same file as this one (or a
    later one this one makes a copy of); alert and queue the delete. Never
    raises: a failure is logged and leaves the unit's own work untouched."""
    try:
        with connection.begin_nested():
            return _detect(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id)
    except Exception:
        logger.warning("p2_same_file_detection_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                       exc_info=True)
        return {"error": True}


def recheck_pairs_of(connection: Connection, *, tenant_id: str, deleted_journey_id: UUID,
                     correlation_id: str | None) -> int:
    """After a journey is deleted: the journeys whose open DUPLICATE_BOOKING
    finding names it as their original are checked again at once (forced),
    so the finding -- and its Team Lead task -- closes instead of pointing
    at a journey that is gone. Runs in the delete's own transaction."""
    from audit_core.uc03_p2_controls import request_control_evaluation

    journeys = connection.execute(
        text("SELECT DISTINCT journey_id FROM auditcore.audit_findings WHERE tenant_id=:t "
             "AND finding_type_code='DUPLICATE_BOOKING' AND rule_key=:key "
             "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
        {"t": tenant_id, "key": f"DUPLICATE_BOOKING:{deleted_journey_id}"},
    ).scalars().all()
    for journey_id in journeys:
        request_control_evaluation(connection, tenant_id=tenant_id, journey_id=UUID(str(journey_id)),
                                   correlation_id=correlation_id, delay_seconds=0, force=True)
    return len(journeys)


# ── the delete, as its own work item ──────────────────────────────────────────
def run_same_file_delete(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    security_client: Any,
    v2_client: Any,
    storage: Any,
    before_record: Callable[[Connection], None] | None = None,
) -> str:
    """Delete the new file and publish its alert. Returns DELETED, GONE (the
    journey was already deleted), KEPT_REVIEWED / KEPT_CLOSED (the delete
    guard refused it) or NOTHING (no pending alert: already handled). A
    technical failure raises, for the work item to be retried."""
    from audit_core.db import set_tenant_context
    from audit_core.uc03_p2_journey_delete import system_delete_journey

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        alert = connection.execute(
            text("SELECT payload FROM auditcore.p2_tenant_alerts WHERE tenant_id=:t AND alert_kind=:k "
                 "AND subject_journey_id=:j AND alert_status='PENDING'"),
            {"t": tenant_id, "k": ALERT_KIND, "j": journey_id},
        ).scalar_one_or_none()
    if alert is None:
        return "NOTHING"
    existing_ref = ((alert or {}).get("existingJourney") or {}).get("reference") or "an earlier journey"
    reason = f"Same file uploaded again: matches {existing_ref} on PAN, mobile, name and model"

    def publish(outcome: str) -> Callable[[Connection], None]:
        return lambda connection: publish_alert(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                outcome=outcome)

    def deleted(connection: Connection) -> None:
        publish(DELETED)(connection)
        recheck_pairs_of(connection, tenant_id=tenant_id, deleted_journey_id=journey_id,
                         correlation_id=correlation_id)

    try:
        system_delete_journey(
            engine, tenant_id=tenant_id, journey_id=journey_id, reason=reason, correlation_id=correlation_id,
            security_client=security_client, v2_client=v2_client, storage=storage,
            on_deleted=deleted, before_record=before_record,
        )
        return DELETED
    except NotFoundError:
        outcome, result = DELETED, "GONE"
    except ConflictError as exc:
        outcome = KEPT_REVIEWED if "reviewed" in exc.title.lower() else KEPT_CLOSED
        result = outcome
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        publish(outcome)(connection)
    return result
