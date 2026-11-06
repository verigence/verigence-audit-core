"""Where each PC, TL and PM opens Verigence from (product owner, 2026-11-06).

"Keep a watch on the locations from which an id is accessed. One location is the
location to which the PC is tagged, geofenced: logging in within 500 metres is
that location. Keep count of the times PC/TL/PM log in from a given location till
22 Nov 2026, and turn the top 3 into a readable address. Only for SuperAdmin and
CEO/Executive."

    POST /p2/v1/tenants/{tenant_id}/access-locations          the app, after a sign-in or a resume
    GET  /p2/v1/tenants/{tenant_id}/access-locations          per-person summary (Executive)
    GET  /v1/admin/tenants/{tenant_id}/access-locations       the same, for SuperAdmin

Each access is classified against the outlets the person is assigned to in the
Project (any role), with the same 500 m geofence as the DealGuard checks and
myHR attendance: within it, the access is at their tagged outlet. Every other
located access (with the device's accuracy within 1 km) is grouped into places:
accesses within 500 m of each other are one place. Each person's three places
with the most accesses get a readable address from Google, looked up once per
place and kept.

Recording stops after WATCH_UNTIL (22 Nov 2026, IST); the summary stays readable.
Accesses from one person less than RECENT_MINUTES apart are counted once (two
tabs, a quick reopen).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
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
from audit_core.errors import AuditCoreError
from audit_core.outlet_geocoding import get_outlet_geocoder
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import caller_role, known_names
from audit_core.uc03_p2_access import check_p2_permission
from audit_core.uc03_p2_location_checks import (
    ACCURACY_LIMIT_M,
    GEOFENCE_RADIUS_M,
    haversine_m,
    outlet_label,
)
from audit_core.uc03_p2_object_keys import IST

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-access-locations"])
admin_router = APIRouter(prefix="/v1/admin/tenants/{tenant_id}", tags=["uc03-phase2-access-locations-admin"])

_READ_PERMISSION = "audit.journey.read"
WATCHED_ROLES = frozenset({"PC", "TL", "PM"})
_VIEWER_ROLES = frozenset({"EXECUTIVE"})
_ADMIN_FUNCTIONAL_ROLES = frozenset({"SUPER_ADMIN"})

# The watch runs from the day it went live to the end of 22 Nov 2026, IST.
WATCH_FROM = date(2026, 11, 6)
WATCH_UNTIL = date(2026, 11, 22)
RECENT_MINUTES = 10
TOP_PLACES = 3
# Google lookups one summary may start; the rest are filled in on a later view.
_MAX_LOOKUPS_PER_VIEW = 12

_ACTIVE = """
    ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
