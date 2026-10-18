"""The Reviewer: the open ledger, the two reviewed marks, and what they guard.

These run against real Postgres, calling the endpoint functions directly the
way the other Phase 2 API tests do.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    AllowAllAuthorization,
    add_extracted_field,
    add_page,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text
from starlette.requests import Request

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_api import ResyncPageCommand, resync_page
from audit_core.uc03_p2_documents import (
    P2FieldCorrectionCommand,
    correct_p2_document_field,
)
from audit_core.uc03_p2_reviewer import (
    get_review_state,
    mark_stage_documents_reviewed,
    record_document_open,
    stage_review_gate,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2rv")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _actor(journey, role_code: str) -> str:
    """A second person on this journey's outlet, holding one role.

    One role per actor is enforced (`resolve_p2_scope` raises on two), so every
    role in a test needs its own actor.
    """
    actor_id = f"{role_code.lower()}-{uuid4().hex[:8]}"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id
                ) VALUES (:t, :a, :r, :d, :o)
                """
            ),
            {
                "t": journey.tenant_id,
                "a": actor_id,
                "r": role_code,
                "d": journey.dealer_id,
                "o": journey.outlet_id,
            },
        )
    return actor_id


def _open(journey, document_id: UUID, actor_id: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        record_document_open(
            connection,
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            document_id=document_id,
            actor_id=actor_id,
        )


def _gate(journey, actor_id: str, stage_code: str = "BOOKING") -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return stage_review_gate(
            connection,
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            stage_code=stage_code,
            actor_id=actor_id,
        )


def _mark(journey, actor_id: str, stage_code: str = "BOOKING") -> dict:
    with journey.engine.begin() as connection:
        return mark_stage_documents_reviewed(
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            stage_code=stage_code,
            human_principal=SimpleNamespace(subject=actor_id),
            authorization_client=AllowAllAuthorization(role_key="REVIEWER"),
            connection=connection,
        )


def _state(journey, actor_id: str) -> dict:
    with journey.engine.begin() as connection:
        return get_review_state(
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            human_principal=SimpleNamespace(subject=actor_id),
            authorization_client=AllowAllAuthorization(role_key="REVIEWER"),
            connection=connection,
        )


def test_a_stage_is_marked_reviewed_only_after_every_document_is_opened(journey):
    """The point of the gate: the mark attests that each document was looked at."""
    reviewer = _actor(journey, "REVIEWER")
    _, _, first = add_page(journey, page_number=1, status="READY")
    _, _, second = add_page(journey, page_number=2, status="READY")

    gate = _gate(journey, reviewer)
    assert gate["documentCount"] == 2
    assert gate["openedCount"] == 0
    assert gate["satisfied"] is False

    _open(journey, first, reviewer)
    gate = _gate(journey, reviewer)
    assert gate["openedCount"] == 1
    assert [item["documentId"] for item in gate["outstanding"]] == [str(second)]

    with pytest.raises(HTTPException) as refused:
        _mark(journey, reviewer)
    assert refused.value.status_code == 409
    assert "not been opened" in refused.value.detail

    _open(journey, second, reviewer)
    assert _gate(journey, reviewer)["satisfied"] is True
    result = _mark(journey, reviewer)
    assert result["documentCount"] == 2
    assert result["reviewedByActorId"] == reviewer


def test_opens_are_personal_so_one_reviewer_cannot_use_anothers(journey):
    """Marking a stage reviewed is an attestation by a person, not a team."""
    first_reviewer = _actor(journey, "REVIEWER")
    second_reviewer = _actor(journey, "REVIEWER")
    _, _, document = add_page(journey, page_number=1, status="READY")

    _open(journey, document, first_reviewer)

    assert _gate(journey, first_reviewer)["satisfied"] is True
    assert _gate(journey, second_reviewer)["satisfied"] is False
    with pytest.raises(HTTPException) as refused:
        _mark(journey, second_reviewer)
    assert refused.value.status_code == 409


def test_a_stage_cannot_be_marked_reviewed_twice(journey):
    reviewer = _actor(journey, "REVIEWER")
    _, _, document = add_page(journey, page_number=1, status="READY")
    _open(journey, document, reviewer)
    _mark(journey, reviewer)

    with pytest.raises(HTTPException) as refused:
        _mark(journey, reviewer)
    assert refused.value.status_code == 409
    assert "already marked reviewed" in refused.value.detail


def test_a_document_arriving_after_the_mark_does_not_reopen_it(journey):
    """Owner decision 2026-10-18: the mark is never revoked. The Reviewer may
    still review the newcomer; there is simply no further mark to set, and the
    recorded count shows what the mark actually covered."""
    reviewer = _actor(journey, "REVIEWER")
    _, _, document = add_page(journey, page_number=1, status="READY")
    _open(journey, document, reviewer)
    _mark(journey, reviewer)

    add_page(journey, page_number=2, status="READY")

    booking = _state(journey, reviewer)["stages"]["BOOKING"]
    assert booking["review"]["documentCount"] == 1
    assert booking["canMarkReviewed"] is False
    # The gate itself still reports the truth -- two documents, one opened --
    # so the newcomer is visible rather than hidden behind the mark.
    assert booking["gate"]["documentCount"] == 2
    assert booking["gate"]["openedCount"] == 1


def test_a_stage_with_no_documents_cannot_be_marked(journey):
    reviewer = _actor(journey, "REVIEWER")
    with pytest.raises(HTTPException) as refused:
        _mark(journey, reviewer, "DELIVERY")
    assert refused.value.status_code == 409
    assert "no delivery documents" in refused.value.detail


def test_resync_is_refused_once_the_stage_is_reviewed_and_a_tl_needs_a_reason(journey):
    """A re-copy overwrites a corrected value in place, so a reviewed stage is
    not re-read. A TL may still force it, because a stage reviewed in error
    must not be stuck with bad data."""
    reviewer = _actor(journey, "REVIEWER")
    team_lead = _actor(journey, "TL")
    _, queue_id, document = add_page(journey, page_number=1, status="READY")

    # Before the mark, the Reviewer can resync freely.
    with journey.engine.begin() as connection:
        allowed = resync_page(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=queue_id,
            request=_request(), human_principal=SimpleNamespace(subject=reviewer),
            authorization_client=AllowAllAuthorization(role_key="REVIEWER"),
            connection=connection, payload=None,
        )
    assert allowed["status"] == "SYNCING_TO_AUDIT_CORE"

    _open(journey, document, reviewer)
    _mark(journey, reviewer)

    with journey.engine.begin() as connection, pytest.raises(HTTPException) as refused:
        resync_page(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=queue_id,
            request=_request(), human_principal=SimpleNamespace(subject=reviewer),
            authorization_client=AllowAllAuthorization(role_key="REVIEWER"),
            connection=connection, payload=None,
        )
    assert refused.value.status_code == 409
    assert "marked reviewed" in refused.value.detail

    with journey.engine.begin() as connection, pytest.raises(HTTPException) as no_reason:
        resync_page(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=queue_id,
            request=_request(), human_principal=SimpleNamespace(subject=team_lead),
            authorization_client=AllowAllAuthorization(role_key="TL"),
            connection=connection, payload=ResyncPageCommand(reason="   "),
        )
    assert no_reason.value.status_code == 422

    with journey.engine.begin() as connection:
        overridden = resync_page(
            tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=queue_id,
            request=_request(), human_principal=SimpleNamespace(subject=team_lead),
            authorization_client=AllowAllAuthorization(role_key="TL"),
            connection=connection,
            payload=ResyncPageCommand(reason="Wrong document was reviewed"),
        )
    assert overridden["status"] == "SYNCING_TO_AUDIT_CORE"

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        details = connection.execute(
            text(
                """
                SELECT details FROM auditcore.p2_activity_events
                WHERE tenant_id=:t AND journey_id=:j
                  AND event_type='PAGE_RESYNC_REQUESTED'
                ORDER BY created_at_utc DESC LIMIT 1
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
    assert details["overrideReason"] == "Wrong document was reviewed"
    assert details["stageCode"] == "BOOKING"


def _correct(journey, document_id, actor_id, role_key, *, value, remarks=None):
    with journey.engine.begin() as connection:
        return correct_p2_document_field(
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            document_id=document_id,
            command=P2FieldCorrectionCommand(
                canonicalFieldId="cf-amount",
                fieldKey="amount",
                sourceFactVersion=1,
                newValue=value,
                remarks=remarks,
            ),
            human_principal=SimpleNamespace(subject=actor_id),
            authorization_client=AllowAllAuthorization(role_key=role_key),
            connection=connection,
        )


def _confident_field(journey, document_id) -> None:
    add_extracted_field(
        journey,
        di_document_id=document_id,
        field_key="amount",
        value="100000",
        document_type="tax_invoice",
        confidence=97.5,
        canonical_field_id="cf-amount",
    )


def test_a_reviewer_corrects_a_confident_value_without_raising_a_task(journey):
    """The role exists because an extraction above 90% can still be wrong, so
    for a Reviewer the adjudication band does not apply and nothing is raised
    for anyone else to approve. The change log is what answers for it."""
    reviewer = _actor(journey, "REVIEWER")
    _, _, document = add_page(journey, page_number=1, status="READY")
    _confident_field(journey, document)

    result = _correct(journey, document, reviewer, "REVIEWER", value="125000")
    assert result["applied"] is True
    assert result["taskId"] is None

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        logged = connection.execute(
            text(
                """
                SELECT field_key, previous_value, new_value, change_reason,
                       actor_id, actor_role_code, stage_code, confidence_score
                FROM auditcore.journey_field_change_log
                WHERE tenant_id=:t AND journey_id=:j
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
        tasks = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.p2_tasks
                WHERE tenant_id=:t AND journey_id=:j
                  AND task_type='FIELD_CORRECTION_REVIEW_P2'
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()

    assert tasks == 0
    assert len(logged) == 1
    entry = logged[0]
    assert entry["change_reason"] == "CORRECTION"
    assert entry["actor_id"] == reviewer
    assert entry["actor_role_code"] == "REVIEWER"
    assert entry["stage_code"] == "BOOKING"
    assert json.loads(json.dumps(entry["new_value"])) == "125000"
    assert float(entry["confidence_score"]) == pytest.approx(97.5)


def test_a_pc_correcting_a_confident_value_still_goes_to_a_team_lead(journey):
    """The band is unchanged for everyone else; only the Reviewer bypasses it."""
    _, _, document = add_page(journey, page_number=1, status="READY")
    _confident_field(journey, document)

    result = _correct(
        journey, document, journey.actor_id, "PC",
        value="125000", remarks="Invoice says 1,25,000",
    )
    assert result["applied"] is False
    assert result["taskId"] is not None
