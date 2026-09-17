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

Match signals, checked in order from strongest to weakest -- a pair only
ever gets ONE finding, on the strongest basis that actually matches, not
one per basis (stacking near-duplicate flags for the same relationship is
noise, not signal):

  - exact PAN number match                                   -- Strong
  - exact Aadhaar number match                                -- Strong
  - this journey's customer name matches the OTHER journey's KYC-recorded
    relationship name (Father/Spouse/Husband), or vice versa  -- Strong
  - fuzzy KYC name match (same surname, and a given name close enough to
    be an OCR/spelling variant of the same person -- see
    _same_person_by_name) AND same address pincode -- name alone is too
    weak a signal on its own (common names collide); pincode alone says
    nothing about identity                                    -- Strong
  - exact buyer GSTIN match (from the Tax Invoice, when either journey
    has one on file)                                          -- Moderate
  - exact normalized mobile number match                      -- Moderate
  - same surname (last token of the KYC name) AND same address pincode --
    weaker than the full-name-and-pincode match above, catches the case
    where two different-but-related people share a surname and an
    address                                                   -- Weak
  - fuzzy full-address match (SequenceMatcher, same technique as name
    matching, lower threshold -- addresses are long and OCR-noisy) without
    requiring an exact pincode                                -- Weak

Given a matched pair, whichever journey actually reached its minimum
booking amount EARLIER holds the booking -- not just "confirmed or not":
``journey_stage_states.booking_confirm_date`` (set by
``evaluate_minimum_booking_payment`` to the receipt date on which the
running total first met the minimum, not the date it happened to be
evaluated) is the real-world "who paid first" signal, compared directly
between the two journeys. If only one side has reached its minimum at
all, that one holds it outright. If neither has, the earlier-CREATED
journey is the fallback -- timing of intent is weaker evidence than
timing of an actual payment commitment, but it's what's left when neither
side has paid anything toward the minimum yet. The finding is raised on
the journey that does NOT hold the booking, naming which journey it is
believed to duplicate.

``sync_duplicate_booking_detection`` is the producer: idempotent, self-heals
(a pairing that no longer matches resolves the finding), never raises.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_customer_identity_consistency import _normalize_name
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding

logger = logging.getLogger(__name__)

_FINDING_TYPE = "DUPLICATE_BOOKING"
_RULE_PREFIX = "DUPLICATE_BOOKING"
_STAGE = "BOOKING"

_KYC_DOCUMENT_TYPES: tuple[str, ...] = ("aadhaar", "pan_card", "customer_kyc")
_NAME_FIELDS: dict[str, str] = {
    "aadhaar": "aadhaar_name",
    "pan_card": "pan_name",
    "customer_kyc": "customer_name",
}
# Preferred to weaker, matching the KYC precedence _NAME_FIELDS already uses.
_RELATIONSHIP_NAME_FIELDS: tuple[str, ...] = ("aadhaar_relationship_name", "pan_relationship_name")
_ADDRESS_FIELDS: tuple[str, ...] = ("aadhaar_address", "customer_address")

_ADDRESS_MATCH_THRESHOLD = 0.72
# Deciding "is this the same specific person" (surname already matched) is
# a stricter question than _names_match's own 0.55 (whether a document's
# stated name plausibly refers to a known reference name -- a different,
# more tolerant job in uc03_customer_identity_consistency.py). A whole-
# string ratio can't tell "Sanjay" vs "Sanjaya" (same person, OCR variant)
# apart from "Anil Sharma" vs "Sunil Sharma" (two different, very common
# names sharing a surname) -- short given names sharing a surname often
# score just as high or higher. Comparing the given-name portion alone,
# with its own tight threshold, is what actually separates the two.
_GIVEN_NAME_MATCH_THRESHOLD = 0.8
_NON_DIGIT = re.compile(r"\D+")
_WHITESPACE_ONLY = re.compile(r"\s+")

# Strongest to weakest -- _match_basis returns the first of these that
# actually matches, so a pair is never flagged more than once.
_SEVERITY_BY_BASIS: dict[str, str] = {
    "PAN": "CRITICAL",
    "AADHAAR": "CRITICAL",
    "CUSTOMER_MATCHES_RELATIVE": "CRITICAL",
    "NAME_AND_ADDRESS": "CRITICAL",
    "GST": "MEDIUM",
    "MOBILE": "MEDIUM",
    "SURNAME_AND_PINCODE": "LOW",
    "SIMILAR_ADDRESS": "LOW",
}

