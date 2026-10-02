from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import Connection

from audit_core.security import HumanPrincipal
from audit_core.verigence_attendance.db import get_connection
from audit_core.verigence_attendance.errors import AttendanceRuleError
from audit_core.verigence_attendance.repository import (
    attendance_history,
    create_employee,
    create_leave_request,
    decide_hr_leave,
    decide_reimbursement,
    decide_team_leave,
    employee_for_user,
    list_employees,
    list_leave_for_employee,
    list_payslips,
    list_reimbursements_by_status,
    list_reimbursements_for_employee,
    list_team_leave,
)
from audit_core.verigence_attendance.schemas import (
    AttendanceDayResponse,
    AttendanceEventResponse,
    EmployeeCreateRequest,
    EmployeeProfile,
    LeaveCreateRequest,
    LeaveDecisionRequest,
    LeaveRequestResponse,
    PayslipResponse,
    ReimbursementDecisionRequest,
    ReimbursementResponse,
)
from audit_core.verigence_attendance.security import human_principal, security_client
from audit_core.verigence_attendance.service import record_attendance, submit_reimbursement
from audit_core.verigence_attendance.storage import storage

router = APIRouter(prefix="/employee-attendance/v1", tags=["employee-attendance"])


def _employee_profile(row: dict) -> EmployeeProfile:
    return EmployeeProfile(
        employeeId=row["employee_id"],
        securityUserId=row["security_user_id"],
        employeeCode=row["employee_code"],
        displayName=row["display_name"],
        primaryEmail=row.get("primary_email"),
        mobile=row.get("mobile"),
        joiningDate=row["joining_date"],
        employmentStatus=row["employment_status"],
        tlUserId=row.get("tl_user_id"),
        pmoUserId=row.get("pmo_user_id"),
        projectTenantId=row.get("project_tenant_id"),
        workLocationId=row.get("work_location_id"),
        workLocationName=row.get("location_name"),
    )


def _leave(row: dict) -> LeaveRequestResponse:
    return LeaveRequestResponse(
        leaveRequestId=row["leave_request_id"],
        employeeId=row["employee_id"],
        employeeName=row["display_name"],
        leaveTypeId=row["leave_type_id"],
        leaveTypeName=row["leave_name"],
        startDate=row["start_date"],
        endDate=row["end_date"],
        requestedDays=row["requested_days"],
        reason=row.get("reason"),
        status=row["status"],
        createdAtUtc=row["created_at_utc"],
    )


def _claim(row: dict) -> ReimbursementResponse:
    return ReimbursementResponse(
        claimId=row["claim_id"],
        employeeId=row["employee_id"],
        employeeName=row["display_name"],
        expenseDate=row["expense_date"],
        category=row["category"],
        amount=row["amount"],
        description=row.get("description"),
        status=row["status"],
        financeApprovalRequired=bool(row["finance_approval_required"]),
        createdAtUtc=row["created_at_utc"],
    )


