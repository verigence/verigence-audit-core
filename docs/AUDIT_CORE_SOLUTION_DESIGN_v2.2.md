# Verigence Audit Core — Consolidated Solution Design UC02 Revision

**Document ID:** VAC-SD-004  
**Version:** 2.2  
**Status:** DRAFT FOR IMPLEMENTATION REVIEW  
**Date:** 2026-08-21  
**Base design:** `VAC-SD-003 v2.1` / `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.1.md`  
**Related decisions:** `docs/AUDIT_CORE_UC02_ADMIN_ALIGNMENT.md`, `docs/AUDIT_CORE_UC02_PRODUCT_MASTER_PHASE1_ALIGNMENT.md`  
**API revision:** `VAC-API-002 v1.1` / `docs/AUDIT_CORE_API_CONTRACT_v1.1.md`  
**Physical-model revision:** `VAC-DM-003` / `docs/AUDIT_CORE_PHYSICAL_DATA_MODEL_v2.2.md`

> This is the Audit Core solution-design revision for UC02 — Project Onboarding & Administration. VAC-SD-003 v2.1 remains authoritative for vehicle-sale Journey/audit behaviour except where this document explicitly supersedes it. No code, SQL migration, machine-readable OpenAPI or permission-catalogue file is changed by this document.

---

## 1. UC02 purpose

UC02 adds first-time Project onboarding and ongoing Project administration to Audit Core while preserving the existing core hierarchy:

```text
PROJECT (= SECURITY TENANT)
  -> DEALER
      -> DEALER OUTLET
          -> CUSTOMER
              -> JOURNEY
```

The user-facing word is **Project**. `tenant_id` remains the canonical internal Project key and cross-module authorization boundary.

UC02 sequence is:

```text
Project Details
 -> Dealers
 -> Dealer Outlets
 -> Employees
 -> Role Mapping
 -> Project Masters
 -> Project Readiness
 -> Activate Project
```

After activation, the same functional areas remain available as Project Administration.

---

## 2. Browser and cross-module boundary

### 2.1 Browser calls Audit Core

For UC02 Project administration, the browser calls Audit Core as the backend boundary.

Audit Core orchestrates Security and DI only where those modules own part of the requested administrative operation.

### 2.2 Two call modes

#### Human administrative operation

For create/update/delete/activate/role-administration and other SuperAdmin-controlled administration, Audit Core retains the initiating Security-issued human JWT and passes that **same human token** to a downstream Security or DI administrative API.

Audit Core SHALL NOT substitute a `ServiceIntegration` token for the human actor on those downstream admin calls.

Each downstream module validates and authorizes the human independently.

#### Machine/integration operation

Normal module-to-module processing, background continuation, DI document processing and Security `/authorization/check` use the existing Security-issued `ServiceIntegration` model.

### 2.3 Supersession of older delegated-token-exchange design

`VAC-SD-AUTH-001 v1.0` used OAuth delegated token exchange for user-driven Audit Core -> DI calls. Security v2.0 has since established a different Phase-1 runtime model: resource servers validate the Security human JWT for identity, obtain live Security functional authorization, and use `ServiceIntegration` for normal backend integration.

UC02 therefore supersedes the older token-exchange requirement as described in `VAC-SD-AUTH-002 v1.1`.

No Clerk integration is introduced into Audit Core.

---

## 3. Project creation and automatic module provisioning

### 3.1 Audit Core is the UC02 create orchestrator

The browser cannot call a Tenant-scoped Project endpoint before a canonical `tenant_id` exists. UC02 therefore requires one Audit Core administrative Project-create operation at platform scope.

Logical flow:

```text
SuperAdmin browser
  -> Audit Core Create Project
       1. validate human Security JWT
       2. establish live SuperAdmin administrative authority through Security
       3. call Security Tenant create with the same human JWT
       4. receive canonical tenant_id
       5. create Audit Core Project projection using that tenant_id
       6. ensure/verify DI Tenant provisioning using the owning DI contract
       7. return Project setup state
```

The normal UI has no separate "Provisioning Modules" step. Provisioning is automatic.

### 3.2 Failure/retry

Project creation is a distributed administrative operation and cannot be treated as one database transaction.

Audit Core SHALL persist an idempotent provisioning operation/receipt sufficient to:

- survive browser timeout/refresh;
- distinguish which module steps completed;
- retry only incomplete steps;
- avoid a second Security Tenant when the same create operation is retried;
- expose an exception-only recovery state to the UI.

A partially provisioned Project stays non-active/configuring and SHALL NOT be reported as ready.

