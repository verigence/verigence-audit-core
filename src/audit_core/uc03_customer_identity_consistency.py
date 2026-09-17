"""uc03_customer_identity_consistency.py — every document on a Journey must
belong to the same customer, and every receipt must belong to this dealer.

Once a KYC document (Aadhaar / PAN / a generic Customer KYC evidence) is
extracted, it establishes the customer's name for this Journey. Every other
document that also carries a person's name (Booking Form, Insurance Cover, a
Bank Approval Letter, any tax/customer invoice) -- uploaded before or after
that KYC document, order does not matter -- is checked against it. A
mismatch raises one HIGH-severity ``WRONG_DOCUMENT_REVIEW`` Task per
mismatching document, and -- unlike most other machine-raised Tasks in this
codebase -- also holds that document's extracted fields out of
materialization (``evidence.identity_check_status='HELD'``): the wrong
customer's paperwork must not silently become this Journey's source of
truth for a price, a model, or anything else while a human is still
deciding whether it belongs here. PC completes the Task with an outcome
(``reject_wrong_document`` on INCORRECT soft-deletes the document and asks
for the right one; ``release_wrong_document_hold`` on CORRECT releases the
hold, recording that this check's fuzzy match under-scored a legitimate
name variant) -- see the module docstring's own note on this move further
down. The same hold applies, with no Task of its own, when no KYC document
has been extracted at all yet -- there's nothing to verify any other
document's name against, so nothing is trusted until one arrives (the
existing document-completeness rules already tell PC to upload it).

Independently, every receipt (dealer_receipt / payment_receipt -- both
schemas require DI to read a dealer_name off the document) is checked
against this Journey's own dealer, from the master record
(auditcore.dealers), not another document. A mismatch means a receipt from a
different dealership was attached here -- its own rule_key so it tracks and
self-heals independently of the customer-name check on the same document.
This narrower check does not hold anything and raises no decision -- a
plain informational ``WRONG_DOCUMENT_DEALER_NOTICE`` Task, see below.

``sync_customer_identity_consistency`` is the producer: idempotent, self-heals
(a later correction that now matches cancels the task), never raises.
Runs alongside every other per-document sync producer, both stages.

Direct user correction (2026-09-17): the customer-name mismatch moves off
Audit onto a plain ``WRONG_DOCUMENT_REVIEW`` Task, assigned straight to PC
("raise a task flag for PC to correct the document and provide the right
set of documents -- we will revisit this scenario later"). Unlike
DUPLICATE_RECEIPT (item 3 of this same correction), this one still carries
a real decision -- PC completes it with outcome CORRECT (this check's
fuzzy match under-scored a legitimate name variant -- release the hold) or
INCORRECT (genuinely the wrong document -- void it and ask for a
reupload), mirroring the exact CORRECT/INCORRECT completion model
PC_VERIFY_UNRECOGNIZED_DOCUMENT already uses (tasks_api.py). Simpler than
today's TL-adjudicated finding by one step (no separate Confirm-Breach/
Mark-False-Positive verdict, PC just says which it is), matching the
"revisit later" scope the user asked for -- the underlying hold mechanic
(``evidence.identity_check_status``) and the reject/release actions
(``reject_wrong_document`` / ``release_wrong_document_hold``) are
unchanged, only who acts and how they're asked to.

The receipt-vs-dealer check never had a hold or a reject/release action to
begin with (see its own note further down) -- it becomes a plain
informational ``WRONG_DOCUMENT_DEALER_NOTICE`` task instead, the same
"tell PC, no decision required" shape as DUPLICATE_RECEIPT_NOTICE.

A ``WRONG_DOCUMENT`` finding raised before this move still resolves (and
a Team Lead can still act on it via Confirm Breach / Mark False Positive)
through the unchanged legacy hook in uc03_audit_flags.py.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_v2_review_materialization import _INVOICE_DOCUMENT_TYPES
from audit_core.workflow import cancel_workflow_task, create_workflow_task

logger = logging.getLogger(__name__)

_FINDING_TYPE = "WRONG_DOCUMENT"
_RULE_PREFIX = "WRONG_DOCUMENT"
_DEALER_RULE_PREFIX = "WRONG_DOCUMENT:DEALER"
_SEVERITY = "HIGH"
TASK_TYPE = "WRONG_DOCUMENT_REVIEW"
DEALER_TASK_TYPE = "WRONG_DOCUMENT_DEALER_NOTICE"
_WORKFLOW_TYPE = "UC03_WRONG_DOCUMENT"
_OPEN_TASK_STATUSES = {"PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT"}

# Both schemas (verigence-di schemas/dealer_receipt.py, payment_receipt.py)
# require DI to read dealer_name off every receipt.
_RECEIPT_DOCUMENT_TYPES: tuple[str, ...] = ("dealer_receipt", "payment_receipt")
_DEALER_NAME_FIELD = "dealer_name"

# KYC document types, in preference order when more than one is present --
# a government photo ID outranks a self-declared KYC form.
_KYC_DOCUMENT_TYPES: tuple[str, ...] = ("aadhaar", "pan_card", "customer_kyc")

# Every document type this check knows carries a natural-person name, and
# which field holds it. Invoice types all share the same DI schema (see
# uc03_v2_review_materialization._INVOICE_DOCUMENT_TYPES) so they all read
# buyer_name.
_NAME_FIELDS_BY_DOCUMENT_TYPE: dict[str, str] = {
    "aadhaar": "aadhaar_name",
    "pan_card": "pan_name",
    "customer_kyc": "customer_name",
    "booking_form": "customer_name",
    "insurance_cover": "insured_name",
    "bank_approval_letter": "applicant_name",
    # Both receipt schemas also require a customer_name -- checked here
    # alongside every other named document; their dealer_name is a
    # separate, independent check (see _receipt_dealer_names) against the
    # dealer master record, not another document's name.
    "dealer_receipt": "customer_name",
    "payment_receipt": "customer_name",
    **{document_type: "buyer_name" for document_type in _INVOICE_DOCUMENT_TYPES},
}

# Below this ratio on the normalized strings, two names are treated as
# genuinely different rather than a formatting/spelling variant. Deliberately
# tolerant, not exact-match: real names vary in spacing, initials, and minor
# transliteration/typos across independently-scanned documents. Biased toward
# flagging when uncertain -- a false positive costs a TL one Accept/Reject
# glance; a missed genuine mismatch costs a wrong customer's paperwork going
# unnoticed.
_NAME_MATCH_THRESHOLD = 0.55

_TITLE_PREFIX = re.compile(
    r"^(mr|mrs|ms|miss|dr|shri|smt|md|prof)\.?\s+", re.IGNORECASE
)
_NON_ALPHA = re.compile(r"[^A-Za-z\s]")
_WHITESPACE = re.compile(r"\s+")


def _normalize_name(value: Any) -> str:
    text_value = str(value or "").strip()
    text_value = _TITLE_PREFIX.sub("", text_value)
    text_value = _NON_ALPHA.sub(" ", text_value)
    return _WHITESPACE.sub(" ", text_value).strip().upper()


def _names_match(a: Any, b: Any) -> bool:
    """True when two extracted name strings plausibly refer to the same
    person. Empty/unparseable input is not this check's job to flag."""
    normalized_a, normalized_b = _normalize_name(a), _normalize_name(b)
    if not normalized_a or not normalized_b:
        return True
    if normalized_a == normalized_b:
        return True
    return SequenceMatcher(None, normalized_a, normalized_b).ratio() >= _NAME_MATCH_THRESHOLD


