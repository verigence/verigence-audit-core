-- Verigence Audit Core — PostgreSQL Physical Schema Baseline
-- Document ID: VAC-DB-001
-- Version: 1.0
-- Status: BASELINED — Physical Design
-- Baseline date: 2026-08-15
-- Requirements: VAC-REQ-001
-- Solution design: VAC-SD-001
--
-- IMPORTANT RUNTIME SECURITY CONTRACT
-- 1. Migration/owner role and runtime application role MUST be different.
-- 2. Runtime role MUST NOT own these tables and MUST NOT have BYPASSRLS.
-- 3. After validating the Verigence Security token and before tenant SQL:
--      SET LOCAL app.tenant_id = '<validated tenant_id>';
-- 4. RLS is ENABLED and FORCED on all tenant tables in this baseline.
--
-- This baseline deliberately does not hard-code open business formulas/thresholds.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS auditcore;

-- -----------------------------------------------------------------------------
-- Helpers
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION auditcore.current_tenant_id()
RETURNS varchar
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::varchar;
$$;

CREATE OR REPLACE FUNCTION auditcore.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at_utc := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION auditcore.prevent_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'immutable audit record cannot be updated or deleted';
END;
$$;

-- -----------------------------------------------------------------------------
-- Tenant-scoped OEM / product catalogue
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.oems (
    tenant_id           varchar(128) NOT NULL,
    oem_id              uuid NOT NULL DEFAULT gen_random_uuid(),
    oem_code            varchar(80) NOT NULL,
    oem_name            varchar(200) NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, oem_id),
    UNIQUE (tenant_id, oem_code)
);

CREATE TABLE auditcore.product_categories (
    tenant_id           varchar(128) NOT NULL,
    product_category_id uuid NOT NULL DEFAULT gen_random_uuid(),
    category_code       varchar(80) NOT NULL,
    category_name       varchar(160) NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, product_category_id),
    UNIQUE (tenant_id, category_code)
);

CREATE TABLE auditcore.product_models (
    tenant_id           varchar(128) NOT NULL,
    model_id            uuid NOT NULL DEFAULT gen_random_uuid(),
    oem_id              uuid NOT NULL,
    model_code          varchar(100) NOT NULL,
    model_name          varchar(200) NOT NULL,
    effective_from      date,
    effective_to        date,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','RETIRED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, model_id),
    UNIQUE (tenant_id, oem_id, model_code),
    FOREIGN KEY (tenant_id, oem_id)
        REFERENCES auditcore.oems(tenant_id, oem_id),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

CREATE TABLE auditcore.product_variants (
    tenant_id           varchar(128) NOT NULL,
    variant_id          uuid NOT NULL DEFAULT gen_random_uuid(),
    model_id            uuid NOT NULL,
    variant_code        varchar(120) NOT NULL,
    variant_name        varchar(220) NOT NULL,
    fuel_type_code      varchar(80),
    powertrain_code     varchar(80),
    effective_from      date,
    effective_to        date,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','RETIRED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, variant_id),
    UNIQUE (tenant_id, model_id, variant_code),
    FOREIGN KEY (tenant_id, model_id)
        REFERENCES auditcore.product_models(tenant_id, model_id),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

CREATE TABLE auditcore.colours (
    tenant_id           varchar(128) NOT NULL,
    colour_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    colour_code         varchar(100) NOT NULL,
    colour_name         varchar(160) NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','RETIRED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, colour_id),
    UNIQUE (tenant_id, colour_code)
);

CREATE TABLE auditcore.product_skus (
    tenant_id           varchar(128) NOT NULL,
    product_sku_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    variant_id          uuid NOT NULL,
    colour_id           uuid,
    sku_code            varchar(140) NOT NULL,
    sku_name            varchar(260) NOT NULL,
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    effective_from      date,
    effective_to        date,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','RETIRED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, product_sku_id),
    UNIQUE (tenant_id, sku_code),
    FOREIGN KEY (tenant_id, variant_id)
        REFERENCES auditcore.product_variants(tenant_id, variant_id),
    FOREIGN KEY (tenant_id, colour_id)
        REFERENCES auditcore.colours(tenant_id, colour_id),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

-- -----------------------------------------------------------------------------
-- Dealers / outlets / projects
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.dealers (
    tenant_id           varchar(128) NOT NULL,
    dealer_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    dealer_code         varchar(100) NOT NULL,
    dealer_name         varchar(240) NOT NULL,
    legal_name          varchar(300),
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dealer_id),
    UNIQUE (tenant_id, dealer_code)
);

CREATE TABLE auditcore.dealer_outlets (
    tenant_id           varchar(128) NOT NULL,
    outlet_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    dealer_id           uuid NOT NULL,
    outlet_code         varchar(100) NOT NULL,
    outlet_name         varchar(240) NOT NULL,
    address_text        text,
    city                varchar(160),
    state_code          varchar(80),
    postal_code         varchar(20),
    latitude            numeric(9,6),
    longitude           numeric(9,6),
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, outlet_id),
    UNIQUE (tenant_id, dealer_id, outlet_code),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

CREATE TABLE auditcore.projects (
    tenant_id              varchar(128) NOT NULL,
    project_id             uuid NOT NULL DEFAULT gen_random_uuid(),
    project_code           varchar(100) NOT NULL,
    project_name           varchar(240) NOT NULL,
    oem_id                 uuid NOT NULL,
    product_category_id    uuid NOT NULL,
    time_zone              varchar(80) NOT NULL,
    effective_start_date   date NOT NULL,
    effective_end_date     date,
    status                 varchar(20) NOT NULL DEFAULT 'DRAFT'
                           CHECK (status IN ('DRAFT','ACTIVE','SUSPENDED','CLOSED')),
    satellite_threshold_monthly integer,
    satellite_classification_mode varchar(20) NOT NULL DEFAULT 'MANUAL'
                           CHECK (satellite_classification_mode IN ('MANUAL','AUTOMATIC')),
    version_no             integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    created_by_actor_id    varchar(160),
    updated_by_actor_id    varchar(160),
    created_at_utc         timestamptz NOT NULL DEFAULT now(),
    updated_at_utc         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id),
    UNIQUE (tenant_id, project_code),
    FOREIGN KEY (tenant_id, oem_id)
        REFERENCES auditcore.oems(tenant_id, oem_id),
    FOREIGN KEY (tenant_id, product_category_id)
        REFERENCES auditcore.product_categories(tenant_id, product_category_id),
    CHECK (effective_end_date IS NULL OR effective_end_date >= effective_start_date),
    CHECK (satellite_threshold_monthly IS NULL OR satellite_threshold_monthly >= 0)
);

