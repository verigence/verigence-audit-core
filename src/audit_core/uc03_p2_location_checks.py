"""DealGuard location checks (product owner, 2026-11-05).

"While a PC is using DealGuard, the app checks the PC's location at random
moments: between 1 and 3 times in each clock hour, only between 10:00 and 19:30
IST." The app (web or Android) picks the moments; each check sends where the
device is, and Audit Core decides what it means:

    POST /p2/v1/tenants/{tenant_id}/location-checks             a PC's check
    GET  /p2/v1/tenants/{tenant_id}/location-checks             the day's log (Executive)
    GET  /v1/admin/tenants/{tenant_id}/location-checks          the same, for SuperAdmin
    GET  /p2/v1/tenants/{tenant_id}/location-checks:away        every AWAY check further than minKm
                                                                over a range of days (Executive)
    GET  /v1/admin/tenants/{tenant_id}/location-checks:away     the same, for SuperAdmin

The distance is worked out here (Haversine) to the nearest outlet the PC is
assigned to in the Project, among those with coordinates; a distance sent by a
device is never trusted. The result:

    AT_OUTLET            within GEOFENCE_RADIUS_M of that outlet
    AWAY                 further than that -- the PC is told, nothing is blocked
    UNCLEAR              the device's own accuracy is worse than ACCURACY_LIMIT_M
                         (typical of a desktop's Wi-Fi / IP location): never AWAY
    NO_OUTLET_LOCATION   none of the PC's outlets has coordinates
    DENIED / UNAVAILABLE the device gave no location (permission refused, timed out)

Every check is logged in ``p2_location_checks`` (append-only). Only a person
whose operating role in the Project is PC is checked. Outside 10:00-19:30 IST
nothing is recorded, nor more than MAX_CHECKS_PER_HOUR in one clock hour (two
open tabs must not double the checks): the answer then says ``recorded: false``.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import caller_role, known_names
from audit_core.uc03_p2_access import check_p2_permission
from audit_core.uc03_p2_object_keys import IST

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-location-checks"])
admin_router = APIRouter(prefix="/v1/admin/tenants/{tenant_id}", tags=["uc03-phase2-location-checks-admin"])

_READ_PERMISSION = "audit.journey.read"
# Product owner, 6 Nov 2026: the location report is for SuperAdmin and Executive only.
_VIEWER_ROLES = frozenset({"EXECUTIVE"})
_ADMIN_FUNCTIONAL_ROLES = frozenset({"SUPER_ADMIN"})

# Within this distance of an assigned outlet a PC is at the outlet. It mirrors
# the default of myHR's attendance geofence (hrmgmt ``attendance.geofence_radius_m``,
# 500 m), so a PC counts as at work by the same measure in both.
GEOFENCE_RADIUS_M = 500.0
# A reading the device itself says is worse than this cannot place anyone at or
# away from an outlet: a desktop browser without GPS reports its Wi-Fi / IP
# location with an accuracy of kilometres. Such a check is UNCLEAR, never AWAY.
# (myHR refuses an attendance reading worse than 100 m; here a reading is only
# logged, so the limit is looser.)
ACCURACY_LIMIT_M = 1000.0

# The window the checks run in, IST (the app follows the same times).
WINDOW_START = time(10, 0)
WINDOW_END = time(19, 30)
# A check the app started just before 19:30 may arrive a moment after it.
_WINDOW_GRACE = timedelta(minutes=5)
MAX_CHECKS_PER_HOUR = 3

_EARTH_RADIUS_M = 6_371_000.0
_ITEMS_PER_PERSON = 200

LOCATED = "LOCATED"
STATUSES = ("AT_OUTLET", "AWAY", "UNCLEAR", "NO_OUTLET_LOCATION", "DENIED", "UNAVAILABLE")

_ACTIVE = """
    ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
"""


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (the same formula as myHR's attendance)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lat = p2 - p1
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def outlet_label(outlet_name: str | None, dealer_name: str | None) -> str | None:
    """"Utkal Mahindra – CDA": the dealership and the outlet, unless the outlet's name already says both."""
    outlet = (outlet_name or "").strip()
    dealer = (dealer_name or "").strip()
    if not outlet:
        return dealer or None
    if not dealer or dealer.lower() in outlet.lower():
        return outlet
    return f"{dealer} – {outlet}"