def _named_documents(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """One row per document that carries a mapped name field: its highest-
    confidence, most-recent value. Reads durable storage only, never DI."""
    mapping_values = ", ".join(
        f"(:doc_type_{i}, :field_key_{i})"
        for i in range(len(_NAME_FIELDS_BY_DOCUMENT_TYPE))
    )
    mapping_params = {
        f"{name}_{i}": value
        for i, (document_type, field_key) in enumerate(_NAME_FIELDS_BY_DOCUMENT_TYPE.items())
        for name, value in (("doc_type", document_type), ("field_key", field_key))
    }
    rows = connection.execute(
        text(
            f"""
            WITH mapping(document_type_key, field_key) AS (VALUES {mapping_values}),
            ranked AS (
                SELECT
                    f.di_document_id,
                    f.evidence_id,
                    f.stage_code,
                    f.source_document_type_key AS document_type_key,
                    f.effective_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.di_document_id
                        ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                    ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                JOIN mapping m
                  ON m.document_type_key = f.source_document_type_key
                 AND m.field_key = f.field_key
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.effective_value IS NOT NULL
            )
            SELECT di_document_id, evidence_id, stage_code, document_type_key, effective_value
            FROM ranked WHERE row_rank = 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, **mapping_params},
    ).mappings().all()
    return [dict(row) for row in rows]


def _reference_name(documents: list[dict[str, Any]]) -> tuple[str, UUID] | None:
    """The customer's KYC name, and the document it came from, by preference
    order. None when no KYC document has been extracted yet -- nothing to
    check other documents against."""
    for kyc_type in _KYC_DOCUMENT_TYPES:
        for document in documents:
            if document["document_type_key"] == kyc_type:
                name = str(document["effective_value"] or "").strip()
                if name:
                    return name, document["di_document_id"]
    return None


def _journey_dealer_name(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> str | None:
    """This Journey's actual dealer, from the master record -- not another
    document. None if the journey/dealer row can't be resolved (never the
    normal case; the caller treats it as nothing to check against)."""
    return connection.execute(
        text(
            """
            SELECT d.dealer_name
            FROM auditcore.journeys j
            JOIN auditcore.dealers d
              ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()


def _receipt_dealer_names(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """One row per receipt document with its latest dealer_name value."""
    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    f.di_document_id, f.stage_code,
                    f.source_document_type_key AS document_type_key,
                    f.effective_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.di_document_id
                        ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                    ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.source_document_type_key = ANY(:document_types)
                  AND f.field_key = :field_key
                  AND f.effective_value IS NOT NULL
            )
            SELECT di_document_id, stage_code, document_type_key, effective_value
            FROM ranked WHERE row_rank = 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_types": list(_RECEIPT_DOCUMENT_TYPES),
            "field_key": _DEALER_NAME_FIELD,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _set_identity_check_status(
    connection: Connection, *, tenant_id: str, evidence_id: UUID | None, status: str
) -> None:
    """Move a document's evidence row between PASSED/HELD -- never touches a
    row a Team Lead already REJECTED (a genuinely re-uploaded replacement
    gets its own fresh evidence row with its own default, not this one
    flipped back)."""
    if evidence_id is None:
        return
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET identity_check_status = :status
            WHERE tenant_id = :tenant_id AND evidence_id = :evidence_id
              AND identity_check_status <> 'REJECTED'
            """
        ),
        {"tenant_id": tenant_id, "evidence_id": evidence_id, "status": status},
    )


def reject_wrong_document(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    di_document_id: UUID,
    stage_code: str,
    actor_id: str,
    reason: str | None,
    correlation_id: str,
    related_finding_id: UUID | None = None,
) -> None:
    """A Team Lead confirmed via Confirm Breach that this really is the
    wrong customer's document: soft-delete it (VOIDED -- the row and its
    audit trail survive, but it's no longer the active evidence for its
    requirement, exactly like the existing supersede-on-reupload pattern)
    and ask PC to upload the correct one against the same requirement, the
    same way a Team Lead's own "request reupload" action does.
    Idempotent: a no-op if the evidence is already not ACTIVE (e.g. this
    verdict is applied twice, or the document was independently replaced).
    """
    evidence_row = connection.execute(
        text(
            """
            SELECT evidence_id, journey_document_requirement_id, document_type_key
            FROM auditcore.evidence
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND di_document_id = :di_document_id AND association_status = 'ACTIVE'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "di_document_id": di_document_id},
    ).mappings().one_or_none()
    if evidence_row is None:
        return

    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status = 'VOIDED',
                identity_check_status = 'REJECTED',
                void_reason = :reason,
                voided_by_actor_id = :actor_id,
                voided_at_utc = now()
            WHERE tenant_id = :tenant_id AND evidence_id = :evidence_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "evidence_id": evidence_row["evidence_id"],
            "reason": reason or "Confirmed as the wrong customer's document (identity check).",
            "actor_id": actor_id,
        },
    )

    if evidence_row["journey_document_requirement_id"] is not None:
        create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type="UC03_DOCUMENT_REUPLOAD",
            process_area=stage_code,
            task_type="PC_DOCUMENT_REUPLOAD",
            assigned_role_code="PC",
            related_finding_id=related_finding_id,
            task_payload={
                "documentId": str(di_document_id),
                "requirementRef": str(evidence_row["journey_document_requirement_id"]),
                "reason": reason or "The uploaded document's name did not match the customer's KYC and was confirmed as the wrong document.",
            },
            effect_key=f"task:identity-reject:{evidence_row['evidence_id']}",
            correlation_id=correlation_id,
        )


