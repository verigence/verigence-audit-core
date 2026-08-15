-- Verigence Audit Core — PostgreSQL Physical Schema Candidate
-- Document ID: VAC-DB-002
-- Version: 1.0
-- Status: DRAFT FOR REVIEW — candidate replacement for VAC-DB-001
-- Date: 2026-08-15
-- Requirements: VAC-REQ-001 + VAC-REQ-ADD-001 + VAC-REQ-ADD-002
-- Solution Design: VAC-SD-003 v2.1
-- API Contract: VAC-API-001 v1.0
--
-- FOUNDATIONAL INVARIANTS
-- 1. One Security Tenant = one Audit Project. projects.tenant_id is the Project key.
-- 2. Business hierarchy: Project -> Dealer -> Outlet -> Customer -> Journey.
-- 3. Audit Core observes/audits; it does not block, approve, reject, stop or cancel dealer operations.
-- 4. Actual/observed delivery/business status is distinct from Audit state/outcome.
-- 5. DI is internal-only behind Audit Core. Public APIs expose evidence_id, not DI identifiers.
-- 6. Workflow is durable: state, task, history, audit event and outbox can commit atomically.
-- 7. Published decision-relevant master versions are immutable.
-- 8. Baseline user-facing lifecycle has no hard-delete semantics.
--
-- RUNTIME DATABASE SECURITY CONTRACT
-- - Migration/owner role and runtime application role MUST be different.
-- - Runtime role MUST NOT own these tables and MUST NOT have BYPASSRLS.
-- - Runtime role SHOULD NOT have DELETE privilege on Audit Core business tables.
-- - After validating the Security token and before tenant SQL:
--       SET LOCAL app.tenant_id = '<validated tenant_id>';
-- - RLS is ENABLED and FORCED on every tenant-scoped table in this schema.
--
-- This file defines a fresh target schema. It is NOT an in-place migration from VAC-DB-001.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS auditcore;

-- =============================================================================
-- Helpers
-- =============================================================================

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

CREATE OR REPLACE FUNCTION auditcore.prevent_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only Audit Core record cannot be updated or deleted';
END;
$$;

CREATE OR REPLACE FUNCTION auditcore.protect_published_version()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_core jsonb;
    new_core jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.lifecycle_status IN ('PUBLISHED','RETIRED') THEN
            RAISE EXCEPTION 'published/retired master version cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.lifecycle_status = 'RETIRED' THEN
        RAISE EXCEPTION 'retired master version is immutable';
    END IF;

    IF OLD.lifecycle_status = 'PUBLISHED' THEN
        IF NEW.lifecycle_status <> 'RETIRED' THEN
            RAISE EXCEPTION 'published master version can only be retired';
        END IF;

        old_core := to_jsonb(OLD)
            - 'lifecycle_status' - 'retired_at_utc' - 'retired_by_actor_id' - 'updated_at_utc';
        new_core := to_jsonb(NEW)
            - 'lifecycle_status' - 'retired_at_utc' - 'retired_by_actor_id' - 'updated_at_utc';

        IF old_core IS DISTINCT FROM new_core THEN
            RAISE EXCEPTION 'published master content cannot be changed while retiring';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION auditcore.protect_version_child_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    tenant_value varchar;
    version_value uuid;
    old_version_value uuid;
    parent_status varchar;
BEGIN
    IF TG_OP = 'DELETE' THEN
        tenant_value := to_jsonb(OLD)->>'tenant_id';
        version_value := NULLIF(to_jsonb(OLD)->>TG_ARGV[1], '')::uuid;
    ELSE
        tenant_value := to_jsonb(NEW)->>'tenant_id';
        version_value := NULLIF(to_jsonb(NEW)->>TG_ARGV[1], '')::uuid;
    END IF;

    EXECUTE format(
        'SELECT lifecycle_status FROM auditcore.%I WHERE tenant_id = $1 AND %I = $2',
        TG_ARGV[0], TG_ARGV[1]
    ) INTO parent_status USING tenant_value, version_value;

    IF parent_status IS DISTINCT FROM 'DRAFT' THEN
        RAISE EXCEPTION 'child rows may only be changed while master version is DRAFT';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        old_version_value := NULLIF(to_jsonb(OLD)->>TG_ARGV[1], '')::uuid;
        IF old_version_value IS DISTINCT FROM version_value THEN
            EXECUTE format(
                'SELECT lifecycle_status FROM auditcore.%I WHERE tenant_id = $1 AND %I = $2',
                TG_ARGV[0], TG_ARGV[1]
            ) INTO parent_status USING tenant_value, old_version_value;
            IF parent_status IS DISTINCT FROM 'DRAFT' THEN
                RAISE EXCEPTION 'child rows cannot be moved out of a published/retired master version';
            END IF;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

-- =============================================================================
-- Shared reference/product catalogue
-- =============================================================================

CREATE TABLE auditcore.product_categories (
    product_category_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category_code       varchar(80) NOT NULL UNIQUE,
    category_name       varchar(160) NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auditcore.oems (
    oem_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    oem_code            varchar(80) NOT NULL UNIQUE,
    oem_name            varchar(200) NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auditcore.product_models (
    model_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    oem_id              uuid NOT NULL REFERENCES auditcore.oems(oem_id),
    model_code          varchar(100) NOT NULL,
    model_name          varchar(200) NOT NULL,
    model_year          integer,
    is_active           boolean NOT NULL DEFAULT true,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (oem_id, model_code)
);

CREATE TABLE auditcore.product_variants (
    variant_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id            uuid NOT NULL REFERENCES auditcore.product_models(model_id),
    variant_code        varchar(120) NOT NULL,
    variant_name        varchar(240) NOT NULL,
    fuel_powertrain     varchar(100),
    transmission        varchar(100),
    body_type           varchar(100),
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active           boolean NOT NULL DEFAULT true,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_id, variant_code)
);

CREATE TABLE auditcore.colours (
    colour_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    oem_id              uuid NOT NULL REFERENCES auditcore.oems(oem_id),
    colour_code         varchar(100) NOT NULL,
    colour_name         varchar(200) NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (oem_id, colour_code)
);

CREATE TABLE auditcore.product_skus (
    product_sku_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    oem_id              uuid NOT NULL REFERENCES auditcore.oems(oem_id),
    model_id            uuid NOT NULL REFERENCES auditcore.product_models(model_id),
    variant_id          uuid NOT NULL REFERENCES auditcore.product_variants(variant_id),
    colour_id           uuid REFERENCES auditcore.colours(colour_id),
    sku_code            varchar(160) NOT NULL,
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active           boolean NOT NULL DEFAULT true,
    created_at_utc      timestamptz NOT NULL DEFAULT now(),
    updated_at_utc      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (oem_id, sku_code)
);

-- =============================================================================
-- Project / Dealer / Outlet / People routing
-- =============================================================================

CREATE TABLE auditcore.projects (
    tenant_id                   varchar(128) PRIMARY KEY,
    project_code                varchar(100) NOT NULL,
    project_name                varchar(240) NOT NULL,
    oem_id                      uuid NOT NULL REFERENCES auditcore.oems(oem_id),
    product_category_id         uuid NOT NULL REFERENCES auditcore.product_categories(product_category_id),
    effective_start_date        date NOT NULL,
    effective_end_date          date,
    timezone_name               varchar(100) NOT NULL DEFAULT 'Asia/Kolkata',
    region_code                 varchar(100),
    project_status              varchar(30) NOT NULL DEFAULT 'ACTIVE'
                                CHECK (project_status IN ('DRAFT','ACTIVE','INACTIVE','CLOSED')),
    created_by_actor_id         varchar(160),
    created_at_utc              timestamptz NOT NULL DEFAULT now(),
    updated_by_actor_id         varchar(160),
    updated_at_utc              timestamptz NOT NULL DEFAULT now(),
    version_no                  bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    CHECK (effective_end_date IS NULL OR effective_end_date >= effective_start_date)
);

CREATE TABLE auditcore.project_policy_versions (
    tenant_id                         varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    policy_version_id                 uuid NOT NULL DEFAULT gen_random_uuid(),
    version_no                        integer NOT NULL CHECK (version_no > 0),
    lifecycle_status                  varchar(20) NOT NULL DEFAULT 'DRAFT'
                                      CHECK (lifecycle_status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from                    date NOT NULL,
    effective_to                      date,
    satellite_monthly_volume_threshold integer,
    policy_settings                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by_actor_id               varchar(160),
    created_at_utc                    timestamptz NOT NULL DEFAULT now(),
    published_by_actor_id             varchar(160),
    published_at_utc                  timestamptz,
    retired_by_actor_id               varchar(160),
    retired_at_utc                    timestamptz,
    updated_at_utc                    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, policy_version_id),
    UNIQUE (tenant_id, version_no),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (satellite_monthly_volume_threshold IS NULL OR satellite_monthly_volume_threshold >= 0)
);

CREATE TABLE auditcore.dealers (
    tenant_id               varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    dealer_id               uuid NOT NULL DEFAULT gen_random_uuid(),
    dealer_code             varchar(100) NOT NULL,
    dealer_name             varchar(240) NOT NULL,
    legal_name              varchar(240),
    gst_reference           varchar(100),
    external_reference      varchar(160),
    status                  varchar(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_by_actor_id     varchar(160),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, dealer_id),
    UNIQUE (tenant_id, dealer_code)
);

CREATE TABLE auditcore.dealer_outlets (
    tenant_id               varchar(128) NOT NULL,
    dealer_id               uuid NOT NULL,
    outlet_id               uuid NOT NULL DEFAULT gen_random_uuid(),
    outlet_code             varchar(100) NOT NULL,
    outlet_name             varchar(240) NOT NULL,
    address_text            text,
    city                    varchar(160),
    state_region            varchar(160),
    postal_code             varchar(40),
    latitude                numeric(9,6),
    longitude               numeric(9,6),
    outlet_classification   varchar(20) NOT NULL DEFAULT 'ONSITE'
                            CHECK (outlet_classification IN ('ONSITE','SATELLITE')),
    monthly_vehicle_volume  integer CHECK (monthly_vehicle_volume IS NULL OR monthly_vehicle_volume >= 0),
    security_location_id    varchar(160),
    status                  varchar(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_by_actor_id     varchar(160),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, outlet_id),
    UNIQUE (tenant_id, dealer_id, outlet_id),
    UNIQUE (tenant_id, dealer_id, outlet_code),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id)
);