def classify(
    *, latitude: float, longitude: float, accuracy_m: float | None, outlets: list[dict[str, Any]],
) -> dict[str, Any]:
    """The result of one located check against the PC's outlets (those with coordinates)."""
    if not outlets:
        return {"status": "NO_OUTLET_LOCATION", "distance_m": None, "outlet": None}
    nearest = min(
        outlets,
        key=lambda o: haversine_m(latitude, longitude, float(o["latitude"]), float(o["longitude"])),
    )
    distance = haversine_m(latitude, longitude, float(nearest["latitude"]), float(nearest["longitude"]))
    if accuracy_m is None or accuracy_m > ACCURACY_LIMIT_M:
        status = "UNCLEAR"
    elif distance <= GEOFENCE_RADIUS_M:
        status = "AT_OUTLET"
    else:
        status = "AWAY"
    return {"status": status, "distance_m": round(distance, 1), "outlet": nearest}


def in_window(now: datetime) -> bool:
    local = now.astimezone(IST)
    start = datetime.combine(local.date(), WINDOW_START, IST)
    end = datetime.combine(local.date(), WINDOW_END, IST) + _WINDOW_GRACE
    return start <= local < end


def pc_outlets_with_coordinates(connection: Connection, *, tenant_id: str, actor_id: str) -> list[dict[str, Any]]:
    """Every outlet the person is assigned to as PC in the Project (a dealer-wide or
    Project-wide assignment covers its outlets), with coordinates."""
    rows = connection.execute(
        text(
            f"""
            SELECT DISTINCT o.outlet_id, o.outlet_name, d.dealer_name, o.latitude, o.longitude
            FROM auditcore.business_assignments ba
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id = ba.tenant_id
             AND (ba.dealer_id IS NULL
                  OR (o.dealer_id = ba.dealer_id AND (ba.outlet_id IS NULL OR o.outlet_id = ba.outlet_id)))
            JOIN auditcore.dealers d ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
            WHERE ba.tenant_id = :t AND ba.security_actor_id = :a AND upper(ba.business_role_code) = 'PC'
              AND {_ACTIVE}
              AND o.latitude IS NOT NULL AND o.longitude IS NOT NULL
            """
        ),
        {"t": tenant_id, "a": actor_id},
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- record a check


class LocationCheckCommand(BaseModel):
    """What the device reports. ``outcome`` LOCATED carries the reading; DENIED /
    UNAVAILABLE say the device could not give one (still logged)."""

    outcome: Literal["LOCATED", "DENIED", "UNAVAILABLE"]
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracyM: float | None = Field(default=None, ge=0, le=10_000_000)
    capturedAt: datetime | None = None
    source: Literal["NATIVE", "BROWSER"]
    deviceType: Literal["WEB", "MOBILE_WEB", "ANDROID", "IOS"] | None = None
    detail: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _located_has_coordinates(self) -> LocationCheckCommand:
        if self.outcome == LOCATED and (self.latitude is None or self.longitude is None):
            raise ValueError("A located check needs latitude and longitude.")
        return self


class LocationCheckResult(BaseModel):
    recorded: bool
    # Why nothing was recorded: OUTSIDE_HOURS or HOURLY_LIMIT.
    reason: str | None = None
    checkId: str | None = None
    status: str | None = None
    distanceM: float | None = None
    outletId: str | None = None
    outletName: str | None = None
    accuracyM: float | None = None
    radiusM: float = GEOFENCE_RADIUS_M
    accuracyLimitM: float = ACCURACY_LIMIT_M
    checkedAtUtc: datetime | None = None


def record_location_check(
    connection: Connection, *, tenant_id: str, actor_id: str, command: LocationCheckCommand,
    now: datetime | None = None,
) -> LocationCheckResult:
    """Classify and log one check. The tenant context is already set and the caller is a PC."""
    now = now or connection.execute(text("SELECT now()")).scalar_one()
    if not in_window(now):
        return LocationCheckResult(recorded=False, reason="OUTSIDE_HOURS")
    # One check at a time per person, so two tabs cannot both pass the hourly count.
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"p2-location-check:{tenant_id}:{actor_id}"},
    )
    local = now.astimezone(IST)
    hour_start = local.replace(minute=0, second=0, microsecond=0)
    done = connection.execute(
        text(
            "SELECT count(*) FROM auditcore.p2_location_checks "
            "WHERE tenant_id = :t AND actor_id = :a AND checked_at_utc >= :s AND checked_at_utc < :e"
        ),
        {"t": tenant_id, "a": actor_id, "s": hour_start, "e": hour_start + timedelta(hours=1)},
    ).scalar_one()
    if done >= MAX_CHECKS_PER_HOUR:
        return LocationCheckResult(recorded=False, reason="HOURLY_LIMIT")

    distance: float | None = None
    outlet: dict[str, Any] | None = None
    if command.outcome == LOCATED:
        assert command.latitude is not None and command.longitude is not None
        outlets = pc_outlets_with_coordinates(connection, tenant_id=tenant_id, actor_id=actor_id)
        result = classify(latitude=command.latitude, longitude=command.longitude,
                          accuracy_m=command.accuracyM, outlets=outlets)
        status, distance, outlet = result["status"], result["distance_m"], result["outlet"]
    else:
        status = command.outcome
    label = outlet_label(outlet["outlet_name"], outlet["dealer_name"]) if outlet else None
    located = command.outcome == LOCATED
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_location_checks (
                tenant_id, actor_id, checked_at_utc, captured_at_utc, check_status, latitude, longitude,
                accuracy_m, distance_m, nearest_outlet_id, nearest_outlet_name, location_source, device_type,
                client_detail
            ) VALUES (:t, :a, :now, :captured, :status, :lat, :lon, :acc, :dist, :outlet_id, :outlet_name,
                      :source, :device, :detail)
            RETURNING check_id, checked_at_utc
            """
        ),
        {
            "t": tenant_id, "a": actor_id, "now": now, "captured": command.capturedAt, "status": status,
            "lat": round(command.latitude, 6) if located and command.latitude is not None else None,
            "lon": round(command.longitude, 6) if located and command.longitude is not None else None,
            "acc": round(command.accuracyM, 1) if command.accuracyM is not None else None,
            "dist": distance, "outlet_id": outlet["outlet_id"] if outlet else None, "outlet_name": label,
            "source": command.source, "device": command.deviceType,
            "detail": (command.detail or "").strip()[:300] or None,
        },
    ).mappings().one()
    return LocationCheckResult(
        recorded=True, checkId=str(row["check_id"]), status=status, distanceM=distance,
        outletId=str(outlet["outlet_id"]) if outlet else None, outletName=label,
        accuracyM=command.accuracyM, checkedAtUtc=row["checked_at_utc"],
    )


@router.post("/location-checks", response_model=LocationCheckResult)
def post_location_check(
    tenant_id: str,
    command: LocationCheckCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> LocationCheckResult:
    check_p2_permission(tenant_id=tenant_id, human_principal=human_principal,
                        authorization_client=authorization_client, permission_key=_READ_PERMISSION)
    set_tenant_context(connection, tenant_id)
    if caller_role(connection, tenant_id=tenant_id, actor_id=human_principal.subject) != "PC":
        # Only a PC is checked (product owner 2026-11-05).
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    return record_location_check(connection, tenant_id=tenant_id, actor_id=human_principal.subject,
                                 command=command)


# --------------------------------------------------------------------------- the log


def ist_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(IST).date()


def _visible_pcs(connection: Connection, *, tenant_id: str, viewer_id: str) -> set[str] | None:
    """The PCs whose checks a TL / PM / Executive sees: all of them with a Project-wide
    assignment (None), otherwise the PCs of the dealerships and outlets they cover."""
    project_wide = connection.execute(
        text(
            f"SELECT 1 FROM auditcore.business_assignments ba WHERE ba.tenant_id = :t "
            f"AND ba.security_actor_id = :v AND ba.dealer_id IS NULL AND {_ACTIVE} LIMIT 1"
        ),
        {"t": tenant_id, "v": viewer_id},
    ).scalar_one_or_none()
    if project_wide is not None:
        return None
    return set(connection.execute(
        text(
            f"""
            SELECT DISTINCT pc.security_actor_id
            FROM auditcore.business_assignments pc
            JOIN auditcore.business_assignments ba
              ON ba.tenant_id = pc.tenant_id AND ba.security_actor_id = :v AND {_ACTIVE}
            WHERE pc.tenant_id = :t AND upper(pc.business_role_code) = 'PC'
              AND (pc.dealer_id IS NULL
                   OR (pc.dealer_id = ba.dealer_id
                       AND (ba.outlet_id IS NULL OR pc.outlet_id IS NULL OR pc.outlet_id = ba.outlet_id)))
            """
        ),
        {"t": tenant_id, "v": viewer_id},
    ).scalars().all())


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


def build_location_log(
    connection: Connection, *, tenant_id: str, day: date, away_only: bool, visible: set[str] | None,
) -> dict[str, Any]:
    """The day's checks per PC: how many, how many AWAY, the last one, and the list."""
    start = datetime.combine(day, time(0, 0), IST)
    rows = connection.execute(
        text(
            """
            SELECT check_id, actor_id, checked_at_utc, captured_at_utc, check_status, accuracy_m, distance_m,
                   nearest_outlet_id, nearest_outlet_name, location_source, device_type
            FROM auditcore.p2_location_checks
            WHERE tenant_id = :t AND checked_at_utc >= :s AND checked_at_utc < :e
            ORDER BY actor_id, checked_at_utc DESC
            """
        ),
        {"t": tenant_id, "s": start, "e": start + timedelta(days=1)},
    ).mappings().all()
    names = known_names(connection, tenant_id=tenant_id)
    people: dict[str, dict[str, Any]] = {}
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        actor = str(row["actor_id"])
        if visible is not None and actor not in visible:
            continue
        status = str(row["check_status"])
        item = {
            "checkId": str(row["check_id"]),
            "checkedAtUtc": row["checked_at_utc"],
            "capturedAtUtc": row["captured_at_utc"],
            "status": status,
            "distanceM": _number(row["distance_m"]),
            "accuracyM": _number(row["accuracy_m"]),
            "outletId": str(row["nearest_outlet_id"]) if row["nearest_outlet_id"] else None,
            "outletName": row["nearest_outlet_name"],
            "source": row["location_source"],
            "deviceType": row["device_type"],
        }
        person = people.setdefault(actor, {
            "actorId": actor, "name": names.get(actor), "checks": 0,
            "counts": dict.fromkeys(STATUSES, 0), "last": item, "items": [],
        })
        person["checks"] += 1
        person["counts"][status] = person["counts"].get(status, 0) + 1
        totals["checks"] += 1
        totals[status] += 1
        if (not away_only or status == "AWAY") and len(person["items"]) < _ITEMS_PER_PERSON:
            person["items"].append(item)
    listed = [p for p in people.values() if not away_only or p["counts"]["AWAY"]]
    for person in listed:
        person["away"] = person["counts"]["AWAY"]
    listed.sort(key=lambda p: (-p["away"], (p["name"] or p["actorId"]).lower()))
    return {
        "day": day.isoformat(),
        "timezone": "Asia/Kolkata",
        "awayOnly": away_only,
        "radiusM": GEOFENCE_RADIUS_M,
        "accuracyLimitM": ACCURACY_LIMIT_M,
        "window": {"from": WINDOW_START.strftime("%H:%M"), "to": WINDOW_END.strftime("%H:%M")},
        "totals": {"pcs": len(people), "checks": totals["checks"],
                   **{status: totals[status] for status in STATUSES}},
        "people": listed,
    }