@router.get("/me", response_model=EmployeeProfile)
def my_profile(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> EmployeeProfile:
    return _employee_profile(employee_for_user(connection, principal.subject))


@router.get("/me/attendance", response_model=list[AttendanceDayResponse])
def my_attendance(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    limit: int = Query(default=31, ge=1, le=366),
) -> list[AttendanceDayResponse]:
    employee = employee_for_user(connection, principal.subject)
    rows = attendance_history(
        connection,
        employee_id=UUID(str(employee["employee_id"])),
        limit=limit,
    )
    return [
        AttendanceDayResponse(
            attendanceDate=row["attendance_date"],
            status=row["status"],
            presentFraction=row["present_fraction"],
            checkInAtUtc=row.get("check_in_at_utc"),
            checkOutAtUtc=row.get("check_out_at_utc"),
        )
        for row in rows
    ]


async def _attendance_action(
    *,
    event_type: str,
    principal: HumanPrincipal,
    connection: Connection,
    latitude: float,
    longitude: float,
    accuracy_meters: float,
    captured_at: datetime,
    photo: UploadFile,
) -> AttendanceEventResponse:
    data = await photo.read()
    result = record_attendance(
        connection,
        user_id=principal.subject,
        event_type=event_type,
        latitude=latitude,
        longitude=longitude,
        accuracy_meters=accuracy_meters,
        captured_at=captured_at,
        photo_data=data,
        photo_content_type=(photo.content_type or "").lower(),
        storage=storage(),
    )
    return AttendanceEventResponse.model_validate(result)


@router.post("/me/attendance/check-in", response_model=AttendanceEventResponse)
async def check_in(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    latitude: Annotated[float, Form(ge=-90, le=90)],
    longitude: Annotated[float, Form(ge=-180, le=180)],
    accuracyMeters: Annotated[float, Form(ge=0)],
    capturedAt: Annotated[datetime, Form()],
    photo: Annotated[UploadFile, File(...)],
) -> AttendanceEventResponse:
    return await _attendance_action(
        event_type="CHECK_IN",
        principal=principal,
        connection=connection,
        latitude=latitude,
        longitude=longitude,
        accuracy_meters=accuracyMeters,
        captured_at=capturedAt,
        photo=photo,
    )


@router.post("/me/attendance/check-out", response_model=AttendanceEventResponse)
async def check_out(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    latitude: Annotated[float, Form(ge=-90, le=90)],
    longitude: Annotated[float, Form(ge=-180, le=180)],
    accuracyMeters: Annotated[float, Form(ge=0)],
    capturedAt: Annotated[datetime, Form()],
    photo: Annotated[UploadFile, File(...)],
) -> AttendanceEventResponse:
    return await _attendance_action(
        event_type="CHECK_OUT",
        principal=principal,
        connection=connection,
        latitude=latitude,
        longitude=longitude,
        accuracy_meters=accuracyMeters,
        captured_at=capturedAt,
        photo=photo,
    )


@router.get("/me/leave", response_model=list[LeaveRequestResponse])
def my_leave(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[LeaveRequestResponse]:
    employee = employee_for_user(connection, principal.subject)
    return [
        _leave(row)
        for row in list_leave_for_employee(
            connection,
            UUID(str(employee["employee_id"])),
        )
    ]


@router.post("/me/leave", response_model=LeaveRequestResponse)
def apply_leave(
    body: LeaveCreateRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> LeaveRequestResponse:
    employee = employee_for_user(connection, principal.subject)
    if body.endDate < body.startDate:
        raise AttendanceRuleError(
            "LEAVE_DATES_INVALID",
            "Leave end date cannot be before start date.",
            status_code=400,
        )
    return _leave(
        create_leave_request(
            connection,
            employee_id=UUID(str(employee["employee_id"])),
            leave_type_id=body.leaveTypeId,
            start_date=body.startDate,
            end_date=body.endDate,
            requested_days=body.requestedDays,
            reason=body.reason,
        )
    )


@router.get("/team/leave", response_model=list[LeaveRequestResponse])
def team_leave_pending(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[LeaveRequestResponse]:
    return [_leave(row) for row in list_team_leave(connection, principal.subject)]


@router.post("/team/leave/{leave_id}/decision", response_model=LeaveRequestResponse)
def team_leave_decision(
    leave_id: UUID,
    body: LeaveDecisionRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> LeaveRequestResponse:
    return _leave(
        decide_team_leave(
            connection,
            leave_id=leave_id,
            actor_user_id=principal.subject,
            decision=body.decision,
            comment=body.comment,
        )
    )


@router.post("/admin/leave/{leave_id}/decision", response_model=LeaveRequestResponse)
def hr_leave_decision(
    leave_id: UUID,
    body: LeaveDecisionRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> LeaveRequestResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.leave.hr.approve",
    )
    return _leave(
        decide_hr_leave(
            connection,
            leave_id=leave_id,
            actor_user_id=principal.subject,
            decision=body.decision,
            comment=body.comment,
        )
    )


@router.get("/me/reimbursements", response_model=list[ReimbursementResponse])
def my_reimbursements(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[ReimbursementResponse]:
    employee = employee_for_user(connection, principal.subject)
    return [
        _claim(row)
        for row in list_reimbursements_for_employee(
            connection,
            UUID(str(employee["employee_id"])),
        )
    ]


@router.post("/me/reimbursements", response_model=ReimbursementResponse)
async def create_my_reimbursement(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    expenseDate: Annotated[date, Form()],
    category: Annotated[str, Form(min_length=1, max_length=40)],
    amount: Annotated[Decimal, Form(gt=0)],
    description: Annotated[str | None, Form(max_length=2000)] = None,
    receipt: Annotated[UploadFile | None, File()] = None,
) -> ReimbursementResponse:
    receipt_data = await receipt.read() if receipt is not None else None
    row = submit_reimbursement(
        connection,
        user_id=principal.subject,
        expense_date=expenseDate,
        category=category,
        amount=amount,
        description=description,
        receipt_data=receipt_data,
        receipt_content_type=(receipt.content_type if receipt is not None else None),
        storage=storage(),
    )
    return _claim(row)


@router.get("/admin/reimbursements", response_model=list[ReimbursementResponse])
def reimbursement_queue(
    stage: Literal["HR", "FINANCE"],
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[ReimbursementResponse]:
    permission = (
        "attendance.reimbursement.hr.approve"
        if stage == "HR"
        else "attendance.reimbursement.finance.approve"
    )
    security_client().require(user_id=principal.subject, permission_key=permission)
    status = "PENDING_HR" if stage == "HR" else "PENDING_FINANCE"
    return [_claim(row) for row in list_reimbursements_by_status(connection, status)]


@router.post(
    "/admin/reimbursements/{claim_id}/decision",
    response_model=ReimbursementResponse,
)
def reimbursement_decision(
    claim_id: UUID,
    stage: Literal["HR", "FINANCE"],
    body: ReimbursementDecisionRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ReimbursementResponse:
    permission = (
        "attendance.reimbursement.hr.approve"
        if stage == "HR"
        else "attendance.reimbursement.finance.approve"
    )
    auth = security_client().require(user_id=principal.subject, permission_key=permission)
    role = str(auth.get("roleKey") or auth.get("classification") or stage)
    return _claim(
        decide_reimbursement(
            connection,
            claim_id=claim_id,
            actor_user_id=principal.subject,
            actor_role=role,
            stage=stage,
            decision=body.decision,
            comment=body.comment,
        )
    )


@router.get("/me/payslips", response_model=list[PayslipResponse])
def my_payslips(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[PayslipResponse]:
    employee = employee_for_user(connection, principal.subject)
    rows = list_payslips(connection, UUID(str(employee["employee_id"])))
    return [
        PayslipResponse(
            payslipId=row["payslip_id"],
            payrollMonth=row["payroll_month"],
            netAmount=row["net_amount"],
            generatedAtUtc=row["generated_at_utc"],
            downloadUrl=storage().presign(object_key=row["pdf_object_key"]),
        )
        for row in rows
    ]


@router.get("/admin/employees", response_model=list[EmployeeProfile])
def admin_employees(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[EmployeeProfile]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.employee.manage",
    )
    return [_employee_profile(row) for row in list_employees(connection)]


@router.post("/admin/employees", response_model=EmployeeProfile)
def admin_create_employee(
    body: EmployeeCreateRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> EmployeeProfile:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.employee.manage",
    )
    row = create_employee(
        connection,
        user_id=body.securityUserId,
        employee_code=body.employeeCode,
        display_name=body.displayName,
        primary_email=body.primaryEmail,
        mobile=body.mobile,
        joining_date=body.joiningDate,
        tl_user_id=body.tlUserId,
        pmo_user_id=body.pmoUserId,
        project_tenant_id=body.projectTenantId,
        work_location_id=body.workLocationId,
        salary={
            "basic_salary": body.basicSalary,
            "hra": body.hra,
            "allowances": body.allowances,
            "other_earnings": body.otherEarnings,
            "fixed_deductions": body.fixedDeductions,
        },
        actor_user_id=principal.subject,
    )
    return _employee_profile(row)
