# Audit Core UC02 — Master Resolution and DI Excel Alignment

**Status:** UC02 DESIGN ALIGNMENT — OWNER DECISIONS CONFIRMED  
**Date:** 2026-08-21  
**Repository:** `verigence/verigence-audit-core`  
**Branch:** `dev`  
**Applies to:** `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.2.md`, `docs/AUDIT_CORE_API_CONTRACT_v1.1.md`, `docs/AUDIT_CORE_PHYSICAL_DATA_MODEL_v2.2.md`  
**Related:** `docs/AUDIT_CORE_UC02_PRODUCT_MASTER_PHASE1_ALIGNMENT.md`, DI `DI_DECISIONS.md` D31, DI `docs/UC02_EXCEL_MASTER_ALIGNMENT.md`

> This amendment records the owner decisions made after the UC02 v2.2 design review. It does not authorize code, SQL, migration, OpenAPI YAML or permission-catalogue changes.

## 1. Product Master effective-version resolver — CONFIRMED

Phase-1 Product Master may contain overlapping effective periods.

The deterministic resolver is now fixed:

> **For a given Project and business/effective date, the applicable published Product Master version is the version with the latest WEF / Valid From that is not later than the requested date. Latest WEF wins.**

Conceptually:

```text
Project P1 Product Master
V1 WEF = 2026-08-01
V2 WEF = 2026-08-15

resolve at 2026-08-10 -> V1
resolve at 2026-08-20 -> V2
```

This rule applies even when the effective periods overlap. Phase 1 therefore does not require the older version to be automatically end-dated merely because a later-WEF version is published.

The design does not invent an additional same-WEF tie-breaker. If implementation data would make the latest-WEF result ambiguous, implementation must surface that configuration conflict rather than silently choose an arbitrary version.

Historical Journey/evaluation records must continue to retain the resolved Product Master version or equivalent immutable snapshot/reference needed for reproducibility.

Price Lists and Discount Schemes must validate/resolve Product/SKU meaning against the Product Master version applicable to the same Project/effective context.

## 2. DI Project Masters — Excel is added, not substituted

The owner confirmed that the following DI-owned configuration domains must support **Excel administration in addition to their existing native form/API administration**:

- Document Types;
- Extraction Profiles;
- Requirement Profiles.

The existing DI domain remains authoritative. Audit Core remains the Web-facing UC02 facade and must not duplicate DI configuration as a second source of truth.

The user-facing Project Masters catalogue may therefore advertise both supported modes for these DI domains, conceptually:

```text
administrationModes = [FORM, EXCEL]
```

This decision does not remove the existing form/API path.

## 3. DI Excel workflow

For the three confirmed DI domains, Excel administration follows the same controlled import principle used by UC02:

```text
select DI master/configuration
 -> download/use owning DI template
 -> upload .xlsx
 -> stage and parse
 -> validate template and rows
 -> show parsed preview + errors/warnings
 -> explicit SuperAdmin confirmation
 -> create/update the owning DI DRAFT/version state
 -> publish separately where the existing DI lifecycle requires publication
```

Upload alone is never treated as a published/authoritative configuration change.

The workbook schema is derived from the existing DI configuration/domain model. Audit Core must not invent DI fields.

## 4. WEF handling for DI Excel

Excel support does **not** automatically make every DI configuration effective-dated.

- if the owning DI domain already requires an effective/valid-from date, the UC02 explicit-WEF rule applies and WEF must not be silently defaulted;
- if the owning DI domain is versioned/published but does not have an approved WEF concept, Excel import must preserve that existing lifecycle rather than inventing an effective-date field.

## 5. Domains not changed by this decision

This owner decision specifically confirms Excel support for Document Types, Extraction Profiles and Requirement Profiles.

It does not newly require Excel administration for Tenant Settings / Retention Policies or Quality configuration. Those remain on their existing DI administration model unless separately approved.

## 6. Implementation consequence

Before coding, the next machine-readable/API and physical-design work must define:

- the DI template/descriptor metadata for the three Excel-enabled domains;
- staging/preview/confirmation contracts;
- validation and error-report behavior;
- mapping from confirmed rows to the existing DI DRAFT/version lifecycle;
- idempotency and audit behavior;
- the Product Master latest-WEF resolver in Audit Core.

No code or schema change is authorized by this amendment.