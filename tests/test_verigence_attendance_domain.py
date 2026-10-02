from decimal import Decimal

from audit_core.verigence_attendance.domain import (
    GeoPoint,
    finance_approval_required,
    leave_can_move_to_hr,
    can_view_employee_attendance,
    can_view_employee_expense,
    within_geofence,
)


def test_geofence_accepts_employee_within_500m():
    office = GeoPoint(30.7333, 76.7794)
    nearby = GeoPoint(30.7340, 76.7800)
    assert within_geofence(nearby, office, 500)


def test_geofence_rejects_employee_outside_500m():
    office = GeoPoint(30.7333, 76.7794)
    far = GeoPoint(30.7433, 76.7894)
    assert not within_geofence(far, office, 500)


def test_finance_threshold_is_month_total_and_strictly_over_3000():
    assert not finance_approval_required(Decimal(2500), Decimal(500))
    assert finance_approval_required(Decimal(2500), Decimal(501))


def test_tl_or_pmo_can_move_leave_to_hr():
    assert leave_can_move_to_hr(tl_approved=True, pmo_approved=False)
    assert leave_can_move_to_hr(tl_approved=False, pmo_approved=True)
    assert not leave_can_move_to_hr(tl_approved=False, pmo_approved=False)



def test_pc_cannot_view_another_employee():
    assert not can_view_employee_attendance(
        actor_user_id="pc-1",
        employee_user_id="pc-2",
        actor_role="PC",
        employee_tl_user_id=None,
        employee_pmo_user_id=None,
    )
    assert not can_view_employee_expense(
        actor_user_id="pc-1",
        employee_user_id="pc-2",
    )


def test_tl_sees_only_employees_assigned_to_them():
    assert can_view_employee_attendance(
        actor_user_id="tl-1",
        employee_user_id="pc-1",
        actor_role="TL",
        employee_tl_user_id="tl-1",
        employee_pmo_user_id="pm-1",
    )
    assert not can_view_employee_attendance(
        actor_user_id="tl-1",
        employee_user_id="pc-2",
        actor_role="TL",
        employee_tl_user_id="tl-2",
        employee_pmo_user_id="pm-1",
    )


def test_pmo_sees_only_employees_assigned_to_them():
    assert can_view_employee_attendance(
        actor_user_id="pm-1",
        employee_user_id="pc-1",
        actor_role="PM",
        employee_tl_user_id="tl-1",
        employee_pmo_user_id="pm-1",
    )
    assert not can_view_employee_attendance(
        actor_user_id="pm-1",
        employee_user_id="pc-2",
        actor_role="PM",
        employee_tl_user_id="tl-1",
        employee_pmo_user_id="pm-2",
    )


def test_tl_and_pmo_never_gain_peer_expense_visibility():
    assert not can_view_employee_expense(actor_user_id="tl-1", employee_user_id="pc-1")
    assert not can_view_employee_expense(actor_user_id="pm-1", employee_user_id="pc-1")
