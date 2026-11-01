"""The customer's name, from the KYC documents.

A Phase 2 Journey starts without a name: the customer record carries the
Journey id as its entered name, the placeholder the database recognises
(migration 0058). Once a PAN card or Aadhaar is read, its name becomes the
customer's legal name (VERIFIED), and the database replaces the
placeholder entered name with it.

Product owner 2026-10-27: when a GST certificate with a GSTIN is on file, the
customer's name is the GSTIN's legal name -- for those customers the PAN is
often not fully readable (masked) and Aadhaar is optional and personal. Only
when a GST certificate carrying a GSTIN is uploaded; otherwise the KYC rule
stands.

Product owner 2026-10-28: a PAN read with its letters masked ("PXXXXJ KXXXR
NXXXX") does not name the customer when an Aadhaar (or Customer KYC) gives the
full name; with a GSTIN on file the GSTIN still names them. A masked name is
used only when no KYC on file gives a full one.

Product owner 2026-11-01: with only a masked PAN on file (no GSTIN, no
Aadhaar naming them), the customer is named from the booking's own booking
form, invoice, receipt or insurance cover -- only when that name fits the
masked letters exactly (ADI-RD-261026-031 "SXXXXXXA SXXXO" is the booking
form's Sushanta Sahoo); otherwise the masked name stays.

Product owner 2026-11-01: a name read from another customer's file does not
stay once that file is taken out of the booking (uc03_p2_file_identity). The
customer's name is cleared until a KYC of the booking's own names them
(ADI-RD-261024-005 kept Bhagabata Mohanty's masked PAN name on Gurupada
Mandal's booking), and the entered name -- set once from the first name read,
so another customer's on a booking their file was dropped into -- follows the
booking's own customer (follow_entered_name).
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_names import (
    is_masked,
    masked_matches,
    person_tokens,
    same_person,
)

_MACHINE_ACTOR = "p2-worker"
_KYC_NAME_FIELDS = {"pan_card": ("pan_name",), "aadhaar": ("aadhaar_name",), "customer_kyc": ("customer_name",),
                    "gst_certificate": ("organisation_legal_name", "legal_name")}
_KYC_PRECEDENCE = ("gst_certificate", "pan_card", "aadhaar", "customer_kyc")
_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
# Where the booking's own documents name the customer, in the order they are
# trusted to unmask a masked PAN name (2026-11-01).
_NAMING_DOCUMENTS = ("booking_docket", "booking_form", "customer_invoice_dms", "tax_invoice_tally",
                     "dealer_receipt", "payment_receipt", "insurance_cover", "cost_sheet")
_NAMING_FIELDS = ("customer_name", "buyer_name", "insured_name")
# Lets follow_entered_name change an entered name (migration 0185).
_ENTERED_NAME_SETTING = "auditcore.entered_name_follows_customer"


def is_gstin(value: object) -> bool:
    """A GSTIN as printed: 15 characters, state code, PAN, entity, Z, check."""
    return bool(_GSTIN.match(re.sub(r"\s", "", str(value or "")).upper()))


def sync_customer_name(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    """Name the Journey's customer from the KYC on file: the PAN card, else
    the Aadhaar, else the Customer KYC (the newest of that kind).

    Product owner 2026-10-20: the KYC's name is the customer's name, and two
    Journeys holding the same PAN must show the same customer. The name
    therefore always follows the KYC documents still on file -- a name taken
    from a KYC page since replaced or removed (UTK-SU-261020-001 kept
    "Brajesh Chandra Bhoi" from a superseded Aadhaar while its PAN and
    Aadhaar on file are Sarbeswar Pradhan's) is replaced. A verified name
    that is the same person as the KYC (a corrected spelling) stands.

    Returns VERIFIED when the name was set, UNCHANGED when it already
    stood, NO_NAME when no KYC on file names the customer."""
    customer = connection.execute(
        text(
            """
            SELECT c.customer_id, c.legal_name, c.legal_name_status, c.legal_name_source_evidence_id,
                   COALESCE(e.association_status = 'ACTIVE', false) AS source_on_file,
                   e.association_status AS source_status
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=c.tenant_id AND e.evidence_id=c.legal_name_source_evidence_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            FOR UPDATE OF c
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    if customer is None:
        return "NO_NAME"
    best: dict[str, dict[str, object]] = {}
    rows = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.document_type_key, f.field_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
              AND e.document_type_key = ANY(:types)
            ORDER BY e.linked_at_utc DESC, f.source_fact_version DESC NULLS LAST, f.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": sorted(_KYC_NAME_FIELDS)},
    ).mappings().all()
    # A GST certificate names the customer only when it carries a GSTIN.
    with_gstin = {r["evidence_id"] for r in rows if r["field_key"] == "gstin" and is_gstin(r["effective_value"])}
    for row in rows:
        value = row["effective_value"]
        name = " ".join(str(value).split()) if isinstance(value, str) else ""
        doc_type = str(row["document_type_key"])
        if doc_type == "gst_certificate" and row["evidence_id"] not in with_gstin:
            continue
        if str(row["field_key"]) in _KYC_NAME_FIELDS.get(doc_type, ()) and name and doc_type not in best:
            best[doc_type] = {"name": name, "evidence": row["evidence_id"]}
    kyc = next((best[t] for t in _KYC_PRECEDENCE if t in best and not is_masked(best[t]["name"])), None) or next(
        (best[t] for t in _KYC_PRECEDENCE if t in best), None)
    if kyc is None:
        if customer["source_status"] == "FOREIGN" and customer["legal_name"]:
            return _clear_foreign_name(connection, tenant_id=tenant_id, journey_id=journey_id, customer=customer)
        return "NO_NAME"
    from_gst = kyc is best.get("gst_certificate")
    if is_masked(kyc["name"]):
        # Only a masked PAN names them: the booking's own documents, when a
        # name there fits the masked letters exactly (2026-11-01).
        full = _full_name_fitting(connection, tenant_id=tenant_id, journey_id=journey_id, masked=str(kyc["name"]))
        if full is not None:
            kyc = {"name": full, "evidence": kyc["evidence"]}
    current, status = customer["legal_name"], customer["legal_name_status"]
    if status == "VERIFIED" and current and (
        current == kyc["name"] or (same_person(current, str(kyc["name"])) and customer["source_on_file"]
                                   and (not from_gst
                                        or customer["legal_name_source_evidence_id"] == kyc["evidence"]))
    ):
        return "UNCHANGED"
    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET legal_name=:name, legal_name_status='VERIFIED', legal_name_source_evidence_id=:evidence,
                legal_name_verified_by_actor_id=:actor, legal_name_verified_at_utc=now(),
                updated_by_actor_id=:actor, updated_at_utc=now(), version_no=version_no+1
            WHERE tenant_id=:t AND customer_id=:c
            """
        ),
        {"t": tenant_id, "c": customer["customer_id"], "name": kyc["name"], "evidence": kyc["evidence"],
         "actor": _MACHINE_ACTOR},
    )
    return "VERIFIED"


def _full_name_fitting(connection: Connection, *, tenant_id: str, journey_id: UUID, masked: str) -> str | None:
    """The customer's full name from the booking's own documents (booking
    form first, then invoices, receipts, insurance, cost sheet; the newest
    of each), when it fits the masked name letter for letter: the same
    words, each as long, every letter shown the same. Honorifics and a
    receipt's appended PAN or number are not part of the name."""
    rows = connection.execute(
        text(
            """
            SELECT e.document_type_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
              AND e.document_type_key = ANY(:types) AND f.field_key = ANY(:fields)
            ORDER BY e.linked_at_utc DESC, f.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(_NAMING_DOCUMENTS), "fields": list(_NAMING_FIELDS)},
    ).mappings().all()
    for doc_type in _NAMING_DOCUMENTS:
        for row in rows:
            if row["document_type_key"] != doc_type or not isinstance(row["effective_value"], str):
                continue
            name = " ".join(person_tokens(row["effective_value"]))
            if name and not is_masked(name) and masked_matches(masked, name):
                return name
    return None


def _clear_foreign_name(connection: Connection, *, tenant_id: str, journey_id: UUID, customer: Any) -> str:
    """The name was read from another customer's file, since taken out, and
    no KYC of the booking's own names the customer: the name goes, until one
    does (2026-11-01)."""
    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET legal_name=NULL, legal_name_status='PENDING', legal_name_source_evidence_id=NULL,
                legal_name_verified_by_actor_id=NULL, legal_name_verified_at_utc=NULL,
                updated_by_actor_id=:actor, updated_at_utc=now(), version_no=version_no+1
            WHERE tenant_id=:t AND customer_id=:c
            """
        ),
        {"t": tenant_id, "c": customer["customer_id"], "actor": _MACHINE_ACTOR},
    )
    from audit_core.uc03_p2_runtime import record_activity

    record_activity(connection, tenant_id=tenant_id, journey_id=journey_id, event_type="CUSTOMER_NAME_CLEARED",
                    subject_type="JOURNEY", subject_id=str(journey_id),
                    details={"name": customer["legal_name"], "by": _MACHINE_ACTOR,
                             "reason": "Read from another customer's file, taken out of this booking."})
    return "CLEARED"


