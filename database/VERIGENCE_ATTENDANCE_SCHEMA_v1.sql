-- Verigence Employee / Attendance module v1.
-- ADDITIVE ONLY. This schema has no foreign keys to existing Audit Core/Security tables.
-- Security USER and optional Project/Tenant ids are logical references only.
CREATE SCHEMA IF NOT EXISTS verigence_attendance;

CREATE TABLE IF NOT EXISTS verigence_attendance.employees (
  employee_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  security_user_id uuid NOT NULL UNIQUE,
  employee_code varchar(80) NOT NULL UNIQUE,
  display_name varchar(240) NOT NULL,
  primary_email varchar(320),
  mobile varchar(40),
  joining_date date NOT NULL,
  employment_status varchar(24) NOT NULL DEFAULT 'ACTIVE'
    CHECK (employment_status IN ('ACTIVE','INACTIVE','EXITED')),
  tl_user_id uuid,
  pmo_user_id uuid,
  project_tenant_id uuid,
  work_location_id uuid,
  bank_account_masked varchar(80),
  pan_masked varchar(32),
  aadhaar_masked varchar(32),
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  updated_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_va_employees_tl
  ON verigence_attendance.employees(tl_user_id)
  WHERE employment_status='ACTIVE';
CREATE INDEX IF NOT EXISTS ix_va_employees_pmo
  ON verigence_attendance.employees(pmo_user_id)
  WHERE employment_status='ACTIVE';
CREATE INDEX IF NOT EXISTS ix_va_employees_project
  ON verigence_attendance.employees(project_tenant_id)
  WHERE employment_status='ACTIVE';

CREATE TABLE IF NOT EXISTS verigence_attendance.work_locations (
  location_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  location_code varchar(80) NOT NULL UNIQUE,
  location_name varchar(240) NOT NULL,
  address_text text,
  latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
  longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
  geofence_radius_meters integer NOT NULL DEFAULT 500
    CHECK (geofence_radius_meters BETWEEN 50 AND 5000),
  status varchar(20) NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE','INACTIVE')),
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  updated_at_utc timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE verigence_attendance.employees
  DROP CONSTRAINT IF EXISTS fk_va_employee_work_location;
ALTER TABLE verigence_attendance.employees
  ADD CONSTRAINT fk_va_employee_work_location
  FOREIGN KEY (work_location_id)
  REFERENCES verigence_attendance.work_locations(location_id);

CREATE TABLE IF NOT EXISTS verigence_attendance.attendance_days (
  attendance_day_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  attendance_date date NOT NULL,
  status varchar(32) NOT NULL DEFAULT 'NOT_STARTED'
    CHECK (status IN ('NOT_STARTED','CHECKED_IN','COMPLETED','ABSENT','LEAVE','HOLIDAY','WEEKLY_OFF','CORRECTED')),
  present_fraction numeric(4,2) NOT NULL DEFAULT 0
    CHECK (present_fraction BETWEEN 0 AND 1),
  check_in_at_utc timestamptz,
  check_out_at_utc timestamptz,
  corrected_by_user_id uuid,
  correction_reason text,
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  updated_at_utc timestamptz NOT NULL DEFAULT now(),
  UNIQUE(employee_id,attendance_date)
);

CREATE INDEX IF NOT EXISTS ix_va_attendance_days_date
  ON verigence_attendance.attendance_days(attendance_date,status);

CREATE TABLE IF NOT EXISTS verigence_attendance.attendance_events (
  attendance_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  attendance_day_id uuid NOT NULL
    REFERENCES verigence_attendance.attendance_days(attendance_day_id),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  event_type varchar(20) NOT NULL
    CHECK (event_type IN ('CHECK_IN','CHECK_OUT','CORRECTION')),
  captured_at_utc timestamptz NOT NULL,
  latitude double precision CHECK (latitude BETWEEN -90 AND 90),
  longitude double precision CHECK (longitude BETWEEN -180 AND 180),
  accuracy_meters numeric(10,2),
  work_location_id uuid REFERENCES verigence_attendance.work_locations(location_id),
  distance_meters numeric(12,2),
  geofence_radius_meters integer,
  geofence_result varchar(24)
    CHECK (geofence_result IS NULL OR geofence_result IN ('WITHIN','OUTSIDE','UNVERIFIABLE')),
  photo_object_key varchar(700),
  photo_sha256 char(64),
  capture_source varchar(20)
    CHECK (capture_source IS NULL OR capture_source='LIVE_CAMERA'),
  created_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_va_attendance_event_employee_time
  ON verigence_attendance.attendance_events(employee_id,captured_at_utc DESC);

CREATE TABLE IF NOT EXISTS verigence_attendance.leave_types (
  leave_type_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  leave_code varchar(40) NOT NULL UNIQUE,
  leave_name varchar(120) NOT NULL,
  is_paid boolean NOT NULL DEFAULT true,
  default_entitlement_days numeric(6,2) NOT NULL DEFAULT 0,
  allow_half_day boolean NOT NULL DEFAULT true,
  status varchar(20) NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE','INACTIVE')),
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  updated_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS verigence_attendance.leave_balances (
  leave_balance_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  leave_type_id uuid NOT NULL REFERENCES verigence_attendance.leave_types(leave_type_id),
  leave_year integer NOT NULL CHECK (leave_year BETWEEN 2000 AND 2200),
  opening_days numeric(6,2) NOT NULL DEFAULT 0,
  entitled_days numeric(6,2) NOT NULL DEFAULT 0,
  adjustment_days numeric(6,2) NOT NULL DEFAULT 0,
  used_days numeric(6,2) NOT NULL DEFAULT 0,
  updated_at_utc timestamptz NOT NULL DEFAULT now(),
  UNIQUE(employee_id,leave_type_id,leave_year)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.leave_requests (
  leave_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  leave_type_id uuid NOT NULL REFERENCES verigence_attendance.leave_types(leave_type_id),
  start_date date NOT NULL,
  end_date date NOT NULL,
  requested_days numeric(6,2) NOT NULL CHECK (requested_days > 0),
  reason text,
  status varchar(32) NOT NULL DEFAULT 'PENDING_OPERATIONAL'
    CHECK (status IN (
      'PENDING_OPERATIONAL','PENDING_HR','APPROVED','REJECTED','CANCELLED'
    )),
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  updated_at_utc timestamptz NOT NULL DEFAULT now(),
  CHECK (end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS ix_va_leave_employee_status
  ON verigence_attendance.leave_requests(employee_id,status,created_at_utc DESC);

CREATE TABLE IF NOT EXISTS verigence_attendance.reimbursement_claims (
  claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  expense_date date NOT NULL,
  category varchar(40) NOT NULL CHECK (category IN ('TRAVEL','FOOD','OTHER')),
  amount numeric(14,2) NOT NULL CHECK (amount > 0),
  description text,
  receipt_object_key varchar(700),
  receipt_sha256 char(64),
  status varchar(32) NOT NULL DEFAULT 'PENDING_HR'
    CHECK (status IN ('PENDING_HR','PENDING_FINANCE','APPROVED','REJECTED','PAID','CANCELLED')),
  finance_approval_required boolean NOT NULL DEFAULT false,
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  updated_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_va_claim_employee_month
  ON verigence_attendance.reimbursement_claims(employee_id,expense_date,status);

CREATE TABLE IF NOT EXISTS verigence_attendance.approval_actions (
  approval_action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type varchar(32) NOT NULL
    CHECK (entity_type IN ('LEAVE','REIMBURSEMENT','PAYROLL')),
  entity_id uuid NOT NULL,
  stage varchar(32) NOT NULL,
  decision varchar(20) NOT NULL CHECK (decision IN ('APPROVE','REJECT','VALIDATE','FINALIZE')),
  actor_user_id uuid NOT NULL,
  actor_role varchar(40) NOT NULL,
  comment text,
  decided_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_va_approval_entity
  ON verigence_attendance.approval_actions(entity_type,entity_id,decided_at_utc);

CREATE TABLE IF NOT EXISTS verigence_attendance.salary_structures (
  salary_structure_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  effective_from date NOT NULL,
  effective_to date,
  basic_salary numeric(14,2) NOT NULL DEFAULT 0,
  hra numeric(14,2) NOT NULL DEFAULT 0,
  allowances numeric(14,2) NOT NULL DEFAULT 0,
  other_earnings numeric(14,2) NOT NULL DEFAULT 0,
  fixed_deductions numeric(14,2) NOT NULL DEFAULT 0,
  created_by_user_id uuid NOT NULL,
  created_at_utc timestamptz NOT NULL DEFAULT now(),
  CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE INDEX IF NOT EXISTS ix_va_salary_employee_effective
  ON verigence_attendance.salary_structures(employee_id,effective_from DESC);

CREATE TABLE IF NOT EXISTS verigence_attendance.holidays (
  holiday_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  holiday_date date NOT NULL,
  holiday_name varchar(240) NOT NULL,
  work_location_id uuid REFERENCES verigence_attendance.work_locations(location_id),
  status varchar(20) NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE','INACTIVE')),
  UNIQUE(holiday_date,work_location_id)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.payroll_runs (
  payroll_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payroll_month date NOT NULL UNIQUE,
  status varchar(24) NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','CALCULATED','FINALIZED','CANCELLED')),
  generated_by_user_id uuid NOT NULL,
  generated_at_utc timestamptz NOT NULL DEFAULT now(),
  finalized_by_user_id uuid,
  finalized_at_utc timestamptz
);

CREATE TABLE IF NOT EXISTS verigence_attendance.payroll_items (
  payroll_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payroll_run_id uuid NOT NULL REFERENCES verigence_attendance.payroll_runs(payroll_run_id),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  scheduled_days numeric(6,2) NOT NULL,
  present_days numeric(6,2) NOT NULL,
  paid_leave_days numeric(6,2) NOT NULL,
  unpaid_leave_days numeric(6,2) NOT NULL,
  payable_days numeric(6,2) NOT NULL,
  gross_amount numeric(14,2) NOT NULL,
  deduction_amount numeric(14,2) NOT NULL,
  net_amount numeric(14,2) NOT NULL,
  calculation_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(payroll_run_id,employee_id)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.payslips (
  payslip_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payroll_item_id uuid NOT NULL UNIQUE
    REFERENCES verigence_attendance.payroll_items(payroll_item_id),
  employee_id uuid NOT NULL REFERENCES verigence_attendance.employees(employee_id),
  pdf_object_key varchar(700) NOT NULL,
  pdf_sha256 char(64) NOT NULL,
  generated_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS verigence_attendance.module_configuration (
  config_key varchar(120) PRIMARY KEY,
  config_value_json jsonb NOT NULL,
  updated_by_user_id uuid NOT NULL,
  updated_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS verigence_attendance.bulk_imports (
  import_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  filename varchar(255) NOT NULL,
  status varchar(24) NOT NULL
    CHECK (status IN ('UPLOADED','PREVIEW_READY','APPLIED','APPLIED_WITH_ERRORS','FAILED')),
  uploaded_by_user_id uuid NOT NULL,
  uploaded_at_utc timestamptz NOT NULL DEFAULT now(),
  applied_at_utc timestamptz
);

CREATE TABLE IF NOT EXISTS verigence_attendance.bulk_import_rows (
  import_row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  import_id uuid NOT NULL REFERENCES verigence_attendance.bulk_imports(import_id),
  row_number integer NOT NULL,
  action varchar(20) NOT NULL CHECK (action IN ('CREATE','UPDATE','UNCHANGED','ERROR')),
  employee_code varchar(80),
  payload_json jsonb NOT NULL,
  messages_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  applied boolean NOT NULL DEFAULT false,
  UNIQUE(import_id,row_number)
);

CREATE TABLE IF NOT EXISTS verigence_attendance.module_audit (
  audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_user_id uuid NOT NULL,
  action_key varchar(160) NOT NULL,
  entity_type varchar(80) NOT NULL,
  entity_id varchar(160),
  before_json jsonb,
  after_json jsonb,
  correlation_id varchar(160),
  occurred_at_utc timestamptz NOT NULL DEFAULT now()
);

-- Default module configuration. SuperAdmin can change these via the new module only.
INSERT INTO verigence_attendance.module_configuration
(config_key,config_value_json,updated_by_user_id)
VALUES
('attendance.default_geofence_meters','500'::jsonb,'00000000-0000-0000-0000-000000000000'),
('reimbursement.finance_threshold_inr','3000'::jsonb,'00000000-0000-0000-0000-000000000000'),
('payroll.working_days_per_week','6'::jsonb,'00000000-0000-0000-0000-000000000000')
ON CONFLICT (config_key) DO NOTHING;