CREATE TABLE auditcore.project_dealers (
    tenant_id           varchar(128) NOT NULL,
    project_id          uuid NOT NULL,
    dealer_id           uuid NOT NULL,
    effective_from      date NOT NULL,
    effective_to        date,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, dealer_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE auditcore.project_outlets (
    tenant_id             varchar(128) NOT NULL,
    project_id            uuid NOT NULL,
    dealer_id             uuid NOT NULL,
    outlet_id             uuid NOT NULL,
    location_class        varchar(20) NOT NULL
                          CHECK (location_class IN ('ONSITE','SATELLITE')),
    monthly_volume_threshold integer,
    classification_reason text,
    effective_from        date NOT NULL,
    effective_to          date,
    status                varchar(20) NOT NULL DEFAULT 'ACTIVE'
                          CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id   varchar(160),
    created_at_utc        timestamptz NOT NULL DEFAULT now(),
    updated_at_utc        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, outlet_id),
    FOREIGN KEY (tenant_id, project_id, dealer_id)
        REFERENCES auditcore.project_dealers(tenant_id, project_id, dealer_id),
    FOREIGN KEY (tenant_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, outlet_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (monthly_volume_threshold IS NULL OR monthly_volume_threshold >= 0)
);

CREATE TABLE auditcore.project_assignments (
    tenant_id           varchar(128) NOT NULL,
    assignment_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    principal_id        varchar(160) NOT NULL,
    business_role_key   varchar(40) NOT NULL
                        CHECK (business_role_key IN ('PC','TL','PM','CRM','EXECUTIVE','OTHER')),
    effective_from      date NOT NULL,
    effective_to        date,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, assignment_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE INDEX ix_project_assignments_actor
    ON auditcore.project_assignments(tenant_id, principal_id, project_id, status);

CREATE TABLE auditcore.project_assignment_scopes (
    tenant_id           varchar(128) NOT NULL,
    assignment_scope_id uuid NOT NULL DEFAULT gen_random_uuid(),
    assignment_id       uuid NOT NULL,
    scope_type          varchar(20) NOT NULL
                        CHECK (scope_type IN ('PROJECT','DEALER','OUTLET')),
    dealer_id           uuid,
    outlet_id           uuid,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, assignment_scope_id),
    FOREIGN KEY (tenant_id, assignment_id)
        REFERENCES auditcore.project_assignments(tenant_id, assignment_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    FOREIGN KEY (tenant_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, outlet_id),
    CHECK (
        (scope_type = 'PROJECT' AND dealer_id IS NULL AND outlet_id IS NULL)
        OR (scope_type = 'DEALER' AND dealer_id IS NOT NULL AND outlet_id IS NULL)
        OR (scope_type = 'OUTLET' AND outlet_id IS NOT NULL)
    )
);

CREATE TABLE auditcore.dealership_participants (
    tenant_id           varchar(128) NOT NULL,
    participant_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    dealer_id           uuid NOT NULL,
    outlet_id           uuid,
    participant_role_code varchar(80) NOT NULL,
    display_name        varchar(240) NOT NULL,
    employee_reference  varchar(120),
    mobile_number       varchar(40),
    email               varchar(320),
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
    effective_from      date,
    effective_to        date,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, participant_id),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    FOREIGN KEY (tenant_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, outlet_id),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

-- -----------------------------------------------------------------------------
-- Generic controlled lookup values
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.lookup_values (
    tenant_id           varchar(128) NOT NULL,
    lookup_value_id     uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid,
    domain_key          varchar(100) NOT NULL,
    code                varchar(100) NOT NULL,
    display_name        varchar(240) NOT NULL,
    sort_order          integer NOT NULL DEFAULT 0,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    effective_from      date,
    effective_to        date,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','RETIRED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, lookup_value_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    UNIQUE (tenant_id, project_id, domain_key, code),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

-- -----------------------------------------------------------------------------
-- Commercial masters
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.commercial_component_types (
    tenant_id           varchar(128) NOT NULL,
    component_type_id   uuid NOT NULL DEFAULT gen_random_uuid(),
    component_code      varchar(100) NOT NULL,
    component_name      varchar(220) NOT NULL,
    component_class     varchar(30) NOT NULL DEFAULT 'CHARGE'
                        CHECK (component_class IN ('CHARGE','DISCOUNT','TAX','TOTAL','OTHER')),
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','RETIRED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, component_type_id),
    UNIQUE (tenant_id, component_code)
);

CREATE TABLE auditcore.price_lists (
    tenant_id           varchar(128) NOT NULL,
    price_list_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    price_list_code     varchar(120) NOT NULL,
    price_list_name     varchar(240) NOT NULL,
    version_no          integer NOT NULL CHECK (version_no > 0),
    currency_code       char(3) NOT NULL DEFAULT 'INR',
    status              varchar(20) NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from      date NOT NULL,
    effective_to        date,
    published_by_actor_id varchar(160),
    published_at_utc    timestamptz,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, price_list_id),
    UNIQUE (tenant_id, project_id, price_list_code, version_no),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK ((status <> 'PUBLISHED') OR published_at_utc IS NOT NULL)
);

CREATE INDEX ix_price_lists_effective
    ON auditcore.price_lists(tenant_id, project_id, status, effective_from, effective_to);

CREATE TABLE auditcore.price_list_items (
    tenant_id           varchar(128) NOT NULL,
    price_list_item_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    price_list_id       uuid NOT NULL,
    product_sku_id      uuid NOT NULL,
    component_type_id   uuid NOT NULL,
    amount              numeric(18,2) NOT NULL,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, price_list_item_id),
    UNIQUE (tenant_id, price_list_id, product_sku_id, component_type_id),
    FOREIGN KEY (tenant_id, price_list_id)
        REFERENCES auditcore.price_lists(tenant_id, price_list_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, product_sku_id)
        REFERENCES auditcore.product_skus(tenant_id, product_sku_id),
    FOREIGN KEY (tenant_id, component_type_id)
        REFERENCES auditcore.commercial_component_types(tenant_id, component_type_id)
);

CREATE TABLE auditcore.discount_schemes (
    tenant_id           varchar(128) NOT NULL,
    discount_scheme_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    scheme_code         varchar(120) NOT NULL,
    scheme_name         varchar(240) NOT NULL,
    discount_type_code  varchar(100) NOT NULL,
    version_no          integer NOT NULL CHECK (version_no > 0),
    status              varchar(20) NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from      date NOT NULL,
    effective_to        date,
    combinability_group varchar(100),
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_by_actor_id varchar(160),
    published_at_utc    timestamptz,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, discount_scheme_id),
    UNIQUE (tenant_id, project_id, scheme_code, version_no),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK ((status <> 'PUBLISHED') OR published_at_utc IS NOT NULL)
);

CREATE INDEX ix_discount_schemes_effective
    ON auditcore.discount_schemes(tenant_id, project_id, status, effective_from, effective_to);