### 3.3 Internal codes

SuperAdmin does not enter technical identifiers.

- Security generates Tenant Code.
- Audit Core generates Dealer Code.
- Audit Core generates Dealer Outlet Code.

These codes are internal stable identifiers/references and are not Project-form inputs.

Exact formatting reuses approved existing validation/conventions and is not invented in this design.

---

## 4. Project Details

Audit Core owns the Project business projection using the canonical Security `tenant_id`.

UC02 Project setup fields are:

- Project Name;
- OEM;
- Product Category;
- Effective Start Date;
- optional Effective End Date;
- Timezone;
- optional Region / Geography.

After operational Journeys or dependent published masters exist:

Editable with audit history:

- Project Name;
- Effective End Date;
- Timezone;
- Region / Geography.

Not directly editable:

- OEM;
- Product Category;
- Effective Start Date.

Changing a restricted field later requires a separately approved migration/rebaseline process. UC02 does not rewrite historical meaning.

---

## 5. Dealers and Dealer Outlets

### 5.1 Dealer

Dealer remains the organisation/business entity below Project.

Dealer does **not** require latitude/longitude.

Dealer supports ongoing create/read/update and Phase-1 SuperAdmin hard delete subject to dependency preflight.

### 5.2 Dealer Outlet

Use **Dealer Outlet** as the user-facing term.

An Outlet owns:

- Dealer parent;
- Outlet Name;
- `ONSITE | SATELLITE` classification;
- address/city/state-region/postal code;
- optional latitude;
- optional longitude;
- optional Google Place ID;
- optional monthly vehicle volume where already supported by the physical model.

Google Maps / Places is an optional data-entry aid. Manual address entry is always valid.

Missing Place ID/coordinates alone does not block Project activation.

Outlet supports ongoing create/read/update and Phase-1 SuperAdmin hard delete subject to dependency preflight.

### 5.3 Map data authority

Google Place ID and coordinates are enrichment values, not Project identity keys.

If a SuperAdmin later changes the address/pin, Audit Core records the approved current values and material administrative change history. Existing Journey/evidence records remain tied to immutable Dealer/Outlet IDs rather than a mutable address string.

---

## 6. Employees and Role Mapping

### 6.1 No independent Project-membership store

UC02 does not add an Audit Core or Security durable `Employee in Project but no role` entity.

The Employees screen is a selection step over approved global Security USERs.

Persisted Project association begins when Role Mapping saves:

```text
Security Tenant operating role
+
Audit Core business assignment where business scope is required
```

### 6.2 Audit Core business-assignment rules

Reuse the existing `business_assignments` hierarchy semantics.

Confirmed Phase-1 mapping:

```text
PC        -> specific Dealer Outlet(s)
TL        -> selected Dealer(s), all Outlets beneath them
PM        -> whole Project
CRM       -> selected Dealer(s) OR whole Project
Executive -> whole Project
```

Audit Core remains business-scope authority. Security remains functional-role/permission authority.

### 6.3 Composite Role Mapping write

Audit Core exposes one UI-oriented Role Mapping operation and orchestrates its owning writes:

1. Security set/replace operating role using the same human admin JWT;
2. Audit Core create/replace the corresponding business assignment(s);
3. persist/reconcile operation state so partial failure is visible/retryable;
4. never report success if the two owning states do not match the requested mapping.

Removing a mapping removes Tenant role/business assignment as applicable but never deletes the global USER.

### 6.4 Readiness coverage

Every ACTIVE Dealer Outlet must have at least one ACTIVE PC business mapping before Project activation.

This is the only new mandatory Phase-1 staffing/cardinality rule introduced by UC02.

---

## 7. Project Masters

### 7.1 Permanent administration capability

Project Masters remain available after activation. They are not a one-time onboarding import.

The Project Masters catalogue is module-owned and may include both Audit Core-owned masters and DI-owned configuration surfaced through the Audit Core façade.

Audit Core-owned Project master domains supported by the current design and UC02 include:

- Product Master — new Project-effective versioned treatment in UC02;
- Price Lists;
- Discount Schemes;
- Document Requirement Profiles;
- Audit Controls;
- Project Policy Versions;
- tenant-owned business/reference status-code configuration where exposed through the master administration catalogue.

This revision does not invent new workbook columns. The owning master/domain defines its template from its approved data model.

### 7.2 Excel staging workflow

For every master that is declared `uploadMode=EXCEL` and effective-dated, the contract is:

