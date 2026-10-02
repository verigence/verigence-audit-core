# Verigence Employee / Attendance Module v1

## Hard boundary
This is an additive module. Existing Audit Core tables, services, routes and business behavior are not modified. The module owns its schema (`verigence_attendance`) and must never be a startup dependency for existing Verigence capabilities.

## Product surfaces
- Employee: Attendance (500m geofence + live photo on check-in and check-out), Leave, Reimbursements, Payslips.
- TL/PMO: employee functions plus mobile Pending Approvals for Leave.
- HR Manager: Web-only Employee Management, bulk Excel onboarding, attendance administration, HR leave validation, reimbursement approval, payroll and reports.
- FinanceAdmin: conditional reimbursement approval and permitted payroll/financial functions.
- SuperAdmin: all functions and policy/configuration.

## Workflow rules
- Leave: Employee -> TL OR PMO -> HR -> valid/approved.
- Reimbursement: Employee -> HR -> Finance only when employee monthly claim total crosses configured threshold (default INR 3000).
- Attendance: server validates captured coordinates against assigned location; default radius 500m. Both check-in and check-out require a live-camera artifact.
- Payroll: default six-day working week; leave, half-day, salary components, holidays, deductions and payroll rules are configurable.
- Payslips: monthly PDF.
- Onboarding: single employee plus Excel bulk import with validate/preview/confirm; no silent partial import.

## Isolation / priority
1. No module network call on existing Audit Core startup path.
2. No module import may be required by an existing Audit Core route.
3. Employee-module dependency failure returns only an Employee-module error.
4. Background/report work must use bounded concurrency and lower-priority queues.
5. Existing Audit Core DB objects are read only through existing supported contracts; no FK from this schema to existing Audit Core business tables.
6. Security remains authoritative for identity/RBAC; no local role engine.
7. Existing Verigence Web pages and project/workspace behavior remain unchanged.
