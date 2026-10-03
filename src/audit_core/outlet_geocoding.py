"""Find an outlet's coordinates from its address (Google Geocoding), for an admin to confirm.

This only answers "where is this address". It writes nothing: the admin reviews the result on the
map and saves it through the existing outlet update. One request per click, a short timeout, no
retries, and a small rate limit so a stuck button or a script cannot run up the Google bill.
"""

import os
import threading
import time
from collections import deque
from functools import lru_cache
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from audit_core.dependencies import HumanAdminRequest, require_project_admin_request
from audit_core.errors import (
    AuditCoreError,
    BusinessValidationError,
    DependencyUnavailableError,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["dealers"])

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT_SECONDS = 5.0
# At most this many Google calls per minute across the whole service.
_MAX_CALLS_PER_MINUTE = 20
# Outlets are in India; a point outside this box is a wrong match, not a location.
_INDIA_LAT = (6.0, 38.0)
_INDIA_LNG = (68.0, 98.0)


class OutletGeocodeRequest(BaseModel):
    addressText: str | None = Field(default=None, max_length=400)
    city: str | None = Field(default=None, max_length=160)
    stateRegion: str | None = Field(default=None, max_length=160)
    postalCode: str | None = Field(default=None, max_length=40)


class OutletGeocodeResponse(BaseModel):
    latitude: float
    longitude: float
    formattedAddress: str
    googlePlaceId: str | None
    # ROOFTOP is a precise building match; everything else is an area estimate.
    precision: str
    approximate: bool
    partialMatch: bool
    resultCount: int


class OutletGeocoder:
    def __init__(
        self,
        *,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        if not api_key.strip():
            raise ValueError("A Google Maps API key is required")
        self._api_key = api_key.strip()
        self._client = httpx.Client(timeout=_TIMEOUT_SECONDS, transport=transport)
        self._clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _take_slot(self) -> None:
        now = self._clock()
        with self._lock:
            while self._calls and now - self._calls[0] >= 60.0:
                self._calls.popleft()
            if len(self._calls) >= _MAX_CALLS_PER_MINUTE:
                raise AuditCoreError(
                    "VAC-SYS-003",
                    429,
                    "Too many requests",
                    "Too many location lookups just now. Wait a minute and try again.",
                )
            self._calls.append(now)

    def geocode(self, address: str) -> OutletGeocodeResponse:
        self._take_slot()
        try:
            response = self._client.get(
                _GEOCODE_URL,
                params={"address": address, "components": "country:IN", "key": self._api_key},
            )
        except httpx.HTTPError as exc:
            logger.warning("outlet_geocode_failed", reason="endpoint_unavailable")
            raise DependencyUnavailableError(
                detail="Google location lookup is not reachable. Try again, or pin the location."
            ) from exc
        if response.status_code != 200:
            logger.warning("outlet_geocode_failed", http_status=response.status_code)
            raise DependencyUnavailableError(
                detail="Google location lookup is unavailable. Try again, or pin the location."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DependencyUnavailableError(
                detail="Google location lookup returned an unreadable answer."
            ) from exc

        status = body.get("status") if isinstance(body, dict) else None
        if status == "ZERO_RESULTS":
            raise BusinessValidationError(
                detail="No location was found for this address. Add a landmark or the PIN code, "
                "or pin the location on site."
            )
        if status != "OK":
            # Only Google's status code is logged; its message can mention the key.
            logger.warning("outlet_geocode_failed", google_status=status)
            raise DependencyUnavailableError(
                detail="Google location lookup is not available right now. Pin the location instead."
            )
        results = body.get("results") or []
        try:
            first = results[0]
            location = first["geometry"]["location"]
            latitude = float(location["lat"])
            longitude = float(location["lng"])
            precision = str(first["geometry"].get("location_type") or "APPROXIMATE")
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise DependencyUnavailableError(
                detail="Google location lookup returned an unreadable answer."
            ) from exc
        if not (
            _INDIA_LAT[0] <= latitude <= _INDIA_LAT[1] and _INDIA_LNG[0] <= longitude <= _INDIA_LNG[1]
        ):
            raise BusinessValidationError(
                detail="The address matched a place outside India. Check the address."
            )
        place_id = first.get("place_id")
        return OutletGeocodeResponse(
            latitude=round(latitude, 6),
            longitude=round(longitude, 6),
            formattedAddress=str(first.get("formatted_address") or address),
            googlePlaceId=place_id if isinstance(place_id, str) else None,
            precision=precision,
            approximate=precision != "ROOFTOP",
            partialMatch=bool(first.get("partial_match", False)),
            resultCount=len(results),
        )


@lru_cache
def _shared_geocoder() -> OutletGeocoder:
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        raise DependencyUnavailableError(
            detail="Location lookup is not set up yet (no Google Maps key). Pin the location instead."
        )
    return OutletGeocoder(api_key=key)


def get_outlet_geocoder() -> OutletGeocoder:
    return _shared_geocoder()


def _address(payload: OutletGeocodeRequest) -> str:
    parts = [payload.addressText, payload.city, payload.stateRegion, payload.postalCode]
    address = ", ".join(" ".join(p.split()) for p in parts if p and p.strip())
    if len(address) < 5:
        raise BusinessValidationError(
            detail="Enter the outlet address, city and PIN code before looking up the location."
        )
    return address


@router.post("/outlet-location/geocode", response_model=OutletGeocodeResponse)
def geocode_outlet_address(
    tenant_id: str,
    payload: OutletGeocodeRequest,
    _admin: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    geocoder: Annotated[OutletGeocoder, Depends(get_outlet_geocoder)],
) -> OutletGeocodeResponse:
    """Where is this address? Read-only; the admin confirms and saves through the outlet editor."""
    return geocoder.geocode(_address(payload))
