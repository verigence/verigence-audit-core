from decimal import Decimal

from audit_core.verigence_attendance.domain import (
    GeoPoint,
    finance_approval_required,
    leave_can_move_to_hr,
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