-- One row represents one conjunction of eligibility dimensions; multiple group_no rows
-- may be treated by the application as alternative (OR) groups.
CREATE TABLE auditcore.discount_eligibility (
    tenant_id           varchar(128) NOT NULL,
    discount_eligibility_id uuid NOT NULL DEFAULT gen_random_uuid(),
    discount_scheme_id  uuid NOT NULL,
    group_no            integer NOT NULL DEFAULT 1 CHECK (group_no > 0),
    model_id            uuid,
    variant_id          uuid,
    product_sku_id      uuid,
    colour_id           uuid,
    dealer_id           uuid,
    outlet_id           uuid,
    customer_type_code  varchar(100),
    criteria_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, discount_eligibility_id),
    FOREIGN KEY (tenant_id, discount_scheme_id)
        REFERENCES auditcore.discount_schemes(tenant_id, discount_scheme_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, model_id)
        REFERENCES auditcore.product_models(tenant_id, model_id),
    FOREIGN KEY (tenant_id, variant_id)
        REFERENCES auditcore.product_variants(tenant_id, variant_id),
    FOREIGN KEY (tenant_id, product_sku_id)
        REFERENCES auditcore.product_skus(tenant_id, product_sku_id),
    FOREIGN KEY (tenant_id, colour_id)
        REFERENCES auditcore.colours(tenant_id, colour_id),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    FOREIGN KEY (tenant_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, outlet_id)
);

CREATE TABLE auditcore.discount_benefits (
    tenant_id           varchar(128) NOT NULL,
    discount_benefit_id uuid NOT NULL DEFAULT gen_random_uuid(),
    discount_scheme_id  uuid NOT NULL,
    component_type_id   uuid,
    benefit_kind        varchar(20) NOT NULL
                        CHECK (benefit_kind IN ('AMOUNT','PERCENTAGE','CONFIGURED')),
    amount              numeric(18,2),
    percentage          numeric(9,4),
    max_amount          numeric(18,2),
    config_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, discount_benefit_id),
    FOREIGN KEY (tenant_id, discount_scheme_id)
        REFERENCES auditcore.discount_schemes(tenant_id, discount_scheme_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, component_type_id)
        REFERENCES auditcore.commercial_component_types(tenant_id, component_type_id),
    CHECK (percentage IS NULL OR (percentage >= 0 AND percentage <= 100))
);

-- -----------------------------------------------------------------------------
-- Business document requirement profiles
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.document_requirement_profiles (
    tenant_id           varchar(128) NOT NULL,
    document_requirement_profile_id uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    profile_code        varchar(120) NOT NULL,
    profile_name        varchar(240) NOT NULL,
    version_no          integer NOT NULL CHECK (version_no > 0),
    status              varchar(20) NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from      date NOT NULL,
    effective_to        date,
    published_by_actor_id varchar(160),
    published_at_utc    timestamptz,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_requirement_profile_id),
    UNIQUE (tenant_id, project_id, profile_code, version_no),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK ((status <> 'PUBLISHED') OR published_at_utc IS NOT NULL)
);

CREATE TABLE auditcore.document_requirement_items (
    tenant_id           varchar(128) NOT NULL,
    document_requirement_item_id uuid NOT NULL DEFAULT gen_random_uuid(),
    document_requirement_profile_id uuid NOT NULL,
    stage_code          varchar(40) NOT NULL,
    document_type_key   varchar(120) NOT NULL,
    requirement_kind    varchar(20) NOT NULL
                        CHECK (requirement_kind IN ('MANDATORY','CONDITIONAL','OPTIONAL')),
    condition_key       varchar(120),
    condition_config    jsonb NOT NULL DEFAULT '{}'::jsonb,
    display_order       integer NOT NULL DEFAULT 0,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_requirement_item_id),
    UNIQUE (tenant_id, document_requirement_profile_id, stage_code, document_type_key, condition_key),
    FOREIGN KEY (tenant_id, document_requirement_profile_id)
        REFERENCES auditcore.document_requirement_profiles(tenant_id, document_requirement_profile_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- Audit control catalogue and project bindings
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.audit_control_definitions (
    tenant_id           varchar(128) NOT NULL,
    audit_control_definition_id uuid NOT NULL DEFAULT gen_random_uuid(),
    control_key         varchar(140) NOT NULL,
    control_name        varchar(260) NOT NULL,
    description         text,
    domain_area         varchar(80) NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','RETIRED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_control_definition_id),
    UNIQUE (tenant_id, control_key)
);

CREATE TABLE auditcore.audit_control_versions (
    tenant_id           varchar(128) NOT NULL,
    audit_control_version_id uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_control_definition_id uuid NOT NULL,
    version_no          integer NOT NULL CHECK (version_no > 0),
    evaluator_key       varchar(160) NOT NULL,
    rule_config         jsonb NOT NULL DEFAULT '{}'::jsonb,
    default_severity    varchar(20) NOT NULL DEFAULT 'MEDIUM'
                        CHECK (default_severity IN ('INFO','LOW','MEDIUM','HIGH','CRITICAL')),
    create_finding_on   varchar(20) NOT NULL DEFAULT 'FAIL'
                        CHECK (create_finding_on IN ('FAIL','REVIEW','FAIL_OR_REVIEW','NEVER')),
    status              varchar(20) NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
    published_by_actor_id varchar(160),
    published_at_utc    timestamptz,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_control_version_id),
    UNIQUE (tenant_id, audit_control_definition_id, version_no),
    FOREIGN KEY (tenant_id, audit_control_definition_id)
        REFERENCES auditcore.audit_control_definitions(tenant_id, audit_control_definition_id),
    CHECK ((status <> 'PUBLISHED') OR published_at_utc IS NOT NULL)
);

