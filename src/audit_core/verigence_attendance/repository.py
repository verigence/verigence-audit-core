from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from audit_core.verigence_attendance.errors import (
    AttendanceNotFoundError,
    AttendanceRuleError,
)


def employee_for_user(connection: Connection, user_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT e.*,w.location_name,w.latitude AS work_latitude,
                   w.longitude AS work_longitude,w.geofence_radius_meters,
                   w.status AS work_location_status
            FROM verigence_attendance.employees e
            LEFT JOIN verigence_attendance.work_locations w
              ON w.location_id=e.work_location_id
            WHERE e.security_user_id=CAST(:user_id AS uuid)
            """
        ),
        {"user_id": user_id},
    ).mappings().first()
    if row is None or row["employment_status"] != "ACTIVE":
        raise AttendanceNotFoundError("Active employee profile not found.")
    return dict(row)


def employee_by_id(connection: Connection, employee_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT e.*,w.location_name,w.latitude AS work_latitude,
                   w.longitude AS work_longitude,w.geofence_radius_meters,
                   w.status AS work_location_status
            FROM verigence_attendance.employees e
            LEFT JOIN verigence_attendance.work_locations w
              ON w.location_id=e.work_location_id
            WHERE e.employee_id=:employee_id
            """
        ),
        {"employee_id": employee_id},
    ).mappings().first()
    if row is None:
        raise AttendanceNotFoundError("Employee not found.")
    return dict(row)


def list_employees(connection: Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT e.*,w.location_name
                FROM verigence_attendance.employees e
                LEFT JOIN verigence_attendance.work_locations w
                  ON w.location_id=e.work_location_id
                ORDER BY lower(e.display_name),e.employee_code
                """
            )
        ).mappings()
    ]


def create_employee(
    connection: Connection,
    *,
    user_id: UUID,
    employee_code: str,
    display_name: str,
    primary_email: str | None,
    mobile: str | None,
    joining_date: date,
    tl_user_id: UUID | None,
    pmo_user_id: UUID | None,
    project_tenant_id: UUID | None,
    work_location_id: UUID | None,
    bank_account_masked: str | None,
    pan_masked: str | None,
    aadhaar_masked: str | None,
    salary: dict[str, Decimal],
    actor_user_id: str,
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
            raise AttendanceRuleError(
                "WORK_LOCATION_INVALID",
                "Selected work location is not active.",
                status_code=400,
            )
    employee_id = uuid4()
    try:
        connection.execute(
            text(
                """
                INSERT INTO verigence_attendance.employees (
                    employee_id,security_user_id,employee_code,display_name,
                    primary_email,mobile,joining_date,tl_user_id,pmo_user_id,
                    project_tenant_id,work_location_id,
                    bank_account_masked,pan_masked,aadhaar_masked
                ) VALUES (
                    :employee_id,:security_user_id,:employee_code,:display_name,
                    :primary_email,:mobile,:joining_date,:tl_user_id,:pmo_user_id,
                    :project_tenant_id,:work_location_id,
                    :bank_account_masked,:pan_masked,:aadhaar_masked
                )
                """
            ),
            {
                "employee_id": employee_id,
                "security_user_id": user_id,
                "employee_code": employee_code.strip(),
                "display_name": display_name.strip(),
                "primary_email": primary_email,
                "mobile": mobile,
                "joining_date": joining_date,
                "tl_user_id": tl_user_id,
                "pmo_user_id": pmo_user_id,
                "project_tenant_id": project_tenant_id,
                "work_location_id": work_location_id,
                "bank_account_masked": bank_account_masked,
                "pan_masked": pan_masked,
                "aadhaar_masked": aadhaar_masked,
            },
        )
    except IntegrityError as exc:
        raise AttendanceRuleError(
            "EMPLOYEE_ALREADY_EXISTS",
            "Employee code or Security user is already onboarded.",
            status_code=409,
        ) from exc

    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.salary_structures (
                employee_id,effective_from,basic_salary,hra,allowances,
                other_earnings,fixed_deductions,created_by_user_id
            ) VALUES (
                :employee_id,:effective_from,:basic_salary,:hra,:allowances,
                :other_earnings,:fixed_deductions,CAST(:actor AS uuid)
            )
            """
        ),
        {
            "employee_id": employee_id,
            "effective_from": joining_date,
            **salary,
            "actor": actor_user_id,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.leave_balances (
                employee_id,leave_type_id,leave_year,opening_days,
                entitled_days,adjustment_days,used_days
            )
            SELECT :employee_id,leave_type_id,:leave_year,0,
                   default_entitlement_days,0,0
            FROM verigence_attendance.leave_types
            WHERE status='ACTIVE'
            ON CONFLICT (employee_id,leave_type_id,leave_year) DO NOTHING
            """
        ),
        {"employee_id": employee_id, "leave_year": joining_date.year},
    )
    return employee_by_id(connection, employee_id)


def attendance_history(
    connection: Connection,
    *,
    employee_id: UUID,
    limit: int = 31,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT attendance_date,status,present_fraction,
                       check_in_at_utc,check_out_at_utc
                FROM verigence_attendance.attendance_days
                WHERE employee_id=:employee_id
                ORDER BY attendance_date DESC
                LIMIT :limit
                """
            ),
            {"employee_id": employee_id, "limit": limit},
        ).mappings()
    ]