"""


def watch_open(now: datetime) -> bool:
    return now.astimezone(IST).date() <= WATCH_UNTIL


def tagged_outlets(connection: Connection, *, tenant_id: str, actor_id: str) -> list[dict[str, Any]]:
    """Every outlet the person is assigned to in the Project in any role (a dealer-wide or
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
            WHERE ba.tenant_id = :t AND ba.security_actor_id = :a AND {_ACTIVE}
              AND o.latitude IS NOT NULL AND o.longitude IS NOT NULL
            """
        ),
        {"t": tenant_id, "a": actor_id},
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- record an access


class AccessLocationCommand(BaseModel):
    kind: Literal["SIGN_IN", "RESUME"]
    outcome: Literal["LOCATED", "DENIED", "UNAVAILABLE"]
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracyM: float | None = Field(default=None, ge=0, le=10_000_000)
    capturedAt: datetime | None = None
    source: Literal["NATIVE", "BROWSER"] | None = None
    deviceType: Literal["WEB", "MOBILE_WEB", "ANDROID", "IOS"] | None = None

    @model_validator(mode="after")
    def _located_has_coordinates(self) -> AccessLocationCommand:
        if self.outcome == "LOCATED" and (self.latitude is None or self.longitude is None):
            raise ValueError("A located access needs latitude and longitude.")
        return self


class AccessLocationResult(BaseModel):
    recorded: bool
    # Why nothing was recorded: WATCH_ENDED, NOT_WATCHED (role), RECENT.
    reason: str | None = None
    accessId: str | None = None
    atTaggedOutlet: bool | None = None


def record_access(
    connection: Connection, *, tenant_id: str, actor_id: str, role: str, command: AccessLocationCommand,
    now: datetime | None = None,
) -> AccessLocationResult:
    now = now or connection.execute(text("SELECT now()")).scalar_one()
    if not watch_open(now):
        return AccessLocationResult(recorded=False, reason="WATCH_ENDED")
    if role not in WATCHED_ROLES:
        return AccessLocationResult(recorded=False, reason="NOT_WATCHED")
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"p2-access-location:{tenant_id}:{actor_id}"},
    )
    recent = connection.execute(
        text(
            "SELECT 1 FROM auditcore.p2_access_locations WHERE tenant_id = :t AND actor_id = :a "
            "AND accessed_at_utc > :since LIMIT 1"
        ),
        {"t": tenant_id, "a": actor_id, "since": now - timedelta(minutes=RECENT_MINUTES)},
    ).scalar_one_or_none()
    if recent is not None:
        return AccessLocationResult(recorded=False, reason="RECENT")

    nearest: dict[str, Any] | None = None
    distance: float | None = None
    located = command.outcome == "LOCATED"
    if located:
        assert command.latitude is not None and command.longitude is not None
        outlets = tagged_outlets(connection, tenant_id=tenant_id, actor_id=actor_id)
        if outlets:
            nearest = min(outlets, key=lambda o: haversine_m(
                command.latitude, command.longitude, float(o["latitude"]), float(o["longitude"])))  # type: ignore[arg-type]
            distance = round(haversine_m(command.latitude, command.longitude,
                                         float(nearest["latitude"]), float(nearest["longitude"])), 1)
    at_tagged = bool(
        located and distance is not None and distance <= GEOFENCE_RADIUS_M
        and command.accuracyM is not None and command.accuracyM <= ACCURACY_LIMIT_M
    )
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_access_locations (
                tenant_id, actor_id, role_code, accessed_at_utc, captured_at_utc, access_kind, outcome,
                latitude, longitude, accuracy_m, nearest_outlet_id, nearest_outlet_name, distance_m,
                at_tagged_outlet, location_source, device_type
            ) VALUES (:t, :a, :role, :now, :captured, :kind, :outcome, :lat, :lon, :acc, :outlet_id,
                      :outlet_name, :dist, :tagged, :source, :device)
            RETURNING access_id
            """
        ),
        {
            "t": tenant_id, "a": actor_id, "role": role, "now": now, "captured": command.capturedAt,
            "kind": command.kind, "outcome": command.outcome,
            "lat": round(command.latitude, 6) if located and command.latitude is not None else None,
            "lon": round(command.longitude, 6) if located and command.longitude is not None else None,
            "acc": round(command.accuracyM, 1) if command.accuracyM is not None else None,
            "outlet_id": nearest["outlet_id"] if nearest else None,
            "outlet_name": outlet_label(nearest["outlet_name"], nearest["dealer_name"]) if nearest else None,
            "dist": distance, "tagged": at_tagged, "source": command.source, "device": command.deviceType,
        },
    ).scalar_one()
    return AccessLocationResult(recorded=True, accessId=str(row), atTaggedOutlet=at_tagged)