CREATE TABLE auditcore.project_control_bindings (
    tenant_id           varchar(128) NOT NULL,
    project_control_binding_id uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    audit_control_version_id uuid NOT NULL,
    stage_code          varchar(40),
    severity_override   varchar(20)
                        CHECK (severity_override IS NULL OR severity_override IN ('INFO','LOW','MEDIUM','HIGH','CRITICAL')),
    trigger_work_type   varchar(100),
    trigger_crm         boolean NOT NULL DEFAULT false,
    effective_from      date NOT NULL,
    effective_to        date,
    enabled             boolean NOT NULL DEFAULT true,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_control_binding_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    FOREIGN KEY (tenant_id, audit_control_version_id)
        REFERENCES auditcore.audit_control_versions(tenant_id, audit_control_version_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

-- -----------------------------------------------------------------------------
-- Customer / Audit Case
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.customers (
    tenant_id           varchar(128) NOT NULL,
    customer_id         uuid NOT NULL DEFAULT gen_random_uuid(),
    customer_type_code  varchar(100) NOT NULL,
    display_name        varchar(260),
    mobile_e164         varchar(40),
    email               varchar(320),
    pan_match_hash      varchar(128),
    aadhaar_match_hash  varchar(128),
    gst_match_hash      varchar(128),
    mobile_match_hash   varchar(128),
    postal_code         varchar(20),
    di_subject_id       uuid,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE','MERGED')),
    merged_into_customer_id uuid,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, customer_id),
    FOREIGN KEY (tenant_id, merged_into_customer_id)
        REFERENCES auditcore.customers(tenant_id, customer_id),
    CHECK ((status <> 'MERGED') OR merged_into_customer_id IS NOT NULL)
);

CREATE INDEX ix_customers_pan_hash ON auditcore.customers(tenant_id, pan_match_hash) WHERE pan_match_hash IS NOT NULL;
CREATE INDEX ix_customers_aadhaar_hash ON auditcore.customers(tenant_id, aadhaar_match_hash) WHERE aadhaar_match_hash IS NOT NULL;
CREATE INDEX ix_customers_gst_hash ON auditcore.customers(tenant_id, gst_match_hash) WHERE gst_match_hash IS NOT NULL;
CREATE INDEX ix_customers_mobile_hash ON auditcore.customers(tenant_id, mobile_match_hash) WHERE mobile_match_hash IS NOT NULL;
CREATE INDEX ix_customers_di_subject ON auditcore.customers(tenant_id, di_subject_id) WHERE di_subject_id IS NOT NULL;

CREATE TABLE auditcore.audit_cases (
    tenant_id           varchar(128) NOT NULL,
    audit_case_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    dealer_id           uuid NOT NULL,
    outlet_id           uuid NOT NULL,
    customer_id         uuid NOT NULL,
    booking_reference   varchar(160) NOT NULL,
    booking_date        date NOT NULL,
    booking_intimated_at_utc timestamptz,
    assigned_pc_principal_id varchar(160),
    sales_participant_id uuid,
    business_stage      varchar(30) NOT NULL DEFAULT 'BOOKING'
                        CHECK (business_stage IN ('BOOKING','ACTIVE','PRE_DELIVERY','DELIVERY','POST_DELIVERY','CLOSED','CANCELLED')),
    review_state        varchar(30) NOT NULL DEFAULT 'PC_IN_PROGRESS'
                        CHECK (review_state IN ('PC_IN_PROGRESS','PC_SUBMITTED','TL_REVIEW','SENT_BACK','REVIEW_COMPLETE')),
    audit_outcome       varchar(20) NOT NULL DEFAULT 'PENDING'
                        CHECK (audit_outcome IN ('PENDING','NO_BREACH','BREACH')),
    scope_status        varchar(30) NOT NULL DEFAULT 'REVIEW_REQUIRED'
                        CHECK (scope_status IN ('IN_SCOPE','OUT_OF_SCOPE','REVIEW_REQUIRED')),
    out_of_scope_reason_code varchar(100),
    deal_type_code      varchar(100),
    deal_source_code    varchar(100),
    lead_source_code    varchar(100),
    finance_type_code   varchar(100),
    price_list_id       uuid,
    document_requirement_profile_id uuid,
    planned_delivery_at_utc timestamptz,
    cancelled_reason    text,
    created_by_actor_id varchar(160),
    updated_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    closed_at_utc       timestamptz,
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, audit_case_id),
    UNIQUE (tenant_id, project_id, dealer_id, booking_reference),
    FOREIGN KEY (tenant_id, project_id, dealer_id)
        REFERENCES auditcore.project_dealers(tenant_id, project_id, dealer_id),
    FOREIGN KEY (tenant_id, project_id, outlet_id)
        REFERENCES auditcore.project_outlets(tenant_id, project_id, outlet_id),
    FOREIGN KEY (tenant_id, customer_id)
        REFERENCES auditcore.customers(tenant_id, customer_id),
    FOREIGN KEY (tenant_id, sales_participant_id)
        REFERENCES auditcore.dealership_participants(tenant_id, participant_id),
    FOREIGN KEY (tenant_id, price_list_id)
        REFERENCES auditcore.price_lists(tenant_id, price_list_id),
    FOREIGN KEY (tenant_id, document_requirement_profile_id)
        REFERENCES auditcore.document_requirement_profiles(tenant_id, document_requirement_profile_id),
    CHECK ((business_stage <> 'CANCELLED') OR cancelled_reason IS NOT NULL)
);

CREATE INDEX ix_audit_cases_project_status
    ON auditcore.audit_cases(tenant_id, project_id, business_stage, review_state, booking_date DESC);
CREATE INDEX ix_audit_cases_outlet_date
    ON auditcore.audit_cases(tenant_id, project_id, outlet_id, booking_date DESC);
CREATE INDEX ix_audit_cases_customer
    ON auditcore.audit_cases(tenant_id, customer_id, booking_date DESC);
CREATE INDEX ix_audit_cases_pc
    ON auditcore.audit_cases(tenant_id, assigned_pc_principal_id, review_state)
    WHERE assigned_pc_principal_id IS NOT NULL;

CREATE TABLE auditcore.case_product_details (
    tenant_id           varchar(128) NOT NULL,
    audit_case_id       uuid NOT NULL,
    product_sku_id      uuid,
    vin_number          varchar(100),
    chassis_number      varchar(100),
    dms_customer_reference varchar(160),
    dms_invoice_number  varchar(160),
    dms_invoice_date    date,
    registration_state_code varchar(80),
    registration_district varchar(160),
    registration_type_code varchar(100),
    registration_category_code varchar(100),
    territory_category_code varchar(100),
    source_kind         varchar(30) NOT NULL DEFAULT 'USER_OPERATIONAL'
                        CHECK (source_kind IN ('USER_OPERATIONAL','UPSTREAM_SYSTEM','DI_MACHINE','DI_HUMAN_VERIFIED','SYSTEM_CALCULATED','MASTER_RESOLVED')),
    source_di_document_id uuid,
    source_field_key    varchar(160),
    updated_by_actor_id varchar(160),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, product_sku_id)
        REFERENCES auditcore.product_skus(tenant_id, product_sku_id)
);

CREATE TABLE auditcore.case_commercial_lines (
    tenant_id           varchar(128) NOT NULL,
    case_commercial_line_id uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    component_type_id   uuid NOT NULL,
    price_list_item_id  uuid,
    standard_amount     numeric(18,2),
    actual_amount       numeric(18,2),
    currency_code       char(3) NOT NULL DEFAULT 'INR',
    source_kind         varchar(30) NOT NULL
                        CHECK (source_kind IN ('USER_OPERATIONAL','UPSTREAM_SYSTEM','DI_MACHINE','DI_HUMAN_VERIFIED','SYSTEM_CALCULATED','MASTER_RESOLVED')),
    source_di_document_id uuid,
    source_field_key    varchar(160),
    provenance_detail   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, case_commercial_line_id),
    UNIQUE (tenant_id, audit_case_id, component_type_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, component_type_id)
        REFERENCES auditcore.commercial_component_types(tenant_id, component_type_id),
    FOREIGN KEY (tenant_id, price_list_item_id)
        REFERENCES auditcore.price_list_items(tenant_id, price_list_item_id)
);

CREATE TABLE auditcore.case_discounts (
    tenant_id           varchar(128) NOT NULL,
    case_discount_id    uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    discount_scheme_id  uuid,
    discount_type_code  varchar(100) NOT NULL,
    standard_amount     numeric(18,2),
    actual_amount       numeric(18,2),
    source_kind         varchar(30) NOT NULL
                        CHECK (source_kind IN ('USER_OPERATIONAL','UPSTREAM_SYSTEM','DI_MACHINE','DI_HUMAN_VERIFIED','SYSTEM_CALCULATED','MASTER_RESOLVED')),
    source_di_document_id uuid,
    source_field_key    varchar(160),
    provenance_detail   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, case_discount_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, discount_scheme_id)
        REFERENCES auditcore.discount_schemes(tenant_id, discount_scheme_id)
);

