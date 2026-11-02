"""A Form 21 is never an invoice (product owner, 2026-11-02).

Live: 57 bookings held a Form 21 (RTO sale certificate) ACTIVE as their
customer DMS (or wholesale) invoice -- DI has no Form 21 type and reads one as
the invoice it looks like -- and on 16 the real invoice had been set aside by
it (REPLACED_BY_NEWER_UPLOAD)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_form_21 as form_21
from audit_core.db import set_tenant_context
from audit_core.di_client import DiFact
from audit_core.uc03_form_21 import Copy, filed_document_type, is_form_21, plan_lineage

# ------------------------------------------------------- the classification


@pytest.mark.parametrize("heading", [
    "FORM 21 SALES CERTIFICATE",
    "FORM 21 {See Rule 47(a) and (d)} SALES CERTIFICATE",
    "FORM 21\n{See Rule 47(a) and (d)}\nSALES CERTIFICATE",
    "FORM 21 [ See Rule 47(a) and (d) ] Sale Certificate",
    "Form 21",
    "FORM-21",
    "Form No. 21",
    "SALES CERTIFICATE",
    "Sale Certificate",
])
def test_a_heading_naming_form_21_or_the_sale_certificate_is_a_form_21(heading):
    assert is_form_21({"invoice_heading_as_printed": heading})


@pytest.mark.parametrize("heading", [
    "TAX INVOICE", "GST INVOICE", "PROFORMA INVOICE", "FREE OF COST APPROVAL FORMAT", "CERTIFICATE",
    "Form 22\n[See Rules 47(1)(g)]\nROAD WORTHINESS CERTIFICATE", "Form 210", "", None,
])
def test_any_other_heading_is_not(heading):
    assert not is_form_21({"invoice_heading_as_printed": heading})


def test_the_heading_is_read_off_di_facts_stored_rows_or_a_mapping():
    fact = DiFact(canonical_field_id="cf", field_key="invoice_heading_as_printed", value="FORM 21",
                  value_source="EXTRACTED", confidence_score=97.0, version_no=1)
    assert is_form_21([fact])
    assert is_form_21([{"field_key": "invoice_heading_as_printed", "effective_value": "Sale Certificate"}])
    assert not is_form_21([{"field_key": "invoice_number", "effective_value": "FORM 21"}])


def test_a_reading_that_prints_a_grand_total_stays_an_invoice():
    # An invoice page grouped with the Form 21 into one document is read with
    # the invoice's total: it is never taken out of the Deal.
    assert not is_form_21({"invoice_heading_as_printed": "FORM 21 SALES CERTIFICATE",
                           "grand_total_amount": "1185000.00"})
    assert is_form_21({"invoice_heading_as_printed": "FORM 21 SALES CERTIFICATE", "grand_total_amount": None})


def test_only_an_invoice_typed_reading_is_filed_as_form_21():
    heading = {"invoice_heading_as_printed": "FORM 21 SALES CERTIFICATE"}
    assert filed_document_type("customer_invoice_dms", heading) == "form_21"
    assert filed_document_type("wholesale_invoice", heading) == "form_21"
    assert filed_document_type("customer_invoice_dms", {"invoice_heading_as_printed": "TAX INVOICE"}) \
        == "customer_invoice_dms"
    assert filed_document_type("pan_card", heading) == "pan_card"
    assert filed_document_type("form_21", {}) == "form_21"  # DI's own type, once DI has it


def test_the_form_21_template_is_optional_and_no_money_source():
    from audit_core.uc03_p2_registry import get_registry

    registry = get_registry()
    template = registry.documents["form_21"]
    assert template.display_name == "Form 21 (Sale Certificate)"
    assert (template.stage, template.requirement, template.di_types) == ("DELIVERY", "OPTIONAL", ("form_21",))
    assert template.controls == () and template.persistence["canonical"] == []
    assert registry.template_for_di_type("form_21").key == "form_21"
    assert "form_21" in registry.candidate_di_types()


def test_invoice_typed_slots_are_decided_when_the_copy_is_read():
    assert form_21.decided_by_reading("customer_invoice_dms")
    assert form_21.decided_by_reading("wholesale_invoice")
    assert form_21.decided_by_reading("form_21")
    assert not form_21.decided_by_reading("pan_card")
    assert not form_21.decided_by_reading("booking_form")


def test_a_link_never_supersedes_an_invoice_typed_copy_before_it_is_read():
    import inspect

    from audit_core.uc03_pc_booking_documents import acknowledge_booking_document_link

    source = inspect.getsource(acknowledge_booking_document_link)
    deferred = source.index('decided_by_reading(requirement["document_type_key"])')
    assert source.index("if not repeatable:") < deferred < source.index("SET association_status='SUPERSEDED'")


# ------------------------------------------------------------- the plan


def _copy(name: str, *, kind: str = "customer_invoice_dms", status: str = "ACTIVE", sup: str | None = None,
          at: int = 0, heading: str | None = "TAX INVOICE", read: bool = True, merged: bool = False,
          reason: str | None = None) -> Copy:
    return Copy(evidence_id=name, di_document_id=f"doc-{name}", requirement_id="invoice", requirement_key=kind,
                document_type_key=kind, status=status,
                void_reason=reason or ("REPLACED_BY_NEWER_UPLOAD" if status == "SUPERSEDED" else None),
                supersedes=sup, linked_at=f"2026-10-2{at}", read=read, merged_page=merged, heading=heading)


F21 = "FORM 21 SALES CERTIFICATE"


def test_the_invoice_two_form_21_copies_set_aside_comes_back():
    plan = plan_lineage([
        _copy("inv", status="SUPERSEDED", at=0),
        _copy("f21a", status="SUPERSEDED", sup="inv", at=1, heading=F21),
        _copy("f21b", sup="f21a", at=2, heading=F21),
    ])
    assert [c.evidence_id for c in plan.refile] == ["f21a", "f21b"]
    assert [c.evidence_id for c in plan.restore] == ["inv"]
    assert [(c.evidence_id, new) for c, new in plan.relink] == [("f21a", None)]  # f21b <- f21a stays
    assert plan.supersede == [] and plan.left == []


def test_a_form_21_an_invoice_set_aside_comes_back_and_the_invoice_chain_closes_over_it():
    plan = plan_lineage([
        _copy("inv1", status="SUPERSEDED", at=0),
        _copy("f21", status="SUPERSEDED", sup="inv1", at=1, heading=F21),
        _copy("inv2", sup="f21", at=2),
    ])
    assert [c.evidence_id for c in plan.restore] == ["f21"]  # inv2 stands: the newer invoice
    assert sorted((c.evidence_id, new) for c, new in plan.relink) == [("f21", None), ("inv2", "inv1")]


def test_what_cannot_come_back_by_itself_is_left_and_reported():
    merged = plan_lineage([
        _copy("page", status="SUPERSEDED", at=0, merged=True),
        _copy("f21", sup="page", at=1, heading=F21),
    ])
    assert merged.restore == [] and [(c.evidence_id, why) for c, why in merged.left] == [
        ("page", "a page merged into a document that holds the Form 21 too")]
    removed = plan_lineage([  # a newer invoice the PC removed: his decision stands
        _copy("inv1", status="SUPERSEDED", at=0),
        _copy("f21", status="SUPERSEDED", sup="inv1", at=1, heading=F21),
        _copy("inv2", status="VOIDED", sup="f21", at=2, reason="P2_DOCUMENT_VOIDED"),
    ])
    assert [c.evidence_id for c in removed.restore] == ["f21"] and removed.left == []
    active = plan_lineage([_copy("inv1", status="SUPERSEDED", at=0), _copy("inv2", sup="inv1", at=1)])
    assert active.empty() and active.left == []  # no Form 21: nothing to do


# ------------------------------------------------------- the pipeline (DB)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="f21")
    with engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.di_subject_mappings (tenant_id, customer_id, di_subject_id, di_subject_type, "
                 "mapping_status) VALUES (:t, :c, :s, 'OTHER', 'ACTIVE')"),
            {"t": created.tenant_id, "c": created.customer_id, "s": uuid4()},
        )
        created.invoice_requirement = UUID(str(connection.execute(
            text("INSERT INTO auditcore.journey_document_requirements (tenant_id, journey_id, requirement_key, "
                 "document_type_key, process_area, requirement_level) VALUES (:t, :j, 'customer_invoice_dms', "
                 "'customer_invoice_dms', 'DELIVERY', 'REQUIRED') RETURNING journey_document_requirement_id"),
            {"t": created.tenant_id, "j": created.journey_id},
        ).scalar_one()))
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _fact(key: str, value, version: int = 1) -> DiFact:
    return DiFact(canonical_field_id=f"cf-{key}", field_key=key, value=value, value_source="EXTRACTED",
                  confidence_score=98.0, version_no=version)


_INVOICE = {"invoice_heading_as_printed": "TAX INVOICE", "invoice_number": "INV/26/0042",
            "grand_total_amount": "1185000.00", "chassis_number": "MA1NE2ZTXR2A12345"}
_FORM_21 = {"invoice_heading_as_printed": "FORM 21 {See Rule 47(a) and (d)} SALES CERTIFICATE",
            "invoice_number": "INV/26/0042", "chassis_number": "MA1NE2ZTXR2A12345"}


def _link(journey, document_id: UUID) -> UUID:
    from audit_core.uc03_pc_booking_documents import (
        BookingDocumentLinkCommand,
        acknowledge_booking_document_link,
    )

    with journey.engine.begin() as connection:
        return acknowledge_booking_document_link(
            payload=BookingDocumentLinkCommand(requirementRef=journey.invoice_requirement, documentId=document_id),
            service_principal=SimpleNamespace(subject="di-service"), connection=connection,
        ).evidenceId


def _read(journey, document_id: UUID, fields: dict) -> None:
    """DI's reading of the document copied in: the one per-document sync."""
    from audit_core import uc03_confidence_review_policy as policy

    fetched = policy._FetchedDocument(
        document=SimpleNamespace(confirmation_status="CONFIRMED", document_type_key="customer_invoice_dms",
                                 processing_status="PROCESSED", verification_state="VERIFIED"),
        facts=[_fact(k, v) for k, v in fields.items()],
    )
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        policy._sync_booking_document(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=document_id,
            service_id="p2-worker", security_client=None, di_client=None, bump_version=True,
            stage_code="DELIVERY", prefetched=fetched, facts_only=True,
        )