def follow_entered_name(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str | None:
    """The entered name (customers.display_name) follows the booking's own
    customer when it is a person of a file taken out of the booking
    (2026-11-01). It is set once, from the first name read (migration 0058),
    so on a booking another customer's file was dropped into it is that
    customer's ("CHINMAYA SENAPATI" on Bikash Kumar Martha's
    ADI-RD-261022-018), and search and the reports show it. The booking's own
    customer is the name verified from its KYC, else the customer its own
    files are about (the file identity's decision). Returns the new entered
    name, or None when it stays.

    Also (2026-11-01): an entered name set from a masked PAN ("KXXXXXI RXXT")
    follows the full name the booking now has for the same customer, when the
    masked letters fit it ("KALANDI ROUT")."""
    row = connection.execute(
        text(
            """
            SELECT c.customer_id, c.display_name, c.legal_name, c.legal_name_status,
                   fi.customer_name AS decided, j.journey_id::text AS placeholder
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.p2_journey_file_identity fi ON fi.tenant_id=j.tenant_id AND fi.journey_id=j.journey_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    if row is None or not row["display_name"] or row["display_name"] == row["placeholder"]:
        return None
    legal = row["legal_name"] if row["legal_name_status"] == "VERIFIED" else None
    # The decided name as its document printed it, less an honorific or an
    # appended number ("Mr. Gurupada Mandal", "GURUPADA MANDAL-EPGPM0671J").
    customer = " ".join(str(legal).split()) if legal and not is_masked(legal) else (
        " ".join(person_tokens(row["decided"])) or None if row["decided"] else None)
    entered = str(row["display_name"])
    if not customer or is_masked(customer) or same_person(entered, customer):
        return None
    masked_self = is_masked(entered) and masked_matches(entered, customer)
    if not masked_self:
        people = [p for p in connection.execute(
            text(
                """
                SELECT identity_person_name FROM auditcore.p2_upload_batches
                WHERE tenant_id=:t AND journey_id=:j AND identity_status='FOREIGN'
                  AND identity_enforced_at_utc IS NOT NULL AND identity_person_name IS NOT NULL
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalars().all() if str(p).strip()]
        if not any(same_person(entered, person) for person in people):
            return None  # not a name from a file taken out: as the PC or the first KYC left it
    connection.execute(text("SELECT set_config(:k, 'on', true)"), {"k": _ENTERED_NAME_SETTING})
    try:
        changed = connection.execute(
            text(
                """
                UPDATE auditcore.customers SET display_name=:name, updated_by_actor_id=:actor, updated_at_utc=now(),
                       version_no=version_no+1
                WHERE tenant_id=:t AND customer_id=:c AND display_name=:entered
                RETURNING display_name
                """
            ),
            {"t": tenant_id, "c": row["customer_id"], "name": customer, "entered": entered, "actor": _MACHINE_ACTOR},
        ).scalar_one_or_none()
    finally:
        connection.execute(text("SELECT set_config(:k, 'off', true)"), {"k": _ENTERED_NAME_SETTING})
    if changed != customer:
        return None  # the entered-name guard is older than migration 0185: nothing changed
    from audit_core.uc03_p2_runtime import record_activity

    record_activity(connection, tenant_id=tenant_id, journey_id=journey_id, event_type="ENTERED_NAME_FOLLOWS_CUSTOMER",
                    subject_type="JOURNEY", subject_id=str(journey_id),
                    details={"from": entered, "to": customer, "by": _MACHINE_ACTOR,
                             "reason": ("The entered name was the masked PAN name of the same customer."
                                        if masked_self else
                                        "The entered name was another customer's, whose file was taken out.")})
    return customer