def lock_attendance_day(
    connection: Connection,
    *,
    employee_id: UUID,
    attendance_date: date,
) -> dict[str, Any]:
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.attendance_days (
                employee_id,attendance_date,status,present_fraction
            ) VALUES (:employee_id,:attendance_date,'NOT_STARTED',0)
            ON CONFLICT (employee_id,attendance_date) DO NOTHING
            """
        ),
        {"employee_id": employee_id, "attendance_date": attendance_date},
    )
    row = connection.execute(
        text(
            """
            SELECT * FROM verigence_attendance.attendance_days
            WHERE employee_id=:employee_id AND attendance_date=:attendance_date
            FOR UPDATE
            """
        ),
        {"employee_id": employee_id, "attendance_date": attendance_date},
    ).mappings().one()
    return dict(row)


def insert_attendance_event(
    connection: Connection,
    *,
    attendance_day_id: UUID,
    employee_id: UUID,
    event_id: UUID,
    event_type: str,
    captured_at: datetime,
    latitude: float,
    longitude: float,
    accuracy_meters: float,
    work_location_id: UUID,
    distance_meters: float,
    radius_meters: int,
    photo_object_key: str,
    photo_sha256: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.attendance_events (
                attendance_event_id,attendance_day_id,employee_id,event_type,
                captured_at_utc,latitude,longitude,accuracy_meters,work_location_id,
                distance_meters,geofence_radius_meters,geofence_result,
                photo_object_key,photo_sha256,capture_source
            ) VALUES (
                :event_id,:day_id,:employee_id,:event_type,:captured_at,
                :latitude,:longitude,:accuracy,:work_location_id,:distance,
                :radius,'WITHIN',:photo_key,:photo_sha256,'LIVE_CAMERA'
            )
            """
        ),
        {
            "event_id": event_id,
            "day_id": attendance_day_id,
            "employee_id": employee_id,
            "event_type": event_type,
            "captured_at": captured_at,
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": accuracy_meters,
            "work_location_id": work_location_id,
            "distance": distance_meters,
            "radius": radius_meters,
            "photo_key": photo_object_key,
            "photo_sha256": photo_sha256,
        },
    )


def update_attendance_day(
    connection: Connection,
    *,
    attendance_day_id: UUID,
    event_type: str,
    captured_at: datetime,
) -> None:
    if event_type == "CHECK_IN":
        connection.execute(
            text(
                """
                UPDATE verigence_attendance.attendance_days
                SET check_in_at_utc=:captured_at,status='CHECKED_IN',
                    present_fraction=0,updated_at_utc=now()
                WHERE attendance_day_id=:day_id
                """
            ),
            {"captured_at": captured_at, "day_id": attendance_day_id},
        )
    else:
        connection.execute(
            text(
                """
                UPDATE verigence_attendance.attendance_days
                SET check_out_at_utc=:captured_at,status='COMPLETED',
                    present_fraction=1,updated_at_utc=now()
                WHERE attendance_day_id=:day_id
                """
            ),
            {"captured_at": captured_at, "day_id": attendance_day_id},
        )


