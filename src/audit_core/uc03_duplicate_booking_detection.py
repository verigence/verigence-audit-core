"""uc03_duplicate_booking_detection.py — the same real customer must not
appear as two separate, unrelated Bookings within one tenant.

Native audit-core rule, not the rule-engine's own CROSS_CASE mechanism
(DUPLICATE_PAN_ACROSS_BOOKINGS etc.) -- that mechanism is parked
(rule-engine migration 0004): it has no existing cross-journey finding
materialization pattern (a finding spans exactly one journey via a
required composite FK), and audit-core has no caller for it at all. This
module builds the check natively where the materialization model, the
customer-identity fuzzy-match logic (already proven in
uc03_customer_identity_consistency.py), and the "which one is original"
signal (BK_MIN_BOOKING_AMOUNT_NOT_MET, also audit-core's own) all already
live in one place.

Match signals, any one of which flags a pair of journeys as a likely
duplicate:
  - exact PAN number match
  - exact Aadhaar number match
  - fuzzy KYC name match (reusing _names_match's tolerant comparison) AND
    same address pincode -- name alone is too weak a signal on its own
    (common names collide); pincode alone says nothing about identity.

Given a matched pair, the one whose minimum-booking-amount is currently
CONFIRMED (no open BK_MIN_BOOKING_AMOUNT_NOT_MET finding) is treated as the
original; if both or neither qualify, the earlier-created journey is the
original -- a real payment commitment is the strongest signal of intent,
timing is the fallback. The finding is raised on the duplicate, not the
original, naming which journey it is believed to duplicate.

``sync_duplicate_booking_detection`` is the producer: idempotent, self-heals
(a pairing that no longer matches resolves the finding), never raises.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_customer_identity_consistency import _names_match
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding

logger = logging.getLogger(__name__)

_FINDING_TYPE = "DUPLICATE_BOOKING"
_RULE_PREFIX = "DUPLICATE_BOOKING"
_SEVERITY = "CRITICAL"
_STAGE = "BOOKING"

_KYC_DOCUMENT_TYPES: tuple[str, ...] = ("aadhaar", "pan_card", "customer_kyc")
_NAME_FIELDS: dict[str, str] = {
    "aadhaar": "aadhaar_name",
    "pan_card": "pan_name",
    "customer_kyc": "customer_name",
}
_MIN_BOOKING_AMOUNT_RULE = "BK_MIN_BOOKING_AMOUNT_NOT_MET"


def _identity_signals(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any] | None:
    """This journey's own PAN / Aadhaar / KYC name / address pincode, each
    the latest-confidence extracted value. None when nothing identifying
    has been extracted yet -- nothing to compare other journeys against."""
    row = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT f.source_document_type_key AS document_type_key,
                       f.field_key, f.effective_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY f.field_key
                           ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                       ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.field_key = ANY(:field_keys)
                  AND f.effective_value IS NOT NULL
            )
            SELECT document_type_key, field_key, effective_value
            FROM ranked WHERE row_rank = 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "field_keys": ["pan_number", "aadhaar_number", "address_pincode", *_NAME_FIELDS.values()],
        },
    ).mappings().all()
    if not row:
        return None

    by_field = {r["field_key"]: str(r["effective_value"] or "").strip() for r in row}
    name = None
    for kyc_type in _KYC_DOCUMENT_TYPES:
        field_key = _NAME_FIELDS.get(kyc_type)
        if field_key and by_field.get(field_key):
            name = by_field[field_key]
            break

    pan = by_field.get("pan_number") or None
    aadhaar = by_field.get("aadhaar_number") or None
    pincode = by_field.get("address_pincode") or None
    if not (pan or aadhaar or name):
        return None
    return {"pan": pan, "aadhaar": aadhaar, "name": name, "pincode": pincode}


def _candidate_journeys(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every other journey in this tenant, with its own latest PAN/Aadhaar/
    name/pincode -- one row per journey, aggregated in SQL so this stays one
    round trip regardless of tenant size."""
    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT f.journey_id, f.field_key, f.effective_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY f.journey_id, f.field_key
                           ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                       ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.journeys j
                  ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
                WHERE f.tenant_id = :tenant_id AND f.journey_id <> :journey_id
                  AND f.field_key = ANY(:field_keys)
                  AND f.effective_value IS NOT NULL
            )
            SELECT r.journey_id, r.field_key, r.effective_value, j.created_at_utc
            FROM ranked r
            JOIN auditcore.journeys j
              ON j.tenant_id = :tenant_id AND j.journey_id = r.journey_id
            WHERE r.row_rank = 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "field_keys": ["pan_number", "aadhaar_number", "address_pincode", *_NAME_FIELDS.values()],
        },
    ).mappings().all()

    by_journey: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        entry = by_journey.setdefault(
            row["journey_id"], {"journey_id": row["journey_id"], "created_at_utc": row["created_at_utc"]}
        )
        entry[row["field_key"]] = str(row["effective_value"] or "").strip()

    candidates = []
    for entry in by_journey.values():
        name = None
        for field_key in _NAME_FIELDS.values():
            if entry.get(field_key):
                name = entry[field_key]
                break
        candidates.append(
            {
                "journey_id": entry["journey_id"],
                "created_at_utc": entry["created_at_utc"],
                "pan": entry.get("pan_number") or None,
                "aadhaar": entry.get("aadhaar_number") or None,
                "name": name,
                "pincode": entry.get("address_pincode") or None,
            }
        )
    return candidates