# A reviewer's real question isn't just "which basis matched" but "how
# likely is this to actually be the same person" -- a deterministic
# confidence estimate per basis, not a model score (there's no training
# data for this), reflecting how rarely each signal collides by chance:
# PAN/Aadhaar are near-unique identifiers; a relative's name repeated as
# another booking's own customer name is a very specific coincidence;
# mobile/GST can legitimately be shared within a household or business;
# surname+pincode or a merely-similar address are the weakest signals,
# common enough to collide between unrelated people.
_CONFIDENCE_PERCENT_BY_BASIS: dict[str, int] = {
    "PAN": 99,
    "AADHAAR": 99,
    "CUSTOMER_MATCHES_RELATIVE": 90,
    "NAME_AND_ADDRESS": 85,
    "GST": 75,
    "MOBILE": 70,
    "SURNAME_AND_PINCODE": 40,
    "SIMILAR_ADDRESS": 35,
}
# One-to-one with _SEVERITY_BY_BASIS's own tiers -- kept as an explicit,
# separate mapping (not derived inline) so a reviewer-facing label never
# silently drifts if the internal severity tiers used for SLA routing
# ever change independently of this.
_CONFIDENCE_LABEL_BY_SEVERITY: dict[str, str] = {
    "CRITICAL": "HIGH",
    "MEDIUM": "MEDIUM",
    "LOW": "LOW",
}

_BASIS_LABEL: dict[str, str] = {
    "PAN": "PAN",
    "AADHAAR": "Aadhaar",
    "CUSTOMER_MATCHES_RELATIVE": "customer name matches a relative on the other booking",
    "NAME_AND_ADDRESS": "name and address",
    "GST": "GSTIN",
    "MOBILE": "mobile number",
    "SURNAME_AND_PINCODE": "surname and pincode",
    "SIMILAR_ADDRESS": "similar address",
}

_IDENTITY_FIELD_KEYS: list[str] = [
    "pan_number", "aadhaar_number", "address_pincode", "customer_phone",
    *_ADDRESS_FIELDS, *_RELATIONSHIP_NAME_FIELDS, *_NAME_FIELDS.values(),
]


def _normalize_mobile(value: Any) -> str | None:
    digits = _NON_DIGIT.sub("", str(value or ""))
    if len(digits) < 10:
        return None
    return digits[-10:]  # drops a country code / leading trunk prefix


def _surname(name: str | None) -> str | None:
    if not name:
        return None
    tokens = _normalize_name(name).split()
    return tokens[-1] if tokens else None


def _name_parts(name: str | None) -> tuple[str, str] | None:
    """(given-name portion, surname) -- surname is the last token, given
    name is everything before it. None when there's only one token (a bare
    surname on its own establishes nothing about whether it's the same
    person, only that _surname() itself matched)."""
    if not name:
        return None
    tokens = _normalize_name(name).split()
    if len(tokens) < 2:
        return None
    return " ".join(tokens[:-1]), tokens[-1]


def _same_person_by_name(a: str | None, b: str | None) -> bool:
    """True when two names plausibly name the same specific person: same
    surname, and a given-name portion close enough to be an OCR/spelling
    variant rather than a different person who happens to share a
    surname. See _GIVEN_NAME_MATCH_THRESHOLD for why a tight threshold on
    the given name alone, not _names_match's whole-string ratio."""
    parts_a, parts_b = _name_parts(a), _name_parts(b)
    if not parts_a or not parts_b:
        return False
    given_a, surname_a = parts_a
    given_b, surname_b = parts_b
    if surname_a != surname_b:
        return False
    if given_a == given_b:
        return True
    return SequenceMatcher(None, given_a, given_b).ratio() >= _GIVEN_NAME_MATCH_THRESHOLD


def _addresses_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    normalized_a = _WHITESPACE_ONLY.sub(" ", a.strip().upper())
    normalized_b = _WHITESPACE_ONLY.sub(" ", b.strip().upper())
    if not normalized_a or not normalized_b:
        return False
    if normalized_a == normalized_b:
        return True
    return SequenceMatcher(None, normalized_a, normalized_b).ratio() >= _ADDRESS_MATCH_THRESHOLD