def release_wrong_document_hold(
    connection: Connection, *, tenant_id: str, di_document_id: UUID
) -> None:
    """A Team Lead Marked False Positive: this check's fuzzy match under-
    scored a legitimate name variant. Release the hold so the document's
    fields are eligible for materialization again on the next resolution
    pass -- the caller is responsible for actually re-running it."""
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET identity_check_status = 'PASSED'
            WHERE tenant_id = :tenant_id AND di_document_id = :di_document_id
              AND identity_check_status = 'HELD'
            """
        ),
        {"tenant_id": tenant_id, "di_document_id": di_document_id},
    )


def apply_wrong_document_verification(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    di_document_id: UUID,
    outcome: str,
    actor_id: str,
    correlation_id: str,
) -> None:
    """Completion side effect for a WRONG_DOCUMENT_REVIEW task
    (tasks_api.py::complete_task), same CORRECT/INCORRECT vocabulary as
    PC_VERIFY_UNRECOGNIZED_DOCUMENT. INCORRECT: genuinely the wrong
    customer's document -- void it and ask for a reupload
    (reject_wrong_document). CORRECT: this check's fuzzy match under-
    scored a legitimate name variant -- release the hold
    (release_wrong_document_hold) and, for a Booking document, refresh
    materialization immediately rather than waiting on some unrelated
    future sync, same as the legacy finding's Mark False Positive hook."""
    if outcome == "INCORRECT":
        reject_wrong_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            di_document_id=di_document_id,
            stage_code=stage_code,
            actor_id=actor_id,
            reason=None,
            correlation_id=correlation_id,
        )
    elif outcome == "CORRECT":
        release_wrong_document_hold(connection, tenant_id=tenant_id, di_document_id=di_document_id)
        if stage_code.upper() == "BOOKING":
            from audit_core.uc03_post_extraction_materialization import (
                materialize_machine_booking_values,
            )

            materialize_machine_booking_values(
                connection, tenant_id=tenant_id, journey_id=journey_id,
            )