-- -----------------------------------------------------------------------------
-- Case evidence requirements and DI references
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.case_document_requirements (
    tenant_id           varchar(128) NOT NULL,
    case_document_requirement_id uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    document_requirement_item_id uuid,
    stage_code          varchar(40) NOT NULL,
    document_type_key   varchar(120) NOT NULL,
    requirement_kind    varchar(20) NOT NULL
                        CHECK (requirement_kind IN ('MANDATORY','CONDITIONAL','OPTIONAL')),
    requirement_status  varchar(20) NOT NULL DEFAULT 'PENDING'
                        CHECK (requirement_status IN ('PENDING','SATISFIED','WAIVED','NOT_APPLICABLE')),
    condition_snapshot  jsonb NOT NULL DEFAULT '{}'::jsonb,
    waiver_reason       text,
    resolved_at_utc     timestamptz,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, case_document_requirement_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, document_requirement_item_id)
        REFERENCES auditcore.document_requirement_items(tenant_id, document_requirement_item_id),
    CHECK ((requirement_status <> 'WAIVED') OR waiver_reason IS NOT NULL)
);

CREATE TABLE auditcore.evidence_links (
    tenant_id           varchar(128) NOT NULL,
    evidence_link_id    uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    case_document_requirement_id uuid,
    di_subject_id       uuid NOT NULL,
    di_document_id      uuid NOT NULL,
    di_entity_link_id   uuid,
    document_type_key   varchar(120),
    evidence_purpose_code varchar(100),
    stage_code          varchar(40),
    cached_upload_status varchar(30),
    cached_processing_status varchar(30),
    cached_confirmation_status varchar(30),
    cached_confidence_score numeric(7,4),
    last_synced_at_utc  timestamptz,
    active              boolean NOT NULL DEFAULT true,
    linked_by_actor_id  varchar(160),
    linked_at_utc       timestamptz NOT NULL DEFAULT now(),
    unlinked_by_actor_id varchar(160),
    unlinked_at_utc     timestamptz,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, evidence_link_id),
    UNIQUE (tenant_id, audit_case_id, di_document_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, case_document_requirement_id)
        REFERENCES auditcore.case_document_requirements(tenant_id, case_document_requirement_id),
    CHECK ((active = true AND unlinked_at_utc IS NULL) OR (active = false AND unlinked_at_utc IS NOT NULL))
);

CREATE INDEX ix_evidence_links_di_document
    ON auditcore.evidence_links(tenant_id, di_document_id);
CREATE INDEX ix_evidence_links_case_active
    ON auditcore.evidence_links(tenant_id, audit_case_id, active, stage_code);

-- -----------------------------------------------------------------------------
-- Payments / Finance
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.payments (
    tenant_id           varchar(128) NOT NULL,
    payment_id          uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    payment_mode_code   varchar(100) NOT NULL,
    payment_date        date,
    amount              numeric(18,2) NOT NULL CHECK (amount >= 0),
    currency_code       char(3) NOT NULL DEFAULT 'INR',
    payer_type          varchar(30)
                        CHECK (payer_type IS NULL OR payer_type IN ('CUSTOMER','FAMILY_MEMBER','THIRD_PARTY','FINANCIER','DEALER','OTHER')),
    payer_name          varchar(260),
    reference_number    varchar(240),
    bank_name           varchar(240),
    realised_amount     numeric(18,2),
    verification_status varchar(30) NOT NULL DEFAULT 'PENDING'
                        CHECK (verification_status IN ('PENDING','VERIFIED','REJECTED','EXCEPTION')),
    verification_detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    verified_by_actor_id varchar(160),
    verified_at_utc     timestamptz,
    source_kind         varchar(30) NOT NULL DEFAULT 'USER_OPERATIONAL'
                        CHECK (source_kind IN ('USER_OPERATIONAL','UPSTREAM_SYSTEM','DI_MACHINE','DI_HUMAN_VERIFIED','SYSTEM_CALCULATED','MASTER_RESOLVED')),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, payment_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    CHECK (realised_amount IS NULL OR realised_amount >= 0)
);

CREATE INDEX ix_payments_case_status
    ON auditcore.payments(tenant_id, audit_case_id, verification_status, payment_date);
CREATE INDEX ix_payments_reference
    ON auditcore.payments(tenant_id, reference_number) WHERE reference_number IS NOT NULL;

CREATE TABLE auditcore.payment_evidence_links (
    tenant_id           varchar(128) NOT NULL,
    payment_id          uuid NOT NULL,
    evidence_link_id    uuid NOT NULL,
    evidence_role       varchar(80) NOT NULL DEFAULT 'PAYMENT_PROOF',
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, payment_id, evidence_link_id),
    FOREIGN KEY (tenant_id, payment_id)
        REFERENCES auditcore.payments(tenant_id, payment_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, evidence_link_id)
        REFERENCES auditcore.evidence_links(tenant_id, evidence_link_id)
);

CREATE TABLE auditcore.finance_records (
    tenant_id           varchar(128) NOT NULL,
    finance_record_id   uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    finance_type_code   varchar(100),
    financier_name      varchar(240),
    delivery_order_reference varchar(200),
    purchase_order_reference varchar(200),
    sanctioned_amount   numeric(18,2),
    realised_amount     numeric(18,2),
    outstanding_amount  numeric(18,2),
    status              varchar(30) NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','PARTIALLY_REALISED','REALISED','EXCEPTION','CANCELLED')),
    detail              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, finance_record_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    CHECK (sanctioned_amount IS NULL OR sanctioned_amount >= 0),
    CHECK (realised_amount IS NULL OR realised_amount >= 0)
);

-- -----------------------------------------------------------------------------
-- Delivery / Insurance / VAS / Trade-In
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.delivery_records (
    tenant_id           varchar(128) NOT NULL,
    delivery_record_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    delivery_status     varchar(30) NOT NULL DEFAULT 'NOT_PLANNED'
                        CHECK (delivery_status IN ('NOT_PLANNED','PLANNED','INTIMATED','READY_FOR_AUDIT','AUDIT_IN_PROGRESS','DELIVERED','EXCEPTION','CANCELLED')),
    planned_delivery_at_utc timestamptz,
    intimated_at_utc    timestamptz,
    prior_intimation_received boolean,
    physical_verification_status varchar(30) NOT NULL DEFAULT 'NOT_STARTED'
                        CHECK (physical_verification_status IN ('NOT_STARTED','IN_PROGRESS','COMPLETED','EXCEPTION')),
    verification_photo_count integer NOT NULL DEFAULT 0 CHECK (verification_photo_count >= 0),
    vin_verified        boolean,
    actual_delivery_at_utc timestamptz,
    dms_invoice_number  varchar(160),
    dms_invoice_date    date,
    exception_remarks   text,
    verified_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, delivery_record_id),
    UNIQUE (tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id)
);

