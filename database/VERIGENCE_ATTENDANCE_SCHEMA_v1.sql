-- Verigence Employee/Attendance module. ADDITIVE ONLY.
-- Own schema: no ALTER/UPDATE/DELETE against existing Audit Core tables.
CREATE SCHEMA IF NOT EXISTS verigence_attendance;

CREATE TABLE IF NOT EXISTS verigence_attendance.employees (
  employee_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  security_user_id uuid NOT NULL,
  employee_code varchar(80) NOT NULL,
  display_name varchar(240) NOT NULL,
  primary_email varchar(320),
  mobile varchar(40),
  joining_date date NOT NULL,
  employment_status varchar(24) NOT NULL DEFAULT 'ACTIVE',
  tl_user_id uuid,
  pmo_user_id uuid,
  work_location_id uuid,
  basic_salary numeric(14,2) NOT NULL DEFAULT 0,
  hra numeric(14,2) NOT NULL DEFAULT 0,
  allowances numeric(14,2) NOT NULL DEFAULT 0,
  other_earnings numeric(14,2) NOT NULL DEFAULT 0,
  fixed_deductions numeric(14,2) NOT NULL DEFAULT 0,
  bank_account_masked varchar(80),
  pan_masked varchar(32),
  aadhaar_masked varchar(32),
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  updated_at_utc timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id, employee_code),
  UNIQUE(tenant_id, security_user_id)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.work_locations (
  location_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  location_code varchar(80) NOT NULL,
  location_name varchar(240) NOT NULL,
  latitude double precision NOT NULL,
  longitude double precision NOT NULL,
  geofence_radius_meters integer NOT NULL DEFAULT 500 CHECK (geofence_radius_meters > 0),
  status varchar(20) NOT NULL DEFAULT 'ACTIVE',
  UNIQUE(tenant_id, location_code)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.attendance_events (
  attendance_event_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  attendance_date date NOT NULL,
  event_type varchar(16) NOT NULL CHECK (event_type IN ('CHECK_IN','CHECK_OUT')),
  captured_at_utc timestamptz NOT NULL,
  latitude double precision NOT NULL,
  longitude double precision NOT NULL,
  accuracy_meters numeric(10,2),
  work_location_id uuid NOT NULL REFERENCES verigence_attendance.work_locations(location_id),
  distance_meters numeric(12,2) NOT NULL,
  geofence_radius_meters integer NOT NULL,
  photo_object_key varchar(700) NOT NULL,
  photo_sha256 varchar(64) NOT NULL,
  source varchar(20) NOT NULL DEFAULT 'LIVE_CAMERA' CHECK (source='LIVE_CAMERA'),
  created_at_utc timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_va_attendance_employee_date ON verigence_attendance.attendance_events(tenant_id,employee_id,attendance_date);

CREATE TABLE IF NOT EXISTS verigence_attendance.leave_requests (
  leave_request_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  leave_type varchar(60) NOT NULL,
  start_date date NOT NULL,
  end_date date NOT NULL,
  requested_days numeric(6,2) NOT NULL,
  reason text,
  status varchar(32) NOT NULL DEFAULT 'PENDING_OPERATIONAL',
  operational_approved_by uuid,
  operational_approver_role varchar(12),
  operational_decided_at_utc timestamptz,
  hr_decided_by uuid,
  hr_decided_at_utc timestamptz,
  created_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS verigence_attendance.reimbursement_claims (
  claim_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  expense_date date NOT NULL,
  category varchar(40) NOT NULL CHECK (category IN ('TRAVEL','FOOD','OTHER')),
  amount numeric(14,2) NOT NULL CHECK (amount > 0),
  description text,
  receipt_object_key varchar(700),
  status varchar(32) NOT NULL DEFAULT 'PENDING_HR',
  finance_approval_required boolean NOT NULL DEFAULT false,
  hr_decided_by uuid,
  hr_decided_at_utc timestamptz,
  finance_decided_by uuid,
  finance_decided_at_utc timestamptz,
  created_at_utc timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_va_claim_employee_month ON verigence_attendance.reimbursement_claims(tenant_id,employee_id,expense_date);

CREATE TABLE IF NOT EXISTS verigence_attendance.payroll_runs (
  payroll_run_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  payroll_month date NOT NULL,
  status varchar(24) NOT NULL DEFAULT 'DRAFT',
  generated_by uuid NOT NULL,
  generated_at_utc timestamptz NOT NULL DEFAULT now(),
  finalized_by uuid,
  finalized_at_utc timestamptz,
  UNIQUE(tenant_id,payroll_month)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.payslips (
  payslip_id uuid PRIMARY KEY,
  payroll_run_id uuid NOT NULL REFERENCES verigence_attendance.payroll_runs(payroll_run_id),
  tenant_id uuid NOT NULL,
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  gross_amount numeric(14,2) NOT NULL,
  deduction_amount numeric(14,2) NOT NULL,
  net_amount numeric(14,2) NOT NULL,
  payable_days numeric(6,2) NOT NULL,
  pdf_object_key varchar(700),
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  UNIQUE(payroll_run_id,employee_id)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.module_configuration (
  tenant_id uuid NOT NULL,
  config_key varchar(120) NOT NULL,
  config_value_json jsonb NOT NULL,
  updated_by uuid NOT NULL,
  updated_at_utc timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,config_key)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.module_audit (
  audit_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  actor_user_id uuid NOT NULL,
  action_key varchar(160) NOT NULL,
  entity_type varchar(80) NOT NULL,
  entity_id varchar(160),
  before_json jsonb,
  after_json jsonb,
  occurred_at_utc timestamptz NOT NULL DEFAULT now()
);
