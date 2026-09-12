from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response
from sqlalchemy import create_engine, text

from audit_core.authorization import AuthorizationError
from audit_core.security import Principal
from audit_core.uc03_audit_flags import FlagLifecycleCommand, FlagRemarkCommand
from audit_core.uc03_daily_ops_flags import (
    DailyOpsFlagCreateCommand,
    act_on_daily_ops_flag,
    add_daily_ops_flag_remark,
    create_daily_ops_flag,
    list_daily_ops_flags,
)

_READ = "audit.daily_ops.read"
_EXECUTE = "audit.daily_ops.execute"


def _principal(actor_id: str, tenant_id: str) -> Principal:
    return Principal(subject=actor_id, tenant_id=tenant_id, permissions=(_READ, _EXECUTE))


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(correlation_id="test-correlation"), headers={})


@pytest.fixture
def daily_ops_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dof-{suffix}"
    pc_actor_id = f"pc-{suffix}"
    tl_actor_id = f"tl-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DOF-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DOF-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DOF', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DOF-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DOF-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets "
                 "(tenant_id, dealer_id, outlet_code, outlet_name, outlet_classification, status) "
                 "VALUES (:t, :d, :c, 'O', 'ONSITE', 'ACTIVE') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DOF-O-{suffix}"},
        ).scalar_one()
        run_id = c.execute(
            text("INSERT INTO auditcore.daily_ops_runs "
                 "(tenant_id, outlet_id, business_date, pc_actor_id, run_status, started_at_utc) "
                 "VALUES (:t, :o, CURRENT_DATE, :pc, 'IN_PROGRESS', now()) RETURNING daily_ops_run_id"),
            {"t": tenant_id, "o": outlet_id, "pc": pc_actor_id},
        ).scalar_one()
        for actor_id, role in ((pc_actor_id, "PC"), (tl_actor_id, "TL")):
            c.execute(
                text("INSERT INTO auditcore.business_assignments "
                     "(tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id, "
                     " effective_from, assignment_status) "
                     "VALUES (:t, :a, :r, :d, :o, now() - interval '1 day', 'ACTIVE')"),
                {"t": tenant_id, "a": actor_id, "r": role, "d": dealer_id, "o": outlet_id},
            )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield SimpleNamespace(
            connection=c, tenant_id=tenant_id, outlet_id=outlet_id, run_id=run_id,
            pc_actor_id=pc_actor_id, tl_actor_id=tl_actor_id,
        )
    engine.dispose()


def _raise_flag(setup, *, actor_id: str, category: str, severity: str = "HIGH", version: int = 1):
    return create_daily_ops_flag(
        setup.tenant_id, setup.outlet_id, setup.run_id,
        DailyOpsFlagCreateCommand(category=category, severity=severity, summary=f"{category} on run"),
        _request(), Response(), idempotency_key=f"idem-{uuid4()}", if_match=f'"{version}"',
        principal=_principal(actor_id, setup.tenant_id), connection=setup.connection,
    )


def test_pc_can_raise_a_daily_ops_flag(daily_ops_setup) -> None:
    result = _raise_flag(daily_ops_setup, actor_id=daily_ops_setup.pc_actor_id, category="PROCESS_NON_COMPLIANCE")
    assert result.flag.status == "OPEN"
    assert result.flag.dailyOpsRunId == daily_ops_setup.run_id
    assert result.flag.category == "PROCESS_NON_COMPLIANCE"

    row = daily_ops_setup.connection.execute(
        text("SELECT subject_kind, journey_id, daily_ops_run_id FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND audit_finding_id=:f"),
        {"t": daily_ops_setup.tenant_id, "f": result.flag.flagId},
    ).mappings().one()
    assert row["subject_kind"] == "DAILY_OPS"
    assert row["journey_id"] is None
    assert row["daily_ops_run_id"] == daily_ops_setup.run_id


