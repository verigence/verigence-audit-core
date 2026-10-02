from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection

from audit_core.security import HumanPrincipal
from audit_core.verigence_attendance.admin_config import (
    create_holiday,
    create_leave_type,
    create_work_location,
    list_configuration,
    list_holidays,
    list_leave_types,
    list_work_locations,
    update_configuration,
)
from audit_core.verigence_attendance.bulk_import import (
    apply_employee_import,
    build_employee_template,
    preview_employee_workbook,
)
from audit_core.verigence_attendance.db import get_connection
from audit_core.verigence_attendance.errors import AttendanceRuleError
from audit_core.verigence_attendance.payroll import (
    finalize_payroll,
    generate_payroll,
    payroll_items,
    payroll_summary,
)
from audit_core.verigence_attendance.repository import (
    attendance_history,
    create_employee,
    create_leave_request,
    decide_hr_leave,
    decide_reimbursement,
    decide_team_leave,
    employee_for_user,
    list_employees,
    leave_balances_for_employee,
    list_leave_by_status,
    list_leave_for_employee,
    list_payslips,
    list_reimbursements_by_status,
    list_reimbursements_for_employee,
    list_team_leave,
)
from audit_core.verigence_attendance.schemas import (
    AdminCapabilities,
    BulkImportResponse,
    ConfigUpdateRequest,
    HolidayCreateRequest,
    HolidayResponse,
    LeaveTypeCreateRequest,
    LeaveTypeResponse,
    AttendanceDayResponse,
    AttendanceEventResponse,
    EmployeeCreateRequest,
    EmployeeProfile,
    LeaveBalanceResponse,
    LeaveCreateRequest,
    LeaveDecisionRequest,
    LeaveRequestResponse,
    PayrollItemResponse,
    PayrollSummaryResponse,
    PayslipResponse,
    ReimbursementDecisionRequest,
    ReimbursementResponse,
    WorkLocationCreateRequest,
    WorkLocationResponse,
)
from audit_core.verigence_attendance.reports import attendance_report, payroll_report
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



