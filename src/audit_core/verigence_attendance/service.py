from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection

from audit_core.verigence_attendance.domain import (
    GeoPoint,
    finance_approval_required,
    distance_meters,
)
from audit_core.verigence_attendance.errors import AttendanceRuleError
from audit_core.verigence_attendance.repository import (
    create_reimbursement,
    employee_for_user,
    insert_attendance_event,
    lock_attendance_day,
    month_claim_total,
    reimbursement_threshold,
    update_attendance_day,
)
from audit_core.verigence_attendance.settings import get_settings
from audit_core.verigence_attendance.storage import AttendanceStorage, AttendanceStorageError

_ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
_ALLOWED_RECEIPT_TYPES = _ALLOWED_PHOTO_TYPES | {"application/pdf"}
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_RECEIPT_BYTES = 15 * 1024 * 1024
_MAX_CAPTURE_AGE_SECONDS = 120


def _fresh_capture(captured_at: datetime) -> datetime:
    if captured_at.tzinfo is None:
        raise AttendanceRuleError(
            "CAPTURE_TIME_INVALID",
            "Capture timestamp must include timezone information.",
            status_code=400,
        )
    normalized = captured_at.astimezone(UTC)
    age = abs((datetime.now(UTC) - normalized).total_seconds())
    if age > _MAX_CAPTURE_AGE_SECONDS:
        raise AttendanceRuleError(
            "CAPTURE_NOT_FRESH",
            "Attendance evidence must be captured live.",
            status_code=400,
        )
    return normalized


def _photo_bytes(data: bytes, content_type: str) -> bytes:
    if content_type not in _ALLOWED_PHOTO_TYPES:
        raise AttendanceRuleError(
            "PHOTO_TYPE_INVALID",
            "Attendance requires a live photo.",
            status_code=400,
        )
    if not data or len(data) > _MAX_PHOTO_BYTES:
        raise AttendanceRuleError(
            "PHOTO_SIZE_INVALID",
            "Attendance photo is empty or too large.",
            status_code=400,
        )
    return data


def record_attendance(
    connection: Connection,
    *,
    user_id: str,
    event_type: str,
    latitude: float,
    longitude: float,
    accuracy_meters: float,
    captured_at: datetime,
    photo_data: bytes,
    photo_content_type: str,
    storage: AttendanceStorage,
) -> dict[str, object]:
    if event_type not in {"CHECK_IN", "CHECK_OUT"}:
        raise ValueError("Unsupported attendance event")
    if accuracy_meters < 0:
        raise AttendanceRuleError(
            "LOCATION_ACCURACY_INVALID",
            "Location accuracy is invalid.",
            status_code=400,
        )

    employee = employee_for_user(connection, user_id)
    if (
        employee.get("work_location_id") is None
        or employee.get("work_latitude") is None
        or employee.get("work_longitude") is None
        or employee.get("work_location_status") != "ACTIVE"
    ):
        raise AttendanceRuleError(
            "WORK_LOCATION_REQUIRED",
            "An active work location is required before attendance can be recorded.",
            status_code=409,
        )

    radius = int(employee.get("geofence_radius_meters") or 500)
    distance = distance_meters(
        GeoPoint(latitude=latitude, longitude=longitude),
        GeoPoint(
            latitude=float(employee["work_latitude"]),
            longitude=float(employee["work_longitude"]),
        ),
    )
    if distance > radius:
        raise AttendanceRuleError(
            "OUTSIDE_GEOFENCE",
            f"You are outside the allowed {radius} metre work-location geofence.",
            status_code=409,
        )

    evidence_time = _fresh_capture(captured_at)
    photo = _photo_bytes(photo_data, photo_content_type)
    business_date = evidence_time.astimezone(ZoneInfo(get_settings().timezone_iana)).date()
    day = lock_attendance_day(
        connection,
        employee_id=UUID(str(employee["employee_id"])),
        attendance_date=business_date,
    )

    if event_type == "CHECK_IN":
        if day.get("check_in_at_utc") is not None:
            raise AttendanceRuleError("ALREADY_CHECKED_IN", "You are already checked in today.")
    else:
        if day.get("check_in_at_utc") is None:
            raise AttendanceRuleError("CHECK_IN_REQUIRED", "Check in before checking out.")
        if day.get("check_out_at_utc") is not None:
            raise AttendanceRuleError("ALREADY_CHECKED_OUT", "You are already checked out today.")
        if evidence_time <= day["check_in_at_utc"]:
            raise AttendanceRuleError(
                "CHECK_OUT_TIME_INVALID",
                "Check-out time must be after check-in.",
                status_code=400,
            )

    event_id = uuid4()
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/heic": "heic",
        "image/heif": "heif",
    }[photo_content_type]
    object_key = (
        f"employee-attendance/{employee['employee_id']}/{business_date.isoformat()}/"
        f"{event_id}.{extension}"
    )
    digest = hashlib.sha256(photo).hexdigest()
    try:
        storage.put(object_key=object_key, data=photo, content_type=photo_content_type)
    except AttendanceStorageError as exc:
        raise AttendanceRuleError(
            "ATTENDANCE_STORAGE_UNAVAILABLE",
            "Could not store attendance photo. Please retry.",
            status_code=503,
        ) from exc

    insert_attendance_event(
        connection,
        attendance_day_id=UUID(str(day["attendance_day_id"])),
        employee_id=UUID(str(employee["employee_id"])),
        event_id=event_id,
        event_type=event_type,
        captured_at=evidence_time,
        latitude=latitude,
        longitude=longitude,
        accuracy_meters=accuracy_meters,
        work_location_id=UUID(str(employee["work_location_id"])),
        distance_meters=distance,
        radius_meters=radius,
        photo_object_key=object_key,
        photo_sha256=digest,
    )
    update_attendance_day(
        connection,
        attendance_day_id=UUID(str(day["attendance_day_id"])),
        event_type=event_type,
        captured_at=evidence_time,
    )
    return {
        "attendanceEventId": event_id,
        "attendanceDate": business_date,
        "eventType": event_type,
        "capturedAtUtc": evidence_time,
        "distanceMeters": round(distance, 2),
        "geofenceRadiusMeters": radius,
    }