```text
Select Module/Master
 -> SuperAdmin explicitly selects WEF / Valid From
 -> upload .xlsx
 -> parse into staging
 -> validate template + rows
 -> show parsed rows to SuperAdmin
 -> show errors/warnings + downloadable error report
 -> explicit Confirm
 -> create DRAFT authoritative version
 -> separate Publish operation
```

WEF is blank by default and MUST NOT be server-defaulted for an Excel-driven effective-dated master upload.

Upload alone never creates a published/authoritative version.

Published versions remain immutable while the Project exists.

### 7.3 Phase-1 overlap rule

Overlapping effective periods are permitted in Phase 1.

- overlap may be a warning;
- overlap alone does not block upload, publish or Project activation;
- each master domain must use its existing/approved deterministic resolver semantics;
- this design does not invent one universal precedence rule.

Phase 2 will add stricter controlled supersede/end-date governance.

---

## 8. Product Master — Phase-1 simple Project scope

### 8.1 Scope

Phase 1 does not provide a "pick existing Product Master" option.

Each Project maintains its own effective-dated Product Master version history through the same Excel staging/preview/confirm/publish pattern.

Phase 2 may add reuse/pick-existing/copy/reference semantics.

### 8.2 Reuse of platform reference identity

VAC-DM-002 currently has shared platform reference entities:

```text
product_categories
oems
product_models
product_variants
colours
product_skus
```

UC02 SHALL NOT mutate shared reference rows in place in a way that changes another Project's meaning.

The Phase-1 Project Product Master adds a Project-effective/versioned catalogue layer over stable canonical product identities.

A Product Master version represents the Product/SKU set and approved Project-effective meaning for a specific WEF/version.

Where a confirmed Product Master introduces a genuinely new model/variant/colour/SKU combination not already represented by a stable canonical reference identity, implementation may create the required new canonical reference identity under the existing product-reference validation rules. It must not reinterpret an existing canonical ID to mean something different.

The exact workbook-to-reference matching rules and existing canonical product columns SHALL be taken from the current approved product schema/template during implementation design; this document deliberately does not invent additional product attributes.

### 8.3 Historical reproducibility

Journeys/evaluations that depend on product identity SHALL retain the effective Product Master version or equivalent immutable reference/snapshot needed to reproduce their historical meaning.

Price Lists and Discount Schemes must validate Product/SKU references against the relevant Project Product Master effective context.

One Project's later Product Master upload cannot silently modify another Project's effective Product catalogue.

---

## 9. DI integration and document storage context

The v2.1 rule remains: user-facing document operations go through Audit Core.

For Audit Core-originated documents, UC02 adds a trusted business storage context:

```text
Project
 -> Dealer
   -> Dealer Outlet
     -> Customer
       -> Documents
```

This hierarchy is not user-configured.

Audit Core passes trusted immutable IDs and safe display context to DI through the DI integration contract. The browser never supplies an object-storage key/path.

Normal document processing uses Audit Core's `ServiceIntegration` machine identity. Human admin DI operations such as Phase-1 Project purge use the same propagated SuperAdmin human JWT.

---

## 10. Project Readiness and activation

Audit Core owns the aggregated Project Readiness view because it spans Audit Core business state plus Security/DI prerequisites.

Blocking checks include at minimum:

- Security Tenant exists in the expected pre-activation lifecycle;
- Audit Core Project setup is complete;
- Dealer/Outlet structural requirements are satisfied;
- every ACTIVE Outlet has at least one ACTIVE PC mapping;
- required operating-role/business mappings are present according to the Project setup policy;
- masters that the Project/module declares required for activation have an acceptable effective/published state;
- required DI provisioning/configuration/storage-context capability is available.

Warnings/non-blockers include:

- optional Google Place ID absent;
- optional coordinates absent when manual address is valid;
- overlapping master effective periods in Phase 1.

The readiness API must explain each failed/warning check and identify the corrective setup area.

Activation flow:

```text
SuperAdmin -> Audit Core Activate Project
Audit Core -> evaluate readiness
  if blocking failure -> do not activate
  else -> Security activate Tenant using same human JWT
       -> record Audit Core Project activated state/result
```

Audit Core does not claim activation success if Security activation fails.

---

## 11. Phase-1 hard delete / rollback

### 11.1 Approved exception to v2.1 no-delete rule

VAC-SD-003/v2.1 and VAC-API-001 had no public destructive delete model. UC02 Phase 1 explicitly supersedes that rule **only for SuperAdmin Project Administration/rollback** because the product is new and setup may need to be rebuilt, including after activation.

Normal PC/TL/PM/CRM/Executive Journey/audit APIs retain the non-destructive v2.1 rules.