def _evidence(journey, evidence_id) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return dict(connection.execute(
            text("SELECT e.association_status, e.void_reason, e.document_type_key, e.supersedes_evidence_id, "
                 "r.requirement_key FROM auditcore.evidence e JOIN auditcore.journey_document_requirements r "
                 "ON r.tenant_id=e.tenant_id AND r.journey_document_requirement_id=e.journey_document_requirement_id "
                 "WHERE e.tenant_id=:t AND e.evidence_id=:e"),
            {"t": journey.tenant_id, "e": evidence_id},
        ).mappings().one())


def _fact_types(journey, document_id: UUID) -> set[str]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return set(connection.execute(
            text("SELECT DISTINCT source_document_type_key FROM auditcore.journey_document_extracted_fields "
                 "WHERE tenant_id=:t AND di_document_id=:d"),
            {"t": journey.tenant_id, "d": document_id},
        ).scalars().all())


def test_a_form_21_read_as_an_invoice_is_filed_as_a_form_21_and_replaces_nothing(journey):
    invoice, form, reissued = uuid4(), uuid4(), uuid4()
    invoice_evidence = _link(journey, invoice)
    _read(journey, invoice, _INVOICE)

    form_evidence = _link(journey, form)
    # Linked beside the invoice: what it is is not known before it is read.
    assert _evidence(journey, invoice_evidence)["association_status"] == "ACTIVE"
    assert _evidence(journey, form_evidence)["association_status"] == "ACTIVE"

    _read(journey, form, _FORM_21)
    filed = _evidence(journey, form_evidence)
    assert (filed["association_status"], filed["document_type_key"], filed["requirement_key"]) == (
        "ACTIVE", "form_21", "p2_form_21")
    assert filed["supersedes_evidence_id"] is None
    assert _fact_types(journey, form) == {"form_21"}
    assert _evidence(journey, invoice_evidence)["association_status"] == "ACTIVE"  # the invoice stays
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        [(details,)] = connection.execute(
            text("SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t AND event_type=:e"),
            {"t": journey.tenant_id, "e": form_21.FILED_EVENT}).all()
    assert details["readAs"] == "customer_invoice_dms" and details["evidenceId"] == str(form_evidence)

    # A newer copy of the invoice replaces the invoice -- once it is read --
    # and never the Form 21.
    reissued_evidence = _link(journey, reissued)
    assert _evidence(journey, invoice_evidence)["association_status"] == "ACTIVE"
    _read(journey, reissued, {**_INVOICE, "invoice_number": "INV/26/0043"})
    old = _evidence(journey, invoice_evidence)
    assert (old["association_status"], old["void_reason"]) == ("SUPERSEDED", "REPLACED_BY_NEWER_UPLOAD")
    assert _evidence(journey, reissued_evidence)["supersedes_evidence_id"] == invoice_evidence
    assert _evidence(journey, form_evidence)["association_status"] == "ACTIVE"

    # A later reading of the same Form 21 does not file it back as an invoice.
    _read(journey, form, _FORM_21)
    assert _evidence(journey, form_evidence)["document_type_key"] == "form_21"
    assert _fact_types(journey, form) == {"form_21"}