def _resolve_stale_legacy_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    current_rule_keys: set[str],
    correlation_id: str,
) -> int:
    """A WRONG_DOCUMENT finding raised before this producer moved onto the
    Task Queue -- nothing else in the codebase resolves it any more on its
    own (a Team Lead can still act on it directly via Confirm Breach / Mark
    False Positive, see uc03_audit_flags.py), but a later correction that
    breaks the match should still close it automatically, same as before.
    Safe to delete once no tenant has one of these findings open any
    longer."""
    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, stage_code, rule_key
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND finding_type_code=:finding_type
              AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "finding_type": _FINDING_TYPE},
    ).mappings().all()
    resolved = 0
    for row in rows:
        if row["rule_key"] in current_rule_keys:
            continue
        _resolve_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=row["stage_code"],
            finding_id=row["audit_finding_id"],
            actor_id=None,
            correlation_id=correlation_id,
            note="Name now matches after a correction.",
        )
        resolved += 1
    return resolved


def _cancel_if_open(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    task_type: str,
    rule_key: str,
    reason: str,
) -> bool:
    """Cancel an open Task for rule_key, if one exists (self-heal: a later
    correction broke the match). Returns whether a Task was actually
    cancelled (for the caller's own counter)."""
    task_id = connection.execute(
        text(
            """
            SELECT workflow_task_id FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND task_type=:task_type
              AND task_payload->>'ruleKey' = :rule_key
              AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "task_type": task_type, "rule_key": rule_key},
    ).scalar_one_or_none()
    if task_id is None:
        return False
    cancel_workflow_task(
        connection, tenant_id=tenant_id, workflow_task_id=task_id, actor_id="SYSTEM", reason=reason,
    )
    return True


def _effect_key(task_type: str, tenant_id: str, journey_id: UUID, rule_key: str) -> str:
    return f"task:{task_type.lower().replace('_', '-')}:{tenant_id}:{journey_id}:{rule_key}"


def _create_task_if_absent(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    task_type: str,
    rule_key: str,
    task_payload: dict[str, Any],
    correlation_id: str,
) -> None:
    effect_key = _effect_key(task_type, tenant_id, journey_id, rule_key)
    existing = connection.execute(
        text(
            "SELECT 1 FROM auditcore.workflow_tasks "
            "WHERE tenant_id = :tenant_id AND effect_key = :effect_key"
        ),
        {"tenant_id": tenant_id, "effect_key": effect_key},
    ).scalar_one_or_none()
    if existing is not None:
        return
    create_workflow_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        workflow_type=_WORKFLOW_TYPE,
        process_area=stage_code,
        task_type=task_type,
        assigned_role_code="PC",
        severity=_SEVERITY,
        task_payload={"ruleKey": rule_key, **task_payload},
        effect_key=effect_key,
        correlation_id=correlation_id,
    )


def sync_customer_identity_consistency(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Raise a WRONG_DOCUMENT_REVIEW task for any document whose name
    doesn't match the Journey's KYC name, and a WRONG_DOCUMENT_DEALER_NOTICE
    task for any receipt whose dealer name doesn't match this Journey's
    dealer; cancel either once a later correction matches. Idempotent,
    self-heals on read, never raises."""
    try:
        raised = 0
        resolved = 0
        examined = 0
        reference_name: str | None = None
        live_rule_keys: set[str] = set()

        documents = _named_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
        reference = _reference_name(documents)
        # Unlike the dealer-name check below (checked against a master
        # record, always available), there is nothing to check customer
        # names against until a KYC document exists -- skip only this half,
        # not the whole producer, so the dealer-name check still runs.
        if reference is not None:
            reference_name, reference_document_id = reference
            for document in documents:
                document_id = document["di_document_id"]
                if document_id == reference_document_id:
                    continue
                document_name = str(document["effective_value"] or "").strip()
                examined += 1
                rule_key = f"{_RULE_PREFIX}:{document_id}"
                if not document_name or _names_match(reference_name, document_name):
                    _set_identity_check_status(
                        connection,
                        tenant_id=tenant_id,
                        evidence_id=document.get("evidence_id"),
                        status="PASSED",
                    )
                    if _cancel_if_open(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        task_type=TASK_TYPE,
                        rule_key=rule_key,
                        reason="Name now matches the customer's KYC document.",
                    ):
                        resolved += 1
                    continue

                raised += 1
                live_rule_keys.add(rule_key)
                # Held out of materialization the instant the mismatch is
                # detected -- this same sync pass' later materialization
                # step (uc03_confidence_review_policy._sync_booking_document)
                # runs after this producer, so a freshly-flagged document's
                # data is protected before it ever gets a chance to
                # materialize wrongly, not just on some later pass.
                _set_identity_check_status(
                    connection,
                    tenant_id=tenant_id,
                    evidence_id=document.get("evidence_id"),
                    status="HELD",
                )
                _create_task_if_absent(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code=document["stage_code"],
                    task_type=TASK_TYPE,
                    rule_key=rule_key,
                    task_payload={
                        "documentTypeKey": document["document_type_key"],
                        "diDocumentId": str(document_id),
                        "extractedName": document_name,
                        "kycName": reference_name,
                        "kycDocumentId": str(reference_document_id),
                        "comment": (
                            f"This {document['document_type_key']} shows the name "
                            f"'{document_name}', which does not match the customer's "
                            f"KYC name '{reference_name}' on this Journey. Please verify "
                            "and, if this is genuinely the wrong customer's document, "
                            "correct it and upload the right one."
                        ),
                    },
                    correlation_id=correlation_id,
                )
        else:
            # No KYC document has been extracted on this Journey yet, so
            # there is nothing to check any other named document against --
            # hold them all rather than trusting an unverifiable name. This
            # doesn't raise its own task: the existing document-
            # completeness rules (e.g. BK_PAN_PRESENT) already tell PC to
            # upload KYC; once one is extracted, the very next sync
            # re-evaluates every held document through the branch above.
            for document in documents:
                if document["document_type_key"] in _KYC_DOCUMENT_TYPES:
                    continue
                _set_identity_check_status(
                    connection,
                    tenant_id=tenant_id,
                    evidence_id=document.get("evidence_id"),
                    status="HELD",
                )

        dealer_name = _journey_dealer_name(connection, tenant_id=tenant_id, journey_id=journey_id)
        if dealer_name:
            for receipt in _receipt_dealer_names(connection, tenant_id=tenant_id, journey_id=journey_id):
                document_id = receipt["di_document_id"]
                receipt_dealer_name = str(receipt["effective_value"] or "").strip()
                examined += 1
                rule_key = f"{_DEALER_RULE_PREFIX}:{document_id}"
                if not receipt_dealer_name or _names_match(dealer_name, receipt_dealer_name):
                    if _cancel_if_open(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        task_type=DEALER_TASK_TYPE,
                        rule_key=rule_key,
                        reason="Dealer name now matches this Journey's dealer.",
                    ):
                        resolved += 1
                    continue

                raised += 1
                live_rule_keys.add(rule_key)
                _create_task_if_absent(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code=receipt["stage_code"],
                    task_type=DEALER_TASK_TYPE,
                    rule_key=rule_key,
                    task_payload={
                        "documentTypeKey": receipt["document_type_key"],
                        "diDocumentId": str(document_id),
                        "extractedDealerName": receipt_dealer_name,
                        "journeyDealerName": dealer_name,
                        "comment": (
                            f"This {receipt['document_type_key']} shows dealer "
                            f"'{receipt_dealer_name}', which does not match this "
                            f"Journey's dealer '{dealer_name}'. A receipt from a "
                            "different dealership may have been attached here -- "
                            "please verify."
                        ),
                    },
                    correlation_id=correlation_id,
                )

        resolved += _resolve_stale_legacy_findings(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            current_rule_keys=live_rule_keys,
            correlation_id=correlation_id,
        )

        return {
            "raised": raised,
            "resolved": resolved,
            "examined": examined,
            "referenceName": reference_name,
        }
    except Exception:
        logger.warning("sync_customer_identity_consistency failed", exc_info=True)
        return {"error": True}


__all__ = [
    "DEALER_TASK_TYPE",
    "TASK_TYPE",
    "apply_wrong_document_verification",
    "reject_wrong_document",
    "release_wrong_document_hold",
    "sync_customer_identity_consistency",
]
