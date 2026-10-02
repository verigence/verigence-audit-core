from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_existing_audit_core_runtime_does_not_mount_employee_attendance() -> None:
    source = (ROOT / "src/audit_core/main.py").read_text(encoding="utf-8")
    assert "verigence_attendance" not in source
    assert "employee-attendance" not in source


def test_employee_attendance_has_separate_runtime_entrypoint() -> None:
    dockerfile = (ROOT / "Dockerfile.employee-attendance").read_text(encoding="utf-8")
    railway = (ROOT / "railway.employee-attendance.toml").read_text(encoding="utf-8")
    entrypoint = "audit_core.verigence_attendance.main:app"
    assert entrypoint in dockerfile
    assert entrypoint in railway
    assert "audit_core.main:app" not in dockerfile
    assert "audit_core.main:app" not in railway


def test_pm_team_reimbursements_are_read_only_and_pm_scoped() -> None:
    repository = (
        ROOT / "src/audit_core/verigence_attendance/repository.py"
    ).read_text(encoding="utf-8")
    api = (ROOT / "src/audit_core/verigence_attendance/api.py").read_text(encoding="utf-8")
    assert "e.pmo_user_id=CAST(:actor AS uuid)" in repository
    assert '@router.get("/team/reimbursements"' in api
    assert '@router.post("/team/reimbursements"' not in api


def test_team_attendance_is_assignment_scoped() -> None:
    repository = (
        ROOT / "src/audit_core/verigence_attendance/repository.py"
    ).read_text(encoding="utf-8")
    assert "e.tl_user_id=CAST(:actor AS uuid)" in repository
    assert "e.pmo_user_id=CAST(:actor AS uuid)" in repository