def create_leave_request(
    connection: Connection,
    *,
    employee_id: UUID,
    leave_type_id: UUID,
    start_date: date,
    end_date: date,
    requested_days: Decimal,
    reason: str | None,
) -> dict[str, Any]:
    leave_type = connection.execute(
        text(
            """
            SELECT leave_type_id,leave_name,status
            FROM verigence_attendance.leave_types
            WHERE leave_type_id=:leave_type_id
            """
        ),
        {"leave_type_id": leave_type_id},
    ).mappings().first()
    if leave_type is None or leave_type["status"] != "ACTIVE":
        raise AttendanceRuleError("LEAVE_TYPE_INVALID", "Leave type is not active.", status_code=400)
    overlap = connection.execute(
        text(
            """
            SELECT 1 FROM verigence_attendance.leave_requests
            WHERE employee_id=:employee_id
              AND status IN ('PENDING_OPERATIONAL','PENDING_HR','APPROVED')
              AND daterange(start_date,end_date,'[]') && daterange(:start_date,:end_date,'[]')
            LIMIT 1
            """
        ),
        {
            "employee_id": employee_id,
            "start_date": start_date,
            "end_date": end_date,
        },
    ).scalar_one_or_none()
    if overlap is not None:
        raise AttendanceRuleError("LEAVE_OVERLAP", "A leave request already overlaps these dates.")
    leave_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.leave_requests (
                leave_request_id,employee_id,leave_type_id,start_date,end_date,
                requested_days,reason
            ) VALUES (
                :leave_id,:employee_id,:leave_type_id,:start_date,:end_date,
                :requested_days,:reason
            )
            """
        ),
        {
            "leave_id": leave_id,
            "employee_id": employee_id,
            "leave_type_id": leave_type_id,
            "start_date": start_date,
            "end_date": end_date,
            "requested_days": requested_days,
            "reason": reason,
        },
    )
    return leave_request(connection, leave_id)


def leave_request(connection: Connection, leave_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT l.*,e.display_name,lt.leave_name
            FROM verigence_attendance.leave_requests l
            JOIN verigence_attendance.employees e ON e.employee_id=l.employee_id
            JOIN verigence_attendance.leave_types lt ON lt.leave_type_id=l.leave_type_id
            WHERE l.leave_request_id=:leave_id
            """
        ),
        {"leave_id": leave_id},
    ).mappings().first()
    if row is None:
        raise AttendanceNotFoundError("Leave request not found.")
    return dict(row)


def list_leave_for_employee(connection: Connection, employee_id: UUID) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT l.*,e.display_name,lt.leave_name
                FROM verigence_attendance.leave_requests l
                JOIN verigence_attendance.employees e ON e.employee_id=l.employee_id
                JOIN verigence_attendance.leave_types lt ON lt.leave_type_id=l.leave_type_id
                WHERE l.employee_id=:employee_id
                ORDER BY l.created_at_utc DESC
                """
            ),
            {"employee_id": employee_id},
        ).mappings()
    ]


def list_team_leave(connection: Connection, actor_user_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT l.*,e.display_name,lt.leave_name
                FROM verigence_attendance.leave_requests l
                JOIN verigence_attendance.employees e ON e.employee_id=l.employee_id
                JOIN verigence_attendance.leave_types lt ON lt.leave_type_id=l.leave_type_id
                WHERE l.status='PENDING_OPERATIONAL'
                  AND (
                    e.tl_user_id=CAST(:actor AS uuid)
                    OR e.pmo_user_id=CAST(:actor AS uuid)
                  )
                ORDER BY l.created_at_utc
                """
            ),
            {"actor": actor_user_id},
        ).mappings()
    ]


def decide_team_leave(
    connection: Connection,
    *,
    leave_id: UUID,
    actor_user_id: str,
    decision: str,
    comment: str | None,
) -> dict[str, Any]:
    row = leave_request(connection, leave_id)
    employee = employee_by_id(connection, UUID(str(row["employee_id"])))
    if actor_user_id not in {
        str(employee["tl_user_id"]) if employee["tl_user_id"] else "",
        str(employee["pmo_user_id"]) if employee["pmo_user_id"] else "",
    }:
        raise AttendanceRuleError("TEAM_SCOPE_DENIED", "Leave request is outside your assigned team.", status_code=403)
    if row["status"] != "PENDING_OPERATIONAL":
        raise AttendanceRuleError("LEAVE_STATE_INVALID", "Leave request is not awaiting TL/PMO action.")
    next_status = "PENDING_HR" if decision == "APPROVE" else "REJECTED"
    connection.execute(
        text(
            """
            UPDATE verigence_attendance.leave_requests
            SET status=:status,updated_at_utc=now()
            WHERE leave_request_id=:leave_id
            """
        ),
        {"status": next_status, "leave_id": leave_id},
    )
    role = "TL" if str(employee.get("tl_user_id") or "") == actor_user_id else "PMO"
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.approval_actions (
                entity_type,entity_id,stage,decision,actor_user_id,actor_role,comment
            ) VALUES ('LEAVE',:leave_id,'TL_OR_PMO',:decision,CAST(:actor AS uuid),:role,:comment)
            """
        ),
        {
            "leave_id": leave_id,
            "decision": decision,
            "actor": actor_user_id,
            "role": role,
            "comment": comment,
        },
    )
    return leave_request(connection, leave_id)


def decide_hr_leave(
    connection: Connection,
    *,
    leave_id: UUID,
    actor_user_id: str,
    decision: str,
    comment: str | None,
) -> dict[str, Any]:
    row = leave_request(connection, leave_id)
    if row["status"] != "PENDING_HR":
        raise AttendanceRuleError("LEAVE_STATE_INVALID", "Leave request is not awaiting HR validation.")
    next_status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    connection.execute(
        text(
            """
            UPDATE verigence_attendance.leave_requests
            SET status=:status,updated_at_utc=now()
            WHERE leave_request_id=:leave_id
            """
        ),
        {"status": next_status, "leave_id": leave_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.approval_actions (
                entity_type,entity_id,stage,decision,actor_user_id,actor_role,comment
            ) VALUES ('LEAVE',:leave_id,'HR',:decision,CAST(:actor AS uuid),'HRADMIN',:comment)
            """
        ),
        {
            "leave_id": leave_id,
            "decision": decision,
            "actor": actor_user_id,
            "comment": comment,
        },
    )
    return leave_request(connection, leave_id)


