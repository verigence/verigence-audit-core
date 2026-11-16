"""Gate passes read again for the chassis number (closing list item 5, 2026-11-16)."""
from __future__ import annotations

from audit_core import uc03_p2_worker


def test_the_job_is_registered(monkeypatch):
    monkeypatch.setattr(uc03_p2_worker, "_active_tenants", lambda engine: ["t1"])
    target, args = uc03_p2_worker._one_shot_target("GATE_PASS_CHASSIS_REREAD", None, "x", [])
    assert target.__name__ == "run_gate_pass_chassis_reread" and args == (None, "x", ["t1"])


def test_the_delivery_order_loan_job_is_registered(monkeypatch):
    # Delivery Orders read again for the loan amount (DI 0069, 2026-11-16).
    monkeypatch.setattr(uc03_p2_worker, "_active_tenants", lambda engine: ["t1"])
    target, args = uc03_p2_worker._one_shot_target("DELIVERY_ORDER_LOAN_REREAD", None, "x", [])
    assert target.__name__ == "run_delivery_order_loan_reread" and args == (None, "x", ["t1"])


def test_both_jobs_read_their_own_type_and_field(monkeypatch):
    from audit_core import uc03_gate_pass_chassis_reread as job

    seen = []
    monkeypatch.setattr(job, "run_field_reread",
                        lambda engine, tag, tenants, *, types, field, event: seen.append((types, field)) or {})
    job.run_gate_pass_chassis_reread(None, "x", [])
    job.run_delivery_order_loan_reread(None, "x", [])
    assert seen == [(("gate_pass",), "chassis_number"), (("delivery_order_cover",), "loan_amount")]