def _match_basis(signals: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    if signals["pan"] and candidate["pan"] and signals["pan"] == candidate["pan"]:
        return "PAN"
    if signals["aadhaar"] and candidate["aadhaar"] and signals["aadhaar"] == candidate["aadhaar"]:
        return "AADHAAR"
    if (
        signals["name"] and candidate["name"]
        and signals["pincode"] and candidate["pincode"]
        and signals["pincode"] == candidate["pincode"]
        and _names_match(signals["name"], candidate["name"])
    ):
        return "NAME_AND_ADDRESS"
    return None


def _min_booking_amount_confirmed(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> bool:
    """True when this journey has no OPEN/ACKNOWLEDGED
    BK_MIN_BOOKING_AMOUNT_NOT_MET finding -- the minimum booking payment
    has been made (or the rule hasn't fired for some other reason, e.g. no
    minimum configured; either way, not a currently-flagged shortfall)."""
    return (
        connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.audit_findings
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                  AND rule_key = :rule_key AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": _MIN_BOOKING_AMOUNT_RULE},
        ).first()
        is None
    )


def sync_duplicate_booking_detection(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Raise DUPLICATE_BOOKING on this journey for every other journey in
    the tenant that looks like the same customer, naming whichever one is
    believed original; resolve it once a pairing no longer matches.
    Idempotent, self-heals, never raises."""
    try:
        signals = _identity_signals(connection, tenant_id=tenant_id, journey_id=journey_id)
        if signals is None:
            return {"raised": 0, "resolved": 0, "examined": 0}

        candidates = _candidate_journeys(connection, tenant_id=tenant_id, journey_id=journey_id)
        this_journey_created = connection.execute(
            text("SELECT created_at_utc FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()

        raised = 0
        current_rule_keys: set[str] = set()
        for candidate in candidates:
            basis = _match_basis(signals, candidate)
            if basis is None:
                continue

            this_confirmed = _min_booking_amount_confirmed(
                connection, tenant_id=tenant_id, journey_id=journey_id
            )
            other_confirmed = _min_booking_amount_confirmed(
                connection, tenant_id=tenant_id, journey_id=candidate["journey_id"]
            )
            if this_confirmed and not other_confirmed:
                is_original = True
            elif other_confirmed and not this_confirmed:
                is_original = False
            else:
                # created_at_utc alone can tie -- two journeys created in the
                # same transaction (common in tests, possible in a real fast
                # double-booking) share Postgres's transaction-start now().
                # journey_id as a stable secondary key guarantees exactly one
                # side of the pair is picked, consistently regardless of
                # which journey's own sync triggered this check.
                is_original = (this_journey_created, str(journey_id)) <= (
                    candidate["created_at_utc"], str(candidate["journey_id"])
                )

            if is_original:
                continue  # the finding belongs on the duplicate, not this journey

            rule_key = f"{_RULE_PREFIX}:{candidate['journey_id']}"
            current_rule_keys.add(rule_key)
            _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=_STAGE,
                rule_key=rule_key,
                finding_type=_FINDING_TYPE,
                severity=_SEVERITY,
                title="Possible duplicate booking for the same customer",
                description=(
                    f"This customer appears to match an earlier booking on this "
                    f"tenant ({basis.replace('_', ' ').title()} match). Confirm "
                    "whether this is a genuine repeat customer or a duplicate entry."
                ),
                correlation_id=correlation_id,
                safe_payload={
                    "matchBasis": basis,
                    "believedOriginalJourneyId": str(candidate["journey_id"]),
                },
            )
            raised += 1

        resolved = 0
        open_findings = connection.execute(
            text(
                """
                SELECT audit_finding_id, rule_key FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND finding_type_code=:finding_type AND finding_status IN ('OPEN','ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "finding_type": _FINDING_TYPE},
        ).mappings().all()
        for finding in open_findings:
            if finding["rule_key"] in current_rule_keys:
                continue
            _resolve_finding(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=_STAGE,
                finding_id=finding["audit_finding_id"],
                actor_id=None,
                correlation_id=correlation_id,
                note="No longer matches an earlier booking after correction.",
            )
            resolved += 1

        return {"raised": raised, "resolved": resolved, "examined": len(candidates)}
    except Exception:
        logger.warning("sync_duplicate_booking_detection failed", exc_info=True)
        return {"error": True}


__all__ = ["sync_duplicate_booking_detection"]