@router.get("/me/leave-balances", response_model=list[LeaveBalanceResponse])
def my_leave_balances(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[LeaveBalanceResponse]:
    employee = employee_for_user(connection, principal.subject)
    year = datetime.now(UTC).year
    return [
        LeaveBalanceResponse(
            leaveTypeId=row["leave_type_id"],
            leaveCode=row["leave_code"],
            leaveName=row["leave_name"],
            isPaid=row["is_paid"],
            allowHalfDay=row["allow_half_day"],
            openingDays=row["opening_days"],
            entitledDays=row["entitled_days"],
            adjustmentDays=row["adjustment_days"],
            usedDays=row["used_days"],
            availableDays=row["available_days"],
        )
        for row in leave_balances_for_employee(
            connection,
            employee_id=UUID(str(employee["employee_id"])),
            leave_year=year,
        )
    ]

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



@router.get("/admin/leave", response_model=list[LeaveRequestResponse])
def hr_leave_queue(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[LeaveRequestResponse]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.leave.hr.approve",
    )
    return [_leave(row) for row in list_leave_by_status(connection, "PENDING_HR")]

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





@router.get("/admin/config")
def admin_config(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, object]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    return list_configuration(connection)


@router.put("/admin/config/{config_key}")
def admin_update_config(
    config_key: str,
    body: ConfigUpdateRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, object]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    return update_configuration(
        connection,
        key=config_key,
        value=body.value,
        actor_user_id=principal.subject,
    )


@router.get("/admin/work-locations", response_model=list[WorkLocationResponse])
def admin_work_locations(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[WorkLocationResponse]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    return [
        WorkLocationResponse(
            locationId=row["location_id"],
            locationCode=row["location_code"],
            locationName=row["location_name"],
            addressText=row.get("address_text"),
            latitude=row["latitude"],
            longitude=row["longitude"],
            geofenceRadiusMeters=row["geofence_radius_meters"],
            status=row["status"],
        )
        for row in list_work_locations(connection)
    ]


@router.post("/admin/work-locations", response_model=WorkLocationResponse)
def admin_create_work_location(
    body: WorkLocationCreateRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> WorkLocationResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    row = create_work_location(
        connection,
        code=body.locationCode,
        name=body.locationName,
        address=body.addressText,
        latitude=body.latitude,
        longitude=body.longitude,
        radius_meters=body.geofenceRadiusMeters,
    )
    return WorkLocationResponse(
        locationId=row["location_id"],
        locationCode=row["location_code"],
        locationName=row["location_name"],
        addressText=row.get("address_text"),
        latitude=row["latitude"],
        longitude=row["longitude"],
        geofenceRadiusMeters=row["geofence_radius_meters"],
        status=row["status"],
    )


@router.get("/admin/leave-types", response_model=list[LeaveTypeResponse])
def admin_leave_types(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[LeaveTypeResponse]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    return [
        LeaveTypeResponse(
            leaveTypeId=row["leave_type_id"],
            leaveCode=row["leave_code"],
            leaveName=row["leave_name"],
            isPaid=row["is_paid"],
            defaultEntitlementDays=row["default_entitlement_days"],
            allowHalfDay=row["allow_half_day"],
            status=row["status"],
        )
        for row in list_leave_types(connection)
    ]


@router.post("/admin/leave-types", response_model=LeaveTypeResponse)
def admin_create_leave_type(
    body: LeaveTypeCreateRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> LeaveTypeResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    row = create_leave_type(
        connection,
        code=body.leaveCode,
        name=body.leaveName,
        is_paid=body.isPaid,
        entitlement_days=body.defaultEntitlementDays,
        allow_half_day=body.allowHalfDay,
    )
    return LeaveTypeResponse(
        leaveTypeId=row["leave_type_id"],
        leaveCode=row["leave_code"],
        leaveName=row["leave_name"],
        isPaid=row["is_paid"],
        defaultEntitlementDays=row["default_entitlement_days"],
        allowHalfDay=row["allow_half_day"],
        status=row["status"],
    )


@router.get("/admin/holidays", response_model=list[HolidayResponse])
def admin_holidays(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[HolidayResponse]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    return [
        HolidayResponse(
            holidayId=row["holiday_id"],
            holidayDate=row["holiday_date"],
            holidayName=row["holiday_name"],
            workLocationId=row.get("work_location_id"),
            status=row["status"],
        )
        for row in list_holidays(connection)
    ]


@router.post("/admin/holidays", response_model=HolidayResponse)
def admin_create_holiday(
    body: HolidayCreateRequest,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> HolidayResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.config.manage",
    )
    row = create_holiday(
        connection,
        holiday_date=body.holidayDate,
        name=body.holidayName,
        work_location_id=body.workLocationId,
    )
    return HolidayResponse(
        holidayId=row["holiday_id"],
        holidayDate=row["holiday_date"],
        holidayName=row["holiday_name"],
        workLocationId=row.get("work_location_id"),
        status=row["status"],
    )

@router.post("/admin/payroll/calculate", response_model=PayrollSummaryResponse)
def calculate_payroll(
    month: date,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PayrollSummaryResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.payroll.manage",
    )
    return PayrollSummaryResponse.model_validate(
        generate_payroll(
            connection,
            payroll_month=month,
            actor_user_id=principal.subject,
        )
    )


@router.get("/admin/payroll/{run_id}", response_model=PayrollSummaryResponse)
def get_payroll(
    run_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PayrollSummaryResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.payroll.manage",
    )
    return PayrollSummaryResponse.model_validate(payroll_summary(connection, run_id))


@router.get(
    "/admin/payroll/{run_id}/items",
    response_model=list[PayrollItemResponse],
)
def get_payroll_items(
    run_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[PayrollItemResponse]:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.payroll.manage",
    )
    return [
        PayrollItemResponse(
            payrollItemId=row["payroll_item_id"],
            employeeId=row["employee_id"],
            employeeCode=row["employee_code"],
            employeeName=row["display_name"],
            scheduledDays=row["scheduled_days"],
            presentDays=row["present_days"],
            paidLeaveDays=row["paid_leave_days"],
            unpaidLeaveDays=row["unpaid_leave_days"],
            payableDays=row["payable_days"],
            grossAmount=row["gross_amount"],
            deductionAmount=row["deduction_amount"],
            netAmount=row["net_amount"],
        )
        for row in payroll_items(connection, run_id)
    ]


@router.post("/admin/payroll/{run_id}/finalize", response_model=PayrollSummaryResponse)
def finalize_payroll_run(
    run_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PayrollSummaryResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.payroll.manage",
    )
    return PayrollSummaryResponse.model_validate(
        finalize_payroll(
            connection,
            run_id=run_id,
            actor_user_id=principal.subject,
            storage=storage(),
        )
    )


@router.get("/admin/reports/attendance")
def export_attendance_report(
    startDate: date,
    endDate: date,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> StreamingResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.report.export",
    )
    if endDate < startDate or (endDate - startDate).days > 31:
        raise AttendanceRuleError(
            "REPORT_RANGE_INVALID",
            "Attendance report range must be between 1 and 32 days.",
            status_code=400,
        )
    data = attendance_report(
        connection,
        start_date=startDate,
        end_date=endDate,
    )
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="attendance-{startDate}-{endDate}.xlsx"'
            )
        },
    )


@router.get("/admin/reports/payroll/{run_id}")
def export_payroll_report(
    run_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> StreamingResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.report.export",
    )
    data = payroll_report(connection, run_id=run_id)
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="payroll-{run_id}.xlsx"'
            )
        },
    )