@router.get("/location-checks")
def get_location_checks(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    day: Annotated[date | None, Query()] = None,
    away_only: Annotated[bool, Query(alias="awayOnly")] = False,
) -> dict[str, Any]:
    """The log for the Executives of the Project (and an admin acting in it)."""
    visible = _reader_scope(connection, tenant_id=tenant_id, human_principal=human_principal,
                            authorization_client=authorization_client)
    return build_location_log(connection, tenant_id=tenant_id, day=day or ist_today(),
                              away_only=away_only, visible=visible)


def _reader_scope(
    connection: Connection, *, tenant_id: str, human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
) -> set[str] | None:
    """Refuse anyone but an Executive of the Project (or an admin acting in it) and
    return the PCs they see (None: all). Sets the tenant context."""
    decision = check_p2_permission(tenant_id=tenant_id, human_principal=human_principal,
                                   authorization_client=authorization_client, permission_key=_READ_PERMISSION)
    set_tenant_context(connection, tenant_id)
    if str(getattr(decision, "role_key", "") or "").upper() in _ADMIN_FUNCTIONAL_ROLES:
        return None
    if caller_role(connection, tenant_id=tenant_id, actor_id=human_principal.subject) not in _VIEWER_ROLES:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    return _visible_pcs(connection, tenant_id=tenant_id, viewer_id=human_principal.subject)