def test_listing_shows_the_raised_flag(daily_ops_setup) -> None:
    _raise_flag(daily_ops_setup, actor_id=daily_ops_setup.pc_actor_id, category="OTHER")
    items = list_daily_ops_flags(
        daily_ops_setup.tenant_id, daily_ops_setup.outlet_id, daily_ops_setup.run_id,
        principal=_principal(daily_ops_setup.pc_actor_id, daily_ops_setup.tenant_id),
        connection=daily_ops_setup.connection,
    )
    assert len(items) == 1
    assert items[0].status == "OPEN"


def test_pc_can_resolve_a_data_gap(daily_ops_setup) -> None:
    # PAYMENT_EXCEPTION classifies as DATA_GAP -- self-serve, a PC may resolve it.
    raised = _raise_flag(daily_ops_setup, actor_id=daily_ops_setup.pc_actor_id, category="PAYMENT_EXCEPTION")
    result = act_on_daily_ops_flag(
        daily_ops_setup.tenant_id, daily_ops_setup.outlet_id, daily_ops_setup.run_id, raised.flag.flagId,
        FlagLifecycleCommand(action="RESOLVE", resolutionReason="Fixed it"),
        _request(), Response(), idempotency_key=f"idem-{uuid4()}", if_match=f'"{raised.flag.version}"',
        principal=_principal(daily_ops_setup.pc_actor_id, daily_ops_setup.tenant_id),
        connection=daily_ops_setup.connection,
    )
    assert result.flag.status == "RESOLVED"
    assert result.flag.disposition == "FIXED"


def test_pc_cannot_resolve_a_violation(daily_ops_setup) -> None:
    # PROCESS_NON_COMPLIANCE classifies as VIOLATION -- a PC may not resolve it.
    raised = _raise_flag(daily_ops_setup, actor_id=daily_ops_setup.pc_actor_id, category="PROCESS_NON_COMPLIANCE")
    with pytest.raises(AuthorizationError):
        act_on_daily_ops_flag(
            daily_ops_setup.tenant_id, daily_ops_setup.outlet_id, daily_ops_setup.run_id, raised.flag.flagId,
            FlagLifecycleCommand(action="RESOLVE", resolutionReason="Fixed it"),
            _request(), Response(), idempotency_key=f"idem-{uuid4()}", if_match=f'"{raised.flag.version}"',
            principal=_principal(daily_ops_setup.pc_actor_id, daily_ops_setup.tenant_id),
            connection=daily_ops_setup.connection,
        )


def test_tl_can_accept_a_violation(daily_ops_setup) -> None:
    raised = _raise_flag(daily_ops_setup, actor_id=daily_ops_setup.pc_actor_id, category="PROCESS_NON_COMPLIANCE")
    result = act_on_daily_ops_flag(
        daily_ops_setup.tenant_id, daily_ops_setup.outlet_id, daily_ops_setup.run_id, raised.flag.flagId,
        FlagLifecycleCommand(action="CONFIRM_BREACH", resolutionReason="Confirmed breach"),
        _request(), Response(), idempotency_key=f"idem-{uuid4()}", if_match=f'"{raised.flag.version}"',
        principal=_principal(daily_ops_setup.tl_actor_id, daily_ops_setup.tenant_id),
        connection=daily_ops_setup.connection,
    )
    assert result.flag.status == "RESOLVED"
    assert result.flag.disposition == "CONFIRMED_BREACH"


def test_remark_bumps_version_without_changing_status(daily_ops_setup) -> None:
    raised = _raise_flag(daily_ops_setup, actor_id=daily_ops_setup.pc_actor_id, category="OTHER")
    result = add_daily_ops_flag_remark(
        daily_ops_setup.tenant_id, daily_ops_setup.outlet_id, daily_ops_setup.run_id, raised.flag.flagId,
        FlagRemarkCommand(remarks="Following up with the outlet."),
        _request(), Response(), idempotency_key=f"idem-{uuid4()}", if_match=f'"{raised.flag.version}"',
        principal=_principal(daily_ops_setup.pc_actor_id, daily_ops_setup.tenant_id),
        connection=daily_ops_setup.connection,
    )
    assert result.flag.status == "OPEN"
    assert result.flag.version == raised.flag.version + 1