CREATE TABLE auditcore.insurance_records (
    tenant_id           varchar(128) NOT NULL,
    insurance_record_id uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    insurance_type_code varchar(100) NOT NULL,
    insurer_name        varchar(240),
    premium_amount      numeric(18,2),
    approved_premium_amount numeric(18,2),
    od_discount_amount  numeric(18,2),
    agent_code          varchar(120),
    policy_or_cover_reference varchar(200),
    verification_status varchar(30) NOT NULL DEFAULT 'PENDING'
                        CHECK (verification_status IN ('PENDING','VERIFIED','EXCEPTION','NOT_APPLICABLE')),
    detail              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, insurance_record_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id)
);

CREATE TABLE auditcore.case_addons (
    tenant_id           varchar(128) NOT NULL,
    case_addon_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    addon_type_code     varchar(100) NOT NULL,
    plan_code           varchar(120),
    classification_code varchar(100),
    standard_amount     numeric(18,2),
    actual_amount       numeric(18,2),
    status              varchar(30) NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','SELECTED','VERIFIED','EXCEPTION','NOT_APPLICABLE')),
    detail              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, case_addon_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id)
);

CREATE TABLE auditcore.trade_in_cases (
    tenant_id           varchar(128) NOT NULL,
    trade_in_case_id    uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    state               varchar(30) NOT NULL DEFAULT 'IDENTIFIED'
                        CHECK (state IN ('NOT_APPLICABLE','IDENTIFIED','VEHICLE_HANDED_OVER','VERIFICATION_PENDING','VERIFIED','PAYMENT_PENDING','COMPLETED','EXCEPTION','CANCELLED')),
    old_vehicle_registration varchar(100),
    registered_owner_name varchar(260),
    owner_relationship_code varchar(100),
    purchase_value      numeric(18,2),
    vehicle_handed_over_at_utc timestamptz,
    ageing_start_date   date,
    payment_realised_amount numeric(18,2),
    exception_detail    text,
    created_by_actor_id varchar(160),
    verified_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, trade_in_case_id),
    UNIQUE (tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    CHECK (purchase_value IS NULL OR purchase_value >= 0),
    CHECK (payment_realised_amount IS NULL OR payment_realised_amount >= 0)
);

-- -----------------------------------------------------------------------------
-- Evaluations / findings / review
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.audit_evaluations (
    tenant_id           varchar(128) NOT NULL,
    audit_evaluation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    project_control_binding_id uuid,
    audit_control_version_id uuid NOT NULL,
    evaluation_run_type varchar(20) NOT NULL DEFAULT 'SYSTEM'
                        CHECK (evaluation_run_type IN ('SYSTEM','MANUAL','REPROCESS')),
    result              varchar(30) NOT NULL
                        CHECK (result IN ('PASS','FAIL','REVIEW','NOT_APPLICABLE','ERROR')),
    expected_snapshot   jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_snapshot   jsonb NOT NULL DEFAULT '{}'::jsonb,
    config_snapshot     jsonb NOT NULL DEFAULT '{}'::jsonb,
    detail              text,
    evaluated_by_actor_id varchar(160),
    correlation_id      varchar(160),
    evaluated_at_utc    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_evaluation_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, project_control_binding_id)
        REFERENCES auditcore.project_control_bindings(tenant_id, project_control_binding_id),
    FOREIGN KEY (tenant_id, audit_control_version_id)
        REFERENCES auditcore.audit_control_versions(tenant_id, audit_control_version_id)
);

CREATE INDEX ix_evaluations_case_time
    ON auditcore.audit_evaluations(tenant_id, audit_case_id, evaluated_at_utc DESC);

CREATE TABLE auditcore.audit_findings (
    tenant_id           varchar(128) NOT NULL,
    audit_finding_id    uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    audit_evaluation_id uuid,
    finding_type_code   varchar(120) NOT NULL,
    title               varchar(300) NOT NULL,
    description         text,
    severity            varchar(20) NOT NULL
                        CHECK (severity IN ('INFO','LOW','MEDIUM','HIGH','CRITICAL')),
    status              varchar(30) NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED','VOIDED')),
    expected_value      jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_value      jsonb NOT NULL DEFAULT '{}'::jsonb,
    opened_by_actor_id  varchar(160),
    opened_at_utc       timestamptz NOT NULL DEFAULT now(),
    acknowledged_by_actor_id varchar(160),
    acknowledged_at_utc timestamptz,
    resolved_by_actor_id varchar(160),
    resolved_at_utc     timestamptz,
    resolution_code     varchar(100),
    resolution_detail   text,
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, audit_finding_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, audit_evaluation_id)
        REFERENCES auditcore.audit_evaluations(tenant_id, audit_evaluation_id),
    CHECK ((status NOT IN ('RESOLVED','VOIDED')) OR resolved_at_utc IS NOT NULL)
);

CREATE INDEX ix_findings_open
    ON auditcore.audit_findings(tenant_id, audit_case_id, severity, status)
    WHERE status IN ('OPEN','ACKNOWLEDGED');

CREATE TABLE auditcore.finding_evidence_refs (
    tenant_id           varchar(128) NOT NULL,
    finding_evidence_ref_id uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_finding_id    uuid NOT NULL,
    evidence_link_id    uuid,
    fact_key            varchar(160),
    source_kind         varchar(30),
    reference_detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, finding_evidence_ref_id),
    FOREIGN KEY (tenant_id, audit_finding_id)
        REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, evidence_link_id)
        REFERENCES auditcore.evidence_links(tenant_id, evidence_link_id)
);

CREATE TABLE auditcore.review_decisions (
    tenant_id           varchar(128) NOT NULL,
    review_decision_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_case_id       uuid NOT NULL,
    decision            varchar(30) NOT NULL
                        CHECK (decision IN ('BREACH','NO_BREACH','SEND_BACK')),
    remarks             text,
    decided_by_actor_id varchar(160) NOT NULL,
    decided_as_business_role varchar(40) NOT NULL,
    correlation_id      varchar(160),
    decided_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, review_decision_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id)
);

CREATE INDEX ix_review_decisions_case
    ON auditcore.review_decisions(tenant_id, audit_case_id, decided_at_utc DESC);

-- -----------------------------------------------------------------------------
-- Lightweight work management
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.work_items (
    tenant_id           varchar(128) NOT NULL,
    work_item_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    audit_case_id       uuid,
    work_type           varchar(100) NOT NULL,
    status              varchar(30) NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','IN_PROGRESS','COMPLETED','CANCELLED','EXPIRED')),
    priority            varchar(20) NOT NULL DEFAULT 'NORMAL'
                        CHECK (priority IN ('LOW','NORMAL','HIGH','URGENT')),
    assigned_principal_id varchar(160),
    assigned_business_role varchar(40),
    due_at_utc          timestamptz,
    source_type         varchar(80),
    source_id           varchar(160),
    dedup_key           varchar(300),
    title               varchar(300) NOT NULL,
    detail              text,
    payload             jsonb NOT NULL DEFAULT '{}'::jsonb,
    claimed_at_utc      timestamptz,
    completed_at_utc    timestamptz,
    completed_by_actor_id varchar(160),
    created_by_actor_id varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, work_item_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    CHECK ((status <> 'COMPLETED') OR completed_at_utc IS NOT NULL)
);

