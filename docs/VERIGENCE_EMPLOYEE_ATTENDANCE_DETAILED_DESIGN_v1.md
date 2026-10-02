# Verigence Employee & Attendance — Detailed Design v1

## 1. Objective

Deliver an Employee/Attendance product embedded in Verigence while preserving the operational independence of existing Verigence Web, Audit Core, DI and Security behavior.

The module supports:

- Employee onboarding, including Excel bulk onboarding.
- Mobile check-in/check-out with live photo and GPS geofence.
- Leave requests and TL/PM operational approval followed by HR validation.
- Travel/food/other reimbursements with HR approval and conditional Finance approval.
- Monthly payroll calculation and PDF payslips.
- Weekly/custom attendance export and monthly payroll export.
- HR/Finance/SuperAdmin Web administration.
- Employee/TL/PM mobile actions only.

## 2. Non-negotiable isolation boundary

### 2.1 Existing Verigence remains untouched

The Employee/Attendance product is additive.

It must not modify existing:
- Audit Core business tables.
- Audit Core booking/delivery/review APIs.
- DI services, schemas or flows.
- Project/Workspace behavior.
- PC/TL/PM/CRM/Executive primary-role cardinality.
- Existing Security authorization algorithm.
- Existing Verigence mobile project flows.

### 2.2 Separate runtime

The Employee/Attendance API has its own FastAPI entry point:

`audit_core.verigence_attendance.main:app`

It is **not mounted into** `audit_core.main:app`.

Deployment/runtime artifacts:
- `Dockerfile.employee-attendance`
- `railway.employee-attendance.toml`
- `scripts/apply_verigence_attendance_schema.py`

The Attendance runtime uses its own DB engine/pool:
- `pool_size=2`
- `max_overflow=1`
- `pool_timeout=3`

An outage, storage failure, Security authorization failure, or DB pool exhaustion in Employee/Attendance must affect only Employee/Attendance.

Existing Audit Core must start and operate without the Employee/Attendance service.

## 3. Web and mobile surfaces

Verigence continues to use the existing React/Ionic/Capacitor application.

### 3.1 Employee entry

A new top-level Employee Attendance entry exists outside project Workspace.

Inside it:

1. Attendance
2. Leave
3. Reimbursements
4. Salary & Payslips
5. Pending Approvals — visible only to TL/PM where applicable

### 3.2 Mobile

Mobile exposes only employee/self-service functions plus TL/PM pending leave approval.

Mobile does not expose HR administration, employee onboarding, payroll administration, or Finance administration.

Check-in/check-out is mobile-only and uses:
- Native live camera.
- Current device location.
- Location accuracy.
- Captured timestamp.

Gallery-upload attendance evidence is not accepted by the backend contract.

### 3.3 Administration

Employee administration is available only through Verigence Web under Administration.

Sections:
- Employees
- Bulk onboarding
- Leave
- Reimbursements
- Payroll
- Reports
- Configuration

## 4. Security and RBAC

Security remains authoritative.

No Employee/Attendance-local RBAC engine is introduced.

### 4.1 Existing primary roles

Existing Verigence primary roles remain unchanged:
- PC
- TL
- PM
- CRM
- Executive
- SuperAdmin

### 4.2 Attendance secondary roles

#### HRADMIN

Existing Attendance-specific `HRADMIN` is reused.

It remains a secondary module role and does not replace or modify a user's operating role.

#### FINANCEADMIN

New Attendance-only secondary role.

`module_key = attendance`
`role_key = FINANCEADMIN`

FinanceAdmin receives only:
- `attendance.reimbursement.read`
- `attendance.reimbursement.finance.approve`

It does not receive HR, employee-management, leave, attendance-correction, payroll-management, configuration, Audit Core, or DI privileges.

### 4.3 SuperAdmin

Existing SuperAdmin remains authoritative and can perform all Attendance module actions through the existing Security SuperAdmin authorization rule.

## 5. Data access rules

These rules are enforced by backend SQL/service authorization, not only by UI visibility.

### Employee / PC

May access only:
- Own attendance.
- Own leave.
- Own reimbursement claims.
- Own finalized payslips.

There is no generic employee endpoint that accepts another employee ID for these resources.

### TL

May:
- Perform own employee actions.
- View today's attendance for employees explicitly assigned to the TL.
- View and approve/reject pending leave for explicitly assigned employees.

TL cannot view team reimbursement or salary/payslips.

### PM / PMO

