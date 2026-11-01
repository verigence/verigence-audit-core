"""OWN_COPY_RESTORE: a customer's own KYC copy replaced by someone else's is
restored, only where the booking's files and documents agree who the customer
is (2026-11-01)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_own_copy_restore as job
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_worker import _one_shot_target


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2ocr")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _reference(journey) -> str:
    return _sql(journey, "SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"
                ).scalar_one()


def _decided(journey, name: str) -> None:
    _sql(journey, "INSERT INTO auditcore.p2_journey_file_identity (tenant_id, journey_id, customer_name, customer_kind) "
                  "VALUES (:t, :j, :n, 'PERSON')", n=name)


def _aadhaar_copies(journey, *names_oldest_first: str) -> list[UUID]:
    """Aadhaar copies of one requirement; the newest active, the others superseded."""
    requirement = _sql(
        journey,
        """INSERT INTO auditcore.journey_document_requirements (
               tenant_id, journey_id, document_requirement_item_id, requirement_key, document_type_key,
               process_area, requirement_level, requirement_status, condition_snapshot)
           VALUES (:t, :j, NULL, 'aadhaar', 'aadhaar', 'BOOKING', 'REQUIRED', 'PENDING', '{}'::jsonb)
           RETURNING journey_document_requirement_id""",
    ).scalar_one()
    out = []
    for i, name in enumerate(names_oldest_first):
        newest = i == len(names_oldest_first) - 1
        document_id = uuid4()
        evidence = add_evidence(journey, di_document_id=document_id, document_type_key="aadhaar",
                                status="ACTIVE" if newest else "SUPERSEDED")
        _sql(journey, "UPDATE auditcore.evidence SET journey_document_requirement_id=:r, "
                      "linked_at_utc=now() - make_interval(mins => :age), "
                      "void_reason=CASE WHEN association_status='SUPERSEDED' THEN 'REPLACED_BY_NEWER_UPLOAD' END "
                      "WHERE tenant_id=:t AND evidence_id=:e",
             r=requirement, e=evidence, age=10 * (len(names_oldest_first) - i))
        add_extracted_field(journey, di_document_id=document_id, field_key="aadhaar_name", value=name,
                            document_type="aadhaar")
        out.append(evidence)
    return out


def _status(journey, evidence_id) -> str:
    return _sql(journey, "SELECT association_status FROM auditcore.evidence WHERE tenant_id=:t AND evidence_id=:e",
                e=evidence_id).scalar_one()


def test_the_job_is_registered(journey) -> None:
    target, args = _one_shot_target("OWN_COPY_RESTORE", journey.engine, "t", ["UTK-SA-261019-005"])
    assert target.__name__ == "run_own_copy_restore" and args[3] == ["UTK-SA-261019-005"]


def test_the_customers_own_aadhaar_is_restored_only_on_commit(journey) -> None:
    """UTK-SA-261019-005: Rakesh Kumar Pattanayak's Aadhaar replaced Suryakant
    Parida's; the docket names Suryakanta Parida."""
    _decided(journey, "SURYAKANT PARIDA")
    add_ready_document(journey, "booking_docket", customer_name="SURYAKANTA PARIDA")
    own, other = _aadhaar_copies(journey, "SURYAKANT PARIDA", "Rakesh Kumar Pattanayak")
    reference = _reference(journey)

    dry = job.restore_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t", commit=False,
                             references=[reference])
    assert dry["toRestore"] == 1 and dry["restored"] == 0
    assert _status(journey, own) == "SUPERSEDED" and _status(journey, other) == "ACTIVE"

    done = job.restore_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t-commit", commit=True,
                              references=[reference])
    assert done["restored"] == 1 and done["failed"] == 0
    assert _status(journey, own) == "ACTIVE" and _status(journey, other) == "SUPERSEDED"
    again = job.restore_tenant(journey.engine, tenant_id=journey.tenant_id, tag="t-commit", commit=True,
                               references=[reference])
    assert again["toRestore"] == 0 and again["restored"] == 0


def test_nothing_is_restored_when_no_document_confirms_the_customer(journey) -> None:
    """ADI-RD-261022-008: the files decided Narayan Sahu, but the PAN, the docket
    and the Aadhaar on file are Harischandra Sahu's."""
    _decided(journey, "NARAYAN SAHU")
    add_ready_document(journey, "booking_docket", customer_name="HARISCHANDRA SAHU")
    _own, other = _aadhaar_copies(journey, "NARAYAN SAHU", "Harischandra Sahu")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        outcome = job.plan(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert outcome["skipped"].startswith("No PAN card, docket")
    assert _status(journey, other) == "ACTIVE"


def test_an_active_copy_naming_the_customer_is_left_alone(journey) -> None:
    _decided(journey, "BIJAY KUMAR ROUT")
    add_ready_document(journey, "booking_docket", customer_name="BIJAY KUMAR ROUT")
    _aadhaar_copies(journey, "Bijoy Kr Rout", "BIJAY KUMAR ROUT")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert job.plan(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["restores"] == []


def test_without_references_nothing_runs(journey) -> None:
    job.run_own_copy_restore(journey.engine, "t-commit", [journey.tenant_id], [])
