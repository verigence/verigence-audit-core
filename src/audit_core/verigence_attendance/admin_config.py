from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from audit_core.verigence_attendance.errors import (
    AttendanceNotFoundError,
    AttendanceRuleError,
)

_ALLOWED_CONFIG_KEYS = {
    "attendance.default_geofence_meters",
    "reimbursement.finance_threshold_inr",
    "payroll.working_days_per_week",
    "payroll.weekly_off_iso_weekdays",
    "payroll.fixed_deductions_prorated",
}


def list_configuration(connection: Connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            """
            SELECT config_key,config_value_json
            FROM verigence_attendance.module_configuration
            ORDER BY config_key
            """
        )
    ).mappings()
    return {str(row["config_key"]): row["config_value_json"] for row in rows}


def _validate_config(key: str, value: Any) -> Any:
    if key not in _ALLOWED_CONFIG_KEYS:
        raise AttendanceRuleError(
            "CONFIG_KEY_INVALID",
            "This Attendance configuration key is not supported.",
            status_code=400,
        )
    if key == "attendance.default_geofence_meters":
        if not isinstance(value, int) or isinstance(value, bool) or not 50 <= value <= 5000:
            raise AttendanceRuleError(
                "CONFIG_VALUE_INVALID",
                "Default geofence must be between 50 and 5000 metres.",
                status_code=400,
            )
    elif key == "reimbursement.finance_threshold_inr":
        try:
            amount = Decimal(str(value))
        except Exception as exc:
            raise AttendanceRuleError(
                "CONFIG_VALUE_INVALID",
                "Reimbursement threshold must be numeric.",
                status_code=400,
            ) from exc
        if amount < 0:
            raise AttendanceRuleError(
                "CONFIG_VALUE_INVALID",
                "Reimbursement threshold cannot be negative.",
                status_code=400,
            )
        value = float(amount)
    elif key == "payroll.working_days_per_week":
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 7:
            raise AttendanceRuleError(
                "CONFIG_VALUE_INVALID",
                "Working days per week must be between 1 and 7.",
                status_code=400,
            )
    elif key == "payroll.weekly_off_iso_weekdays":
        if (
            not isinstance(value, list)
            or any(
                not isinstance(item, int)
                or isinstance(item, bool)
                or not 1 <= item <= 7
                for item in value
            )
            or len(set(value)) != len(value)
        ):
            raise AttendanceRuleError(
                "CONFIG_VALUE_INVALID",
                "Weekly offs must be unique ISO weekdays from 1 to 7.",
                status_code=400,
            )
    elif key == "payroll.fixed_deductions_prorated" and not isinstance(value, bool):
        raise AttendanceRuleError(
            "CONFIG_VALUE_INVALID",
            "Deduction proration must be true or false.",
            status_code=400,
        )
    return value


def update_configuration(
    connection: Connection,
    *,
    key: str,
    value: Any,
    actor_user_id: str,
) -> dict[str, Any]:
    normalized = _validate_config(key, value)
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.module_configuration (
                config_key,config_value_json,updated_by_user_id,updated_at_utc
            ) VALUES (
                :key,CAST(:value AS jsonb),CAST(:actor AS uuid),now()
            )
            ON CONFLICT (config_key) DO UPDATE SET
                config_value_json=EXCLUDED.config_value_json,
                updated_by_user_id=EXCLUDED.updated_by_user_id,
                updated_at_utc=now()
            """
        ),
        {
            "key": key,
            "value": json.dumps(normalized),
            "actor": actor_user_id,
        },
    )
    return list_configuration(connection)


def list_work_locations(connection: Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT location_id,location_code,location_name,address_text,
                       latitude,longitude,geofence_radius_meters,status
                FROM verigence_attendance.work_locations
                ORDER BY lower(location_name),location_code
                """
            )
        ).mappings()
    ]


def create_work_location(
    connection: Connection,
    *,
    code: str,
    name: str,
    address: str | None,
    latitude: float,
    longitude: float,
    radius_meters: int,
) -> dict[str, Any]:
    location_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.work_locations (
                location_id,location_code,location_name,address_text,
                latitude,longitude,geofence_radius_meters
            ) VALUES (
                :location_id,:code,:name,:address,:latitude,:longitude,:radius
            )
            """
        ),
        {
            "location_id": location_id,
            "code": code.strip(),
            "name": name.strip(),
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
            "radius": radius_meters,
        },
    )
    return next(
        item for item in list_work_locations(connection) if item["location_id"] == location_id
    )


def list_leave_types(connection: Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT leave_type_id,leave_code,leave_name,is_paid,
                       default_entitlement_days,allow_half_day,status
                FROM verigence_attendance.leave_types
                ORDER BY leave_code
                """
            )
        ).mappings()
    ]


def create_leave_type(
    connection: Connection,
    *,
    code: str,
    name: str,
    is_paid: bool,
    entitlement_days: Decimal,
    allow_half_day: bool,
) -> dict[str, Any]:
    if entitlement_days < 0:
        raise AttendanceRuleError(
            "LEAVE_ENTITLEMENT_INVALID",
            "Leave entitlement cannot be negative.",
            status_code=400,
        )
    leave_type_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.leave_types (
                leave_type_id,leave_code,leave_name,is_paid,
                default_entitlement_days,allow_half_day
            ) VALUES (
                :leave_type_id,:code,:name,:is_paid,:entitlement,:allow_half_day
            )
            """
        ),
        {
            "leave_type_id": leave_type_id,
            "code": code.strip().upper(),
            "name": name.strip(),
            "is_paid": is_paid,
            "entitlement": entitlement_days,
            "allow_half_day": allow_half_day,
        },
    )
    return next(
        item for item in list_leave_types(connection) if item["leave_type_id"] == leave_type_id
    )


def list_holidays(connection: Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT holiday_id,holiday_date,holiday_name,work_location_id,status
                FROM verigence_attendance.holidays
                ORDER BY holiday_date,holiday_name
                """
            )
        ).mappings()
    ]


def create_holiday(
    connection: Connection,
    *,
    holiday_date: date,
    name: str,
    work_location_id: UUID | None,
) -> dict[str, Any]:
    if work_location_id is not None:
        exists = connection.execute(
            text(
                """
                SELECT 1 FROM verigence_attendance.work_locations
                WHERE location_id=:location_id AND status='ACTIVE'
                """
            ),
            {"location_id": work_location_id},
        ).scalar_one_or_none()
        if exists is None:
            raise AttendanceNotFoundError("Work location not found.")
    holiday_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.holidays (
                holiday_id,holiday_date,holiday_name,work_location_id
            ) VALUES (
                :holiday_id,:holiday_date,:name,:work_location_id
            )
            """
        ),
        {
            "holiday_id": holiday_id,
            "holiday_date": holiday_date,
            "name": name.strip(),
            "work_location_id": work_location_id,
        },
    )
    return next(item for item in list_holidays(connection) if item["holiday_id"] == holiday_id)