def test_an_unread_newer_copy_leaves_the_invoice_on_file(journey):
    invoice, unread = uuid4(), uuid4()
    invoice_evidence = _link(journey, invoice)
    _read(journey, invoice, _INVOICE)
    unread_evidence = _link(journey, unread)
    _read(journey, unread, {})  # DI confirmed, read nothing
    assert _evidence(journey, invoice_evidence)["association_status"] == "ACTIVE"
    assert _evidence(journey, unread_evidence)["association_status"] == "ACTIVE"


def test_a_classification_poll_keeps_a_form_21_a_form_21(journey):
    from audit_core.uc03_unified_document_capture import apply_di_classification

    form = uuid4()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.document_capture_v2_documents (tenant_id, journey_id, stage_code, "
                 "di_document_id, client_upload_id, capture_status, created_by_actor_id) "
                 "VALUES (:t, :j, 'DELIVERY', :d, :c, 'CLASSIFIED', :a)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": form, "c": f"cap-{uuid4().hex}",
             "a": journey.actor_id},
        )
    _link(journey, form)
    _read(journey, form, _FORM_21)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        apply_di_classification(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            di_documents=[{"documentId": str(form), "classifiedDocumentTypeKey": "customer_invoice_dms",
                           "state": "CLASSIFIED"}],
            actor_id="SYSTEM", actor_role="SYSTEM", correlation_id="",
        )
        row = connection.execute(
            text("SELECT classified_document_type_key, requirement_key FROM auditcore.document_capture_v2_documents "
                 "WHERE tenant_id=:t AND di_document_id=:d"), {"t": journey.tenant_id, "d": form}).one()
    assert tuple(row) == ("form_21", "p2_form_21")