@router.post("/access-locations", response_model=AccessLocationResult)
def post_access_location(
    tenant_id: str,
    command: AccessLocationCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> AccessLocationResult:
    check_p2_permission(tenant_id=tenant_id, human_principal=human_principal,
                        authorization_client=authorization_client, permission_key=_READ_PERMISSION)
    set_tenant_context(connection, tenant_id)
    role = caller_role(connection, tenant_id=tenant_id, actor_id=human_principal.subject) or ""
    return record_access(connection, tenant_id=tenant_id, actor_id=human_principal.subject, role=role,
                         command=command)


# --------------------------------------------------------------------------- the summary


@dataclass
class Place:
    latitude: float
    longitude: float
    count: int = 0
    last_at: datetime | None = None
    members: list[tuple[float, float]] = field(default_factory=list)

    def add(self, latitude: float, longitude: float, at: datetime) -> None:
        self.members.append((latitude, longitude))
        self.count += 1
        self.latitude = sum(m[0] for m in self.members) / self.count
        self.longitude = sum(m[1] for m in self.members) / self.count
        if self.last_at is None or at > self.last_at:
            self.last_at = at


def group_places(points: list[tuple[float, float, datetime]], radius_m: float = GEOFENCE_RADIUS_M) -> list[Place]:
    """Accesses within radius_m of a place's centre are one place; most accesses first."""
    places: list[Place] = []
    for latitude, longitude, at in points:
        home = next(
            (p for p in places if haversine_m(latitude, longitude, p.latitude, p.longitude) <= radius_m), None
        )
        if home is None:
            home = Place(latitude=latitude, longitude=longitude)
            places.append(home)
        home.add(latitude, longitude, at)
    places.sort(key=lambda p: (-p.count, -(p.last_at.timestamp() if p.last_at else 0)))
    return places


def _key(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


class _Addresses:
    """Readable addresses for places, kept per Project; Google is asked only for a new place."""

    def __init__(self, connection: Connection, tenant_id: str) -> None:
        self.connection = connection
        self.tenant_id = tenant_id
        self.lookups = 0
        self._geocoder: Any = None
        self._geocoder_missing = False

    def _google(self) -> Any:
        if self._geocoder is None and not self._geocoder_missing:
            try:
                self._geocoder = get_outlet_geocoder()
            except AuditCoreError:
                self._geocoder_missing = True
        return self._geocoder

    def address(self, latitude: float, longitude: float) -> str | None:
        lat_key, lng_key = _key(latitude), _key(longitude)
        kept = self.connection.execute(
            text(
                "SELECT address FROM auditcore.p2_place_addresses "
                "WHERE tenant_id = :t AND lat_key = :la AND lng_key = :lo"
            ),
            {"t": self.tenant_id, "la": lat_key, "lo": lng_key},
        ).scalar_one_or_none()
        if kept is not None:
            return str(kept)
        if self.lookups >= _MAX_LOOKUPS_PER_VIEW:
            return None
        google = self._google()
        if google is None:
            return None
        self.lookups += 1
        found = google.address_for(float(lat_key), float(lng_key))
        if found:
            self.connection.execute(
                text(
                    "INSERT INTO auditcore.p2_place_addresses (tenant_id, lat_key, lng_key, address) "
                    "VALUES (:t, :la, :lo, :ad) ON CONFLICT (tenant_id, lat_key, lng_key) "
                    "DO UPDATE SET address = EXCLUDED.address, looked_up_at = now()"
                ),
                {"t": self.tenant_id, "la": lat_key, "lo": lng_key, "ad": found},
            )
        return found


def build_access_summary(
    connection: Connection, *, tenant_id: str, start: date, end: date, role: str | None,
) -> dict[str, Any]:
    first = datetime.combine(start, time(0, 0), IST)
    last = datetime.combine(end + timedelta(days=1), time(0, 0), IST)
    rows = connection.execute(
        text(
            """
            SELECT actor_id, role_code, accessed_at_utc, outcome, latitude, longitude, accuracy_m,
                   nearest_outlet_name, distance_m, at_tagged_outlet
            FROM auditcore.p2_access_locations
            WHERE tenant_id = :t AND accessed_at_utc >= :s AND accessed_at_utc < :e
              AND (CAST(:r AS varchar) IS NULL OR role_code = :r)
            ORDER BY accessed_at_utc
            """
        ),
        {"t": tenant_id, "s": first, "e": last, "r": role},
    ).mappings().all()
    names = known_names(connection, tenant_id=tenant_id)
    by_person: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_person[str(row["actor_id"])].append(row)
    addresses = _Addresses(connection, tenant_id)
    people = []
    for actor, items in by_person.items():
        tagged = [r for r in items if r["at_tagged_outlet"]]
        located = [r for r in items if r["outcome"] == "LOCATED"]
        unclear = [r for r in located if r["accuracy_m"] is None or float(r["accuracy_m"]) > ACCURACY_LIMIT_M]
        elsewhere = [
            r for r in located
            if not r["at_tagged_outlet"] and r["accuracy_m"] is not None and float(r["accuracy_m"]) <= ACCURACY_LIMIT_M
        ]
        places = group_places([(float(r["latitude"]), float(r["longitude"]), r["accessed_at_utc"]) for r in elsewhere])
        top = []
        for place in places[:TOP_PLACES]:
            top.append({
                "latitude": round(place.latitude, 5),
                "longitude": round(place.longitude, 5),
                "count": place.count,
                "lastAtUtc": place.last_at,
                "address": addresses.address(place.latitude, place.longitude),
            })
        tagged_names = sorted({str(r["nearest_outlet_name"]) for r in tagged if r["nearest_outlet_name"]})
        nearest_names = sorted({str(r["nearest_outlet_name"]) for r in items if r["nearest_outlet_name"]})
        people.append({
            "actorId": actor,
            "name": names.get(actor),
            "role": items[-1]["role_code"],
            "accesses": len(items),
            "atTaggedOutlet": len(tagged),
            "taggedOutlets": tagged_names or nearest_names,
            "elsewhere": len(elsewhere),
            "places": len(places),
            "topPlaces": top,
            "otherPlacesAccesses": sum(p.count for p in places[TOP_PLACES:]),
            "unclear": len(unclear),
            "noLocation": sum(1 for r in items if r["outcome"] != "LOCATED"),
            "lastAccessAtUtc": items[-1]["accessed_at_utc"],
        })
    people.sort(key=lambda p: (-p["elsewhere"], (p["name"] or p["actorId"]).lower()))
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "watchFrom": WATCH_FROM.isoformat(),
        "watchUntil": WATCH_UNTIL.isoformat(),
        "radiusM": GEOFENCE_RADIUS_M,
        "accuracyLimitM": ACCURACY_LIMIT_M,
        "totals": {
            "people": len(people),
            "accesses": len(rows),
            "atTaggedOutlet": sum(p["atTaggedOutlet"] for p in people),
            "elsewhere": sum(p["elsewhere"] for p in people),
            "noLocation": sum(p["noLocation"] for p in people),
        },
        "people": people,
    }


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    today = datetime.now(UTC).astimezone(IST).date()
    first = start or WATCH_FROM
    last = end if end is not None else max(first, min(today, WATCH_UNTIL))
    if last < first:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'.")
    if (last - first).days > 62:
        raise HTTPException(status_code=422, detail="Choose at most 62 days.")
    return first, last


@router.get("/access-locations")
def get_access_locations(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    role: Annotated[Literal["PC", "TL", "PM"] | None, Query()] = None,
) -> dict[str, Any]:
    """Executive of the Project (and SuperAdmin acting in it) only."""
    decision = check_p2_permission(tenant_id=tenant_id, human_principal=human_principal,
                                   authorization_client=authorization_client, permission_key=_READ_PERMISSION)
    set_tenant_context(connection, tenant_id)
    admin = str(getattr(decision, "role_key", "") or "").upper() in _ADMIN_FUNCTIONAL_ROLES
    if not admin and caller_role(
        connection, tenant_id=tenant_id, actor_id=human_principal.subject
    ) not in _VIEWER_ROLES:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    first, last = _range(start, end)
    return build_access_summary(connection, tenant_id=tenant_id, start=first, end=last, role=role)


@admin_router.get("/access-locations")
def admin_get_access_locations(
    tenant_id: str,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    role: Annotated[Literal["PC", "TL", "PM"] | None, Query()] = None,
) -> dict[str, Any]:
    if not admin.admin_context.is_super_admin:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    set_tenant_context(connection, tenant_id)
    if connection.execute(text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :t"),
                          {"t": tenant_id}).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    first, last = _range(start, end)
    return build_access_summary(connection, tenant_id=tenant_id, start=first, end=last, role=role)
