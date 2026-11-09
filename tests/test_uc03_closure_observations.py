"""Observations on a Reviewer's closure (product owner 2026-11-09): each item
stands as an observation with a severity or is justified as not one; the
Reviewer and the Team Lead add observations of their own."""
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
from test_p2_reviewer_completes_delivery import _ready_for_delivery, _role

from audit_core.db import set_tenant_context
from audit_core.uc03_closure_observations import (
    clean_added,
    decide_items,
    observation_rows,
    severity_counts,
    suggested_severity,
)
from audit_core.uc03_reviewer_closure import (
    ClosureCommand,
    post_closure,
    preview_closure,
)

# ── the decisions on their own ───────────────────────────────────────────────

ITEMS = [
    {"key": "document:rto", "kind": "MISSING_DOCUMENT", "label": "RTO Challan", "requirement": "MANDATORY",
     "suggestedSeverity": "HIGH"},
    {"key": "finding:f1", "kind": "OPEN_FINDING", "label": "Deal charged below the standard price",
     "severity": "HIGH", "suggestedSeverity": "HIGH"},
    {"key": "document:rsa", "kind": "MISSING_DOCUMENT", "label": "RSA Invoice", "requirement": "CONDITIONAL",
     "prefilledAnswer": "Not mandatory for this journey. PC: no RSA sold.", "suggestedSeverity": "MEDIUM"},
]


def test_severities_are_suggested_from_what_the_item_is():
    assert suggested_severity(ITEMS[0]) == "HIGH"
    assert suggested_severity({"kind": "MISSING_DOCUMENT", "requirement": "CONDITIONAL"}) == "MEDIUM"
    assert suggested_severity({"kind": "OPEN_TASK", "severity": "LOW"}) == "LOW"
    assert suggested_severity({"kind": "NOTE"}) == "LOW"


def test_an_item_stands_with_a_severity_or_is_justified():
    decided, missing = decide_items(ITEMS, observations={
        "document:rto": {"stands": True, "severity": "HIGH", "note": "Challan not given by the dealer"},
        "finding:f1": {"stands": False, "justification": "Scrappage concession, CoD on file"},
    }, answers={})
    assert missing == []
    rto, finding, rsa = decided
    assert rto["stands"] and rto["observationSeverity"] == "HIGH" and rto["note"] == "Challan not given by the dealer"
    assert not finding["stands"] and finding["justification"] == "Scrappage concession, CoD on file"
    # An accepted not-mandatory claim answers on its own.
    assert not rsa["stands"] and rsa["answerSource"] == "ACCEPTED_CLAIM"


def test_a_short_justification_or_a_missing_severity_is_named():
    _, missing = decide_items(ITEMS[:2], observations={
        "document:rto": {"stands": True},
        "finding:f1": {"stands": False, "justification": "ok"},
    }, answers={})
    assert missing == ["RTO Challan (choose High, Medium or Low)",
                       ("Deal charged below the standard price (justify why it is not an observation, "
                        "at least 10 characters)")]


def test_an_older_closure_reads_its_answers_as_justifications():
    closure = {"items": [{"key": "finding:f1", "kind": "OPEN_FINDING", "label": "X", "answer": "Settled with GM"}],
               "rejections": []}
    [row] = observation_rows(closure)
    assert row["stands"] is False and row["justification"] == "Settled with GM"


def test_added_observations_need_a_title_and_a_severity():
    with pytest.raises(ValueError):
        clean_added([{"title": "Hi", "severity": "HIGH"}], by_role="REVIEWER")
    with pytest.raises(ValueError):
        clean_added([{"title": "Customer signature missing", "severity": "SEVERE"}], by_role="REVIEWER")
    [added] = clean_added([{"title": "Customer signature missing on NDC", "severity": "MEDIUM"}], by_role="TL")
    assert added["key"] == "added:tl:1" and added["addedBy"] == "TL"


def test_counts_leave_out_what_the_team_lead_rejected():
    rows = [{"stands": True, "severity": "HIGH"}, {"stands": True, "severity": "HIGH", "tlRejection": "no"},
            {"stands": False, "severity": None}, {"stands": True, "severity": "LOW"}]
    assert severity_counts(rows) == {"HIGH": 1, "MEDIUM": 0, "LOW": 1}


# ── through the endpoints ────────────────────────────────────────────────────

@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2obs")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _finding(journey, title, status="OPEN", disposition=None):
    return _sql(journey, "INSERT INTO auditcore.audit_findings (tenant_id, journey_id, finding_type_code, severity, "
                         "finding_status, finding_class, title, disposition, created_at_utc) VALUES (:t, :j, "
                         "'PRICING_ANOMALY', 'HIGH', :s, 'VIOLATION', :title, :d, now()) RETURNING audit_finding_id",
                title=title, s=status, d=disposition).scalar_one()


