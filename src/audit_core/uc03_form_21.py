"""A Form 21 is never an invoice (product owner, 2026-11-02).

Form 21 is the RTO sale certificate ("FORM 21 {See Rule 47(a) and (d)} SALES
CERTIFICATE"). Document Intelligence has no type for it yet, so it reads one as
the invoice it looks like (customer_invoice_dms, sometimes wholesale_invoice):
an invoice number, the chassis and the model, and no amounts. Filed into the
single-slot invoice requirement, it replaced the real vehicle invoice
(REPLACED_BY_NEWER_UPLOAD): live on 2026-11-02, 57 bookings held one ACTIVE as
an invoice (39 headed "Form 21" / "Sale Certificate", 18 only "SALES
CERTIFICATE"), and on about a third the real invoice had been set aside by it.

Product owner's rules: no document is lost or hidden. A Form 21 is not an
invoice: it never counts as an invoice, never fills an invoice requirement and
never replaces a different document; it stays on the booking, listed as a
Form 21. A new upload replaces only a copy of the same document. One place in
the pipeline, every booking.

That place is ``file_reading``, called by the one per-document sync
(uc03_confidence_review_policy._sync_booking_document) where DI's facts become
Audit Core's: an invoice-typed reading whose ``invoice_heading_as_printed``
says Form 21 / Sale(s) Certificate (``is_form_21``) is filed as ``form_21``
(template "Form 21 (Sale Certificate)", requirement ``p2_form_21``) -- its
evidence, its facts' type, its page's template -- and every reader that keys
on those (the Deal, the invoice materialisation, the checklist, the audit
rules, the SKU resolution) no longer sees an invoice. DI will add ``form_21``
later; its readings are filed the same way without this rule.

Whether a new invoice-typed copy replaces the one on file can only be known
once it is read, so for those single-slot requirements (and the Form 21's own)
the replacement is decided here too (``settle_slot``), not when DI links the
page (uc03_pc_booking_documents.acknowledge_booking_document_link): the
newest copy that was read is the one on file. A Form 21 therefore never
replaces anything.

``run_form_21_refile`` (p2_worker_jobs kind FORM_21_REFILE) repairs what was
filed before: a DRY RUN unless the tag ends with "-commit".
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

FORM_21_DI_TYPE = "form_21"
FORM_21_TEMPLATE = "form_21"
FORM_21_REQUIREMENT_KEY = "p2_form_21"
HEADING_FIELD = "invoice_heading_as_printed"
TOTAL_FIELD = "grand_total_amount"
JOB = "FORM_21_REFILE"
ACTOR = "SYSTEM:FORM_21"
FILED_EVENT = "DOCUMENT_FILED_AS_FORM_21"
BACK_EVENT = "FORM_21_COPY_BACK"
_REPLACED = "REPLACED_BY_NEWER_UPLOAD"
# "FORM 21", "Form-21", "FORM NO. 21" (never "Form 210" or "Form 22"), and the
# certificate's own title, "Sale Certificate" / "SALES CERTIFICATE" -- the
# Mahindra DMS prints the second without "Form 21" on 85 live readings.
_FORM_21_HEADING = re.compile(
    r"\bform\s*[-_.]?\s*(?:no\.?\s*)?21\b|\bsales?\s+certificate\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def invoice_document_types() -> frozenset[str]:
    """DI's generalized-invoice types: the readings a Form 21 can come back as."""
    from audit_core.uc03_v2_review_materialization import _INVOICE_DOCUMENT_TYPES

    return frozenset(_INVOICE_DOCUMENT_TYPES)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _fields(fields: Any) -> dict[str, Any]:
    """A field-key -> value view of a mapping, DI facts (``field_key`` /
    ``value``) or stored rows (``field_key`` / ``effective_value``)."""
    if isinstance(fields, Mapping):
        return dict(fields)
    out: dict[str, Any] = {}
    for item in fields or ():
        if isinstance(item, Mapping):
            key = item.get("field_key") or item.get("fieldKey")
            value = item.get("effective_value", item.get("value"))
        else:
            key = getattr(item, "field_key", None) or getattr(item, "fieldKey", None)
            value = getattr(item, "effective_value", getattr(item, "value", None))
        if key and value not in (None, ""):
            out[str(key)] = value
    return out


