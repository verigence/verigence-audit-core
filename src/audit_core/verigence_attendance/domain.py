from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from decimal import Decimal


DEFAULT_GEOFENCE_METERS = 500
DEFAULT_FINANCE_THRESHOLD = Decimal("3000.00")


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float


def distance_meters(a: GeoPoint, b: GeoPoint) -> float:
    """Server-side Haversine distance; client-reported distance is never trusted."""
    earth_radius_m = 6_371_000.0
    lat1, lat2 = radians(a.latitude), radians(b.latitude)
    dlat = radians(b.latitude - a.latitude)
    dlon = radians(b.longitude - a.longitude)
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * earth_radius_m * asin(sqrt(h))


def within_geofence(captured: GeoPoint, assigned: GeoPoint, radius_meters: int = DEFAULT_GEOFENCE_METERS) -> bool:
    if radius_meters <= 0:
        raise ValueError("radius_meters must be positive")
    return distance_meters(captured, assigned) <= radius_meters


def finance_approval_required(month_claim_total: Decimal, new_claim_amount: Decimal, threshold: Decimal = DEFAULT_FINANCE_THRESHOLD) -> bool:
    if month_claim_total < 0 or new_claim_amount <= 0 or threshold < 0:
        raise ValueError("claim values must be valid positive amounts")
    return month_claim_total + new_claim_amount > threshold


def leave_can_move_to_hr(*, tl_approved: bool, pmo_approved: bool) -> bool:
    """Either TL OR PMO approval satisfies the operational approval stage."""
    return tl_approved or pmo_approved


def payable_days(*, scheduled_days: Decimal, paid_leave_days: Decimal, present_days: Decimal, unpaid_leave_days: Decimal) -> Decimal:
    if min(scheduled_days, paid_leave_days, present_days, unpaid_leave_days) < 0:
        raise ValueError("day counts cannot be negative")
    credited = present_days + paid_leave_days
    return min(scheduled_days, max(Decimal("0"), credited - unpaid_leave_days))
