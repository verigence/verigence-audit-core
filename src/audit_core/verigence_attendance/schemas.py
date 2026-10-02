from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class EmployeeProfile(BaseModel):
    employeeId: UUID
    securityUserId: UUID
    employeeCode: str
    displayName: str
    primaryEmail: str | None = None
    mobile: str | None = None
    joiningDate: date
    employmentStatus: str
    tlUserId: UUID | None = None
    pmoUserId: UUID | None = None
    projectTenantId: UUID | None = None
    workLocationId: UUID | None = None
    workLocationName: str | None = None


class EmployeeCreateRequest(BaseModel):
    securityUserId: UUID
    employeeCode: str = Field(min_length=1, max_length=80)
    displayName: str = Field(min_length=1, max_length=240)
    primaryEmail: str | None = Field(default=None, max_length=320)
    mobile: str | None = Field(default=None, max_length=40)
    joiningDate: date
    tlUserId: UUID | None = None
    pmoUserId: UUID | None = None
    projectTenantId: UUID | None = None
    workLocationId: UUID | None = None
    basicSalary: Decimal = Decimal(0)
    hra: Decimal = Decimal(0)
    allowances: Decimal = Decimal(0)
    otherEarnings: Decimal = Decimal(0)
    fixedDeductions: Decimal = Decimal(0)
    bankAccountMasked: str | None = Field(default=None, max_length=80)
    panMasked: str | None = Field(default=None, max_length=32)
    aadhaarMasked: str | None = Field(default=None, max_length=32)


class AttendanceEventResponse(BaseModel):
    attendanceEventId: UUID
    attendanceDate: date
    eventType: Literal["CHECK_IN", "CHECK_OUT"]
    capturedAtUtc: datetime
    distanceMeters: float
    geofenceRadiusMeters: int


class AttendanceDayResponse(BaseModel):
    attendanceDate: date
    status: str
    presentFraction: Decimal
    checkInAtUtc: datetime | None = None
    checkOutAtUtc: datetime | None = None


class LeaveCreateRequest(BaseModel):
    leaveTypeId: UUID
    startDate: date
    endDate: date
    requestedDays: Decimal = Field(gt=0)
    reason: str | None = Field(default=None, max_length=2000)


class LeaveDecisionRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=2000)


class LeaveRequestResponse(BaseModel):
    leaveRequestId: UUID
    employeeId: UUID
    employeeName: str
    leaveTypeId: UUID
    leaveTypeName: str
    startDate: date
    endDate: date
    requestedDays: Decimal
    reason: str | None = None
    status: str
    createdAtUtc: datetime


class ReimbursementDecisionRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=2000)


class ReimbursementResponse(BaseModel):
    claimId: UUID
    employeeId: UUID
    employeeName: str
    expenseDate: date
    category: str
    amount: Decimal
    description: str | None = None
    status: str
    financeApprovalRequired: bool
    createdAtUtc: datetime


class PayslipResponse(BaseModel):
    payslipId: UUID
    payrollMonth: date
    netAmount: Decimal
    generatedAtUtc: datetime
    downloadUrl: str


class AdminCapabilities(BaseModel):
    employeeManage: bool
    leaveHrApprove: bool
    reimbursementHrApprove: bool
    reimbursementFinanceApprove: bool
    payrollManage: bool
    reportRead: bool
    configManage: bool


class BulkImportRowResponse(BaseModel):
    rowNumber: int
    employeeCode: str | None = None
    action: Literal["CREATE", "UPDATE", "UNCHANGED", "ERROR"]
    messages: list[str] = Field(default_factory=list)
    applied: bool = False


class BulkImportResponse(BaseModel):
    importId: UUID
    filename: str
    status: str
    counts: dict[str, int]
    rows: list[BulkImportRowResponse]


class PayrollSummaryResponse(BaseModel):
    payrollRunId: UUID
    payrollMonth: date
    status: str
    employeeCount: int
    totalNetAmount: Decimal
    generatedAtUtc: datetime
    finalizedAtUtc: datetime | None = None


class PayrollItemResponse(BaseModel):
    payrollItemId: UUID
    employeeId: UUID
    employeeCode: str
    employeeName: str
    scheduledDays: Decimal
    presentDays: Decimal
    paidLeaveDays: Decimal
    unpaidLeaveDays: Decimal
    payableDays: Decimal
    grossAmount: Decimal
    deductionAmount: Decimal
    netAmount: Decimal


class ConfigUpdateRequest(BaseModel):
    value: object


class WorkLocationCreateRequest(BaseModel):
    locationCode: str = Field(min_length=1, max_length=80)
    locationName: str = Field(min_length=1, max_length=240)
    addressText: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    geofenceRadiusMeters: int = Field(default=500, ge=50, le=5000)


class WorkLocationResponse(BaseModel):
    locationId: UUID
    locationCode: str
    locationName: str
    addressText: str | None = None
    latitude: float
    longitude: float
    geofenceRadiusMeters: int
    status: str


class LeaveTypeCreateRequest(BaseModel):
    leaveCode: str = Field(min_length=1, max_length=40)
    leaveName: str = Field(min_length=1, max_length=120)
    isPaid: bool = True
    defaultEntitlementDays: Decimal = Field(default=Decimal(0), ge=0)
    allowHalfDay: bool = True


class LeaveTypeResponse(BaseModel):
    leaveTypeId: UUID
    leaveCode: str
    leaveName: str
    isPaid: bool
    defaultEntitlementDays: Decimal
    allowHalfDay: bool
    status: str


class HolidayCreateRequest(BaseModel):
    holidayDate: date
    holidayName: str = Field(min_length=1, max_length=240)
    workLocationId: UUID | None = None


class HolidayResponse(BaseModel):
    holidayId: UUID
    holidayDate: date
    holidayName: str
    workLocationId: UUID | None = None
    status: str


class LeaveBalanceResponse(BaseModel):
    leaveTypeId: UUID
    leaveCode: str
    leaveName: str
    isPaid: bool
    allowHalfDay: bool
    openingDays: Decimal
    entitledDays: Decimal
    adjustmentDays: Decimal
    usedDays: Decimal
    availableDays: Decimal