# ------------------------------------------------------------ the job (DB)


def _legacy_form_21_over_the_invoice(journey) -> dict[str, UUID]:
    """What the old rule left: the real invoice SUPERSEDED by a Form 21 linked
    into the same slot, the Form 21 ACTIVE as an invoice with an invoice row."""
    ids = {"invoice": uuid4(), "form": uuid4()}
    at = datetime.now(UTC) - timedelta(hours=2)
    out: dict[str, UUID] = {}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        batch = uuid4()
        connection.execute(
            text("INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                 "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id) "
                 "VALUES (:t, :b, :j, 'delivery.pdf', 'application/pdf', 100, 2, :k, 'COMPLETED', :a)"),
            {"t": journey.tenant_id, "b": batch, "j": journey.journey_id, "k": f"p2/{batch}", "a": journey.actor_id},
        )
        prior = None
        for page, (name, fields) in enumerate((("invoice", _INVOICE), ("form", _FORM_21)), start=1):
            document_id = ids[name]
            connection.execute(
                text("INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, "
                     "page_sha256, page_object_key, client_upload_id, di_document_id, queue_status, "
                     "classified_document_type, template_key, business_stage) VALUES (:t, :b, :j, :p, :sha, :k, :c, "
                     ":d, 'READY', 'customer_invoice_dms', 'customer_invoice_dms', 'DELIVERY')"),
                {"t": journey.tenant_id, "b": batch, "j": journey.journey_id, "p": page, "sha": uuid4().hex,
                 "k": f"p2/{batch}/{page}", "c": f"p2-{uuid4().hex}", "d": document_id},
            )
            connection.execute(
                text("INSERT INTO auditcore.document_capture_v2_documents (tenant_id, journey_id, stage_code, "
                     "di_document_id, client_upload_id, capture_status, classified_document_type_key, "
                     "requirement_key, created_by_actor_id) VALUES (:t, :j, 'DELIVERY', :d, :c, :s, "
                     "'customer_invoice_dms', 'customer_invoice_dms', :a)"),
                {"t": journey.tenant_id, "j": journey.journey_id, "d": document_id, "c": f"cap-{uuid4().hex}",
                 "s": "SUPERSEDED" if name == "invoice" else "CLASSIFIED", "a": journey.actor_id},
            )
            evidence_id = connection.execute(
                text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, "
                     "journey_document_requirement_id, di_subject_id, di_document_id, document_type_key, "
                     "evidence_purpose, process_area, association_status, void_reason, supersedes_evidence_id, "
                     "linked_at_utc) VALUES (:t, :j, :c, :r, :s, :d, 'customer_invoice_dms', 'DELIVERY_DOCUMENT', "
                     "'DELIVERY', :st, :vr, :prior, :at) RETURNING evidence_id"),
                {"t": journey.tenant_id, "j": journey.journey_id, "c": journey.customer_id,
                 "r": journey.invoice_requirement, "s": uuid4(), "d": document_id, "prior": prior,
                 "st": "SUPERSEDED" if name == "invoice" else "ACTIVE",
                 "vr": "REPLACED_BY_NEWER_UPLOAD" if name == "invoice" else None, "at": at + timedelta(minutes=page)},
            ).scalar_one()
            prior = evidence_id
            out[name] = UUID(str(evidence_id))
            out[f"{name}_document"] = document_id
            for version, (key, value) in enumerate(fields.items(), start=1):
                connection.execute(
                    text("INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, "
                         "di_document_id, source_fact_version, field_key, stage_code, source_canonical_field_id, "
                         "source_document_type_key, extracted_value, effective_value, confidence_score, "
                         "confidence_scale) VALUES (:t, :j, :d, 1, :f, 'DELIVERY', :cf, 'customer_invoice_dms', "
                         "CAST(:v AS jsonb), CAST(:v AS jsonb), 98, 'PERCENT')"),
                    {"t": journey.tenant_id, "j": journey.journey_id, "d": document_id, "f": key,
                     "cf": f"cf-{key}-{version}", "v": json.dumps(value)},
                )
            connection.execute(
                text("INSERT INTO auditcore.invoice_review_values (tenant_id, journey_id, source_di_document_id, "
                     "source_evidence_id, document_type_key, invoice_heading_as_printed, invoice_number, "
                     "grand_total_amount, reviewed_by_actor_id) VALUES (:t, :j, :d, :e, 'customer_invoice_dms', :h, "
                     ":n, :g, 'SYSTEM:DI_AUTO')"),
                {"t": journey.tenant_id, "j": journey.journey_id, "d": document_id, "e": evidence_id,
                 "h": fields["invoice_heading_as_printed"], "n": fields["invoice_number"],
                 "g": fields.get("grand_total_amount")},
            )
    return out