def is_form_21(fields: Mapping[str, Any] | Iterable[Any] | None) -> bool:
    """The reading's printed heading says Form 21 / Sale(s) Certificate and
    it prints no grand total. A Form 21 bills nothing (none of the 245 live
    readings with that heading has a total); a reading that does carry one
    -- an invoice page grouped with the Form 21 into one document -- stays an
    invoice rather than take a real bill out of the Deal."""
    values = _fields(fields)
    heading = _text(values.get(HEADING_FIELD))
    if not heading or not _FORM_21_HEADING.search(heading):
        return False
    return values.get(TOTAL_FIELD) in (None, "", "null")


def filed_document_type(di_type: str | None, fields: Mapping[str, Any] | Iterable[Any] | None) -> str | None:
    """The type a DI reading is filed under: ``form_21`` for an invoice-typed
    reading of a Form 21, else DI's own type. The one classification rule."""
    if di_type and str(di_type).strip().lower() in invoice_document_types() and is_form_21(fields):
        return FORM_21_DI_TYPE
    return di_type


def decided_by_reading(document_type_key: str | None) -> bool:
    """A single-slot requirement of this type learns what a new copy is only
    once it is read (an invoice-typed one may be a Form 21): the copy it
    replaces is decided by ``settle_slot``, not when DI links the page."""
    key = str(document_type_key or "").strip().lower()
    return key == FORM_21_DI_TYPE or key in invoice_document_types()


def _repeatable(requirement_key: str | None) -> bool:
    from audit_core.uc03_pc_booking_documents import _is_repeatable_requirement

    return _is_repeatable_requirement(requirement_key)


# ------------------------------------------------------------------ filing