May:
- Perform own employee actions.
- View today's attendance for employees explicitly assigned to the PM/PMO.
- View and approve/reject pending leave for explicitly assigned employees.
- View reimbursement claims for employees explicitly assigned to the PM/PMO.

PM reimbursement access is **read-only**.

PM cannot approve reimbursement unless the same user separately holds HRADMIN or FINANCEADMIN.

### HRADMIN

May perform HR Employee/Attendance functions granted by the Attendance permission catalog:
- Employee onboarding.
- Leave final validation.
- HR reimbursement approval.
- Payroll administration.
- Reports.

### FINANCEADMIN

May:
- View only the Finance reimbursement approval queue.
- Approve/reject claims that require Finance review.

### SuperAdmin

All Employee/Attendance functions.

## 6. Team assignment model

The Attendance module owns operational employee supervisor references:

- `employees.tl_user_id`
- `employees.pmo_user_id`
- optional logical `employees.project_tenant_id`

These are logical identity/project references and do not create a foreign key into Audit Core or Security tables.

Team data queries scope directly by the authenticated Security user ID.

Examples:
- TL/PM attendance: `tl_user_id = actor OR pmo_user_id = actor`
- Team leave: same scope.
- PM reimbursement: `pmo_user_id = actor` only.

This avoids introducing a dependency from Employee/Attendance runtime onto an existing Audit Core project endpoint.

A future reconciliation/read-only integration with Audit Core `business_assignments` can be considered separately, but it is not required for v1 and is intentionally not added to existing Audit Core runtime in this design.

## 7. Database ownership

All new business tables are owned by schema:

`verigence_attendance`

There are no foreign keys to existing Audit Core or Security business tables.

### 7.1 Tables

1. `employees`
2. `work_locations`
3. `attendance_days`
4. `attendance_events`
5. `leave_types`
6. `leave_balances`
7. `leave_requests`
8. `reimbursement_claims`
9. `approval_actions`
10. `salary_structures`
11. `holidays`
12. `payroll_runs`
13. `payroll_items`
14. `payslips`
15. `module_configuration`
16. `bulk_imports`
17. `bulk_import_rows`
18. `module_audit`

## 8. Employee onboarding

### 8.1 Single onboarding

Web HR onboarding captures:
- Security User ID
- Employee code
- Display name
- Email/mobile
- Joining date
- TL
- PM/PMO
- Optional logical project reference
- Work location
- Basic salary
- HRA
- Allowances
- Other earnings
- Fixed deductions
- Masked bank/PAN/Aadhaar fields

Salary data is stored as an effective-dated `salary_structures` record rather than directly in the employee row.

### 8.2 Excel onboarding

The module supports:
1. Download Employee template.
2. Populate Excel.
3. Upload.
4. Validate.
5. Preview.
6. Apply.

Nothing is applied during preview.

Import staging is stored in:
- `bulk_imports`
- `bulk_import_rows`

Each row is classified as:
- CREATE
- UPDATE
- UNCHANGED
- ERROR

Import errors remain visible at row level.

## 9. Attendance

### 9.1 Check-in/check-out

Both check-in and check-out require:
- Authenticated employee.
- Current GPS coordinates.
- Location accuracy.
- Live-camera photo.
- Captured timestamp.

The backend calculates distance using Haversine distance.

The client does not decide whether the user is inside the geofence.

### 9.2 Geofence

Default: 500 metres.

Work locations may configure their own radius.

Valid range: 50–5000 metres.

Attendance evidence stores:
- Coordinates.
- Accuracy.
- Assigned work location.
- Calculated distance.
- Radius used.
- Geofence result.
- Photo object key.
- Photo SHA-256.
- `LIVE_CAMERA` capture source.

### 9.3 Attendance data

`attendance_days` stores daily state.

`attendance_events` stores immutable check-in/check-out/correction evidence.

## 10. Leave

Workflow:

`Employee -> TL OR PM/PMO -> HRADMIN -> Approved`

Either TL or PM/PMO satisfies the operational approval stage.

States:
- PENDING_OPERATIONAL
- PENDING_HR
- APPROVED
- REJECTED
- CANCELLED

Approval decisions are recorded in `approval_actions`.

Approved paid leave:
- Verifies available leave balance.
- Updates `leave_balances.used_days`.

v1 requires one leave request to remain within one calendar year.

Leave type configuration includes:
- Paid/unpaid.
- Default annual entitlement.
- Half-day allowed.

## 11. Reimbursements

Supported categories:
- TRAVEL
- FOOD
- OTHER