def _extract_signals(rows: list[Any], *, gst: str | None) -> dict[str, Any] | None:
    """Shared shape-building for both this journey's own signals and a
    candidate's: pick the highest-confidence value per field, then merge
    into one flat, comparison-ready record. gst is looked up separately
    (invoice_review_values, not journey_document_extracted_fields) and
    passed in rather than derived here -- a journey whose only identifying
    signal is its buyer GSTIN (no KYC extraction at all yet) must still
    count as having something to compare, not be silently dropped."""
    by_field = {r["field_key"]: str(r["effective_value"] or "").strip() for r in rows}
    name = None
    for kyc_type in _KYC_DOCUMENT_TYPES:
        field_key = _NAME_FIELDS.get(kyc_type)
        if field_key and by_field.get(field_key):
            name = by_field[field_key]
            break
    relative_name = next((by_field[k] for k in _RELATIONSHIP_NAME_FIELDS if by_field.get(k)), None)
    address = next((by_field[k] for k in _ADDRESS_FIELDS if by_field.get(k)), None)

    pan = by_field.get("pan_number") or None
    aadhaar = by_field.get("aadhaar_number") or None
    pincode = by_field.get("address_pincode") or None
    mobile = _normalize_mobile(by_field.get("customer_phone"))
    if not (pan or aadhaar or name or mobile or address or gst):
        return None
    return {
        "pan": pan, "aadhaar": aadhaar, "name": name, "pincode": pincode,
        "mobile": mobile, "address": address, "relative_name": relative_name,
        "surname": _surname(name), "gst": gst,
    }


def _identity_signals(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any] | None:
    """This journey's own PAN / Aadhaar / KYC name / mobile / address /
    relationship name / pincode / buyer GSTIN, each the latest-confidence
    extracted value. None when nothing identifying has been captured yet
    -- nothing to compare other journeys against."""
    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT f.field_key, f.effective_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY f.field_key
                           ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                       ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.field_key = ANY(:field_keys)
                  AND f.effective_value IS NOT NULL
            )
            SELECT field_key, effective_value FROM ranked WHERE row_rank = 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "field_keys": _IDENTITY_FIELD_KEYS},
    ).mappings().all()
    gst = _buyer_gstin(connection, tenant_id=tenant_id, journey_id=journey_id)
    return _extract_signals(rows, gst=gst)


