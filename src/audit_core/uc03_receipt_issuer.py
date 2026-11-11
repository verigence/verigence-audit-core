"""A receipt is a payment only when the dealership issued it (product owner 2026-11-11).

"This is the insurance payment receipt for self insurance case, but this payment
receipt is coming under payment receipt which is not correct.. payment receipt
only in the name of dealership or outlet should be there."

A money receipt on file was a payment whoever issued it: Document Intelligence
types every money receipt ``dealer_receipt`` / ``payment_receipt`` and reads its
issuer as ``dealer_name``, and the booking's payments were built from all of
them. Live on Mahindra Orissa, 8 of 3,478 receipts counted as payments were not
the dealership's: 5 insurers' premium receipts (Rs 2,03,741 on 5 bookings, the
customer's own policy paid to National Insurance), 2 from the Visakhapatnam
Area Depot (Rs 28,47,500) and 1 from another dealership (Rs 21,000). Each
overstated what the dealership received.

So, before a booking is rebuilt from its documents, a receipt whose issuer was
read and does not name the booking's dealership is set aside: the page stays on
file as a supporting document ("Receipt from <issuer>"), its document leaves the
booking (SUPERSEDED, never deleted), and with it its payment -- every reader of
payments counts a receipt only while its document is on the booking.

The issuer names the dealership when it shares one of the dealership's own
words: "Utkal Automobiles Pvt Ltd" is Utkal Mahindra, "Shivnath Group" is
Shivnath Motors. Words every dealer or the manufacturer carries ("Motors",
"Mahindra", "Pvt Ltd") decide nothing: "CDA Mahindra" is not Utkal Mahindra. An
earlier check compared the two names for likeness (0.55) and would have called
"Utkal Automobiles Pvt Ltd" another dealer; it raised a notice in the previous
flow only and never ran here.

The page is typed by the rule (``type_overridden_by_actor_id``), so the PC is not
asked to "Set the document type" of it (uc03_p2_task_producer raises that task
for a supporting page nobody typed).

Left as they are: a receipt with no issuer read, a dealership whose name has no
word of its own, and a page whose type a person set by hand. A PC who finds a
receipt set aside wrongly (the issuer misread) sets the page's type back to a
receipt; it then stays.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

RECEIPT_TYPES: tuple[str, ...] = ("dealer_receipt", "payment_receipt")
ISSUER_FIELD = "dealer_name"
SUPPORTING = "supporting_document"
ACTOR = "SYSTEM:RECEIPT_ISSUER"

# Words a dealership's name shares with every other dealership, the
# manufacturer, or a company's legal form: they identify nobody.
_GENERIC_WORDS = frozenset({
    "motors", "motor", "automobiles", "automobile", "automotive", "automotives", "auto", "autos", "autocare",
    "cars", "car", "vehicles", "vehicle", "wheels", "sales", "service", "services", "showroom", "dealer",
    "dealers", "dealership", "agency", "agencies", "enterprise", "enterprises", "traders", "trading",
    "pvt", "private", "ltd", "limited", "llp", "company", "corporation", "corp", "inc", "group", "india",
    "and", "the", "for", "new", "branch", "unit", "division",
    "mahindra", "tata", "maruti", "suzuki", "hyundai", "honda", "toyota", "kia", "renault", "nissan",
    "skoda", "volkswagen", "ford", "jeep", "citroen", "morris", "garages",
})
_WORD = re.compile(r"[a-z]+")
# A dealership's own word read with a letter wrong ("Shivnth") is still its word.
_SAME_WORD = 0.85


def _words(name: Any) -> list[str]:
    return [w for w in _WORD.findall(str(name or "").lower()) if len(w) >= 3]


def own_words(names: list[Any]) -> set[str]:
    """The words that are the dealership's own, from its names on the master."""
    return {w for name in names for w in _words(name) if w not in _GENERIC_WORDS}


def issued_by_dealership(issuer: Any, dealership_names: list[Any]) -> bool | None:
    """True / False when the issuer read on a receipt does / does not name the
    dealership; None when that cannot be told (no issuer read, or the
    dealership's name has no word of its own)."""
    issuer_words = _words(issuer)
    own = own_words(dealership_names)
    if not issuer_words or not own:
        return None
    for word in issuer_words:
        if word in own or any(SequenceMatcher(None, word, mine).ratio() >= _SAME_WORD for mine in own):
            return True
    return False