def ensure_form_21_requirement(connection: Connection, *, tenant_id: str, journey_id: UUID) -> UUID:
    """The booking's Form 21 checklist row (OPTIONAL, Delivery), created on
    demand like uc03_p2_worker._ensure_p2_requirement_rows' own rows."""
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            )
            SELECT CAST(:t AS varchar), CAST(:j AS uuid), NULL, CAST(:rk AS varchar),
                   CAST(:dt AS varchar), 'DELIVERY', 'OPTIONAL', 'PENDING', '{}'::jsonb
            WHERE NOT EXISTS (
                SELECT 1 FROM auditcore.journey_document_requirements
                WHERE tenant_id=CAST(:t AS varchar) AND journey_id=CAST(:j AS uuid)
                  AND document_type_key=CAST(:dt AS varchar)
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
            """
        ),
        {"t": tenant_id, "j": journey_id, "rk": FORM_21_REQUIREMENT_KEY, "dt": FORM_21_DI_TYPE},
    )
    return UUID(str(connection.execute(
        text(
            """
            SELECT journey_document_requirement_id FROM auditcore.journey_document_requirements
            WHERE tenant_id=:t AND journey_id=:j AND document_type_key=:dt
            ORDER BY (requirement_key = :rk) DESC, requirement_key
            LIMIT 1
            """
        ),
        {"t": tenant_id, "j": journey_id, "dt": FORM_21_DI_TYPE, "rk": FORM_21_REQUIREMENT_KEY},
    ).scalar_one()))


def refile_evidence(connection: Connection, *, tenant_id: str, journey_id: UUID, evidence_id: UUID,
                    actor_id: str = ACTOR, heading: str | None = None) -> bool:
    """File one invoice-typed evidence as the Form 21 it is: its evidence row
    moves to the Form 21 requirement, and its facts, its page and its invoice
    projection row (which the runtime role may not delete) say form_21, so no
    invoice reader takes it again. Its status is left as it is. False when it
    is already filed as a Form 21."""
    current = connection.execute(
        text(
            """
            SELECT di_document_id, document_type_key FROM auditcore.evidence
            WHERE tenant_id=:t AND journey_id=:j AND evidence_id=:e
            FOR UPDATE
            """
        ),
        {"t": tenant_id, "j": journey_id, "e": evidence_id},
    ).mappings().one_or_none()
    if current is None or current["document_type_key"] == FORM_21_DI_TYPE:
        return False
    requirement_id = ensure_form_21_requirement(connection, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence SET document_type_key=:dt, journey_document_requirement_id=:r
            WHERE tenant_id=:t AND evidence_id=:e
            """
        ),
        {"t": tenant_id, "e": evidence_id, "dt": FORM_21_DI_TYPE, "r": requirement_id},
    )
    moved = {"di_document_id": current["di_document_id"], "old_type": current["document_type_key"]}
    params = {"t": tenant_id, "j": journey_id, "d": moved["di_document_id"], "dt": FORM_21_DI_TYPE,
              "rk": FORM_21_REQUIREMENT_KEY}
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_document_extracted_fields
            SET source_document_type_key=:dt, updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
              AND source_document_type_key IS DISTINCT FROM :dt
            """
        ),
        params,
    )
    # A PC's own choice of type (type_overridden_by_actor_id) is overridden
    # too: there was no Form 21 to choose, and a Form 21 is never an invoice.
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET template_key=:dt, classified_document_type=:dt, updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
              AND (template_key IS DISTINCT FROM :dt OR classified_document_type IS DISTINCT FROM :dt)
            """
        ),
        params,
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents
            SET classified_document_type_key=:dt, requirement_key=:rk, updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
              AND (classified_document_type_key IS DISTINCT FROM :dt OR requirement_key IS DISTINCT FROM :rk)
            """
        ),
        params,
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.invoice_review_values SET document_type_key=:dt
            WHERE tenant_id=:t AND journey_id=:j AND source_di_document_id=:d AND document_type_key <> :dt
            """
        ),
        params,
    )
    from audit_core.uc03_p2_runtime import record_activity

    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type=FILED_EVENT,
        subject_type="DOCUMENT", subject_id=str(moved["di_document_id"]),
        details={"evidenceId": str(evidence_id), "readAs": moved["old_type"], "heading": heading, "by": actor_id},
    )
    logger.info("form_21_filed", tenant_id=tenant_id, journey_id=str(journey_id), evidence_id=str(evidence_id),
                read_as=moved["old_type"])
    return True


def form_21_documents(connection: Connection, *, tenant_id: str, journey_id: UUID) -> set[str]:
    """The booking's documents filed as a Form 21, so DI's own classification
    of them, read again on every reconcile, never files them back as invoices
    (uc03_unified_document_capture.apply_di_classification)."""
    return {str(d) for d in connection.execute(
        text("SELECT di_document_id FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
             "AND document_type_key=:dt"),
        {"t": tenant_id, "j": journey_id, "dt": FORM_21_DI_TYPE},
    ).scalars().all()}