def _buyer_gstin(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str | None:
    """The latest non-null buyer GSTIN off any Tax Invoice materialized for
    this journey -- lives in invoice_review_values, a separate table from
    journey_document_extracted_fields (see uc03_invoice_materialization.py)."""
    return connection.execute(
        text(
            """
            SELECT buyer_gstin FROM auditcore.invoice_review_values
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND buyer_gstin IS NOT NULL
            ORDER BY created_at_utc DESC LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()


def _candidate_journeys(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every other journey in this tenant, with its own latest identity
    signals -- one row per journey, aggregated in SQL so this stays one
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
        {"tenant_id": tenant_id, "journey_id": journey_id, "field_keys": _IDENTITY_FIELD_KEYS},
    ).mappings().all()

    by_journey: dict[UUID, list[Any]] = {}
    created_at_by_journey: dict[UUID, Any] = {}
    for row in rows:
        by_journey.setdefault(row["journey_id"], []).append(row)
        created_at_by_journey[row["journey_id"]] = row["created_at_utc"]

    gst_rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (v.journey_id) v.journey_id, v.buyer_gstin, j.created_at_utc
            FROM auditcore.invoice_review_values v
            JOIN auditcore.journeys j ON j.tenant_id = v.tenant_id AND j.journey_id = v.journey_id
            WHERE v.tenant_id = :tenant_id AND v.journey_id <> :journey_id AND v.buyer_gstin IS NOT NULL
            ORDER BY v.journey_id, v.created_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    gst_by_journey = {row["journey_id"]: row["buyer_gstin"] for row in gst_rows}
    for row in gst_rows:
        created_at_by_journey.setdefault(row["journey_id"], row["created_at_utc"])

    candidates = []
    # A journey whose only identifying signal is its buyer GSTIN (no KYC
    # extraction at all yet) has no entry in by_journey -- iterate the
    # union of both sources, or GST-only candidates are silently dropped
    # before ever being compared (the exact bug this fixes).
    for candidate_journey_id in by_journey.keys() | gst_by_journey.keys():
        journey_rows = by_journey.get(candidate_journey_id, [])
        signals = _extract_signals(journey_rows, gst=gst_by_journey.get(candidate_journey_id))
        if signals is None:
            continue
        signals["journey_id"] = candidate_journey_id
        signals["created_at_utc"] = created_at_by_journey[candidate_journey_id]
        candidates.append(signals)
    return candidates


def _match_basis(signals: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    if signals["pan"] and candidate["pan"] and signals["pan"] == candidate["pan"]:
        return "PAN"
    if signals["aadhaar"] and candidate["aadhaar"] and signals["aadhaar"] == candidate["aadhaar"]:
        return "AADHAAR"
    if _same_person_by_name(signals["name"], candidate["relative_name"]) or _same_person_by_name(
        candidate["name"], signals["relative_name"]
    ):
        return "CUSTOMER_MATCHES_RELATIVE"
    if (
        signals["pincode"] and candidate["pincode"] and signals["pincode"] == candidate["pincode"]
        and _same_person_by_name(signals["name"], candidate["name"])
    ):
        return "NAME_AND_ADDRESS"
    if signals["gst"] and candidate["gst"] and signals["gst"] == candidate["gst"]:
        return "GST"
    if signals["mobile"] and candidate["mobile"] and signals["mobile"] == candidate["mobile"]:
        return "MOBILE"
    if (
        signals["surname"] and candidate["surname"] and signals["surname"] == candidate["surname"]
        and signals["pincode"] and candidate["pincode"] and signals["pincode"] == candidate["pincode"]
    ):
        return "SURNAME_AND_PINCODE"
    if _addresses_match(signals["address"], candidate["address"]):
        return "SIMILAR_ADDRESS"
    return None


def _booking_confirm_date(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Any:
    """The receipt date on which this journey's running Booking payments
    first met the minimum booking amount -- set by
    ``evaluate_minimum_booking_payment`` onto
    ``journey_stage_states.booking_confirm_date``. None when the minimum
    hasn't been reached yet (or no Booking stage row exists at all)."""
    return connection.execute(
        text(
            """
            SELECT booking_confirm_date FROM auditcore.journey_stage_states
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND stage_code = 'BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()


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

            this_confirm_date = _booking_confirm_date(
                connection, tenant_id=tenant_id, journey_id=journey_id
            )
            other_confirm_date = _booking_confirm_date(
                connection, tenant_id=tenant_id, journey_id=candidate["journey_id"]
            )
            if this_confirm_date and other_confirm_date:
                if this_confirm_date != other_confirm_date:
                    is_original = this_confirm_date < other_confirm_date
                    originality_basis = "EARLIER_PAYMENT_DATE"
                else:
                    # Same receipt date on both sides -- fall through to the
                    # created_at/journey_id tiebreak below, same as neither
                    # having paid at all.
                    is_original = (this_journey_created, str(journey_id)) <= (
                        candidate["created_at_utc"], str(candidate["journey_id"])
                    )
                    originality_basis = "CREATED_AT_TIEBREAK"
            elif this_confirm_date and not other_confirm_date:
                is_original = True
                originality_basis = "ONLY_THIS_SIDE_PAID"
            elif other_confirm_date and not this_confirm_date:
                is_original = False
                originality_basis = "ONLY_OTHER_SIDE_PAID"
            else:
                # Neither side has reached its minimum booking amount yet --
                # created_at_utc alone can tie (two journeys created in the
                # same transaction share Postgres's transaction-start now()),
                # so journey_id as a stable secondary key guarantees exactly
                # one side of the pair is picked, consistently regardless of
                # which journey's own sync triggered this check.
                is_original = (this_journey_created, str(journey_id)) <= (
                    candidate["created_at_utc"], str(candidate["journey_id"])
                )
                originality_basis = "CREATED_AT_TIEBREAK"

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
                severity=_SEVERITY_BY_BASIS[basis],
                title="Possible duplicate booking for the same customer",
                description=(
                    f"This customer appears to match an earlier booking on this "
                    f"tenant ({_BASIS_LABEL[basis]} match). Confirm whether this "
                    "is a genuine repeat customer or a duplicate entry."
                ),
                correlation_id=correlation_id,
                safe_payload={
                    "matchBasis": basis,
                    "believedOriginalJourneyId": str(candidate["journey_id"]),
                    "originalityBasis": originality_basis,
                    "thisBookingConfirmDate": this_confirm_date,
                    "holderBookingConfirmDate": other_confirm_date,
                    "matchConfidencePercent": _CONFIDENCE_PERCENT_BY_BASIS[basis],
                    "matchConfidenceLabel": _CONFIDENCE_LABEL_BY_SEVERITY[_SEVERITY_BY_BASIS[basis]],
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