def _dealership_names(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[Any]:
    row = connection.execute(
        text(
            """
            SELECT d.dealer_name, d.legal_name
            FROM auditcore.journeys j
            JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
            WHERE j.tenant_id = :t AND j.journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    return [v for v in (row["dealer_name"], row["legal_name"]) if v] if row is not None else []


def _receipts(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Each receipt on the booking with the issuer read on it and its page."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (e.di_document_id)
                   e.di_document_id, e.document_type_key, f.effective_value #>> '{}' AS issuer,
                   q.queue_id, q.type_overridden_by_actor_id
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
             AND f.di_document_id = e.di_document_id AND f.field_key = :field
            LEFT JOIN auditcore.p2_document_queue q
              ON q.tenant_id = e.tenant_id AND q.journey_id = e.journey_id
             AND q.di_document_id = e.di_document_id AND q.retired_at_utc IS NULL
             AND q.queue_status NOT IN ('MERGED', 'CANCELLED')
            WHERE e.tenant_id = :t AND e.journey_id = :j AND e.association_status = 'ACTIVE'
              AND e.document_type_key = ANY(:types)
            ORDER BY e.di_document_id, f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "field": ISSUER_FIELD, "types": list(RECEIPT_TYPES)},
    ).mappings().all()
    return [dict(row) for row in rows]


def _set_by_a_person(actor: Any) -> bool:
    return bool(actor) and not str(actor).upper().startswith("SYSTEM")


# p2_document_queue.display_name is varchar(120). The first release cut the name
# at 200: a depot's receipt, whose issuer was read with its address (125
# characters), failed the update and stayed a payment.
_NAME_MAX = 120


def receipt_name(issuer: Any) -> str:
    return ("Receipt from " + " ".join(str(issuer or "").split()).title())[:_NAME_MAX].rstrip(" ,(")


def _mark_pages_as_decided(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    """A page this rule set aside is typed: what it is has been decided, so the
    PC is not asked to "Set the document type" of it. The first receipts set
    aside (11 Nov, 8 pages) were left untyped and each raised that task; this
    types them on the booking's next recompute, which closes the task."""
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue q
            SET type_overridden_by_actor_id = :actor, updated_at_utc = now()
            WHERE q.tenant_id = :t AND q.journey_id = :j AND q.queue_status = 'SUPPORTING'
              AND q.type_overridden_by_actor_id IS NULL AND q.di_document_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM auditcore.evidence e
                  WHERE e.tenant_id = q.tenant_id AND e.journey_id = q.journey_id
                    AND e.di_document_id = q.di_document_id AND e.voided_by_actor_id = :actor)
            """
        ),
        {"t": tenant_id, "j": journey_id, "actor": ACTOR},
    )


def set_aside_receipts_not_from_the_dealership(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Take every receipt another party issued out of the booking's payments,
    keeping its page on file as a supporting document. Returns what was set
    aside. Idempotent: a receipt set aside is no longer on the booking."""
    from audit_core.uc03_p2_runtime import record_activity

    _mark_pages_as_decided(connection, tenant_id=tenant_id, journey_id=journey_id)
    names = _dealership_names(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not own_words(names):
        return []
    out: list[dict[str, Any]] = []
    for receipt in _receipts(connection, tenant_id=tenant_id, journey_id=journey_id):
        if _set_by_a_person(receipt["type_overridden_by_actor_id"]):
            continue
        if issued_by_dealership(receipt["issuer"], names) is not False:
            continue
        name = receipt_name(receipt["issuer"])
        why = (f"Kept as a supporting document ({name}): issued by {receipt['issuer']}, not by {names[0]}. "
               "Only the dealership's own receipt is a payment.")
        if receipt["queue_id"] is not None:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status = 'SUPPORTING', template_key = :k, display_name = :n, status_reason = :why,
                        type_overridden_by_actor_id = :actor, updated_at_utc = now()
                    WHERE tenant_id = :t AND queue_id = :q
                    """
                ),
                {"t": tenant_id, "q": receipt["queue_id"], "k": SUPPORTING, "n": name, "why": why, "actor": ACTOR},
            )
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence
                SET association_status = 'SUPERSEDED', void_reason = :why, voided_by_actor_id = :actor,
                    voided_at_utc = now()
                WHERE tenant_id = :t AND journey_id = :j AND di_document_id = :d AND association_status = 'ACTIVE'
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": receipt["di_document_id"], "actor": ACTOR, "why": why},
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PAGE_KEPT_AS_OTHER",
            subject_type="DOCUMENT_PAGE", subject_id=str(receipt["queue_id"] or receipt["di_document_id"]),
            details={"templateKey": SUPPORTING, "name": name, "by": "system:receipt-issuer",
                     "issuer": receipt["issuer"], "dealership": names[0],
                     "was": receipt["document_type_key"]},
            correlation_id=correlation_id,
        )
        out.append({"diDocumentId": str(receipt["di_document_id"]), "issuer": receipt["issuer"], "name": name})
    return out