@router.get("/admin/capabilities", response_model=AdminCapabilities)
def admin_capabilities(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
) -> AdminCapabilities:
    client = security_client()
    user_id = principal.subject
    return AdminCapabilities(
        employeeManage=client.allowed(user_id=user_id, permission_key="attendance.employee.manage"),
        leaveHrApprove=client.allowed(user_id=user_id, permission_key="attendance.leave.hr.approve"),
        reimbursementHrApprove=client.allowed(
            user_id=user_id,
            permission_key="attendance.reimbursement.hr.approve",
        ),
        reimbursementFinanceApprove=client.allowed(
            user_id=user_id,
            permission_key="attendance.reimbursement.finance.approve",
        ),
        payrollManage=client.allowed(user_id=user_id, permission_key="attendance.payroll.manage"),
        reportRead=client.allowed(user_id=user_id, permission_key="attendance.report.read"),
        configManage=client.allowed(user_id=user_id, permission_key="attendance.config.manage"),
    )


@router.get("/admin/employees/import-template")
def employee_import_template(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
) -> StreamingResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.employee.manage",
    )
    data = build_employee_template()
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                'attachment; filename="verigence-employee-onboarding-template.xlsx"'
            )
        },
    )


@router.post("/admin/employees/imports/preview", response_model=BulkImportResponse)
async def employee_import_preview(
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    workbook: Annotated[UploadFile, File(...)],
) -> BulkImportResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.employee.manage",
    )
    data = await workbook.read()
    if len(data) > 5 * 1024 * 1024:
        raise AttendanceRuleError(
            "IMPORT_TOO_LARGE",
            "Employee onboarding workbook exceeds 5 MB.",
            status_code=400,
        )
    result = preview_employee_workbook(
        connection,
        filename=workbook.filename or "employee-onboarding.xlsx",
        data=data,
        actor_user_id=principal.subject,
    )
    return BulkImportResponse.model_validate(result)


@router.post(
    "/admin/employees/imports/{import_id}/apply",
    response_model=BulkImportResponse,
)
def employee_import_apply(
    import_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BulkImportResponse:
    security_client().require(
        user_id=principal.subject,
        permission_key="attendance.employee.manage",
    )
    result = apply_employee_import(
        connection,
        import_id=import_id,
        actor_user_id=principal.subject,
    )
    return BulkImportResponse.model_validate(result)

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
        bank_account_masked=body.bankAccountMasked,
        pan_masked=body.panMasked,
        aadhaar_masked=body.aadhaarMasked,
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