CREATE UNIQUE INDEX ux_work_items_open_dedup
    ON auditcore.work_items(tenant_id, dedup_key)
    WHERE dedup_key IS NOT NULL AND status IN ('OPEN','IN_PROGRESS');

CREATE INDEX ix_work_items_queue
    ON auditcore.work_items(tenant_id, project_id, assigned_principal_id, status, due_at_utc)
    WHERE status IN ('OPEN','IN_PROGRESS');

CREATE TABLE auditcore.work_item_history (
    tenant_id           varchar(128) NOT NULL,
    work_item_history_id uuid NOT NULL DEFAULT gen_random_uuid(),
    work_item_id        uuid NOT NULL,
    from_status         varchar(30),
    to_status           varchar(30) NOT NULL,
    actor_id            varchar(160),
    reason              text,
    occurred_at_utc     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, work_item_history_id),
    FOREIGN KEY (tenant_id, work_item_id)
        REFERENCES auditcore.work_items(tenant_id, work_item_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- CRM / escalation
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.crm_interactions (
    tenant_id           varchar(128) NOT NULL,
    crm_interaction_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    audit_case_id       uuid,
    work_item_id        uuid,
    interaction_type_code varchar(100) NOT NULL DEFAULT 'CALL',
    contact_target      varchar(240),
    contact_reference   varchar(160),
    outcome_code        varchar(120),
    notes               text,
    attempted_by_actor_id varchar(160) NOT NULL,
    attempted_at_utc    timestamptz NOT NULL DEFAULT now(),
    completed           boolean NOT NULL DEFAULT false,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, crm_interaction_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, work_item_id)
        REFERENCES auditcore.work_items(tenant_id, work_item_id)
);

CREATE TABLE auditcore.escalations (
    tenant_id           varchar(128) NOT NULL,
    escalation_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    audit_case_id       uuid,
    audit_finding_id    uuid,
    work_item_id        uuid,
    severity            varchar(20) NOT NULL
                        CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    status              varchar(30) NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','ACKNOWLEDGED','CLOSED','CANCELLED')),
    summary             varchar(300) NOT NULL,
    detail              text,
    owner_principal_id  varchar(160),
    opened_by_actor_id  varchar(160),
    opened_at_utc       timestamptz NOT NULL DEFAULT now(),
    closed_by_actor_id  varchar(160),
    closed_at_utc       timestamptz,
    resolution_detail   text,
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, escalation_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    FOREIGN KEY (tenant_id, audit_finding_id)
        REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id),
    FOREIGN KEY (tenant_id, work_item_id)
        REFERENCES auditcore.work_items(tenant_id, work_item_id),
    CHECK ((status <> 'CLOSED') OR closed_at_utc IS NOT NULL)
);

-- -----------------------------------------------------------------------------
-- Daily operations / EOD / activity / notes
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.daily_ops_runs (
    tenant_id           varchar(128) NOT NULL,
    daily_ops_run_id    uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    dealer_id           uuid NOT NULL,
    outlet_id           uuid NOT NULL,
    business_date       date NOT NULL,
    pc_principal_id     varchar(160) NOT NULL,
    status              varchar(30) NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','IN_PROGRESS','COMPLETED','EXCEPTION','CANCELLED')),
    started_at_utc      timestamptz,
    completed_at_utc    timestamptz,
    reviewed_by_actor_id varchar(160),
    reviewed_at_utc     timestamptz,
    summary             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, daily_ops_run_id),
    UNIQUE (tenant_id, project_id, outlet_id, business_date, pc_principal_id),
    FOREIGN KEY (tenant_id, project_id, dealer_id)
        REFERENCES auditcore.project_dealers(tenant_id, project_id, dealer_id),
    FOREIGN KEY (tenant_id, project_id, outlet_id)
        REFERENCES auditcore.project_outlets(tenant_id, project_id, outlet_id),
    CHECK ((status <> 'COMPLETED') OR completed_at_utc IS NOT NULL)
);

CREATE INDEX ix_daily_ops_project_date
    ON auditcore.daily_ops_runs(tenant_id, project_id, business_date DESC, status);

CREATE TABLE auditcore.daily_ops_items (
    tenant_id           varchar(128) NOT NULL,
    daily_ops_item_id   uuid NOT NULL DEFAULT gen_random_uuid(),
    daily_ops_run_id    uuid NOT NULL,
    item_type_code      varchar(120) NOT NULL,
    result              varchar(30) NOT NULL DEFAULT 'PENDING'
                        CHECK (result IN ('PENDING','PASS','FAIL','REVIEW','NOT_APPLICABLE')),
    expected_snapshot   jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_snapshot   jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_link_id    uuid,
    remarks             text,
    completed_by_actor_id varchar(160),
    completed_at_utc    timestamptz,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, daily_ops_item_id),
    FOREIGN KEY (tenant_id, daily_ops_run_id)
        REFERENCES auditcore.daily_ops_runs(tenant_id, daily_ops_run_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, evidence_link_id)
        REFERENCES auditcore.evidence_links(tenant_id, evidence_link_id)
);

CREATE TABLE auditcore.daily_activity_entries (
    tenant_id           varchar(128) NOT NULL,
    daily_activity_entry_id uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    principal_id        varchar(160) NOT NULL,
    business_role_key   varchar(40) NOT NULL,
    business_date       date NOT NULL,
    activity_type_code  varchar(120) NOT NULL,
    quantity            numeric(12,2) NOT NULL DEFAULT 1,
    duration_minutes    integer,
    audit_case_id       uuid,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, daily_activity_entry_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id),
    CHECK (quantity >= 0),
    CHECK (duration_minutes IS NULL OR duration_minutes >= 0)
);

CREATE INDEX ix_daily_activity_actor_date
    ON auditcore.daily_activity_entries(tenant_id, project_id, principal_id, business_date DESC);

CREATE TABLE auditcore.daily_notes (
    tenant_id           varchar(128) NOT NULL,
    daily_note_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid NOT NULL,
    principal_id        varchar(160) NOT NULL,
    business_date       date NOT NULL,
    note_text           text NOT NULL,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, daily_note_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id)
);

-- -----------------------------------------------------------------------------
-- Idempotency / outbox / inbox
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.idempotency_keys (
    tenant_id           varchar(128) NOT NULL,
    idempotency_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    operation_scope     varchar(160) NOT NULL,
    idempotency_key     varchar(240) NOT NULL,
    request_hash        varchar(128) NOT NULL,
    response_status     integer,
    response_body       jsonb,
    resource_type       varchar(100),
    resource_id         varchar(160),
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    expires_at_utc      timestamptz,
    PRIMARY KEY (tenant_id, idempotency_id),
    UNIQUE (tenant_id, operation_scope, idempotency_key)
);