def _supersede(connection: Connection, *, tenant_id: str, journey_id: UUID, evidence_id: Any, actor_id: str,
               by_evidence_id: Any) -> bool:
    """The write acknowledge_booking_document_link makes when a newer copy
    replaces a single-slot document (decision 2026-10-01): the earlier copy
    stays on file SUPERSEDED, restorable from its card."""
    document_id = connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='SUPERSEDED', void_reason=:reason, voided_by_actor_id=:actor, voided_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND evidence_id=:e AND association_status='ACTIVE'
            RETURNING di_document_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "e": evidence_id, "actor": actor_id, "reason": _REPLACED},
    ).scalar_one_or_none()
    if document_id is None:
        return False
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents SET capture_status='SUPERSEDED', updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": document_id},
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence SET supersedes_evidence_id=:old
            WHERE tenant_id=:t AND evidence_id=:new AND supersedes_evidence_id IS NULL
            """
        ),
        {"t": tenant_id, "new": by_evidence_id, "old": evidence_id},
    )
    logger.info("uc03_document_superseded_by_newer_upload", tenant_id=tenant_id, journey_id=str(journey_id),
                prior_evidence_id=str(evidence_id), new_evidence_id=str(by_evidence_id), decided="ON_READING")
    return True


def settle_slot(connection: Connection, *, tenant_id: str, journey_id: UUID, evidence_id: UUID,
                actor_id: str) -> list[str]:
    """A single-slot requirement whose copies are told apart only once read
    (``decided_by_reading``): the newest copy that was read is the one on
    file. Called when ``evidence_id`` has just been read. Copies not read yet
    are left as they are (the earlier one keeps counting meanwhile). Returns
    the evidence ids superseded."""
    me = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.journey_document_requirement_id, e.document_type_key, e.association_status,
                   e.linked_at_utc, r.requirement_key
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_requirements r
              ON r.tenant_id=e.tenant_id AND r.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.evidence_id=:e
            """
        ),
        {"t": tenant_id, "j": journey_id, "e": evidence_id},
    ).mappings().one_or_none()
    if (me is None or me["association_status"] != "ACTIVE" or not decided_by_reading(me["document_type_key"])
            or _repeatable(me["requirement_key"])):
        return []
    others = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.linked_at_utc
            FROM auditcore.evidence e
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.journey_document_requirement_id=:r
              AND e.association_status='ACTIVE' AND e.evidence_id <> :e
              AND EXISTS (SELECT 1 FROM auditcore.journey_document_extracted_fields f
                          WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                            AND f.di_document_id=e.di_document_id)
            ORDER BY e.linked_at_utc, e.evidence_id
            FOR UPDATE OF e
            """
        ),
        {"t": tenant_id, "j": journey_id, "r": me["journey_document_requirement_id"], "e": evidence_id},
    ).mappings().all()

    def order(row: Any) -> tuple[Any, str]:
        return row["linked_at_utc"], str(row["evidence_id"])

    newer = [o for o in others if order(o) > order(me)]
    if newer:
        winner = max(newer, key=order)
        _supersede(connection, tenant_id=tenant_id, journey_id=journey_id, evidence_id=evidence_id,
                   actor_id=actor_id, by_evidence_id=winner["evidence_id"])
        return [str(evidence_id)]
    superseded = []
    for older in sorted(others, key=order, reverse=True):
        if _supersede(connection, tenant_id=tenant_id, journey_id=journey_id, evidence_id=older["evidence_id"],
                      actor_id=actor_id, by_evidence_id=evidence_id):
            superseded.append(str(older["evidence_id"]))
    return superseded


def file_reading(connection: Connection, *, tenant_id: str, journey_id: UUID, evidence_id: UUID,
                 di_type: str | None, facts: Iterable[Any], actor_id: str) -> str | None:
    """Where a DI reading becomes a document on the booking: the type its
    facts are stored under (``filed_document_type``), the Form 21 filed as
    one, and the single-slot copy it replaces, if any (``settle_slot``)."""
    facts = list(facts or [])
    current = connection.execute(
        text("SELECT document_type_key FROM auditcore.evidence WHERE tenant_id=:t AND evidence_id=:e"),
        {"t": tenant_id, "e": evidence_id},
    ).scalar_one_or_none()
    # Once filed as a Form 21 it stays one (a later reading of the same page
    # does not file it back as an invoice).
    filed = FORM_21_DI_TYPE if current == FORM_21_DI_TYPE else filed_document_type(di_type, facts)
    if filed == FORM_21_DI_TYPE and current != FORM_21_DI_TYPE and refile_evidence(
        connection, tenant_id=tenant_id, journey_id=journey_id, evidence_id=evidence_id, actor_id=actor_id,
        heading=_text(_fields(facts).get(HEADING_FIELD)),
    ):
        # Linked before this rule (or by any path that still replaces at
        # link time): what it set aside comes back. The caller's own
        # recompute follows.
        repair_journey(connection, tenant_id=tenant_id, journey_id=journey_id, commit=True, recompute=False)
    if facts:
        settle_slot(connection, tenant_id=tenant_id, journey_id=journey_id, evidence_id=evidence_id,
                    actor_id=actor_id)
    return filed


# ------------------------------------------------------------- the repair


@dataclass(frozen=True)
class Copy:
    """One invoice-typed (or Form 21) evidence of a booking."""

    evidence_id: str
    di_document_id: str
    requirement_id: str | None
    requirement_key: str | None
    document_type_key: str
    status: str
    void_reason: str | None
    supersedes: str | None
    linked_at: Any
    read: bool
    merged_page: bool
    heading: str | None
    process_area: str | None = None
    grand_total: Any = None

    @property
    def form_21(self) -> bool:
        return self.document_type_key == FORM_21_DI_TYPE or is_form_21(
            {HEADING_FIELD: self.heading, TOTAL_FIELD: self.grand_total})

    @property
    def order(self) -> tuple[str, str]:
        at = self.linked_at.isoformat() if isinstance(self.linked_at, datetime) else str(self.linked_at)
        return at, self.evidence_id


@dataclass
class LineagePlan:
    refile: list[Copy] = field(default_factory=list)
    restore: list[Copy] = field(default_factory=list)
    supersede: list[tuple[Copy, Copy]] = field(default_factory=list)
    relink: list[tuple[Copy, str | None]] = field(default_factory=list)
    # Reported, never changed: an invoice requirement a Form 21 emptied whose
    # earlier copy cannot come back by itself (a page merged into a document,
    # a copy never read). A person uploads the invoice again.
    left: list[tuple[Copy, str]] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.refile or self.restore or self.supersede or self.relink)


def plan_lineage(copies: list[Copy]) -> LineagePlan:
    """What filing a booking's Form 21s as Form 21s changes (pure):

    - ``refile``: every invoice-typed copy whose heading says Form 21.
    - ``restore``: per invoice requirement left with no active copy, the
      newest copy set aside (REPLACED_BY_NEWER_UPLOAD) whose replacements
      down the supersede links (``supersedes_evidence_id`` on the newer row)
      include a Form 21 and no read copy of its own kind; likewise the newest
      Form 21 set aside by an invoice when the booking has no active Form 21.
      Never a page merged into a document (not a document of its own), never
      a copy that was not read, never one the PC replaced or removed.
    - ``supersede``: several Form 21s active once filed together -- copies of
      one document: the newest stays (the single-slot rule).
    - ``relink``: a supersede link between a Form 21 and an invoice was never
      true; it points to the nearest earlier copy of its own kind instead.
    """
    by_id = {c.evidence_id: c for c in copies}
    successors: dict[str, list[Copy]] = {}
    for c in copies:
        if c.supersedes:
            successors.setdefault(c.supersedes, []).append(c)

    def descendants(c: Copy) -> list[Copy]:
        seen: set[str] = {c.evidence_id}
        out: list[Copy] = []
        stack = list(successors.get(c.evidence_id, []))
        while stack:
            s = stack.pop()
            if s.evidence_id in seen:
                continue
            seen.add(s.evidence_id)
            out.append(s)
            stack.extend(successors.get(s.evidence_id, []))
        return out

    plan = LineagePlan(refile=[c for c in copies if c.form_21 and c.document_type_key != FORM_21_DI_TYPE])

    for c in copies:
        p = by_id.get(c.supersedes or "")
        if p is None or p.form_21 == c.form_21:
            continue
        q, seen = p, {c.evidence_id}
        while q is not None and q.form_21 != c.form_21 and q.evidence_id not in seen:
            seen.add(q.evidence_id)
            q = by_id.get(q.supersedes or "")
        plan.relink.append((c, q.evidence_id if q is not None and q.form_21 == c.form_21 else None))

    def set_aside(c: Copy) -> bool:
        return c.status == "SUPERSEDED" and c.void_reason == _REPLACED and not c.merged_page and c.read

    forms = sorted((c for c in copies if c.form_21), key=lambda c: c.order)
    active_forms = [c for c in forms if c.status == "ACTIVE"]
    for older in active_forms[:-1]:
        plan.supersede.append((older, active_forms[-1]))
    if not active_forms:
        eligible = [c for c in forms if set_aside(c)
                    and any(not d.form_21 and d.read for d in descendants(c))
                    and not any(d.form_21 for d in descendants(c))]
        if eligible:
            plan.restore.append(max(eligible, key=lambda c: c.order))

    slots: dict[str, list[Copy]] = {}
    for c in copies:
        if not c.form_21 and c.requirement_id and not _repeatable(c.requirement_key):
            slots.setdefault(c.requirement_id, []).append(c)
    for members in slots.values():
        if any(m.status == "ACTIVE" for m in members):
            continue
        replaced_by_form_21 = [m for m in members if m.status == "SUPERSEDED" and m.void_reason == _REPLACED
                               and any(d.form_21 for d in descendants(m))
                               and not any(not d.form_21 and d.read for d in descendants(m))]
        eligible = [m for m in replaced_by_form_21 if set_aside(m)]
        if eligible:
            plan.restore.append(max(eligible, key=lambda c: c.order))
        elif replaced_by_form_21:
            m = max(replaced_by_form_21, key=lambda c: c.order)
            plan.left.append((m, "a page merged into a document that holds the Form 21 too" if m.merged_page
                              else "never read"))
    return plan


def load_copies(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[Copy]:
    rows = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_document_id, e.journey_document_requirement_id, r.requirement_key,
                   e.document_type_key, e.association_status, e.void_reason, e.supersedes_evidence_id,
                   e.linked_at_utc, e.process_area,
                   EXISTS (SELECT 1 FROM auditcore.journey_document_extracted_fields f
                           WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                             AND f.di_document_id=e.di_document_id) AS read,
                   EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                           WHERE q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id
                             AND q.di_document_id=e.di_document_id
                             AND q.unit_kind='PAGE' AND q.queue_status='MERGED') AS merged_page,
                   (SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                     WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                       AND f.di_document_id=e.di_document_id AND f.field_key=:heading
                       AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
                     ORDER BY f.source_fact_version DESC NULLS LAST, f.updated_at_utc DESC LIMIT 1) AS heading,
                   (SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f
                     WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                       AND f.di_document_id=e.di_document_id AND f.field_key=:total
                       AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
                     ORDER BY f.source_fact_version DESC NULLS LAST, f.updated_at_utc DESC LIMIT 1) AS grand_total
            FROM auditcore.evidence e
            LEFT JOIN auditcore.journey_document_requirements r
              ON r.tenant_id=e.tenant_id AND r.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.document_type_key = ANY(:types)
            ORDER BY e.linked_at_utc, e.evidence_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "heading": HEADING_FIELD, "total": TOTAL_FIELD,
         "types": sorted(invoice_document_types() | {FORM_21_DI_TYPE})},
    ).mappings().all()
    return [
        Copy(
            evidence_id=str(r["evidence_id"]), di_document_id=str(r["di_document_id"]),
            requirement_id=str(r["journey_document_requirement_id"]) if r["journey_document_requirement_id"] else None,
            requirement_key=r["requirement_key"], document_type_key=str(r["document_type_key"]),
            status=str(r["association_status"]), void_reason=r["void_reason"],
            supersedes=str(r["supersedes_evidence_id"]) if r["supersedes_evidence_id"] else None,
            linked_at=r["linked_at_utc"], read=bool(r["read"]), merged_page=bool(r["merged_page"]),
            heading=r["heading"], process_area=r["process_area"], grand_total=r["grand_total"],
        )
        for r in rows
    ]


def _restore(connection: Connection, *, tenant_id: str, journey_id: UUID, copy: Copy, tag: str | None) -> bool:
    restored = connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='ACTIVE', void_reason=NULL, voided_by_actor_id=NULL, voided_at_utc=NULL
            WHERE tenant_id=:t AND journey_id=:j AND evidence_id=:e AND association_status='SUPERSEDED'
            RETURNING evidence_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "e": UUID(copy.evidence_id)},
    ).scalar_one_or_none()
    if restored is None:
        return False
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents SET capture_status='CLASSIFIED', updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND capture_status='SUPERSEDED'
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": UUID(copy.di_document_id)},
    )
    from audit_core.uc03_p2_runtime import record_activity

    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type=BACK_EVENT,
        subject_type="DOCUMENT", subject_id=copy.di_document_id,
        details={"evidenceId": copy.evidence_id,
                 "documentType": FORM_21_DI_TYPE if copy.form_21 else copy.document_type_key,
                 "reason": ("An invoice had replaced this Form 21." if copy.form_21
                            else "A Form 21 had replaced this document."),
                 "by": ACTOR, **({"tag": tag} if tag else {})},
    )
    return True


def _repoint_assessments(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    """The requirement's "current document" pointer (journey_document_assessments)
    names the newest active copy of that requirement, not a Form 21 moved out."""
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_document_assessments a
            SET evidence_id = (SELECT e2.evidence_id FROM auditcore.evidence e2
                               WHERE e2.tenant_id=a.tenant_id AND e2.journey_id=a.journey_id
                                 AND e2.journey_document_requirement_id=a.journey_document_requirement_id
                                 AND e2.association_status='ACTIVE'
                               ORDER BY e2.linked_at_utc DESC, e2.evidence_id DESC LIMIT 1),
                version_no = a.version_no + 1, updated_at_utc = now()
            WHERE a.tenant_id=:t AND a.journey_id=:j AND a.evidence_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM auditcore.evidence e
                          WHERE e.tenant_id=a.tenant_id AND e.evidence_id=a.evidence_id
                            AND e.journey_document_requirement_id IS DISTINCT FROM a.journey_document_requirement_id)
            """
        ),
        {"t": tenant_id, "j": journey_id},
    )


