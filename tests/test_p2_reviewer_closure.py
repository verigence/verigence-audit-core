"""A Reviewer closes a journey with a closure summary (product owner 2026-11-04).

Every mandatory document not on file, every open finding and every other open
task needs its own answer; the summary is kept and the Delivery is completed
for the Team Lead's review."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    AllowAllAuthorization,
    create_p2_journey,
    database_engine,
    principal,
)
from sqlalchemy import text
from test_p2_reviewer_completes_delivery import _ready_for_delivery, _role, _row

from audit_core.db import set_tenant_context
from audit_core.uc03_reviewer_closure import (
    ClosureCommand,
    post_closure,
    preview_closure,
)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2clo")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _finding(journey, title="Discount exceeds policy by Rs 25,000"):
    return _sql(journey, "INSERT INTO auditcore.audit_findings (tenant_id, journey_id, finding_type_code, severity, "
                         "finding_status, finding_class, title, created_at_utc) VALUES (:t, :j, 'DISCOUNT_ANOMALY', "
                         "'HIGH', 'OPEN', 'VIOLATION', :title, now()) RETURNING audit_finding_id",
                title=title).scalar_one()


def _preview(journey, role="REVIEWER"):
    with journey.engine.begin() as connection:
        return preview_closure(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               human_principal=principal(journey), authorization_client=AllowAllAuthorization(role),
                               connection=connection)


def _close(journey, comment, answers, role="REVIEWER"):
    with journey.engine.begin() as connection:
        return post_closure(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            command=ClosureCommand(comment=comment, answers=answers),
                            human_principal=principal(journey), authorization_client=AllowAllAuthorization(role),
                            connection=connection)


def test_the_closure_lists_every_open_item_and_needs_an_answer_for_each(journey):
    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    finding = _finding(journey)
    preview = _preview(journey)
    kinds = {item["kind"] for item in preview["items"]}
    assert "MISSING_DOCUMENT" in kinds and "OPEN_FINDING" in kinds
    assert f"finding:{finding}" in {item["key"] for item in preview["items"]}
    assert preview["canClose"] is True and preview["blockers"] == []
    # The same journey gives the same list, in the same order.
    assert [i["key"] for i in _preview(journey)["items"]] == [i["key"] for i in preview["items"]]

    answers = {item["key"]: f"Specific answer for {item['label']}" for item in preview["items"]}
    one_missing = dict(answers)
    one_missing.pop(f"finding:{finding}")
    with pytest.raises(HTTPException) as unanswered:
        _close(journey, "Delivery confirmed with the outlet manager", one_missing)
    assert unanswered.value.status_code == 422
    assert unanswered.value.detail["unanswered"] == ["Discount exceeds policy by Rs 25,000"]
    assert _row(journey, "SELECT count(*) AS n FROM auditcore.p2_journey_closures "
                         "WHERE tenant_id=:t AND journey_id=:j")["n"] == 0

    with pytest.raises(HTTPException) as short_comment:
        _close(journey, "ok", answers)
    assert short_comment.value.status_code == 422

    result = _close(journey, "Delivery confirmed with the outlet manager", answers)
    assert result["deliveryOutcome"] == "COMPLETED" and result["itemCount"] == len(preview["items"])
    runtime = _row(journey, "SELECT delivery_completion_state FROM auditcore.p2_journey_runtime "
                            "WHERE tenant_id=:t AND journey_id=:j")
    assert runtime["delivery_completion_state"] == "COMPLETE"   # the TL review follows as for any delivery
    last = _preview(journey)["lastClosure"]
    assert last["comment"] == "Delivery confirmed with the outlet manager"
    finding_item = next(i for i in last["items"] if i["key"] == f"finding:{finding}")
    assert finding_item["answer"] == "Specific answer for Discount exceeds policy by Rs 25,000"
    assert finding_item["answerSource"] == "REVIEWER"


def test_only_a_reviewer_closes(journey):
    _ready_for_delivery(journey)
    _role(journey, "PC")
    with pytest.raises(HTTPException) as refused:
        _close(journey, "Delivery confirmed with the outlet manager", {})
    assert refused.value.status_code == 403


def test_an_accepted_not_mandatory_claim_answers_its_document_and_a_pending_one_blocks(journey):
    from audit_core.uc03_not_mandatory_claims import decide_claim
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh
    from audit_core.uc03_p2_tasks import submit_action

    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    preview = _preview(journey)
    doc = next(i for i in preview["items"] if i["kind"] == "MISSING_DOCUMENT")
    requirement = doc["key"].split(":", 1)[1]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        task_id, _ = raise_or_refresh(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            dedupe_key=f"document-missing:{journey.journey_id}:{requirement}", task_type="DOCUMENT_MISSING",
            source_type="DOCUMENT", source_code="DOCUMENT_MISSING", title=f"Upload the {doc['label']}",
            description="Upload it.", reference={"sourceCode": "DOCUMENT_MISSING", "requirementKey": requirement,
                                                 "stage": doc["stage"]},
            severity="HIGH", registry=get_registry())
        submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action="CLOSE_TASK",
                      actor_id=journey.actor_id, actor_role_code="PC",
                      comment="Document not required to be uploaded: not applicable to this fleet sale",
                      details={"choice": "Document not required to be uploaded"})
    blocked = _preview(journey)
    assert blocked["canClose"] is False and blocked["blockers"]
    with pytest.raises(HTTPException) as refused:
        _close(journey, "Delivery confirmed with the outlet manager", {})
    assert refused.value.status_code == 409

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        decide_claim(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, task_id=task_id,
                     decision="ACCEPT", reason="Fleet sale letter seen", actor_id="rv", actor_role_code="REVIEWER")
    item = next(i for i in _preview(journey)["items"] if i["key"] == doc["key"])
    assert "not applicable to this fleet sale" in item["prefilledAnswer"]
    assert "Fleet sale letter seen" in item["prefilledAnswer"]
