"""Gate passes read again for the chassis number (closing list item 5, 2026-11-16)."""
from __future__ import annotations

from audit_core import uc03_p2_worker


def test_the_job_is_registered(monkeypatch):
    monkeypatch.setattr(uc03_p2_worker, "_active_tenants", lambda engine: ["t1"])
    target, args = uc03_p2_worker._one_shot_target("GATE_PASS_CHASSIS_REREAD", None, "x", [])
    assert target.__name__ == "run_gate_pass_chassis_reread" and args == (None, "x", ["t1"])