def _describe(plan: LineagePlan) -> dict[str, Any]:
    def name(c: Copy) -> str:
        return FORM_21_DI_TYPE if c.form_21 else c.document_type_key

    return {
        "refile": [{"documentId": c.di_document_id, "readAs": c.document_type_key, "status": c.status,
                    "heading": c.heading} for c in plan.refile],
        "restore": [{"documentId": c.di_document_id, "documentType": name(c)} for c in plan.restore],
        "supersede": [{"documentId": old.di_document_id, "by": new.di_document_id} for old, new in plan.supersede],
        "relink": len(plan.relink),
        "left": [{"documentId": c.di_document_id, "documentType": c.document_type_key, "why": why}
                 for c, why in plan.left],
    }


def repair_journey(connection: Connection, *, tenant_id: str, journey_id: UUID, commit: bool,
                   recompute: bool = True, tag: str | None = None) -> dict[str, Any]:
    """One booking: what ``plan_lineage`` finds and, with ``commit``, done --
    then (``recompute``) the stages it touched re-materialised and the
    booking's recompute requested, as the other copy repairs do
    (uc03_accessory_invoice_repair, uc03_p2_copy_restore)."""
    plan = plan_lineage(load_copies(connection, tenant_id=tenant_id, journey_id=journey_id))
    item = _describe(plan)
    if not commit or plan.empty():
        return item
    for c in plan.refile:
        refile_evidence(connection, tenant_id=tenant_id, journey_id=journey_id, evidence_id=UUID(c.evidence_id),
                        heading=c.heading)
    for c, new_pred in plan.relink:
        connection.execute(
            text("UPDATE auditcore.evidence SET supersedes_evidence_id=:p WHERE tenant_id=:t AND evidence_id=:e"),
            {"t": tenant_id, "e": UUID(c.evidence_id), "p": UUID(new_pred) if new_pred else None},
        )
    for old, new in plan.supersede:
        _supersede(connection, tenant_id=tenant_id, journey_id=journey_id, evidence_id=UUID(old.evidence_id),
                   actor_id=ACTOR, by_evidence_id=UUID(new.evidence_id))
    for c in plan.restore:
        _restore(connection, tenant_id=tenant_id, journey_id=journey_id, copy=c, tag=tag)
    _repoint_assessments(connection, tenant_id=tenant_id, journey_id=journey_id)
    if recompute:
        from audit_core.uc03_p2_runtime import note_facts_changed
        from audit_core.uc03_p2_worker import _rematerialize_stage

        touched = [*plan.refile, *plan.restore, *(old for old, _ in plan.supersede)]
        for stage in sorted({str(c.process_area or "DELIVERY").upper() for c in touched}):
            _rematerialize_stage(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage)
        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason=JOB,
                           correlation_id=f"{JOB.lower()}:{tag}" if tag else None)
    return item


