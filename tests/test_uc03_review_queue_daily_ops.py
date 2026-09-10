"""Daily Operations findings surface in the shared cross-journey Review
Queue behind subjectKind='DAILY_OPS' -- see uc03_review_queue.py's own
module docstring. uc03_review_queue.py has no pre-existing endpoint-level
test suite (no TestClient/auth-mock harness established for it); this adds
focused coverage for the new _load_daily_ops_queue path only, at the same
direct-function-call level as test_uc03_daily_ops_flags.py.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response
from sqlalchemy import create_engine, text

from audit_core.security import Principal
from audit_core.uc03_daily_ops_flags import (
    DailyOpsFlagCreateCommand,
    create_daily_ops_flag,
)
from audit_core.uc03_finding_routing import resolve_sla_policy
from audit_core.uc03_review_queue import _load_daily_ops_queue


def _principal(actor_id: str, tenant_id: str) -> Principal:
    return Principal(subject=actor_id, tenant_id=tenant_id, permissions=("audit.daily_ops.read", "audit.daily_ops.execute"))


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(correlation_id="test-correlation"), headers={})


@pytest.fixture
def daily_ops_queue_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-rqo-{suffix}"
    pc_actor_id = f"pc-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"RQO-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"RQO-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'RQO', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"RQO-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"RQO-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets "
                 "(tenant_id, dealer_id, outlet_code, outlet_name, outlet_classification, status) "
                 "VALUES (:t, :d, :c, 'O', 'ONSITE', 'ACTIVE') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"RQO-O-{suffix}"},
        ).scalar_one()
        run_id = c.execute(
            text("INSERT INTO auditcore.daily_ops_runs "
                 "(tenant_id, outlet_id, business_date, pc_actor_id, run_status, started_at_utc) "
                 "VALUES (:t, :o, CURRENT_DATE, :pc, 'IN_PROGRESS', now()) RETURNING daily_ops_run_id"),
            {"t": tenant_id, "o": outlet_id, "pc": pc_actor_id},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.business_assignments "
                 "(tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id, "
                 " effective_from, assignment_status) "
                 "VALUES (:t, :a, 'PC', :d, :o, now() - interval '1 day', 'ACTIVE')"),
            {"t": tenant_id, "a": pc_actor_id, "d": dealer_id, "o": outlet_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield SimpleNamespace(
            connection=c, tenant_id=tenant_id, outlet_id=outlet_id, run_id=run_id, pc_actor_id=pc_actor_id,
        )
    engine.dispose()


def test_daily_ops_flag_appears_in_the_shared_review_queue(daily_ops_queue_setup) -> None:
    setup = daily_ops_queue_setup
    raised = create_daily_ops_flag(
        setup.tenant_id, setup.outlet_id, setup.run_id,
        DailyOpsFlagCreateCommand(category="PAYMENT_EXCEPTION", severity="HIGH", summary="Cash count mismatch"),
        _request(), Response(), idempotency_key=f"idem-{uuid4()}", if_match='"1"',
        principal=_principal(setup.pc_actor_id, setup.tenant_id), connection=setup.connection,
    )

    items = _load_daily_ops_queue(
        setup.connection, tenant_id=setup.tenant_id, actor_id=setup.pc_actor_id,
        roles=["PC"], policy=resolve_sla_policy(None), now=datetime.now(UTC), finding_class=None,
    )
    assert len(items) == 1
    item, escalated = items[0]
    assert item.flagId == raised.flag.flagId
    assert item.subjectKind == "DAILY_OPS"
    assert item.dailyOpsRunId == setup.run_id
    assert item.outletId == setup.outlet_id
    assert item.journeyId is None
    assert item.stage is None
    assert item.findingClass == "DATA_GAP"
    assert item.isMine is True
    assert escalated is False