def reimbursement_threshold(connection: Connection) -> Decimal:
    value = connection.execute(
        text(
            """
            SELECT config_value_json
            FROM verigence_attendance.module_configuration
            WHERE config_key='reimbursement.finance_threshold_inr'
            """
        )
    ).scalar_one_or_none()
    return Decimal(str(value if value is not None else 3000))


def month_claim_total(
    connection: Connection,
    *,
    employee_id: UUID,
    expense_date: date,
) -> Decimal:
    value = connection.execute(
        text(
            """
            SELECT COALESCE(sum(amount),0)
            FROM verigence_attendance.reimbursement_claims
            WHERE employee_id=:employee_id
              AND date_trunc('month',expense_date)=date_trunc('month',CAST(:expense_date AS date))
              AND status NOT IN ('REJECTED','CANCELLED')
            """
        ),
        {"employee_id": employee_id, "expense_date": expense_date},
    ).scalar_one()
    return Decimal(str(value))


def create_reimbursement(
    connection: Connection,
    *,
    employee_id: UUID,
    expense_date: date,
    category: str,
    amount: Decimal,
    description: str | None,
    receipt_object_key: str | None,
    receipt_sha256: str | None,
    finance_required: bool,
) -> dict[str, Any]:
    claim_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.reimbursement_claims (
                claim_id,employee_id,expense_date,category,amount,description,
                receipt_object_key,receipt_sha256,finance_approval_required
            ) VALUES (
                :claim_id,:employee_id,:expense_date,:category,:amount,:description,
                :receipt_key,:receipt_sha256,:finance_required
            )
            """
        ),
        {
            "claim_id": claim_id,
            "employee_id": employee_id,
            "expense_date": expense_date,
            "category": category,
            "amount": amount,
            "description": description,
            "receipt_key": receipt_object_key,
            "receipt_sha256": receipt_sha256,
            "finance_required": finance_required,
        },
    )
    return reimbursement(connection, claim_id)


def reimbursement(connection: Connection, claim_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT c.*,e.display_name
            FROM verigence_attendance.reimbursement_claims c
            JOIN verigence_attendance.employees e ON e.employee_id=c.employee_id
            WHERE c.claim_id=:claim_id
            """
        ),
        {"claim_id": claim_id},
    ).mappings().first()
    if row is None:
        raise AttendanceNotFoundError("Reimbursement claim not found.")
    return dict(row)


def list_reimbursements_for_employee(
    connection: Connection,
    employee_id: UUID,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT c.*,e.display_name
                FROM verigence_attendance.reimbursement_claims c
                JOIN verigence_attendance.employees e ON e.employee_id=c.employee_id
                WHERE c.employee_id=:employee_id
                ORDER BY c.created_at_utc DESC
                """
            ),
            {"employee_id": employee_id},
        ).mappings()
    ]


def list_pm_team_reimbursements(
    connection: Connection,
    actor_user_id: str,
) -> list[dict[str, Any]]:
    """Read-only team claims for employees explicitly assigned to this PM/PMO.

    This intentionally does not grant approval authority. Scope is enforced in SQL,
    so a normal PC/TL cannot enumerate another employee's reimbursement claims.
    """
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT c.*,e.display_name
                FROM verigence_attendance.reimbursement_claims c
                JOIN verigence_attendance.employees e ON e.employee_id=c.employee_id
                WHERE e.employment_status='ACTIVE'
                  AND e.pmo_user_id=CAST(:actor AS uuid)
                ORDER BY c.expense_date DESC,c.created_at_utc DESC
                """
            ),
            {"actor": actor_user_id},
        ).mappings()
    ]