# ------------------------------------------------------------- one-off job

_SAMPLES = 25


def _journeys(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Bookings holding an invoice-typed copy whose heading may name a Form 21
    (a loose SQL filter; ``is_form_21`` decides)."""
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT DISTINCT e.journey_id, j.journey_reference
            FROM auditcore.evidence e
            JOIN auditcore.journeys j ON j.tenant_id=e.tenant_id AND j.journey_id=e.journey_id
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.document_type_key = ANY(:types)
              AND f.field_key=:heading AND f.effective_value #>> '{}' ~* '(form|certificate)'
            ORDER BY j.journey_reference
            """
        ),
        {"t": tenant_id, "types": sorted(invoice_document_types()), "heading": HEADING_FIELD},
    ).mappings().all()]


def run_form_21_refile(engine: Engine, tag: str, tenant_ids: list[str]) -> dict[str, Any]:
    """Every active Project: each booking's Form 21s filed as Form 21s and
    what they set aside given back (``repair_journey``). A DRY RUN unless the
    tag ends with "-commit"; with it each booking changed is re-materialised
    and recomputed. One transaction per booking; idempotent (a second run
    finds nothing). Returns the counts and sample references."""
    commit = tag.endswith("-commit")
    summary: dict[str, Any] = {"job": JOB, "tag": tag, "commit": commit, "tenants": {}}
    for tenant_id in tenant_ids:
        totals: dict[str, Any] = {
            "journeys": 0, "refiled": 0, "refiledActive": 0, "invoicesBack": 0, "form21Back": 0,
            "form21Superseded": 0, "relinked": 0, "invoiceLeft": 0, "failed": 0, "samples": [], "left": [],
            "failures": [],
        }
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = _journeys(connection, tenant_id)
        except Exception:
            logger.warning("form_21_refile_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            totals["failed"] += 1
            summary["tenants"][tenant_id] = totals
            continue
        for journey in journeys:
            journey_id = UUID(str(journey["journey_id"]))
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    item = repair_journey(connection, tenant_id=tenant_id, journey_id=journey_id, commit=commit,
                                          tag=tag)
            except Exception:
                totals["failed"] += 1
                totals["failures"].append(journey["journey_reference"])
                logger.warning("form_21_refile_item_failed", tag=tag, tenant_id=tenant_id,
                               reference=journey["journey_reference"], exc_info=True)
                continue
            if not (item["refile"] or item["restore"] or item["supersede"] or item["relink"] or item["left"]):
                continue
            totals["journeys"] += 1
            totals["refiled"] += len(item["refile"])
            totals["refiledActive"] += sum(1 for c in item["refile"] if c["status"] == "ACTIVE")
            totals["invoicesBack"] += sum(1 for c in item["restore"] if c["documentType"] != FORM_21_DI_TYPE)
            totals["form21Back"] += sum(1 for c in item["restore"] if c["documentType"] == FORM_21_DI_TYPE)
            totals["form21Superseded"] += len(item["supersede"])
            totals["relinked"] += item["relink"]
            totals["invoiceLeft"] += len(item["left"])
            if item["left"] and len(totals["left"]) < _SAMPLES:
                totals["left"].append({"reference": journey["journey_reference"],
                                       "why": [c["why"] for c in item["left"]]})
            if len(totals["samples"]) < _SAMPLES and (item["restore"] or any(
                    c["status"] == "ACTIVE" for c in item["refile"])):
                totals["samples"].append({
                    "reference": journey["journey_reference"],
                    "refiled": [c["readAs"] + ":" + c["status"] for c in item["refile"]],
                    "back": [c["documentType"] for c in item["restore"]],
                })
            logger.info("form_21_refile_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        reference=journey["journey_reference"], **item)
        logger.info("form_21_refile_finished", tag=tag, tenant_id=tenant_id, commit=commit,
                    **{k: v for k, v in totals.items() if k not in ("samples", "left", "failures")})
        summary["tenants"][tenant_id] = totals
    return summary
