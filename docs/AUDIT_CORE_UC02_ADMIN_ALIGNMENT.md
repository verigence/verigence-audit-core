# Audit Core UC02 — Administrative Alignment

**Status:** UC02 DESIGN ALIGNMENT — OWNER DECISIONS CONFIRMED  
**Date:** 2026-08-21  
**Repository:** `verigence/verigence-audit-core`  
**Branch:** `dev`  
**Related baseline:** `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.1.md`, `docs/AUDIT_CORE_API_CONTRACT_v1.0.md`, `docs/AUDIT_CORE_PHYSICAL_DATA_MODEL_v2.1.md`

> This is a narrow UC02 alignment amendment. It records confirmed Phase-1 Project Onboarding/Administration decisions that intentionally supersede specific v2.1 assumptions. It must be incorporated into the next consolidated Audit Core solution/API/data-model revision before implementation.

## 1. Human-admin routing through Audit Core

UC02 keeps Audit Core as the browser backend boundary.

There are two backend call modes:

1. **Human administrative operation** — create/update/delete/activate and other SuperAdmin-controlled administration. Audit Core executes its own administration under the authenticated Security human identity. If it calls a downstream Security or DI administrative API as part of the same operation, it passes the same Security-issued human Bearer token/identity through. It does not replace the human identity with a `ServiceIntegration` token.
2. **Machine/integration operation** — ordinary module-to-module processing/background work and Security authorization-check calls use the registered ServiceIntegration identity appropriate to the target audience.

Downstream owning modules remain responsible for authorizing their own administrative operation.

## 2. Phase-1 hard delete intentionally supersedes the v2.1 no-public-delete baseline

Audit Core v2.1 states that the baseline user-facing API has no HTTP DELETE operations and the runtime role should not receive DELETE on business tables.

For UC02 Phase 1, this rule is explicitly superseded **for SuperAdmin Project Administration / rollback APIs** because the product is new and a Project may need to be rebuilt even after activation.

This does not grant destructive access to normal PC/TL/PM/CRM/Executive audit operations.

### 2.1 Required Phase-1 administrative hard-delete capability

Audit Core must design and implement SuperAdmin-only hard delete for UC02 administrative entities, including:

- Project rollback / Start Fresh;
- Dealer;
- Dealer Outlet;
- Project business-role mappings;
- DRAFT Project master/import data where applicable.

Direct deletion of a narrower entity may fail with a dependency/preflight response when operational descendants make independent deletion unsafe. The supported rollback path for a Project with broad operational descendants is whole-Project hard delete.

A global Security USER is never deleted by Audit Core.

### 2.2 Whole-Project delete orchestration

Audit Core is the UC02 browser-facing orchestrator.

Required high-level sequence:

1. authorize the current human SuperAdmin;
2. create/resume an idempotent deletion operation/receipt;
3. preflight Project-owned dependencies and stop/reject new Project-scoped writes while deletion is active;
4. invoke DI administrative purge with the same human identity for DI-owned Project/Tenant content/configuration;
5. delete Audit Core Project-owned rows in a dependency-safe transaction/batched sequence;
6. invoke Security Tenant hard delete with the same human identity **last**;
7. verify cross-module zero state before reporting completion.

The exact physical delete graph and retry model must be defined in the implementation design. Cross-module partial failure must be resumable rather than reported as success.

### 2.3 Phase-2 direction

Phase 2 will replace broad rollback-oriented hard delete with process-oriented lifecycle controls, including maker/checker where required, retention, inactivate/end-date/retire/supersede and stronger historical-preservation rules.

## 3. Employee / Project association

No new independent Employee-to-Project membership model is introduced in Audit Core for UC02.

Security's existing Tenant operating-role assignment is the persisted Phase-1 Project association. Audit Core continues to own only Dealer/Dealer Outlet business scope in `business_assignments`.

Role Mapping therefore combines:

- Security Tenant operating role; and
- Audit Core business assignment where the role requires Dealer/Outlet scope.

Removing a Project assignment must never delete the global Security USER.

## 4. Project field mutability after activation

Confirmed Phase-1 rule:

- Project Name: editable with audit history;
- Effective End Date: editable with audit history;
- Timezone: editable with audit history;
- Region / Geography: editable with audit history;
- OEM: not directly editable after operational Journeys or dependent published masters exist;
- Product Category: same restriction as OEM;
- Effective Start Date: same restriction as OEM.

If a restricted field must later change after dependencies exist, a separate approved migration/rebaseline process is required. UC02 must not silently rewrite historical meaning.

## 5. Dealer Outlet Google Maps / Places is optional

Google Maps / Places is the approved optional provider for Dealer Outlet location assistance.

Audit Core must support:

- manual address entry without Maps;
- optional persisted Google Place ID;
- optional latitude/longitude;
- address/city/state/postal data as supplied/confirmed by the administrator;
- later update of optional map/place information.

`google_place_id` is nullable and requires a schema/API addition because it is not present in the v2.1 physical model.

Missing Google Place ID or map coordinates alone is **not** a Project Readiness blocker in Phase 1.

## 6. Project Readiness — PC coverage

The earlier design deferred staffing/cardinality rules. UC02 introduces one explicit Phase-1 readiness rule:

```text
every ACTIVE Dealer Outlet must have at least one ACTIVE PC mapping
```

This is a blocking activation condition.

No other staffing ratios/cardinality rules are implied by this amendment.

## 7. Master effective-period overlap

Phase 1 allows overlapping effective periods for Project master versions.

Therefore:

- overlap alone must not block upload, publish or Project activation;
- UI/readiness may warn about overlap;
- each owning master domain must retain or define deterministic resolver/selection semantics in its API/design;
- this amendment does not invent a universal precedence rule across Product, Price, Discount or other masters.

**Phase 2:** introduce process-oriented master governance that prevents overlapping published effective periods unless a controlled supersede/end-date operation resolves the prior period.

Published versions remain immutable in place while the Project is live; a separate whole-Project Phase-1 hard-delete rollback may remove Project-scoped master history as part of deleting the Project.

## 8. Product Master scope remains open

UC02 requires effective-dated, repeatable Product Master uploads and historical Product/SKU reproducibility.

One design point remains deliberately unresolved:

> If two Projects use the same OEM, may they maintain different Product Master versions / active sellable SKU sets?

Do not implement the Product Master physical model until this owner decision is confirmed.
