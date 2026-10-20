"""The customer's name, from the KYC documents.

A Phase 2 Journey starts without a name: the customer record carries the
Journey id as its entered name, the placeholder the database recognises
(migration 0058). Once a PAN card or Aadhaar is read, its name becomes the
customer's legal name (VERIFIED), and the database replaces the
placeholder entered name with it.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_names import same_person

_MACHINE_ACTOR = "p2-worker"
_KYC_NAME_FIELDS = {"pan_card": "pan_name", "aadhaar": "aadhaar_name", "customer_kyc": "customer_name"}
_KYC_PRECEDENCE = ("pan_card", "aadhaar", "customer_kyc")


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
            SELECT c.customer_id, c.legal_name, c.legal_name_status,
                   COALESCE(e.association_status = 'ACTIVE', false) AS source_on_file
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
    for row in connection.execute(
        text(
            """
            SELECT e.evidence_id, e.document_type_key, f.field_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
              AND e.document_type_key = ANY(:types)
            ORDER BY e.linked_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": sorted(_KYC_NAME_FIELDS)},
    ).mappings().all():
        value = row["effective_value"]
        name = " ".join(str(value).split()) if isinstance(value, str) else ""
        doc_type = str(row["document_type_key"])
        if _KYC_NAME_FIELDS.get(doc_type) == str(row["field_key"]) and name and doc_type not in best:
            best[doc_type] = {"name": name, "evidence": row["evidence_id"]}
    kyc = next((best[t] for t in _KYC_PRECEDENCE if t in best), None)
    if kyc is None:
        return "NO_NAME"
    current, status = customer["legal_name"], customer["legal_name_status"]
    if status == "VERIFIED" and current and (
        current == kyc["name"] or (same_person(current, str(kyc["name"])) and customer["source_on_file"])
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