Employee provides:
- Expense date.
- Amount.
- Category.
- Description.
- Optional receipt.

Receipt evidence is stored in object storage with hash.

Employee, PM team view, HR and Finance receive a short-lived presigned receipt URL when evidence exists.

### 11.1 Approval workflow

`Employee -> HRADMIN -> FINANCEADMIN if required -> Approved`

Finance review is based on cumulative employee claims for the claim month.

Default threshold: INR 3000.

Rejected/cancelled claims do not count toward cumulative total.

If the new claim causes the monthly total to exceed the configured threshold:
- HR approval changes state to PENDING_FINANCE.
- FINANCEADMIN must approve/reject.

Otherwise HR approval completes the approval.

PM team reimbursement is read-only and does not alter this approval chain.

## 12. Payroll

Default policy:
- Six working days/week.
- Sunday weekly off.

SuperAdmin may configure:
- Working days/week.
- Weekly-off weekdays.
- Fixed-deduction proration.
- Holidays.
- Leave types and entitlement.
- Geofence.
- Finance threshold.

The working-days value and weekly-off list are kept consistent by configuration writes.

Payroll calculation considers:
- Joining date.
- Scheduled working dates.
- Weekly offs.
- Holidays.
- Attendance present fraction.
- Approved paid leave.
- Approved unpaid leave.
- Effective salary structure.
- Configured fixed-deduction proration.

Payroll states:
- DRAFT
- CALCULATED
- FINALIZED
- CANCELLED

## 13. Payslips

Payroll finalization creates one PDF per payroll item.

Payslip stores:
- Employee.
- Payroll item.
- PDF object key.
- PDF hash.
- Generated timestamp.

Employee can retrieve only their own finalized payslips.

The Web/mobile client receives short-lived download links.

## 14. Reports

### Attendance

Excel export supports weekly or custom ranges up to the API-defined maximum.

Includes:
- Employee code/name.
- Date.
- Attendance status.
- Present fraction.
- Check-in.
- Check-out.

### Payroll

Monthly payroll Excel includes:
- Employee.
- Scheduled days.
- Present days.
- Paid leave.
- Unpaid leave.
- Payable days.
- Gross amount.
- Deductions.
- Net amount.

## 15. Storage

Attendance photos, reimbursement receipts and payslips use the Attendance runtime's own configured S3-compatible storage settings.

The module stores object references and SHA-256 hashes in its own database.

Existing Verigence document/DI storage workflows are not modified.

## 16. API surface

Base:

`/employee-attendance/v1`

### Employee
- `GET /me`
- `GET /me/attendance`
- `POST /me/attendance/check-in`
- `POST /me/attendance/check-out`
- `GET /me/leave-balances`
- `GET /me/leave`
- `POST /me/leave`
- `GET /me/reimbursements`
- `POST /me/reimbursements`
- `GET /me/payslips`

### TL/PM team
- `GET /team/attendance`
- `GET /team/leave`
- `POST /team/leave/{leaveId}/decision`
- `GET /team/reimbursements` — PM/PMO scope only and read-only

### Administration
- Employee create/list
- Excel template/preview/apply
- HR leave queue/decision
- HR/Finance reimbursement queue/decision
- Payroll calculate/items/finalize
- Attendance/payroll exports
- Configuration
- Work locations
- Leave types
- Holidays
- Admin capability discovery

## 17. Failure behavior

If Employee/Attendance is unavailable:
- Existing Verigence navigation and project modules remain available.
- Audit Core does not import or initialize Attendance.
- DI is unaffected.
- Security core authentication/authorization is unaffected.
- Web Employee Attendance requests time out/fail locally and display a module error.
- Existing Booking/Delivery/Journey/Review requests never wait on Attendance.

## 18. CI and regression expectations

Before merge:
- Audit Core existing CI must remain green.
- Web CI must remain green.
- Android validation must remain green.
- Security CI must remain green.
- Isolation guard must verify `audit_core.main` does not mount Attendance.
- Team reimbursement endpoint must remain GET-only.
- Team queries must retain TL/PM supervisor predicates.

## 19. Explicitly out of scope for v1

- Modifying existing Audit Core project/workspace logic.
- Modifying DI.
- Adding HR/Finance to the primary Verigence role model.
- Replacing Security authorization.
- Background dependency from Audit Core to Attendance.
- Payroll tax/statutory engine beyond configured earnings/deductions.
- Cross-year single leave requests.
- Finance approval by PM unless PM separately holds FINANCEADMIN.