def _admin_project(connection: Connection, *, tenant_id: str, admin: HumanAdminRequest) -> None:
    """SuperAdmin only, and the Project must exist. Sets the tenant context."""
    if not admin.admin_context.is_super_admin:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    set_tenant_context(connection, tenant_id)
    if connection.execute(text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :t"),
                          {"t": tenant_id}).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Project not found.")


@admin_router.get("/location-checks")
def admin_get_location_checks(
    tenant_id: str,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    day: Annotated[date | None, Query()] = None,
    away_only: Annotated[bool, Query(alias="awayOnly")] = False,
) -> dict[str, Any]:
    """The same log for SuperAdmin, one Project at a time."""
    _admin_project(connection, tenant_id=tenant_id, admin=admin)
    return build_location_log(connection, tenant_id=tenant_id, day=day or ist_today(),
                              away_only=away_only, visible=None)


# --------------------------------------------------------------------------- away instances over a range

# Product owner, 9 Nov 2026: "can we show just consolidate view date wise and
# roughly how far they were during each instance.. you should report the one for
# which they were away more than 5 km". Every AWAY check further than minKm from
# the PC's nearest outlet, over a range of India dates, with a summary per date and
# per PC. An UNCLEAR check (a reading too inaccurate to place anyone) is never AWAY,
# so it is never listed here however far it points.
DEFAULT_AWAY_MIN_KM = 5.0
MAX_RANGE_DAYS = 92  # 'to' at most 92 days after 'from' (93 days, about a quarter)
_MAX_INSTANCES = 5000