### 11.2 Narrow-entity delete

SuperAdmin may request hard delete of:

- Dealer;
- Dealer Outlet;
- Role Mapping/business assignment;
- DRAFT import/master staging data where the owning master permits it.

Audit Core first performs dependency preflight.

A narrow delete may be rejected if operational descendants make independent deletion unsafe. It SHALL NOT silently cascade unknown Customer/Journey/evidence/workflow/audit data from a row-level Delete button.

### 11.3 Whole-Project delete

For broad rollback, whole-Project hard delete is the supported operation.

Audit Core owns the durable cross-module deletion operation.

Sequence:

```text
1. authorize current human SuperAdmin
2. create/resume deletion operation
3. preflight and stop/reject new Project-scoped writes
4. invoke DI administrative purge with same human JWT
5. verify DI zero state
6. delete Audit Core Project-owned data in dependency-safe order
7. verify Audit Core zero state while retaining operation receipt outside Tenant cascade
8. invoke Security Tenant hard delete with same human JWT LAST
9. verify Security Tenant zero state
10. mark overall deletion COMPLETED
```

Partial failure remains resumable. Browser refresh/retry cannot create a second destructive operation for the same idempotent request.

Global Security USERs are not deleted by Project rollback.

### 11.4 Database privilege separation

The v2.1 normal runtime role's no-DELETE principle remains valid for ordinary business/audit requests.

UC02 requires a separately controlled administrative deletion execution path capable of performing the explicitly approved SuperAdmin rollback delete graph.

The exact deployment credential/role mechanism is an implementation/deployment design decision and is not invented in this solution document. It must not broaden normal runtime DELETE capability.

### 11.5 Phase 2

Phase 2 will replace broad rollback-oriented deletion with stronger process/lifecycle controls including maker/checker where approved, retention and inactivate/end-date/retire/supersede semantics.

---

## 12. Ongoing Project Administration

After activation the same screens/APIs support controlled updates:

- allowed Project fields;
- Dealers;
- Dealer Outlets and optional map data;
- operating Role Mapping/business coverage;
- effective-dated master versions;
- Readiness re-evaluation;
- Phase-1 SuperAdmin hard-delete rollback.

Every material administrative mutation is audited and uses optimistic concurrency/idempotency where the resource/operation requires it.

---

## 13. Authorization design for UC02 administration

The current Audit Core permission catalogue contains no destructive Project-delete permission, and this design does not silently invent one.

UC02 Project create/delete are explicit **SuperAdmin administrative operations**. Audit Core must establish the actor's live SuperAdmin administrative classification through Security, not by trusting a role string in the human JWT.

Existing functional permission checks remain unchanged for ordinary Audit Core APIs.

Before code is implemented, the final Security `/authorization/check` response/administrative-attestation contract must provide a reliable way for Audit Core to distinguish the one active SuperAdmin for these control-plane operations. If a new Audit Core administrative permission key is preferred instead, it must be separately added to the approved module permission catalogue; it is not added by this Markdown-only design revision.

---

## 14. Release gates

UC02 cannot be marked implementation-complete until tests prove:

- Project create retry does not create duplicate Security Tenant/Audit Project;
- same human SuperAdmin identity is seen by downstream Security/DI admin APIs;
- codes are generated server-side;
- Dealer and Outlet CRUD stay Tenant-isolated;
- optional Google Place ID/manual address paths both work;
- Role Mapping reconciles Security role + Audit Core business scope;
- every ACTIVE Outlet without ACTIVE PC blocks activation;
- Excel upload is staging-only until confirmation;
- WEF is explicit and never defaulted for effective-dated Excel masters;
- Product Master history is Project-effective and does not mutate another Project;
- Phase-1 overlap warns but does not block solely for overlap;
- active Project rollback is resumable;
- Security Tenant is deleted last;
- global USERs survive Project deletion;
- Project can be recreated cleanly after successful rollback;
- cross-Tenant tampering/deletion attempts are denied.

---

## 15. Supersession summary

For UC02, VAC-SD-004 v2.2 supersedes these v2.1 assumptions where they conflict:

- baseline no-public-DELETE for SuperAdmin administrative rollback;
- delegated OAuth token exchange as the required normal Audit Core -> DI user-driven path;
- Dealer/Outlet assignment being treated as a single implementation scope;
- Product data being sufficient only as non-versioned shared platform references;
- absence of first-time Project provisioning/readiness administration.

All v2.1 vehicle-sale Journey, evidence, audit-state, workflow, immutable-history and audit-only operating principles remain authoritative.