CREATE TABLE auditcore.dealership_staff (
    tenant_id               varchar(128) NOT NULL,
    dealer_id               uuid NOT NULL,
    outlet_id               uuid NOT NULL,
    dealership_staff_id     uuid NOT NULL DEFAULT gen_random_uuid(),
    staff_role_code         varchar(80) NOT NULL,
    display_name            varchar(240) NOT NULL,
    employee_reference      varchar(160),
    mobile_reference        varchar(80),
    email_reference         varchar(240),
    status                  varchar(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE','INACTIVE')),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dealership_staff_id),
    FOREIGN KEY (tenant_id, dealer_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, dealer_id, outlet_id)
);

CREATE TABLE auditcore.business_assignments (
    tenant_id               varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    assignment_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    security_actor_id       varchar(160) NOT NULL,
    business_role_code      varchar(80) NOT NULL,
    dealer_id               uuid,
    outlet_id               uuid,
    effective_from          timestamptz NOT NULL DEFAULT now(),
    effective_to            timestamptz,
    assignment_status       varchar(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (assignment_status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, assignment_id),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    FOREIGN KEY (tenant_id, dealer_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, dealer_id, outlet_id),
    CHECK (outlet_id IS NULL OR dealer_id IS NOT NULL),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE auditcore.business_status_codes (
    tenant_id               varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    domain_key              varchar(80) NOT NULL,
    status_code             varchar(100) NOT NULL,
    status_label            varchar(240) NOT NULL,
    description             text,
    effective_from          date,
    effective_to            date,
    is_active               boolean NOT NULL DEFAULT true,
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, domain_key, status_code),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

-- =============================================================================
-- Versioned decision-relevant masters
-- =============================================================================

CREATE TABLE auditcore.price_lists (
    tenant_id               varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    price_list_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    price_list_code         varchar(120) NOT NULL,
    price_list_name         varchar(240) NOT NULL,
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, price_list_id),
    UNIQUE (tenant_id, price_list_code)
);

CREATE TABLE auditcore.price_list_versions (
    tenant_id               varchar(128) NOT NULL,
    price_list_id           uuid NOT NULL,
    price_list_version_id   uuid NOT NULL DEFAULT gen_random_uuid(),
    version_no              integer NOT NULL CHECK (version_no > 0),
    lifecycle_status        varchar(20) NOT NULL DEFAULT 'DRAFT'
                            CHECK (lifecycle_status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from          date NOT NULL,
    effective_to            date,
    currency_code           char(3) NOT NULL DEFAULT 'INR',
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    published_by_actor_id   varchar(160),
    published_at_utc        timestamptz,
    retired_by_actor_id     varchar(160),
    retired_at_utc          timestamptz,
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, price_list_version_id),
    UNIQUE (tenant_id, price_list_id, version_no),
    FOREIGN KEY (tenant_id, price_list_id)
        REFERENCES auditcore.price_lists(tenant_id, price_list_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE auditcore.price_list_items (
    tenant_id               varchar(128) NOT NULL,
    price_list_version_id   uuid NOT NULL,
    price_list_item_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    product_sku_id          uuid NOT NULL REFERENCES auditcore.product_skus(product_sku_id),
    component_key           varchar(100) NOT NULL,
    standard_amount         numeric(18,2) NOT NULL,
    tax_inclusive           boolean,
    metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, price_list_item_id),
    UNIQUE (tenant_id, price_list_version_id, product_sku_id, component_key),
    FOREIGN KEY (tenant_id, price_list_version_id)
        REFERENCES auditcore.price_list_versions(tenant_id, price_list_version_id)
);

CREATE TABLE auditcore.discount_schemes (
    tenant_id               varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    discount_scheme_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    scheme_code             varchar(120) NOT NULL,
    scheme_name             varchar(240) NOT NULL,
    scheme_category         varchar(100),
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, discount_scheme_id),
    UNIQUE (tenant_id, scheme_code)
);

CREATE TABLE auditcore.discount_scheme_versions (
    tenant_id                   varchar(128) NOT NULL,
    discount_scheme_id          uuid NOT NULL,
    discount_scheme_version_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    version_no                  integer NOT NULL CHECK (version_no > 0),
    lifecycle_status            varchar(20) NOT NULL DEFAULT 'DRAFT'
                                CHECK (lifecycle_status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from              date NOT NULL,
    effective_to                date,
    combinability_code          varchar(100),
    precedence_rank             integer,
    created_by_actor_id         varchar(160),
    created_at_utc              timestamptz NOT NULL DEFAULT now(),
    published_by_actor_id       varchar(160),
    published_at_utc            timestamptz,
    retired_by_actor_id         varchar(160),
    retired_at_utc              timestamptz,
    updated_at_utc              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, discount_scheme_version_id),
    UNIQUE (tenant_id, discount_scheme_id, version_no),
    FOREIGN KEY (tenant_id, discount_scheme_id)
        REFERENCES auditcore.discount_schemes(tenant_id, discount_scheme_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE auditcore.discount_scheme_eligibility (
    tenant_id                   varchar(128) NOT NULL,
    discount_scheme_version_id  uuid NOT NULL,
    eligibility_id              uuid NOT NULL DEFAULT gen_random_uuid(),
    dealer_id                   uuid,
    outlet_id                   uuid,
    model_id                    uuid REFERENCES auditcore.product_models(model_id),
    variant_id                  uuid REFERENCES auditcore.product_variants(variant_id),
    colour_id                   uuid REFERENCES auditcore.colours(colour_id),
    product_sku_id              uuid REFERENCES auditcore.product_skus(product_sku_id),
    customer_type_code          varchar(80),
    criteria                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, eligibility_id),
    FOREIGN KEY (tenant_id, discount_scheme_version_id)
        REFERENCES auditcore.discount_scheme_versions(tenant_id, discount_scheme_version_id),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    FOREIGN KEY (tenant_id, dealer_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, dealer_id, outlet_id),
    CHECK (outlet_id IS NULL OR dealer_id IS NOT NULL)
);

CREATE TABLE auditcore.discount_scheme_benefits (
    tenant_id                   varchar(128) NOT NULL,
    discount_scheme_version_id  uuid NOT NULL,
    benefit_id                  uuid NOT NULL DEFAULT gen_random_uuid(),
    benefit_key                 varchar(100) NOT NULL,
    benefit_type                varchar(30) NOT NULL
                                CHECK (benefit_type IN ('AMOUNT','PERCENTAGE','OTHER')),
    amount_value                numeric(18,2),
    percentage_value            numeric(9,4),
    benefit_config              jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, benefit_id),
    FOREIGN KEY (tenant_id, discount_scheme_version_id)
        REFERENCES auditcore.discount_scheme_versions(tenant_id, discount_scheme_version_id),
    CHECK (percentage_value IS NULL OR (percentage_value >= 0 AND percentage_value <= 100))
);

CREATE TABLE auditcore.document_requirement_profiles (
    tenant_id                       varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    document_requirement_profile_id uuid NOT NULL DEFAULT gen_random_uuid(),
    profile_code                    varchar(120) NOT NULL,
    profile_name                    varchar(240) NOT NULL,
    created_by_actor_id             varchar(160),
    created_at_utc                  timestamptz NOT NULL DEFAULT now(),
    updated_at_utc                  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_requirement_profile_id),
    UNIQUE (tenant_id, profile_code)
);

CREATE TABLE auditcore.document_requirement_profile_versions (
    tenant_id                               varchar(128) NOT NULL,
    document_requirement_profile_id         uuid NOT NULL,
    document_requirement_profile_version_id uuid NOT NULL DEFAULT gen_random_uuid(),
    version_no                              integer NOT NULL CHECK (version_no > 0),
    lifecycle_status                        varchar(20) NOT NULL DEFAULT 'DRAFT'
                                            CHECK (lifecycle_status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from                          date NOT NULL,
    effective_to                            date,
    created_by_actor_id                     varchar(160),
    created_at_utc                          timestamptz NOT NULL DEFAULT now(),
    published_by_actor_id                   varchar(160),
    published_at_utc                        timestamptz,
    retired_by_actor_id                     varchar(160),
    retired_at_utc                          timestamptz,
    updated_at_utc                          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_requirement_profile_version_id),
    UNIQUE (tenant_id, document_requirement_profile_id, version_no),
    FOREIGN KEY (tenant_id, document_requirement_profile_id)
        REFERENCES auditcore.document_requirement_profiles(tenant_id, document_requirement_profile_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE auditcore.document_requirement_items (
    tenant_id                               varchar(128) NOT NULL,
    document_requirement_profile_version_id uuid NOT NULL,
    document_requirement_item_id            uuid NOT NULL DEFAULT gen_random_uuid(),
    requirement_key                         varchar(120) NOT NULL,
    document_type_key                       varchar(120) NOT NULL,
    process_area                            varchar(80) NOT NULL,
    requirement_level                       varchar(20) NOT NULL
                                            CHECK (requirement_level IN ('REQUIRED','CONDITIONAL','OPTIONAL')),
    condition_config                        jsonb NOT NULL DEFAULT '{}'::jsonb,
    sort_order                              integer NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, document_requirement_item_id),
    UNIQUE (tenant_id, document_requirement_profile_version_id, requirement_key),
    FOREIGN KEY (tenant_id, document_requirement_profile_version_id)
        REFERENCES auditcore.document_requirement_profile_versions(tenant_id, document_requirement_profile_version_id)
);

CREATE TABLE auditcore.audit_controls (
    tenant_id               varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
    audit_control_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    control_key             varchar(120) NOT NULL,
    control_name            varchar(240) NOT NULL,
    process_area            varchar(80) NOT NULL,
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_control_id),
    UNIQUE (tenant_id, control_key)
);

CREATE TABLE auditcore.audit_control_versions (
    tenant_id               varchar(128) NOT NULL,
    audit_control_id        uuid NOT NULL,
    audit_control_version_id uuid NOT NULL DEFAULT gen_random_uuid(),
    version_no              integer NOT NULL CHECK (version_no > 0),
    lifecycle_status        varchar(20) NOT NULL DEFAULT 'DRAFT'
                            CHECK (lifecycle_status IN ('DRAFT','PUBLISHED','RETIRED')),
    effective_from          date NOT NULL,
    effective_to            date,
    evaluator_key           varchar(160) NOT NULL,
    execution_mode          varchar(30) NOT NULL DEFAULT 'ON_SAVE'
                            CHECK (execution_mode IN ('ON_SAVE','NIGHTLY','ON_DEMAND')),
    default_severity        varchar(30) NOT NULL DEFAULT 'MEDIUM',
    applicability_config    jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_config             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    published_by_actor_id   varchar(160),
    published_at_utc        timestamptz,
    retired_by_actor_id     varchar(160),
    retired_at_utc          timestamptz,
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_control_version_id),
    UNIQUE (tenant_id, audit_control_id, version_no),
    FOREIGN KEY (tenant_id, audit_control_id)
        REFERENCES auditcore.audit_controls(tenant_id, audit_control_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

-- =============================================================================
-- Customer / DI subject / Journey
-- =============================================================================

CREATE TABLE auditcore.customers (
    tenant_id               varchar(128) NOT NULL,
    dealer_id               uuid NOT NULL,
    outlet_id               uuid NOT NULL,
    customer_id             uuid NOT NULL DEFAULT gen_random_uuid(),
    customer_type_code      varchar(80) NOT NULL,
    display_name            varchar(240) NOT NULL,
    mobile_last4            varchar(4),
    email_reference         varchar(240),
    external_customer_ref   varchar(160),
    status                  varchar(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE','INACTIVE')),
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_by_actor_id     varchar(160),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, customer_id),
    UNIQUE (tenant_id, dealer_id, outlet_id, customer_id),
    FOREIGN KEY (tenant_id, dealer_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, dealer_id, outlet_id)
);

CREATE TABLE auditcore.customer_identity_index (
    tenant_id               varchar(128) NOT NULL,
    customer_id             uuid NOT NULL,
    identity_index_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    identity_type           varchar(40) NOT NULL,
    match_hash              varchar(256) NOT NULL,
    match_hint              varchar(80),
    source_kind             varchar(30) NOT NULL
                            CHECK (source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM','DERIVED')),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, identity_index_id),
    FOREIGN KEY (tenant_id, customer_id)
        REFERENCES auditcore.customers(tenant_id, customer_id)
);

CREATE TABLE auditcore.di_subject_mappings (
    tenant_id               varchar(128) NOT NULL,
    customer_id             uuid NOT NULL,
    di_subject_mapping_id   uuid NOT NULL DEFAULT gen_random_uuid(),
    di_subject_id           uuid NOT NULL,
    di_subject_type         varchar(30) NOT NULL,
    mapping_status          varchar(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (mapping_status IN ('ACTIVE','INACTIVE')),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    last_synced_at_utc      timestamptz,
    PRIMARY KEY (tenant_id, di_subject_mapping_id),
    UNIQUE (tenant_id, di_subject_id),
    FOREIGN KEY (tenant_id, customer_id)
        REFERENCES auditcore.customers(tenant_id, customer_id)
);

CREATE UNIQUE INDEX uq_di_subject_active_customer
ON auditcore.di_subject_mappings(tenant_id, customer_id)
WHERE mapping_status = 'ACTIVE';

CREATE TABLE auditcore.journeys (
    tenant_id               varchar(128) NOT NULL,
    dealer_id               uuid NOT NULL,
    outlet_id               uuid NOT NULL,
    customer_id             uuid NOT NULL,
    journey_id              uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_reference       varchar(160),
    observed_status_domain  varchar(80) NOT NULL DEFAULT 'JOURNEY'
                            CHECK (observed_status_domain = 'JOURNEY'),
    observed_status_code    varchar(100),
    observed_status_source  varchar(30)
                            CHECK (observed_status_source IS NULL OR observed_status_source IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM','CALCULATED')),
    audit_state             varchar(30) NOT NULL DEFAULT 'NOT_STARTED'
                            CHECK (audit_state IN ('NOT_STARTED','IN_PROGRESS','PC_SUBMITTED','TL_REVIEW','SENT_BACK','PM_REVIEW','REVIEW_COMPLETE')),
    audit_outcome           varchar(30) NOT NULL DEFAULT 'PENDING'
                            CHECK (audit_outcome IN ('PENDING','NO_BREACH','BREACH')),
    audit_started_at_utc    timestamptz,
    pc_submitted_at_utc     timestamptz,
    review_completed_at_utc timestamptz,
    price_list_version_id   uuid,
    document_requirement_profile_version_id uuid,
    policy_version_id       uuid,
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_by_actor_id     varchar(160),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    correlation_id          varchar(128),
    PRIMARY KEY (tenant_id, journey_id),
    UNIQUE (tenant_id, dealer_id, outlet_id, customer_id, journey_id),
    FOREIGN KEY (tenant_id, dealer_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, dealer_id, outlet_id),
    FOREIGN KEY (tenant_id, customer_id)
        REFERENCES auditcore.customers(tenant_id, customer_id),
    FOREIGN KEY (tenant_id, observed_status_domain, observed_status_code)
        REFERENCES auditcore.business_status_codes(tenant_id, domain_key, status_code),
    FOREIGN KEY (tenant_id, price_list_version_id)
        REFERENCES auditcore.price_list_versions(tenant_id, price_list_version_id),
    FOREIGN KEY (tenant_id, document_requirement_profile_version_id)
        REFERENCES auditcore.document_requirement_profile_versions(tenant_id, document_requirement_profile_version_id),
    FOREIGN KEY (tenant_id, policy_version_id)
        REFERENCES auditcore.project_policy_versions(tenant_id, policy_version_id)
);

CREATE TABLE auditcore.audit_state_events (
    tenant_id               varchar(128) NOT NULL,
    audit_state_event_id    uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id              uuid NOT NULL,
    from_audit_state        varchar(30),
    to_audit_state          varchar(30) NOT NULL,
    event_reason            text,
    actor_id                varchar(160),
    actor_type              varchar(40),
    correlation_id          varchar(128),
    occurred_at_utc         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_state_event_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

-- =============================================================================
-- Booking / Evidence facade / process data
-- =============================================================================

CREATE TABLE auditcore.bookings (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    booking_id              uuid NOT NULL DEFAULT gen_random_uuid(),
    booking_reference       varchar(160),
    booking_date            date,
    booking_intimated_at_utc timestamptz,
    pc_handoff_at_utc       timestamptz,
    sales_staff_id          uuid,
    deal_type_code          varchar(100),
    deal_source_code        varchar(100),
    lead_source_code        varchar(100),
    actual_status_domain    varchar(80) NOT NULL DEFAULT 'BOOKING'
                            CHECK (actual_status_domain = 'BOOKING'),
    actual_status_code      varchar(100),
    status_source           varchar(30)
                            CHECK (status_source IS NULL OR status_source IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_reference        varchar(240),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, booking_id),
    UNIQUE (tenant_id, journey_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, sales_staff_id)
        REFERENCES auditcore.dealership_staff(tenant_id, dealership_staff_id),
    FOREIGN KEY (tenant_id, actual_status_domain, actual_status_code)
        REFERENCES auditcore.business_status_codes(tenant_id, domain_key, status_code)
);

CREATE TABLE auditcore.journey_products (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    journey_product_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    product_sku_id          uuid REFERENCES auditcore.product_skus(product_sku_id),
    model_code_snapshot     varchar(100),
    model_name_snapshot     varchar(200),
    variant_code_snapshot   varchar(120),
    variant_name_snapshot   varchar(240),
    colour_code_snapshot    varchar(100),
    colour_name_snapshot    varchar(200),
    selection_source        varchar(30)
                            CHECK (selection_source IS NULL OR selection_source IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, journey_product_id),
    UNIQUE (tenant_id, journey_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

CREATE TABLE auditcore.journey_document_requirements (
    tenant_id                       varchar(128) NOT NULL,
    journey_id                      uuid NOT NULL,
    journey_document_requirement_id uuid NOT NULL DEFAULT gen_random_uuid(),
    document_requirement_item_id    uuid,
    requirement_key                 varchar(120) NOT NULL,
    document_type_key               varchar(120) NOT NULL,
    process_area                    varchar(80) NOT NULL,
    requirement_level               varchar(20) NOT NULL
                                    CHECK (requirement_level IN ('REQUIRED','CONDITIONAL','OPTIONAL')),
    requirement_status              varchar(30) NOT NULL DEFAULT 'PENDING'
                                    CHECK (requirement_status IN ('PENDING','SATISFIED','WAIVED','NOT_APPLICABLE')),
    condition_snapshot              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc                  timestamptz NOT NULL DEFAULT now(),
    updated_at_utc                  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, journey_document_requirement_id),
    UNIQUE (tenant_id, journey_id, requirement_key),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, document_requirement_item_id)
        REFERENCES auditcore.document_requirement_items(tenant_id, document_requirement_item_id)
);

CREATE TABLE auditcore.evidence_ingestion_operations (
    tenant_id                   varchar(128) NOT NULL,
    evidence_ingestion_operation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id                  uuid NOT NULL,
    customer_id                 uuid NOT NULL,
    idempotency_key             varchar(240) NOT NULL,
    evidence_purpose            varchar(160) NOT NULL,
    requirement_key             varchar(120),
    document_type_key           varchar(120),
    operation_status            varchar(30) NOT NULL DEFAULT 'RECEIVED'
                                CHECK (operation_status IN ('RECEIVED','DI_SUBMITTING','DI_ACCEPTED','LINKED','RETRY_WAIT','FAILED','DEAD_LETTER')),
    di_subject_id               uuid,
    di_document_id              uuid,
    evidence_id                 uuid,
    attempt_count               integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at_utc         timestamptz,
    last_error_code             varchar(80),
    last_error_summary          text,
    correlation_id              varchar(128),
    created_at_utc              timestamptz NOT NULL DEFAULT now(),
    updated_at_utc              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, evidence_ingestion_operation_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, customer_id)
        REFERENCES auditcore.customers(tenant_id, customer_id)
);

CREATE TABLE auditcore.evidence (
    tenant_id                       varchar(128) NOT NULL,
    evidence_id                     uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id                      uuid NOT NULL,
    customer_id                     uuid NOT NULL,
    journey_document_requirement_id uuid,
    di_subject_id                   uuid NOT NULL,
    di_document_id                  uuid NOT NULL,
    document_type_key               varchar(120),
    evidence_purpose                varchar(160) NOT NULL,
    process_area                    varchar(80),
    processing_status_cache         varchar(80),
    verification_status_cache       varchar(80),
    confirmation_status_cache       varchar(80),
    cache_updated_at_utc            timestamptz,
    association_status              varchar(30) NOT NULL DEFAULT 'ACTIVE'
                                    CHECK (association_status IN ('ACTIVE','VOIDED','SUPERSEDED','UNLINKED')),
    supersedes_evidence_id          uuid,
    linked_by_actor_id              varchar(160),
    linked_at_utc                   timestamptz NOT NULL DEFAULT now(),
    void_reason                     text,
    voided_by_actor_id              varchar(160),
    voided_at_utc                   timestamptz,
    correlation_id                  varchar(128),
    PRIMARY KEY (tenant_id, evidence_id),
    UNIQUE (tenant_id, di_document_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, customer_id)
        REFERENCES auditcore.customers(tenant_id, customer_id),
    FOREIGN KEY (tenant_id, journey_document_requirement_id)
        REFERENCES auditcore.journey_document_requirements(tenant_id, journey_document_requirement_id),
    FOREIGN KEY (tenant_id, supersedes_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

ALTER TABLE auditcore.evidence_ingestion_operations
    ADD CONSTRAINT fk_evidence_ingestion_evidence
    FOREIGN KEY (tenant_id, evidence_id)
    REFERENCES auditcore.evidence(tenant_id, evidence_id);

CREATE TABLE auditcore.evidence_facts (
    tenant_id               varchar(128) NOT NULL,
    evidence_fact_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    evidence_id             uuid NOT NULL,
    journey_id              uuid NOT NULL,
    field_key               varchar(160) NOT NULL,
    value_type              varchar(30) NOT NULL
                            CHECK (value_type IN ('TEXT','NUMBER','DATE','DATETIME','BOOLEAN','JSON')),
    value_json              jsonb,
    normalized_value        text,
    confidence_score        numeric(7,4),
    di_field_reference      varchar(240),
    verification_status     varchar(80),
    fetched_at_utc          timestamptz NOT NULL DEFAULT now(),
    valid_from_utc          timestamptz NOT NULL DEFAULT now(),
    superseded_at_utc       timestamptz,
    PRIMARY KEY (tenant_id, evidence_fact_id),
    FOREIGN KEY (tenant_id, evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 100))
);

CREATE TABLE auditcore.commercial_lines (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    commercial_line_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    component_key           varchar(100) NOT NULL,
    standard_amount         numeric(18,2),
    actual_amount           numeric(18,2),
    currency_code           char(3) NOT NULL DEFAULT 'INR',
    price_list_item_id      uuid,
    actual_source_kind      varchar(30)
                            CHECK (actual_source_kind IS NULL OR actual_source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM','CALCULATED')),
    source_evidence_id      uuid,
    source_reference        varchar(240),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, commercial_line_id),
    UNIQUE (tenant_id, journey_id, component_key),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, price_list_item_id)
        REFERENCES auditcore.price_list_items(tenant_id, price_list_item_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.discount_applications (
    tenant_id                   varchar(128) NOT NULL,
    journey_id                  uuid NOT NULL,
    discount_application_id     uuid NOT NULL DEFAULT gen_random_uuid(),
    discount_scheme_version_id  uuid,
    discount_key                varchar(120) NOT NULL,
    standard_eligible_amount    numeric(18,2),
    actual_discount_amount      numeric(18,2),
    eligibility_result          varchar(30),
    actual_source_kind          varchar(30)
                                CHECK (actual_source_kind IS NULL OR actual_source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM','CALCULATED')),
    source_evidence_id          uuid,
    details                     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc              timestamptz NOT NULL DEFAULT now(),
    updated_at_utc              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, discount_application_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, discount_scheme_version_id)
        REFERENCES auditcore.discount_scheme_versions(tenant_id, discount_scheme_version_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.payments (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    payment_id              uuid NOT NULL DEFAULT gen_random_uuid(),
    payment_at_utc          timestamptz,
    amount                  numeric(18,2) NOT NULL CHECK (amount >= 0),
    currency_code           char(3) NOT NULL DEFAULT 'INR',
    payment_method_code     varchar(80),
    payment_reference       varchar(240),
    actual_status_domain    varchar(80) NOT NULL DEFAULT 'PAYMENT'
                            CHECK (actual_status_domain = 'PAYMENT'),
    actual_status_code      varchar(100),
    status_source           varchar(30)
                            CHECK (status_source IS NULL OR status_source IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id      uuid,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, payment_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, actual_status_domain, actual_status_code)
        REFERENCES auditcore.business_status_codes(tenant_id, domain_key, status_code),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.payment_verification_events (
    tenant_id                   varchar(128) NOT NULL,
    payment_verification_event_id uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id                  uuid NOT NULL,
    payment_id                  uuid NOT NULL,
    verification_result         varchar(30) NOT NULL
                                CHECK (verification_result IN ('VERIFIED','EXCEPTION','REJECTED','REVIEW_REQUIRED')),
    verification_notes          text,
    verified_by_actor_id        varchar(160) NOT NULL,
    verified_by_role_code       varchar(80),
    source_evidence_id          uuid,
    correlation_id              varchar(128),
    occurred_at_utc             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, payment_verification_event_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, payment_id)
        REFERENCES auditcore.payments(tenant_id, payment_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.finance_records (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    finance_record_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    finance_type_code       varchar(80),
    provider_name           varchar(240),
    do_reference            varchar(240),
    po_reference            varchar(240),
    financed_amount         numeric(18,2),
    actual_status_code      varchar(100),
    source_kind             varchar(30)
                            CHECK (source_kind IS NULL OR source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id      uuid,
    details                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, finance_record_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.insurance_records (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    insurance_record_id     uuid NOT NULL DEFAULT gen_random_uuid(),
    insurer_name            varchar(240),
    policy_reference        varchar(240),
    cover_note_reference    varchar(240),
    standard_premium_amount numeric(18,2),
    actual_premium_amount   numeric(18,2),
    self_insurance_flag     boolean,
    actual_status_code      varchar(100),
    source_kind             varchar(30)
                            CHECK (source_kind IS NULL OR source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id      uuid,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, insurance_record_id),
    UNIQUE (tenant_id, journey_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.journey_addons (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    journey_addon_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    addon_type_code         varchar(80) NOT NULL,
    provider_name           varchar(240),
    standard_amount         numeric(18,2),
    actual_amount           numeric(18,2),
    reference_number        varchar(240),
    source_kind             varchar(30)
                            CHECK (source_kind IS NULL OR source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id      uuid,
    details                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, journey_addon_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.trade_in_cases (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    trade_in_case_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    actual_status_domain    varchar(80) NOT NULL DEFAULT 'TRADE_IN'
                            CHECK (actual_status_domain = 'TRADE_IN'),
    actual_status_code      varchar(100),
    old_vehicle_registration varchar(120),
    old_vehicle_make_model  varchar(240),
    quoted_value            numeric(18,2),
    actual_value            numeric(18,2),
    handover_at_utc         timestamptz,
    payment_at_utc          timestamptz,
    resale_at_utc           timestamptz,
    source_kind             varchar(30)
                            CHECK (source_kind IS NULL OR source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id      uuid,
    details                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, trade_in_case_id),
    UNIQUE (tenant_id, journey_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, actual_status_domain, actual_status_code)
        REFERENCES auditcore.business_status_codes(tenant_id, domain_key, status_code),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.vehicle_records (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    vehicle_record_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    vin                     varchar(120),
    chassis_number          varchar(120),
    dms_reference           varchar(160),
    invoice_reference       varchar(160),
    allocated_at_utc        timestamptz,
    source_kind             varchar(30)
                            CHECK (source_kind IS NULL OR source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id      uuid,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, vehicle_record_id),
    UNIQUE (tenant_id, journey_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.registration_records (
    tenant_id               varchar(128) NOT NULL,
    journey_id              uuid NOT NULL,
    registration_record_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    registration_state      varchar(160),
    registration_territory  varchar(160),
    registration_district   varchar(160),
    registration_type_code  varchar(100),
    registration_category_code varchar(100),
    registration_number     varchar(120),
    actual_status_code      varchar(100),
    source_kind             varchar(30)
                            CHECK (source_kind IS NULL OR source_kind IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id      uuid,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, registration_record_id),
    UNIQUE (tenant_id, journey_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.deliveries (
    tenant_id                   varchar(128) NOT NULL,
    journey_id                  uuid NOT NULL,
    delivery_id                 uuid NOT NULL DEFAULT gen_random_uuid(),
    planned_delivery_at         timestamptz,
    delivery_intimated_at       timestamptz,
    actual_status_domain        varchar(80) NOT NULL DEFAULT 'DELIVERY'
                                CHECK (actual_status_domain = 'DELIVERY'),
    actual_delivery_status_code varchar(100),
    status_label_snapshot       varchar(240),
    actual_delivered_at         timestamptz,
    status_source               varchar(30)
                                CHECK (status_source IS NULL OR status_source IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id          uuid,
    recorded_by_actor_id        varchar(160),
    created_at_utc              timestamptz NOT NULL DEFAULT now(),
    updated_at_utc              timestamptz NOT NULL DEFAULT now(),
    version_no                  bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    PRIMARY KEY (tenant_id, delivery_id),
    UNIQUE (tenant_id, journey_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, actual_status_domain, actual_delivery_status_code)
        REFERENCES auditcore.business_status_codes(tenant_id, domain_key, status_code),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.delivery_status_history (
    tenant_id                   varchar(128) NOT NULL,
    delivery_status_history_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    delivery_id                 uuid NOT NULL,
    journey_id                  uuid NOT NULL,
    actual_status_domain        varchar(80) NOT NULL DEFAULT 'DELIVERY'
                                CHECK (actual_status_domain = 'DELIVERY'),
    actual_delivery_status_code varchar(100) NOT NULL,
    status_label_snapshot       varchar(240),
    actual_delivered_at         timestamptz,
    status_source               varchar(30) NOT NULL
                                CHECK (status_source IN ('EVIDENCE','OPERATIONAL_INPUT','SOURCE_SYSTEM')),
    source_evidence_id          uuid,
    recorded_by_actor_id        varchar(160),
    correlation_id              varchar(128),
    recorded_at_utc             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, delivery_status_history_id),
    FOREIGN KEY (tenant_id, delivery_id)
        REFERENCES auditcore.deliveries(tenant_id, delivery_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, actual_status_domain, actual_delivery_status_code)
        REFERENCES auditcore.business_status_codes(tenant_id, domain_key, status_code),
    FOREIGN KEY (tenant_id, source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

-- =============================================================================
-- Audit evaluations / findings / review
-- =============================================================================

CREATE TABLE auditcore.audit_evaluations (
    tenant_id                   varchar(128) NOT NULL,
    audit_evaluation_id         uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id                  uuid NOT NULL,
    audit_control_version_id    uuid NOT NULL,
    process_area                varchar(80) NOT NULL,
    evaluation_result           varchar(30) NOT NULL
                                CHECK (evaluation_result IN ('PASS','FAIL','REVIEW_REQUIRED','NOT_APPLICABLE')),
    expected_snapshot           jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_snapshot           jsonb NOT NULL DEFAULT '{}'::jsonb,
    explanation                 text,
    evaluator_key_snapshot      varchar(160) NOT NULL,
    correlation_id              varchar(128),
    evaluated_at_utc            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_evaluation_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, audit_control_version_id)
        REFERENCES auditcore.audit_control_versions(tenant_id, audit_control_version_id)
);

CREATE TABLE auditcore.audit_findings (
    tenant_id               varchar(128) NOT NULL,
    audit_finding_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id              uuid NOT NULL,
    audit_evaluation_id     uuid,
    finding_type_code       varchar(100),
    severity                varchar(30) NOT NULL DEFAULT 'MEDIUM',
    finding_status          varchar(30) NOT NULL DEFAULT 'OPEN'
                            CHECK (finding_status IN ('OPEN','ACKNOWLEDGED','RESOLVED','VOIDED')),
    title                   varchar(300) NOT NULL,
    description             text,
    expected_summary        text,
    observed_summary        text,
    resolution_reason       text,
    resolved_by_actor_id    varchar(160),
    resolved_at_utc         timestamptz,
    created_by_actor_id     varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    correlation_id          varchar(128),
    PRIMARY KEY (tenant_id, audit_finding_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, audit_evaluation_id)
        REFERENCES auditcore.audit_evaluations(tenant_id, audit_evaluation_id)
);

CREATE TABLE auditcore.finding_evidence (
    tenant_id               varchar(128) NOT NULL,
    finding_evidence_id     uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_finding_id        uuid NOT NULL,
    evidence_id             uuid NOT NULL,
    evidence_fact_id        uuid,
    linkage_purpose         varchar(160),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, finding_evidence_id),
    FOREIGN KEY (tenant_id, audit_finding_id)
        REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id),
    FOREIGN KEY (tenant_id, evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id),
    FOREIGN KEY (tenant_id, evidence_fact_id)
        REFERENCES auditcore.evidence_facts(tenant_id, evidence_fact_id)
);

CREATE TABLE auditcore.finding_remarks (
    tenant_id               varchar(128) NOT NULL,
    finding_remark_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    audit_finding_id        uuid NOT NULL,
    remark_text             text NOT NULL,
    actor_id                varchar(160) NOT NULL,
    actor_role_code         varchar(80),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, finding_remark_id),
    FOREIGN KEY (tenant_id, audit_finding_id)
        REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id)
);

CREATE TABLE auditcore.review_decisions (
    tenant_id               varchar(128) NOT NULL,
    review_decision_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id              uuid NOT NULL,
    decision                varchar(30) NOT NULL
                            CHECK (decision IN ('BREACH','NO_BREACH','SEND_BACK')),
    reviewer_actor_id       varchar(160) NOT NULL,
    reviewer_role_code      varchar(80),
    remarks                 text,
    correlation_id          varchar(128),
    decided_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, review_decision_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

-- =============================================================================
-- Durable Audit Workflow
-- =============================================================================

CREATE TABLE auditcore.workflow_instances (
    tenant_id               varchar(128) NOT NULL,
    workflow_instance_id    uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id              uuid NOT NULL,
    workflow_type           varchar(100) NOT NULL,
    workflow_version        integer NOT NULL DEFAULT 1 CHECK (workflow_version > 0),
    workflow_status         varchar(30) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (workflow_status IN ('ACTIVE','COMPLETED','CANCELLED','FAILED')),
    current_state           varchar(100),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    completed_at_utc        timestamptz,
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    correlation_id          varchar(128),
    PRIMARY KEY (tenant_id, workflow_instance_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

CREATE TABLE auditcore.workflow_tasks (
    tenant_id               varchar(128) NOT NULL,
    workflow_task_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    workflow_instance_id    uuid NOT NULL,
    journey_id              uuid NOT NULL,
    process_area            varchar(80) NOT NULL,
    task_type               varchar(120) NOT NULL,
    task_status             varchar(30) NOT NULL DEFAULT 'READY'
                            CHECK (task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT','COMPLETED','FAILED','CANCELLED','DEAD_LETTER')),
    priority                integer NOT NULL DEFAULT 50,
    assigned_role_code      varchar(80),
    assigned_actor_id       varchar(160),
    dealer_id               uuid,
    outlet_id               uuid,
    available_at_utc        timestamptz NOT NULL DEFAULT now(),
    due_at_utc              timestamptz,
    claimed_at_utc          timestamptz,
    started_at_utc          timestamptz,
    completed_at_utc        timestamptz,
    cancelled_at_utc        timestamptz,
    cancelled_by_actor_id   varchar(160),
    cancel_reason           text,
    attempt_count           integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts            integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    next_attempt_at_utc     timestamptz,
    lease_owner             varchar(200),
    lease_acquired_at_utc   timestamptz,
    lease_heartbeat_at_utc  timestamptz,
    lease_expires_at_utc    timestamptz,
    effect_key              varchar(240),
    task_payload            jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error_code         varchar(80),
    last_error_summary      text,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
    correlation_id          varchar(128),
    PRIMARY KEY (tenant_id, workflow_task_id),
    FOREIGN KEY (tenant_id, workflow_instance_id)
        REFERENCES auditcore.workflow_instances(tenant_id, workflow_instance_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, dealer_id)
        REFERENCES auditcore.dealers(tenant_id, dealer_id),
    FOREIGN KEY (tenant_id, dealer_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, dealer_id, outlet_id),
    CHECK (outlet_id IS NULL OR dealer_id IS NOT NULL),
    CHECK (due_at_utc IS NULL OR due_at_utc >= created_at_utc),
    CHECK (lease_expires_at_utc IS NULL OR lease_acquired_at_utc IS NULL OR lease_expires_at_utc >= lease_acquired_at_utc)
);

CREATE UNIQUE INDEX uq_workflow_task_effect_key
ON auditcore.workflow_tasks(tenant_id, effect_key)
WHERE effect_key IS NOT NULL;

CREATE INDEX ix_workflow_tasks_ready_queue
ON auditcore.workflow_tasks(tenant_id, task_status, available_at_utc, priority, due_at_utc)
WHERE task_status IN ('READY','RETRY_WAIT');

CREATE INDEX ix_workflow_tasks_lease_recovery
ON auditcore.workflow_tasks(tenant_id, lease_expires_at_utc)
WHERE task_status IN ('CLAIMED','IN_PROGRESS') AND lease_expires_at_utc IS NOT NULL;

CREATE TABLE auditcore.workflow_task_events (
    tenant_id               varchar(128) NOT NULL,
    workflow_task_event_id  uuid NOT NULL DEFAULT gen_random_uuid(),
    workflow_task_id        uuid NOT NULL,
    workflow_instance_id    uuid NOT NULL,
    journey_id              uuid NOT NULL,
    event_type              varchar(100) NOT NULL,
    from_status             varchar(30),
    to_status               varchar(30),
    actor_id                varchar(160),
    actor_type              varchar(40),
    reason                  text,
    event_payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
    correlation_id          varchar(128),
    occurred_at_utc         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workflow_task_event_id),
    FOREIGN KEY (tenant_id, workflow_task_id)
        REFERENCES auditcore.workflow_tasks(tenant_id, workflow_task_id),
    FOREIGN KEY (tenant_id, workflow_instance_id)
        REFERENCES auditcore.workflow_instances(tenant_id, workflow_instance_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

CREATE TABLE auditcore.workflow_task_attempts (
    tenant_id               varchar(128) NOT NULL,
    workflow_task_attempt_id uuid NOT NULL DEFAULT gen_random_uuid(),
    workflow_task_id        uuid NOT NULL,
    attempt_no              integer NOT NULL CHECK (attempt_no > 0),
    worker_id               varchar(200),
    started_at_utc          timestamptz NOT NULL,
    ended_at_utc            timestamptz,
    attempt_result          varchar(30)
                            CHECK (attempt_result IS NULL OR attempt_result IN ('SUCCEEDED','RETRYABLE_FAILURE','NON_RETRYABLE_FAILURE','LEASE_LOST')),
    error_code              varchar(80),
    error_summary           text,
    next_retry_at_utc       timestamptz,
    correlation_id          varchar(128),
    PRIMARY KEY (tenant_id, workflow_task_attempt_id),
    UNIQUE (tenant_id, workflow_task_id, attempt_no),
    FOREIGN KEY (tenant_id, workflow_task_id)
        REFERENCES auditcore.workflow_tasks(tenant_id, workflow_task_id)
);

CREATE TABLE auditcore.workflow_dead_letters (
    tenant_id               varchar(128) NOT NULL,
    workflow_dead_letter_id uuid NOT NULL DEFAULT gen_random_uuid(),
    workflow_task_id        uuid NOT NULL,
    journey_id              uuid NOT NULL,
    dead_letter_reason      text NOT NULL,
    last_error_code         varchar(80),
    last_error_summary      text,
    dead_lettered_at_utc    timestamptz NOT NULL DEFAULT now(),
    resolution_status       varchar(30) NOT NULL DEFAULT 'OPEN'
                            CHECK (resolution_status IN ('OPEN','REQUEUED','RESOLVED')),
    resolved_by_actor_id    varchar(160),
    resolved_at_utc         timestamptz,
    resolution_notes        text,
    PRIMARY KEY (tenant_id, workflow_dead_letter_id),
    FOREIGN KEY (tenant_id, workflow_task_id)
        REFERENCES auditcore.workflow_tasks(tenant_id, workflow_task_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

CREATE UNIQUE INDEX uq_workflow_dead_letter_open
ON auditcore.workflow_dead_letters(tenant_id, workflow_task_id)
WHERE resolution_status = 'OPEN';

-- =============================================================================
-- Daily operations / CRM / Escalation
-- =============================================================================

CREATE TABLE auditcore.daily_ops_runs (
    tenant_id               varchar(128) NOT NULL,
    daily_ops_run_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    outlet_id               uuid NOT NULL,
    business_date           date NOT NULL,
    pc_actor_id             varchar(160) NOT NULL,
    run_status              varchar(30) NOT NULL DEFAULT 'IN_PROGRESS'
                            CHECK (run_status IN ('IN_PROGRESS','COMPLETED','EXCEPTION')),
    started_at_utc          timestamptz NOT NULL DEFAULT now(),
    completed_at_utc        timestamptz,
    correlation_id          varchar(128),
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, daily_ops_run_id),
    FOREIGN KEY (tenant_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, outlet_id)
);

CREATE TABLE auditcore.daily_ops_items (
    tenant_id               varchar(128) NOT NULL,
    daily_ops_item_id       uuid NOT NULL DEFAULT gen_random_uuid(),
    daily_ops_run_id        uuid NOT NULL,
    item_type               varchar(100) NOT NULL,
    item_status             varchar(30) NOT NULL DEFAULT 'PENDING'
                            CHECK (item_status IN ('PENDING','COMPLETED','EXCEPTION','NOT_APPLICABLE')),
    journey_id              uuid,
    evidence_id             uuid,
    details                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_at_utc        timestamptz,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, daily_ops_item_id),
    FOREIGN KEY (tenant_id, daily_ops_run_id)
        REFERENCES auditcore.daily_ops_runs(tenant_id, daily_ops_run_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
);

CREATE TABLE auditcore.activity_records (
    tenant_id               varchar(128) NOT NULL,
    activity_record_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    outlet_id               uuid,
    journey_id              uuid,
    actor_id                varchar(160) NOT NULL,
    actor_role_code         varchar(80),
    activity_type           varchar(120) NOT NULL,
    activity_at_utc         timestamptz NOT NULL DEFAULT now(),
    details                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, activity_record_id),
    FOREIGN KEY (tenant_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, outlet_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

CREATE TABLE auditcore.pc_daily_notes (
    tenant_id               varchar(128) NOT NULL,
    pc_daily_note_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    pc_actor_id             varchar(160) NOT NULL,
    outlet_id               uuid,
    note_date               date NOT NULL,
    note_text               text NOT NULL,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, pc_daily_note_id),
    FOREIGN KEY (tenant_id, outlet_id)
        REFERENCES auditcore.dealer_outlets(tenant_id, outlet_id)
);

CREATE TABLE auditcore.crm_interactions (
    tenant_id               varchar(128) NOT NULL,
    crm_interaction_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id              uuid NOT NULL,
    interaction_type        varchar(100) NOT NULL,
    interaction_status      varchar(30) NOT NULL,
    outcome_code            varchar(100),
    notes                   text,
    actor_id                varchar(160),
    attempted_at_utc        timestamptz,
    completed_at_utc        timestamptz,
    workflow_task_id        uuid,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, crm_interaction_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id),
    FOREIGN KEY (tenant_id, workflow_task_id)
        REFERENCES auditcore.workflow_tasks(tenant_id, workflow_task_id)
);

CREATE TABLE auditcore.escalations (
    tenant_id               varchar(128) NOT NULL,
    escalation_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    journey_id              uuid,
    escalation_type         varchar(100) NOT NULL,
    severity                varchar(30) NOT NULL DEFAULT 'MEDIUM',
    escalation_status       varchar(30) NOT NULL DEFAULT 'OPEN'
                            CHECK (escalation_status IN ('OPEN','ACKNOWLEDGED','RESOLVED','CLOSED')),
    assigned_role_code      varchar(80),
    assigned_actor_id       varchar(160),
    summary                 varchar(300) NOT NULL,
    details                 text,
    opened_at_utc           timestamptz NOT NULL DEFAULT now(),
    resolved_at_utc         timestamptz,
    resolution_notes        text,
    created_by_actor_id     varchar(160),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    version_no              bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, escalation_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

-- =============================================================================
-- Reliability: idempotency / outbox / inbox
-- =============================================================================

CREATE TABLE auditcore.idempotency_records (
    tenant_id               varchar(128) NOT NULL,
    idempotency_record_id   uuid NOT NULL DEFAULT gen_random_uuid(),
    operation_key           varchar(160) NOT NULL,
    idempotency_key         varchar(240) NOT NULL,
    request_hash            varchar(128) NOT NULL,
    logical_result_id       varchar(240),
    response_status         integer,
    response_body           jsonb,
    created_at_utc          timestamptz NOT NULL DEFAULT now(),
    expires_at_utc          timestamptz,
    PRIMARY KEY (tenant_id, idempotency_record_id),
    UNIQUE (tenant_id, operation_key, idempotency_key)
);

CREATE TABLE auditcore.outbox_events (
    tenant_id               varchar(128) NOT NULL,
    outbox_event_id         uuid NOT NULL DEFAULT gen_random_uuid(),
    event_type              varchar(160) NOT NULL,
    schema_version          integer NOT NULL DEFAULT 1,
    aggregate_type          varchar(100) NOT NULL,
    aggregate_id            varchar(160) NOT NULL,
    journey_id              uuid,
    event_payload           jsonb NOT NULL,
    event_status            varchar(30) NOT NULL DEFAULT 'PENDING'
                            CHECK (event_status IN ('PENDING','PUBLISHING','PUBLISHED','RETRY_WAIT','FAILED','DEAD_LETTER')),
    occurred_at_utc         timestamptz NOT NULL DEFAULT now(),
    available_at_utc        timestamptz NOT NULL DEFAULT now(),
    published_at_utc        timestamptz,
    attempt_count           integer NOT NULL DEFAULT 0,
    next_attempt_at_utc     timestamptz,
    last_error_code         varchar(80),
    last_error_summary      text,
    correlation_id          varchar(128),
    actor_id                varchar(160),
    PRIMARY KEY (tenant_id, outbox_event_id),
    FOREIGN KEY (tenant_id, journey_id)
        REFERENCES auditcore.journeys(tenant_id, journey_id)
);

CREATE INDEX ix_outbox_dispatch
ON auditcore.outbox_events(tenant_id, event_status, available_at_utc, next_attempt_at_utc)
WHERE event_status IN ('PENDING','RETRY_WAIT');

CREATE TABLE auditcore.inbox_events (
    tenant_id               varchar(128) NOT NULL,
    inbox_event_id          uuid NOT NULL DEFAULT gen_random_uuid(),
    producer                varchar(160) NOT NULL,
    producer_event_id       varchar(240) NOT NULL,
    event_type              varchar(160) NOT NULL,
    event_payload           jsonb NOT NULL,
    processing_status       varchar(30) NOT NULL DEFAULT 'RECEIVED'
                            CHECK (processing_status IN ('RECEIVED','PROCESSING','PROCESSED','FAILED')),
    received_at_utc         timestamptz NOT NULL DEFAULT now(),
    processed_at_utc        timestamptz,
    last_error_code         varchar(80),
    correlation_id          varchar(128),
    PRIMARY KEY (tenant_id, inbox_event_id),
    UNIQUE (tenant_id, producer, producer_event_id)
);

-- =============================================================================
-- Authoritative Audit Core business audit trail
-- =============================================================================

CREATE TABLE auditcore.audit_chain_heads (
    tenant_id               varchar(128) NOT NULL,
    entity_type             varchar(100) NOT NULL,
    entity_id               varchar(160) NOT NULL,
    last_sequence_no        bigint NOT NULL DEFAULT 0,
    last_event_hash         varchar(128),
    updated_at_utc          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, entity_type, entity_id)
);

CREATE TABLE auditcore.audit_events (
    tenant_id               varchar(128) NOT NULL,
    audit_event_id          uuid NOT NULL DEFAULT gen_random_uuid(),
    entity_type             varchar(100) NOT NULL,
    entity_id               varchar(160) NOT NULL,
    sequence_no             bigint NOT NULL CHECK (sequence_no > 0),
    event_type              varchar(160) NOT NULL,
    event_payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
    previous_event_hash     varchar(128),
    event_hash              varchar(128) NOT NULL,
    actor_id                varchar(160),
    actor_type              varchar(40),
    access_session_id       varchar(160),
    correlation_id          varchar(128),
    occurred_at_utc         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_event_id),
    UNIQUE (tenant_id, entity_type, entity_id, sequence_no),
    UNIQUE (tenant_id, entity_type, entity_id, event_hash)
);

-- =============================================================================
-- Immutability triggers
-- =============================================================================

CREATE TRIGGER trg_project_policy_versions_immutable
BEFORE UPDATE OR DELETE ON auditcore.project_policy_versions
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_published_version();

CREATE TRIGGER trg_price_list_versions_immutable
BEFORE UPDATE OR DELETE ON auditcore.price_list_versions
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_published_version();

CREATE TRIGGER trg_discount_scheme_versions_immutable
BEFORE UPDATE OR DELETE ON auditcore.discount_scheme_versions
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_published_version();

CREATE TRIGGER trg_document_requirement_profile_versions_immutable
BEFORE UPDATE OR DELETE ON auditcore.document_requirement_profile_versions
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_published_version();

CREATE TRIGGER trg_audit_control_versions_immutable
BEFORE UPDATE OR DELETE ON auditcore.audit_control_versions
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_published_version();

CREATE TRIGGER trg_price_list_items_draft_only
BEFORE INSERT OR UPDATE OR DELETE ON auditcore.price_list_items
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_version_child_mutation('price_list_versions','price_list_version_id');

CREATE TRIGGER trg_discount_eligibility_draft_only
BEFORE INSERT OR UPDATE OR DELETE ON auditcore.discount_scheme_eligibility
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_version_child_mutation('discount_scheme_versions','discount_scheme_version_id');

CREATE TRIGGER trg_discount_benefits_draft_only
BEFORE INSERT OR UPDATE OR DELETE ON auditcore.discount_scheme_benefits
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_version_child_mutation('discount_scheme_versions','discount_scheme_version_id');

CREATE TRIGGER trg_document_requirement_items_draft_only
BEFORE INSERT OR UPDATE OR DELETE ON auditcore.document_requirement_items
FOR EACH ROW EXECUTE FUNCTION auditcore.protect_version_child_mutation('document_requirement_profile_versions','document_requirement_profile_version_id');

CREATE TRIGGER trg_audit_state_events_append_only
BEFORE UPDATE OR DELETE ON auditcore.audit_state_events
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation();

CREATE TRIGGER trg_delivery_status_history_append_only
BEFORE UPDATE OR DELETE ON auditcore.delivery_status_history
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation();

CREATE TRIGGER trg_payment_verification_events_append_only
BEFORE UPDATE OR DELETE ON auditcore.payment_verification_events
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation();

CREATE TRIGGER trg_review_decisions_append_only
BEFORE UPDATE OR DELETE ON auditcore.review_decisions
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation();

CREATE TRIGGER trg_finding_remarks_append_only
BEFORE UPDATE OR DELETE ON auditcore.finding_remarks
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation();

CREATE TRIGGER trg_workflow_task_events_append_only
BEFORE UPDATE OR DELETE ON auditcore.workflow_task_events
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation();

CREATE TRIGGER trg_audit_events_append_only
BEFORE UPDATE OR DELETE ON auditcore.audit_events
FOR EACH ROW EXECUTE FUNCTION auditcore.prevent_append_only_mutation();

-- =============================================================================
-- updated_at triggers
-- =============================================================================

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.table_name
        FROM information_schema.columns c
        WHERE c.table_schema = 'auditcore'
          AND c.column_name = 'updated_at_utc'
          AND c.table_name NOT IN ('audit_chain_heads')
        GROUP BY c.table_name
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE ON auditcore.%I FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()',
            'trg_' || r.table_name || '_updated_at',
            r.table_name
        );
    END LOOP;
END $$;

-- =============================================================================
-- Tenant Row Level Security
-- =============================================================================

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.table_name
        FROM information_schema.columns c
        WHERE c.table_schema = 'auditcore'
          AND c.column_name = 'tenant_id'
        GROUP BY c.table_name
    LOOP
        EXECUTE format('ALTER TABLE auditcore.%I ENABLE ROW LEVEL SECURITY', r.table_name);
        EXECUTE format('ALTER TABLE auditcore.%I FORCE ROW LEVEL SECURITY', r.table_name);
        EXECUTE format(
            'CREATE POLICY %I ON auditcore.%I USING (tenant_id = auditcore.current_tenant_id()) WITH CHECK (tenant_id = auditcore.current_tenant_id())',
            'tenant_isolation_' || r.table_name,
            r.table_name
        );
    END LOOP;
END $$;

-- =============================================================================
-- Query/operational indexes
-- =============================================================================

CREATE INDEX ix_dealers_status ON auditcore.dealers(tenant_id, status, dealer_name);
CREATE INDEX ix_outlets_dealer_status ON auditcore.dealer_outlets(tenant_id, dealer_id, status, outlet_name);
CREATE INDEX ix_assignments_actor_active ON auditcore.business_assignments(tenant_id, security_actor_id, assignment_status, effective_from, effective_to);
CREATE INDEX ix_identity_match ON auditcore.customer_identity_index(tenant_id, identity_type, match_hash);
CREATE INDEX ix_customers_outlet ON auditcore.customers(tenant_id, outlet_id, status, display_name);
CREATE INDEX ix_journeys_customer ON auditcore.journeys(tenant_id, customer_id, created_at_utc DESC);
CREATE INDEX ix_journeys_audit_queue ON auditcore.journeys(tenant_id, audit_state, audit_outcome, updated_at_utc);
CREATE INDEX ix_journeys_observed_status ON auditcore.journeys(tenant_id, observed_status_code, updated_at_utc);
CREATE INDEX ix_evidence_ingestion_retry ON auditcore.evidence_ingestion_operations(tenant_id, operation_status, next_attempt_at_utc) WHERE operation_status IN ('RETRY_WAIT','DI_ACCEPTED');
CREATE INDEX ix_evidence_journey ON auditcore.evidence(tenant_id, journey_id, association_status, linked_at_utc DESC);
CREATE INDEX ix_evidence_di_document ON auditcore.evidence(tenant_id, di_document_id);
CREATE INDEX ix_evidence_facts_lookup ON auditcore.evidence_facts(tenant_id, journey_id, field_key, superseded_at_utc);
CREATE INDEX ix_payments_journey ON auditcore.payments(tenant_id, journey_id, payment_at_utc);
CREATE INDEX ix_delivery_status ON auditcore.deliveries(tenant_id, actual_delivery_status_code, actual_delivered_at);
CREATE INDEX ix_findings_open ON auditcore.audit_findings(tenant_id, finding_status, severity, created_at_utc DESC);
CREATE INDEX ix_tasks_actor ON auditcore.workflow_tasks(tenant_id, assigned_actor_id, task_status, due_at_utc);
CREATE INDEX ix_tasks_role ON auditcore.workflow_tasks(tenant_id, assigned_role_code, task_status, due_at_utc);
CREATE INDEX ix_daily_ops_outlet_date ON auditcore.daily_ops_runs(tenant_id, outlet_id, business_date DESC);
CREATE INDEX ix_escalations_open ON auditcore.escalations(tenant_id, escalation_status, severity, opened_at_utc DESC);
CREATE INDEX ix_audit_events_entity ON auditcore.audit_events(tenant_id, entity_type, entity_id, sequence_no);

-- =============================================================================
-- Deployment notes
-- =============================================================================
-- 1. Runtime role: SELECT, INSERT, UPDATE on required Audit Core tables/sequences only.
-- 2. Runtime role: NO DELETE on business/audit/master/workflow tables in this baseline.
-- 3. Runtime role: NO BYPASSRLS and must not own schema/tables.
-- 4. Migration role: separate privileged owner role.
-- 5. Service must set app.tenant_id in every tenant transaction after JWT validation.
-- 6. Commands use version_no / If-Match optimistic concurrency where API contract requires it.
-- 7. Worker loops use transactional locking (for example FOR UPDATE SKIP LOCKED) and persisted leases.
-- 8. Logging/metrics/traces are emitted to Observability; raw PII/documents/provider payloads are not logged.

COMMIT;
