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
    # The item is named in plain text: the caller sees which one, not just "Business validation failed".
    assert isinstance(unanswered.value.detail, str)
    assert unanswered.value.detail.endswith("Still without one: Discount exceeds policy by Rs 25,000.")
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


# ── The Reviewer's closure replaces the TL review; the TL accepts or rejects it (2026-11-04) ──

def _decide(journey, closure_id, decision, rejections=(), role="TL"):
    from audit_core.uc03_reviewer_closure import decide_closure

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return decide_closure(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                              closure_id=closure_id, decision=decision, rejections=list(rejections), comment=None,
                              actor_id="a-tl", actor_role=role)


def _closed_journey(journey):
    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    finding = _finding(journey)
    preview = _preview(journey)
    answers = {item["key"]: f"Specific answer for {item['label']}" for item in preview["items"]}
    result = _close(journey, "Delivery confirmed with the outlet manager", answers)
    return result, finding, preview


def test_the_reviewers_closure_is_the_review_and_the_tl_gets_one_task_to_accept(journey):
    result, finding, preview = _closed_journey(journey)
    reviewed = _row(journey, "SELECT review_completed_at_utc, audit_state FROM auditcore.journeys "
                             "WHERE tenant_id=:t AND journey_id=:j")
    assert reviewed["review_completed_at_utc"] is not None and reviewed["audit_state"] == "REVIEW_COMPLETE"
    assert _row(journey, "SELECT count(*) AS n FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                         "AND task_type='DELIVERY_REVIEW' AND task_status <> 'VERIFIED_COMPLETE'")["n"] == 0
    task = _row(journey, "SELECT assigned_role_code, task_status, reference, due_at_utc FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND journey_id=:j AND task_type='CLOSURE_ACCEPTANCE'")
    assert task["assigned_role_code"] == "TL" and task["task_status"] == "READY"
    assert len(task["reference"]["items"]) == len(preview["items"]) and task["due_at_utc"] is not None
    with pytest.raises(HTTPException) as reviewer:
        _decide(journey, result["closureId"], "ACCEPT", role="REVIEWER")
    assert reviewer.value.status_code == 403
    assert _decide(journey, result["closureId"], "ACCEPT")["acceptanceStatus"] == "ACCEPTED"
    assert _row(journey, "SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                         "AND task_type='CLOSURE_ACCEPTANCE'")["task_status"] == "VERIFIED_COMPLETE"
    with pytest.raises(HTTPException) as again:
        _decide(journey, result["closureId"], "REJECT", [{"key": f"finding:{finding}", "reason": "x" * 12}])
    assert again.value.status_code == 409


def test_a_rejected_item_goes_back_to_the_reviewer_and_the_rest_keep_their_answers(journey):
    result, finding, _preview_before = _closed_journey(journey)
    key = f"finding:{finding}"
    with pytest.raises(HTTPException) as no_reason:
        _decide(journey, result["closureId"], "REJECT", [{"key": key, "reason": "no"}])
    assert no_reason.value.status_code == 422
    _decide(journey, result["closureId"], "REJECT", [{"key": key, "reason": "Discount needs the GM's approval letter"}])
    journey_row = _row(journey, "SELECT review_completed_at_utc, audit_state FROM auditcore.journeys "
                                "WHERE tenant_id=:t AND journey_id=:j")
    assert journey_row["review_completed_at_utc"] is None and journey_row["audit_state"] == "SENT_BACK"
    rework = _row(journey, "SELECT assigned_role_code, task_status, reference FROM auditcore.p2_tasks "
                           "WHERE tenant_id=:t AND journey_id=:j AND task_type='CLOSURE_REWORK'")
    assert rework["assigned_role_code"] == "REVIEWER" and rework["task_status"] == "READY"
    assert [r["key"] for r in rework["reference"]["rejections"]] == [key]
    # No TL delivery review comes back: the review is the Reviewer's closure.
    assert _row(journey, "SELECT count(*) AS n FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                         "AND task_type='DELIVERY_REVIEW' AND task_status <> 'VERIFIED_COMPLETE'")["n"] == 0

    again = _preview(journey)
    rejected = next(i for i in again["items"] if i["key"] == key)
    assert rejected["prefilledAnswer"] is None
    assert rejected["rejection"]["reason"] == "Discount needs the GM's approval letter"
    others = [i for i in again["items"] if i["key"] != key]
    assert others and all(i["prefilledAnswer"] for i in others)
    with pytest.raises(HTTPException):
        _close(journey, "Answered the rejected item", {})            # the rejected item needs a new answer
    second = _close(journey, "Answered the rejected item", {key: "GM approval letter uploaded and checked"})
    assert second["closureId"] != result["closureId"]
    assert _row(journey, "SELECT task_status FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                         "AND task_type='CLOSURE_REWORK'")["task_status"] == "VERIFIED_COMPLETE"
    last = _preview(journey)["lastClosure"]
    sources = {i["key"]: i["answerSource"] for i in last["items"]}
    assert sources[key] == "REVIEWER" and {v for k, v in sources.items() if k != key} == {"PREVIOUS_CLOSURE"}
    assert last["acceptanceStatus"] == "PENDING"


def test_a_closure_not_decided_in_96_hours_is_accepted_automatically_and_listed(journey):
    from audit_core.uc03_reviewer_closure import auto_accept_due, list_closures

    result, _, _ = _closed_journey(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert auto_accept_due(connection, tenant_id=journey.tenant_id) == 0     # not yet due
    _sql(journey, "UPDATE auditcore.p2_journey_closures SET acceptance_due_utc=now() - interval '1 minute' "
                  "WHERE tenant_id=:t AND journey_id=:j")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert auto_accept_due(connection, tenant_id=journey.tenant_id) == 1
    closure = _preview(journey)["lastClosure"]
    assert (closure["acceptanceStatus"], closure["decidedBy"]) == ("AUTO_ACCEPTED", "SYSTEM")
    task = _row(journey, "SELECT task_status, completion_result FROM auditcore.p2_tasks WHERE tenant_id=:t "
                         "AND journey_id=:j AND task_type='CLOSURE_ACCEPTANCE'")
    assert task["task_status"] == "VERIFIED_COMPLETE" and task["completion_result"]["outcome"] == "AUTO_ACCEPTED"
    with journey.engine.begin() as connection:
        listed = list_closures(tenant_id=journey.tenant_id, human_principal=principal(journey),
                               authorization_client=AllowAllAuthorization("TL"), connection=connection,
                               status="AUTO_ACCEPTED")
    assert [c["closureId"] for c in listed["closures"]] == [result["closureId"]]
    assert listed["counts"]["AUTO_ACCEPTED"] == 1


def test_every_view_reads_the_same_closed_marker(journey):
    """Product owner 2026-11-04: show on every view a journey that got closed."""
    from audit_core.uc03_reviewer_closure import closure_states, journey_closure_state

    _ready_for_delivery(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        before = journey_closure_state(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert before["closed"] is False and before["reviewerClosure"] is None
    _closed_journey_after_ready = _close_ready(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        one = journey_closure_state(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        many = closure_states(connection, tenant_id=journey.tenant_id, journey_ids=[journey.journey_id])
    assert one["closed"] is True and one["reviewerClosure"]["status"] == "PENDING"
    assert many[str(journey.journey_id)] == one
    assert _closed_journey_after_ready["closureId"]


def _close_ready(journey):
    _role(journey, "REVIEWER")
    preview = _preview(journey)
    return _close(journey, "Delivery confirmed with the outlet manager",
                  {item["key"]: f"Specific answer for {item['label']}" for item in preview["items"]})


def test_an_item_that_opens_after_the_form_was_loaded_is_named_in_the_refusal(journey):
    """SHI-SS-261027-013 (2026-11-06): a task reopened by a re-check 3 seconds
    after the Reviewer opened the form; the close was refused with only
    "Business validation failed". The refusal names the new item."""
    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    preview = _preview(journey)
    answers = {item["key"]: f"Specific answer for {item['label']}" for item in preview["items"]}
    _finding(journey)   # opens after the preview was read
    with pytest.raises(HTTPException) as refused:
        _close(journey, "Delivery confirmed with the outlet manager", answers)
    assert refused.value.status_code == 422
    assert "Discount exceeds policy by Rs 25,000" in refused.value.detail


def test_a_structured_http_detail_keeps_its_message_and_lists():
    from audit_core.errors import http_detail_text

    assert http_detail_text("Plain reason.", "Title") == "Plain reason."
    assert http_detail_text({"message": "Rejection incomplete.", "problems": ["a", "b"]}, "Title") == \
        "Rejection incomplete. a; b"
    assert http_detail_text({"code": 1}, "Title") == "Title"
    assert http_detail_text(None, "Title") == "Title"