def list_reimbursements_by_status(
    connection: Connection,
    status: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT c.*,e.display_name
                FROM verigence_attendance.reimbursement_claims c
                JOIN verigence_attendance.employees e ON e.employee_id=c.employee_id
                WHERE c.status=:status
                ORDER BY c.created_at_utc
                """
            ),
            {"status": status},
        ).mappings()
    ]


def decide_reimbursement(
    connection: Connection,
    *,
    claim_id: UUID,
    actor_user_id: str,
    actor_role: str,
    stage: str,
    decision: str,
    comment: str | None,
) -> dict[str, Any]:
    row = reimbursement(connection, claim_id)
    expected = "PENDING_HR" if stage == "HR" else "PENDING_FINANCE"
    if row["status"] != expected:
        raise AttendanceRuleError(
            "REIMBURSEMENT_STATE_INVALID",
            f"Claim is not awaiting {stage} approval.",
        )
    if decision == "REJECT":
        next_status = "REJECTED"
    elif stage == "HR" and bool(row["finance_approval_required"]):
        next_status = "PENDING_FINANCE"
    else:
        next_status = "APPROVED"
    connection.execute(
        text(
            """
            UPDATE verigence_attendance.reimbursement_claims
            SET status=:status,updated_at_utc=now()
            WHERE claim_id=:claim_id
            """
        ),
        {"status": next_status, "claim_id": claim_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO verigence_attendance.approval_actions (
                entity_type,entity_id,stage,decision,actor_user_id,actor_role,comment
            ) VALUES (
                'REIMBURSEMENT',:claim_id,:stage,:decision,
                CAST(:actor AS uuid),:actor_role,:comment
            )
            """
        ),
        {
            "claim_id": claim_id,
            "stage": stage,
            "decision": decision,
            "actor": actor_user_id,
            "actor_role": actor_role,
            "comment": comment,
        },
    )
    return reimbursement(connection, claim_id)


def list_payslips(connection: Connection, employee_id: UUID) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT p.payslip_id,p.pdf_object_key,p.generated_at_utc,
                       r.payroll_month,i.net_amount
                FROM verigence_attendance.payslips p
                JOIN verigence_attendance.payroll_items i
                  ON i.payroll_item_id=p.payroll_item_id
                JOIN verigence_attendance.payroll_runs r
                  ON r.payroll_run_id=i.payroll_run_id
                WHERE p.employee_id=:employee_id AND r.status='FINALIZED'
                ORDER BY r.payroll_month DESC
                """
            ),
            {"employee_id": employee_id},
        ).mappings()
    ]



def leave_balances_for_employee(
    connection: Connection,
    *,
    employee_id: UUID,
    leave_year: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT lt.leave_type_id,lt.leave_code,lt.leave_name,lt.is_paid,
                       lt.allow_half_day,
                       COALESCE(lb.opening_days,0) AS opening_days,
                       COALESCE(lb.entitled_days,lt.default_entitlement_days) AS entitled_days,
                       COALESCE(lb.adjustment_days,0) AS adjustment_days,
                       COALESCE(lb.used_days,0) AS used_days,
                       (
                         COALESCE(lb.opening_days,0)
                         + COALESCE(lb.entitled_days,lt.default_entitlement_days)
                         + COALESCE(lb.adjustment_days,0)
                         - COALESCE(lb.used_days,0)
                       ) AS available_days
                FROM verigence_attendance.leave_types lt
                LEFT JOIN verigence_attendance.leave_balances lb
                  ON lb.leave_type_id=lt.leave_type_id
                 AND lb.employee_id=:employee_id
                 AND lb.leave_year=:leave_year
                WHERE lt.status='ACTIVE'
                ORDER BY lt.leave_code
                """
            ),
            {"employee_id": employee_id, "leave_year": leave_year},
        ).mappings()
    ]


def list_leave_by_status(connection: Connection, status: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT l.*,e.display_name,lt.leave_name
                FROM verigence_attendance.leave_requests l
                JOIN verigence_attendance.employees e ON e.employee_id=l.employee_id
                JOIN verigence_attendance.leave_types lt ON lt.leave_type_id=l.leave_type_id
                WHERE l.status=:status
                ORDER BY l.created_at_utc
                """
            ),
            {"status": status},
        ).mappings()
    ]