def _snapshot(journey, legacy) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        q = connection.execute(
            text("SELECT di_document_id, template_key FROM auditcore.p2_document_queue WHERE tenant_id=:t"),
            {"t": journey.tenant_id}).all()
        capture = connection.execute(
            text("SELECT di_document_id, capture_status FROM auditcore.document_capture_v2_documents "
                 "WHERE tenant_id=:t"), {"t": journey.tenant_id}).all()
        rows = connection.execute(
            text("SELECT source_di_document_id, document_type_key FROM auditcore.invoice_review_values "
                 "WHERE tenant_id=:t"), {"t": journey.tenant_id}).all()
        recompute = connection.execute(
            text("SELECT work_status FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type='STAGE_RECOMPUTE'"),
            {"t": journey.tenant_id}).scalars().all()
        from audit_core.uc03_invoice_materialization import load_invoice_readings

        readings = [str(r["documentId"]) for r in load_invoice_readings(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)]
    return {
        "invoice": _evidence(journey, legacy["invoice"]), "form": _evidence(journey, legacy["form"]),
        "templates": {str(d): k for d, k in q}, "capture": {str(d): s for d, s in capture},
        "rows": {str(d): k for d, k in rows}, "recompute": recompute, "readings": readings,
    }


def test_the_job_dry_run_lists_and_the_commit_refiles_and_gives_the_invoice_back(journey):
    legacy = _legacy_form_21_over_the_invoice(journey)
    before = _snapshot(journey, legacy)
    assert before["readings"] == [str(legacy["form_document"])]  # the Form 21 is the only invoice read

    dry = form_21.run_form_21_refile(journey.engine, "form-21-2026-11-02", [journey.tenant_id])
    totals = dry["tenants"][journey.tenant_id]
    assert dry["commit"] is False
    assert (totals["journeys"], totals["refiled"], totals["refiledActive"], totals["invoicesBack"],
            totals["form21Back"], totals["failed"]) == (1, 1, 1, 1, 0, 0)
    [sample] = totals["samples"]
    assert sample["refiled"] == ["customer_invoice_dms:ACTIVE"] and sample["back"] == ["customer_invoice_dms"]
    assert _snapshot(journey, legacy) == before  # a dry run changes nothing

    done = form_21.run_form_21_refile(journey.engine, "form-21-2026-11-02-commit", [journey.tenant_id])
    assert done["commit"] is True and done["tenants"][journey.tenant_id]["invoicesBack"] == 1
    after = _snapshot(journey, legacy)
    assert (after["form"]["association_status"], after["form"]["document_type_key"],
            after["form"]["requirement_key"], after["form"]["supersedes_evidence_id"]) == (
        "ACTIVE", "form_21", "p2_form_21", None)
    assert (after["invoice"]["association_status"], after["invoice"]["void_reason"],
            after["invoice"]["requirement_key"]) == ("ACTIVE", None, "customer_invoice_dms")
    form_doc, invoice_doc = str(legacy["form_document"]), str(legacy["invoice_document"])
    assert after["templates"] == {form_doc: "form_21", invoice_doc: "customer_invoice_dms"}
    assert after["capture"][invoice_doc] == "CLASSIFIED"
    assert after["rows"] == {form_doc: "form_21", invoice_doc: "customer_invoice_dms"}
    assert after["readings"] == [invoice_doc]  # the real invoice is the invoice again
    assert _fact_types(journey, legacy["form_document"]) == {"form_21"}
    assert after["recompute"] == ["PENDING"]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        events = connection.execute(
            text("SELECT event_type, subject_id FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                 "AND event_type = ANY(:e) ORDER BY event_id"),
            {"t": journey.tenant_id, "e": [form_21.FILED_EVENT, form_21.BACK_EVENT]}).all()
    assert [tuple(e) for e in events] == [(form_21.FILED_EVENT, form_doc), (form_21.BACK_EVENT, invoice_doc)]

    from audit_core.uc03_p2_journey360 import _document_facts

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        on_file = _document_facts(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                  di_types=("customer_invoice_dms", "wholesale_invoice"))
    assert [d["documentId"] for d in on_file] == [invoice_doc]  # the Deal's invoices

    again = form_21.run_form_21_refile(journey.engine, "form-21-2026-11-02-commit", [journey.tenant_id])
    assert again["tenants"][journey.tenant_id]["journeys"] == 0  # idempotent


def test_a_form_21_linked_under_the_old_rule_gives_the_invoice_back_when_it_is_read(journey):
    legacy = _legacy_form_21_over_the_invoice(journey)
    _read(journey, legacy["form_document"], _FORM_21)  # its reading copied in again
    assert _evidence(journey, legacy["form"])["document_type_key"] == "form_21"
    invoice = _evidence(journey, legacy["invoice"])
    assert (invoice["association_status"], invoice["void_reason"]) == ("ACTIVE", None)
    assert _evidence(journey, legacy["form"])["supersedes_evidence_id"] is None


def test_the_job_is_a_worker_job_kind_and_every_booking_gets_a_form_21_row(monkeypatch):
    from audit_core import uc03_p2_worker as worker

    monkeypatch.setattr(worker, "_active_tenants", lambda engine: ["tenant-a"])
    target, args = worker._one_shot_target("FORM_21_REFILE", None, "form-21-commit", [])
    assert target is form_21.run_form_21_refile and args == (None, "form-21-commit", ["tenant-a"])
    assert ("p2_form_21", "form_21") in worker._P2_ONLY_REQUIREMENTS