def _km(metres: Any) -> float:
    """Metres as kilometres rounded to 0.1 (half up: 5,150 m is 5.2 km)."""
    return float((Decimal(str(metres)) / 1000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def away_range(*, date_from: date | None, date_to: date | None, today: date) -> tuple[date, date]:
    """The India dates asked for. Neither given: this calendar month so far. Only
    'to': from the first of its month. Only 'from': up to today."""
    last = date_to or today
    first = date_from or last.replace(day=1)
    if last < first:
        raise HTTPException(status_code=422, detail="'to' is before 'from'.")
    if (last - first).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"Choose at most {MAX_RANGE_DAYS + 1} days at a time.")
    return first, last


def build_away_report(
    connection: Connection, *, tenant_id: str, date_from: date, date_to: date, min_km: float,
    visible: set[str] | None,
) -> dict[str, Any]:
    """The AWAY checks further than ``min_km`` between two India dates (both included):
    every instance by date and time, and a summary per date and per PC."""
    start = datetime.combine(date_from, time(0, 0), IST)
    end = datetime.combine(date_to + timedelta(days=1), time(0, 0), IST)
    rows = connection.execute(
        text(
            """
            SELECT check_id, actor_id, checked_at_utc, distance_m, accuracy_m, nearest_outlet_id,
                   nearest_outlet_name, location_source, device_type
            FROM auditcore.p2_location_checks
            WHERE tenant_id = :t AND check_status = 'AWAY' AND distance_m > :min_m
              AND checked_at_utc >= :s AND checked_at_utc < :e
              AND (CAST(:everyone AS boolean) OR actor_id = ANY(CAST(:actors AS varchar[])))
            ORDER BY checked_at_utc, actor_id, check_id
            """
        ),
        {"t": tenant_id, "min_m": Decimal(str(min_km)) * 1000, "s": start, "e": end,
         "everyone": visible is None, "actors": sorted(visible or ())},
    ).mappings().all()
    names = known_names(connection, tenant_id=tenant_id) if rows else {}

    instances: list[dict[str, Any]] = []
    days: dict[str, dict[str, Any]] = {}
    people: dict[str, dict[str, Any]] = {}
    for row in rows:
        actor = str(row["actor_id"])
        local = row["checked_at_utc"].astimezone(IST)
        day_key = local.date().isoformat()
        km = _km(row["distance_m"])
        item = {
            "checkId": str(row["check_id"]),
            "actorId": actor,
            "name": names.get(actor),
            "date": day_key,
            "time": local.strftime("%H:%M"),
            "checkedAtUtc": row["checked_at_utc"],
            "distanceM": _number(row["distance_m"]),
            "distanceKm": km,
            "outletId": str(row["nearest_outlet_id"]) if row["nearest_outlet_id"] else None,
            "outletName": row["nearest_outlet_name"],
            "accuracyM": _number(row["accuracy_m"]),
            "source": row["location_source"],
            "deviceType": row["device_type"],
        }
        if len(instances) < _MAX_INSTANCES:
            instances.append(item)
        summary = days.setdefault(day_key, {"date": day_key, "instances": 0, "actors": set(), "furthestKm": 0.0})
        summary["instances"] += 1
        summary["actors"].add(actor)
        summary["furthestKm"] = max(summary["furthestKm"], km)
        person = people.setdefault(actor, {
            "actorId": actor, "name": names.get(actor), "instances": 0, "dates": set(), "furthestKm": 0.0,
        })
        person["instances"] += 1
        person["dates"].add(day_key)
        person["furthestKm"] = max(person["furthestKm"], km)
        person["last"] = item  # rows come oldest first

    by_date = [
        {"date": d["date"], "instances": d["instances"], "pcs": len(d["actors"]), "furthestKm": d["furthestKm"]}
        for d in days.values()
    ]
    by_pc = [
        {
            "actorId": p["actorId"], "name": p["name"], "instances": p["instances"], "days": len(p["dates"]),
            "furthestKm": p["furthestKm"],
            "last": {key: p["last"][key] for key in ("date", "time", "checkedAtUtc", "distanceKm", "outletName")},
        }
        for p in people.values()
    ]
    by_pc.sort(key=lambda p: (-p["instances"], -p["furthestKm"], (p["name"] or p["actorId"]).lower()))
    return {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "timezone": "Asia/Kolkata",
        "minKm": min_km,
        "radiusM": GEOFENCE_RADIUS_M,
        "accuracyLimitM": ACCURACY_LIMIT_M,
        "maxRangeDays": MAX_RANGE_DAYS + 1,
        "totals": {
            "instances": len(rows), "pcs": len(people), "days": len(days),
            "furthestKm": max((p["furthestKm"] for p in by_pc), default=None),
        },
        "byDate": by_date,
        "byPc": by_pc,
        "instances": instances,
        "truncated": len(rows) > len(instances),
    }


_MinKm = Annotated[float, Query(alias="minKm", gt=0, le=1000)]


@router.get("/location-checks:away")
def get_location_checks_away(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    min_km: _MinKm = DEFAULT_AWAY_MIN_KM,
) -> dict[str, Any]:
    """Away more than minKm, date-wise, for the Executives of the Project (product owner 2026-11-09)."""
    visible = _reader_scope(connection, tenant_id=tenant_id, human_principal=human_principal,
                            authorization_client=authorization_client)
    first, last = away_range(date_from=date_from, date_to=date_to, today=ist_today())
    return build_away_report(connection, tenant_id=tenant_id, date_from=first, date_to=last, min_km=min_km,
                             visible=visible)


@admin_router.get("/location-checks:away")
def admin_get_location_checks_away(
    tenant_id: str,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    min_km: _MinKm = DEFAULT_AWAY_MIN_KM,
) -> dict[str, Any]:
    """The same for SuperAdmin, one Project at a time."""
    _admin_project(connection, tenant_id=tenant_id, admin=admin)
    first, last = away_range(date_from=date_from, date_to=date_to, today=ist_today())
    return build_away_report(connection, tenant_id=tenant_id, date_from=first, date_to=last, min_km=min_km,
                             visible=None)