def submit_reimbursement(
    connection: Connection,
    *,
    user_id: str,
    expense_date,
    category: str,
    amount: Decimal,
    description: str | None,
    receipt_data: bytes | None,
    receipt_content_type: str | None,
    storage: AttendanceStorage,
) -> dict[str, object]:
    employee = employee_for_user(connection, user_id)
    normalized_category = category.strip().upper()
    if normalized_category not in {"TRAVEL", "FOOD", "OTHER"}:
        raise AttendanceRuleError(
            "REIMBURSEMENT_CATEGORY_INVALID",
            "Reimbursement category must be Travel, Food or Other.",
            status_code=400,
        )
    if amount <= 0:
        raise AttendanceRuleError(
            "REIMBURSEMENT_AMOUNT_INVALID",
            "Reimbursement amount must be greater than zero.",
            status_code=400,
        )

    receipt_key: str | None = None
    receipt_sha: str | None = None
    if receipt_data is not None:
        content_type = (receipt_content_type or "").strip().lower()
        if content_type not in _ALLOWED_RECEIPT_TYPES:
            raise AttendanceRuleError(
                "RECEIPT_TYPE_INVALID",
                "Receipt must be a photo or PDF.",
                status_code=400,
            )
        if not receipt_data or len(receipt_data) > _MAX_RECEIPT_BYTES:
            raise AttendanceRuleError(
                "RECEIPT_SIZE_INVALID",
                "Receipt is empty or too large.",
                status_code=400,
            )
        receipt_id = uuid4()
        extension = "pdf" if content_type == "application/pdf" else content_type.split("/")[-1]
        receipt_key = (
            f"employee-reimbursements/{employee['employee_id']}/"
            f"{expense_date.isoformat()}/{receipt_id}.{extension}"
        )
        receipt_sha = hashlib.sha256(receipt_data).hexdigest()
        try:
            storage.put(
                object_key=receipt_key,
                data=receipt_data,
                content_type=content_type,
            )
        except AttendanceStorageError as exc:
            raise AttendanceRuleError(
                "REIMBURSEMENT_STORAGE_UNAVAILABLE",
                "Could not store reimbursement receipt. Please retry.",
                status_code=503,
            ) from exc

    employee_id = UUID(str(employee["employee_id"]))
    month_total = month_claim_total(
        connection,
        employee_id=employee_id,
        expense_date=expense_date,
    )
    threshold = reimbursement_threshold(connection)
    needs_finance = finance_approval_required(month_total, amount, threshold)
    return create_reimbursement(
        connection,
        employee_id=employee_id,
        expense_date=expense_date,
        category=normalized_category,
        amount=amount,
        description=description,
        receipt_object_key=receipt_key,
        receipt_sha256=receipt_sha,
        finance_required=needs_finance,
    )