def _preview(journey, role="REVIEWER"):
    with journey.engine.begin() as connection:
        return preview_closure(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               human_principal=principal(journey), authorization_client=AllowAllAuthorization(role),
                               connection=connection)


def _close(journey, **command):
    with journey.engine.begin() as connection:
        return post_closure(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            command=ClosureCommand(**command), human_principal=principal(journey),
                            authorization_client=AllowAllAuthorization("REVIEWER"), connection=connection)


def _decide(journey, closure_id, *, decision, rejections=(), added=()):
    from audit_core.uc03_reviewer_closure import decide_closure

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return decide_closure(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                              closure_id=closure_id, decision=decision, rejections=list(rejections), comment=None,
                              actor_id="a-tl", actor_role="TL", added=list(added))


def test_a_closure_with_observations_added_and_remarks_reads_back_for_the_report(journey):
    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    open_finding = _finding(journey, "Deal charged below the standard price")
    confirmed = _finding(journey, "Discount given beyond the entitlement", status="RESOLVED",
                         disposition="CONFIRMED_BREACH")
    preview = _preview(journey)
    by_key = {i["key"]: i for i in preview["items"]}
    assert by_key[f"finding:{open_finding}"]["suggestedSeverity"] == "HIGH"
    assert by_key[f"confirmed:{confirmed}"]["kind"] == "CONFIRMED_FINDING"
    assert preview["severities"] == ["HIGH", "MEDIUM", "LOW"]

    observations = {key: {"stands": False, "justification": f"Checked with the outlet: {item['label']}"}
                    for key, item in by_key.items()}
    observations[f"finding:{open_finding}"] = {"stands": True, "severity": "MEDIUM", "note": "Insurance below standard"}
    result = _close(journey, comment="Delivery confirmed with the outlet manager", observations=observations,
                    added=[{"title": "Customer signature missing on the NDC", "severity": "LOW"}],
                    remarks="Outlet asked to collect the signed NDC.")
    assert result["severityCounts"] == {"HIGH": 0, "MEDIUM": 1, "LOW": 1}

    _decide(journey, result["closureId"], decision="ACCEPT",
            added=[{"title": "Gate pass dated before the invoice", "severity": "HIGH"}])
    last = _preview(journey)["lastClosure"]
    assert last["remarks"] == "Outlet asked to collect the signed NDC."
    standing = [(r["label"], r["severity"], r["source"]) for r in last["observations"] if r["stands"]]
    assert standing == [("Gate pass dated before the invoice", "HIGH", "TL"),
                        ("Deal charged below the standard price", "MEDIUM", "SYSTEM"),
                        ("Customer signature missing on the NDC", "LOW", "REVIEWER")]
    assert last["severityCounts"] == {"HIGH": 1, "MEDIUM": 1, "LOW": 1}
    not_accepted = [r for r in last["observations"] if not r["stands"]]
    assert all(r["justification"].startswith("Checked with the outlet") for r in not_accepted)


def test_the_team_lead_can_reject_an_observation_the_reviewer_added(journey):
    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    preview = _preview(journey)
    observations = {i["key"]: {"stands": False, "justification": "Not applicable to this deal"} for i in preview["items"]}
    result = _close(journey, comment="Delivery confirmed with the outlet manager", observations=observations,
                    added=[{"title": "Customer signature missing on the NDC", "severity": "LOW"}])
    _decide(journey, result["closureId"], decision="REJECT",
            rejections=[{"key": "added:reviewer:1", "reason": "The signature is on page 2 of the NDC"}])
    last = _preview(journey)["lastClosure"]
    added = next(r for r in last["observations"] if r["key"] == "added:reviewer:1")
    assert added["tlRejection"] == "The signature is on page 2 of the NDC"


def test_a_closure_missing_a_decision_names_the_item(journey):
    _ready_for_delivery(journey)
    _role(journey, "REVIEWER")
    _finding(journey, "Deal charged below the standard price")
    preview = _preview(journey)
    observations = {i["key"]: {"stands": False, "justification": "Not applicable to this deal"}
                    for i in preview["items"] if i["kind"] != "OPEN_FINDING"}
    with pytest.raises(HTTPException) as refused:
        _close(journey, comment="Delivery confirmed with the outlet manager", observations=observations)
    assert refused.value.status_code == 422 and "Deal charged below the standard price" in refused.value.detail