CREATE TABLE auditcore.outbox_events (
    tenant_id           varchar(128) NOT NULL,
    event_id            uuid NOT NULL DEFAULT gen_random_uuid(),
    project_id          uuid,
    audit_case_id       uuid,
    event_type          varchar(180) NOT NULL,
    schema_version      integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    aggregate_type      varchar(100) NOT NULL,
    aggregate_id        varchar(160) NOT NULL,
    correlation_id      varchar(160),
    actor_id            varchar(160),
    payload             jsonb NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','PUBLISHED','FAILED')),
    attempt_count       integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at_utc timestamptz,
    last_error          text,
    occurred_at_utc     timestamptz NOT NULL DEFAULT now(),
    published_at_utc    timestamptz,
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES auditcore.projects(tenant_id, project_id),
    FOREIGN KEY (tenant_id, audit_case_id)
        REFERENCES auditcore.audit_cases(tenant_id, audit_case_id)
);

CREATE INDEX ix_outbox_dispatch
    ON auditcore.outbox_events(status, next_attempt_at_utc, occurred_at_utc)
    WHERE status IN ('PENDING','FAILED');

CREATE TABLE auditcore.inbox_events (
    tenant_id           varchar(128) NOT NULL,
    inbox_event_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    producer            varchar(120) NOT NULL,
    producer_event_id   varchar(200) NOT NULL,
    event_type          varchar(180) NOT NULL,
    payload_hash        varchar(128),
    processed_at_utc    timestamptz NOT NULL DEFAULT now(),
    result              varchar(20) NOT NULL DEFAULT 'PROCESSED'
                        CHECK (result IN ('PROCESSED','IGNORED','FAILED')),
    PRIMARY KEY (tenant_id, inbox_event_id),
    UNIQUE (tenant_id, producer, producer_event_id)
);

-- -----------------------------------------------------------------------------
-- Authoritative tamper-evident Audit Core trail
-- -----------------------------------------------------------------------------

CREATE TABLE auditcore.audit_chain_heads (
    tenant_id           varchar(128) NOT NULL,
    entity_type         varchar(100) NOT NULL,
    entity_id           varchar(160) NOT NULL,
    last_sequence_no    bigint NOT NULL DEFAULT 0 CHECK (last_sequence_no >= 0),
    head_hash           varchar(128),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, entity_type, entity_id)
);

CREATE TABLE auditcore.audit_events (
    tenant_id           varchar(128) NOT NULL,
    audit_event_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    entity_type         varchar(100) NOT NULL,
    entity_id           varchar(160) NOT NULL,
    sequence_no         bigint NOT NULL CHECK (sequence_no > 0),
    event_type          varchar(180) NOT NULL,
    actor_id            varchar(160),
    actor_type          varchar(40),
    access_session_id   varchar(160),
    business_role_key   varchar(40),
    correlation_id      varchar(160),
    previous_hash       varchar(128),
    event_hash          varchar(128) NOT NULL,
    payload             jsonb NOT NULL,
    occurred_at_utc     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_event_id),
    UNIQUE (tenant_id, entity_type, entity_id, sequence_no)
);

CREATE INDEX ix_audit_events_entity
    ON auditcore.audit_events(tenant_id, entity_type, entity_id, sequence_no);

CREATE TRIGGER trg_audit_events_immutable
BEFORE UPDATE OR DELETE ON auditcore.audit_events
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_mutation();

-- -----------------------------------------------------------------------------
-- updated_at triggers for mutable tables
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'oems','product_categories','product_models','product_variants','colours','product_skus',
        'dealers','dealer_outlets','projects','project_outlets','project_assignments',
        'dealership_participants','lookup_values','commercial_component_types','price_lists',
        'discount_schemes','document_requirement_profiles','audit_control_definitions',
        'audit_control_versions','project_control_bindings','customers','audit_cases',
        'case_product_details','case_commercial_lines','case_discounts','case_document_requirements',
        'payments','finance_records','delivery_records','insurance_records','case_addons',
        'trade_in_cases','audit_findings','work_items','escalations','daily_ops_runs',
        'daily_ops_items','daily_notes'
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE ON auditcore.%I FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()',
            'trg_' || t || '_updated_at', t
        );
    END LOOP;
END $$;

-- -----------------------------------------------------------------------------
-- Row Level Security — fail closed if app.tenant_id is not set
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'oems','product_categories','product_models','product_variants','colours','product_skus',
        'dealers','dealer_outlets','projects','project_dealers','project_outlets',
        'project_assignments','project_assignment_scopes','dealership_participants','lookup_values',
        'commercial_component_types','price_lists','price_list_items','discount_schemes',
        'discount_eligibility','discount_benefits','document_requirement_profiles',
        'document_requirement_items','audit_control_definitions','audit_control_versions',
        'project_control_bindings','customers','audit_cases','case_product_details',
        'case_commercial_lines','case_discounts','case_document_requirements','evidence_links',
        'payments','payment_evidence_links','finance_records','delivery_records','insurance_records',
        'case_addons','trade_in_cases','audit_evaluations','audit_findings','finding_evidence_refs',
        'review_decisions','work_items','work_item_history','crm_interactions','escalations',
        'daily_ops_runs','daily_ops_items','daily_activity_entries','daily_notes','idempotency_keys',
        'outbox_events','inbox_events','audit_chain_heads','audit_events'
    ]
    LOOP
        EXECUTE format('ALTER TABLE auditcore.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE auditcore.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON auditcore.%I USING (tenant_id = auditcore.current_tenant_id()) WITH CHECK (tenant_id = auditcore.current_tenant_id())',
            t
        );
    END LOOP;
END $$;

-- -----------------------------------------------------------------------------
-- Comments documenting cross-module identifiers and critical semantics
-- -----------------------------------------------------------------------------

COMMENT ON COLUMN auditcore.customers.di_subject_id IS
'External Verigence DI Subject UUID. Not a local foreign key. Subject maps to Customer (PERSON/ORGANIZATION/OTHER).';

COMMENT ON COLUMN auditcore.evidence_links.di_document_id IS
'External Verigence DI Document UUID. DI remains authoritative for document content/processing/extraction.';

COMMENT ON COLUMN auditcore.project_assignments.principal_id IS
'Verigence Security principal identifier (JWT sub). Security remains authoritative for identity/permissions.';

COMMENT ON TABLE auditcore.work_items IS
'Lightweight Audit Core human-work queue; not the authoritative business-state store and not a generic BPM engine.';

COMMENT ON TABLE auditcore.outbox_events IS
'Transactional integration-event outbox. A broker is optional; publisher runs asynchronously after commit.';

COMMENT ON TABLE auditcore.audit_events IS
'Authoritative Audit Core tamper-evident business audit trail. Separate from operational logs/Observability.';

COMMIT;
