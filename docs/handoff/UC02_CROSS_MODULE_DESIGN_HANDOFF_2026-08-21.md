# UC02 Cross-Module Design Handoff — 21-Aug-2026

**Use case:** UC02 — Project Onboarding & Administration  
**Status:** DESIGN HANDOFF — NO CODE AUTHORIZED BY THIS HANDOFF  
**Date:** 2026-08-21  
**Primary orchestration module:** Audit Core

> This handoff captures the current UC02 design state across Security, Audit Core and Document Intelligence (DI), including the exact working branches, documents created during the UC02 design work, governing references, closed owner decisions and remaining implementation-design work. It is intended to allow a new session/developer to resume without reconstructing the discussion from chat history.

---

## 1. Repositories and working branches

| Module | Repository | Working branch | Role in UC02 |
|---|---|---|---|
| Security | `verigence/verigence-security` | `dev` | Canonical Tenant/Project identity, global USER, human authentication/token, live functional authorization, operating/admin roles, Tenant lifecycle |
| Audit Core | `verigence/verigence-audit-core` | `dev` | Browser-facing UC02 orchestration, Project/Dealer/Dealer Outlet hierarchy, business assignments, Project Masters, Readiness, activation and Project rollback orchestration |
| Document Intelligence | `verigence/verigence-di` | `dev` | DI-owned document/configuration authority, Project/Tenant provisioning, Audit storage context, document processing and DI purge |

No application-code branch was created for this design work. The design revisions were made on the existing `dev` branches only.

---

## 2. Change boundary for this design work

The UC02 work in this handoff is **design/documentation only**.

No application code, migration, SQL, machine-readable OpenAPI/YAML, permission catalogue or runtime configuration change is authorized merely because it appears in these design documents.

Any implementation must first reconcile the approved/current repository design and current `dev` implementation against the documents listed below.

---

## 3. Security — source of truth and UC02 documents

### 3.1 Baseline references read

- `docs/SECURITY_SOLUTION_DESIGN_v2.0.md` — 19-Aug-2026
- `docs/SECURITY_IMPLEMENTATION_DESIGN_v2.0.md` — 19-Aug-2026

These remain the baseline for global USER identity, Security-only Clerk integration, Security-issued human tokens, live Security authorization, role taxonomy, one operating role per USER/Tenant, one PM per Tenant, SuperAdmin, TenantAdmin, ModuleAdmin and ServiceIntegration.

### 3.2 UC02 documents created

| Document | Purpose | Creation commit |
|---|---|---|
| `docs/SECURITY_SOLUTION_DESIGN_v2.1.md` | UC02 solution-design revision: Project=Tenant, human-admin token propagation, server-generated Tenant Code, real Dealer/Outlet boundary correction, Tenant hard-delete rollback | `33e19474ead968c82680ec6863add9f7123608a8` |
| `docs/SECURITY_IMPLEMENTATION_DESIGN_v2.1.md` | UC02 implementation-design consequences for the Security module | `ff64cea294c2ae12bf735fc4009cc433c5d685be` |

### 3.3 UC02 Security rules now fixed

- Web user-facing term is **Project**; Security internal canonical entity remains **Tenant**.
- One Security Tenant ID is the cross-module Project/Tenant ID used by Audit Core and DI.
- Tenant Code is server-generated for UC02; SuperAdmin does not type it.
- Audit Core forwards the **same Security-issued human SuperAdmin JWT** to Security human-admin APIs for the initiating admin action.
- `ServiceIntegration` is not accepted as a substitute actor on human-admin-only APIs.
- Security owns operating role only; Dealer/Dealer Outlet business scope stays in Audit Core.
- Phase-1 whole-Project rollback deletes the Security Tenant **last** and preserves global USER identities.

The latest Product-Master WEF and DI-Excel decisions do not require a new Security ownership change.

---

## 4. Audit Core — source of truth and UC02 documents

### 4.1 Baseline references read

- `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.1.md`
- `docs/AUDIT_CORE_API_CONTRACT_v1.0.md`
- `docs/AUDIT_CORE_PHYSICAL_DATA_MODEL_v2.1.md`
- `docs/AUDIT_CORE_CROSS_MODULE_AUTH_DESIGN_v1.0.md`
- `docs/AUDIT_CORE_DEFAULT_ROLE_BUNDLES_v1.0.md`
- `design/AUDIT_CORE_SECURITY_CATALOG_v2.1.json`
- `docs/AUDIT_CORE_UC02_ADMIN_ALIGNMENT.md`
- `docs/AUDIT_CORE_UC02_PRODUCT_MASTER_PHASE1_ALIGNMENT.md`

Important baseline ownership remains:

```text
Project (= Security Tenant)
  -> Dealer
      -> Dealer Outlet
          -> Customer
              -> Journey
```

Audit Core owns Dealer/Outlet/Customer/Journey and business-scope assignment.

### 4.2 UC02 documents created

| Document | Purpose | Creation commit |
|---|---|---|
| `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.2.md` | Consolidated UC02 Project Onboarding/Admin solution revision | `ce8a4d6c2a0d26a2bc60f97568156f11d896f1c2` |
| `docs/AUDIT_CORE_API_CONTRACT_v1.1.md` | UC02 Markdown API contract revision; machine-readable OpenAPI intentionally not changed | `6a3f2ba6b7557b229d89bf20ffbf559609c7452c` |
| `docs/AUDIT_CORE_PHYSICAL_DATA_MODEL_v2.2.md` | UC02 physical-model design revision including Project-effective Product Master and durable provisioning/delete operations | `ca70eb0c479735a3cdc4c02b1bc9485a3df7882b` |
| `docs/AUDIT_CORE_CROSS_MODULE_AUTH_DESIGN_v1.1.md` | Reconciles UC02 human-admin propagation and normal ServiceIntegration use with Security v2 | `1b107693fd646c10ba0752c0c8b87708cdf85f1d` |
| `docs/AUDIT_CORE_UC02_MASTER_RESOLUTION_ALIGNMENT.md` | Owner-confirmed latest-WEF Product Master resolver and DI Excel-administration addition | `0c9a6cb3c79afa0a2dda1907754b921b4daf870b` |

### 4.3 UC02 Audit Core rules now fixed

#### Project onboarding

Browser calls Audit Core. Audit Core orchestrates:

```text
SuperAdmin browser
  -> Audit Core
       -> Security Tenant creation using same human JWT
       -> Audit Core Project projection
       -> DI Tenant/Project provisioning verification
```

Provisioning must be idempotent/recoverable; the normal UI does not expose a separate module-provisioning step.

#### Dealer/Outlet scope

```text
PC        -> specific Dealer Outlet(s)
TL        -> selected Dealer(s), covering their Outlets
PM        -> whole Project
CRM       -> selected Dealer(s) OR whole Project
Executive -> whole Project
```

Every ACTIVE Dealer Outlet must have at least one ACTIVE PC mapping before Project activation.

#### Product Master Phase 1

- Product Master is maintained per Project.
- No pick/reuse-existing Product Master in Phase 1.
- Excel upload is staging -> validation -> preview -> explicit confirmation -> DRAFT -> separate publish.
- WEF/Valid From is explicit where required; it is not silently defaulted.
- Overlap is allowed in Phase 1.
- **Latest WEF wins** for Product Master resolution: for a requested business/effective date, resolve the published Product Master version having the greatest WEF not later than that date.
- The design does not invent a same-WEF tie-breaker; ambiguous same-WEF data must not be silently resolved.
- Historical Journey/evaluation meaning must remain reproducible from the resolved immutable Product Master version/reference.

#### DI configuration in Project Masters

Audit Core surfaces DI-owned configuration but DI stays authoritative.

Owner-confirmed Excel addition applies to:

- Document Types;
- Extraction Profiles;
- Requirement Profiles.

These support **FORM + EXCEL**; Excel does not replace the existing DI form/API lifecycle.

Tenant Settings/Retention and Quality configuration were not newly made Excel-driven by this decision.

#### Phase-1 rollback

Whole-Project hard delete is a durable Audit Core orchestration:

```text
DI purge + DI zero-state
 -> Audit Core Project-owned delete + zero-state
 -> Security Tenant hard delete LAST
 -> overall completion
```

Global Security USERs survive Project deletion.

---

## 5. DI — source of truth and UC02 documents

### 5.1 Baseline references read

- `DI_DECISIONS.md`
- `DI_MASTER_REFERENCE.md`
- `DI_DESIGN_SUMMARY.md`
- `design/DI_ARCHITECTURE_v2.2.md`
- `design/DI_LLD_v2.2.md`
- `design/DI_DATA_MODEL_v2.2.md`
- `design/DI_SECURITY_RBAC_v2.2.md`
- `docs/SECURITY_AUTHORIZATION_ALIGNMENT_INCREMENT_I.md`
- `docs/UC02_ADMIN_OPERATION_ALIGNMENT.md`

### 5.2 UC02 documents created/updated

| Document | Purpose | Commit |
|---|---|---|
| `DI_DECISIONS.md` | Append-only D28-D31: Audit storage hierarchy, human-admin propagation, Project provisioning/purge, DI-owned Project Masters boundary | `edae65222538d0ddb278b6a45c32df683c6d21d9` |
| `design/DI_ARCHITECTURE_v2.3.md` | UC02 architecture revision | `0c2c3807c297a1ecee5abc7d6e136e55a2f847e0` |
| `design/DI_LLD_v2.3.md` | UC02 low-level-design revision | `7b4daf099408a80541c80f044a37f4c42eb16bc4` |
| `design/DI_DATA_MODEL_v2.3.md` | UC02 data-model revision | `c51266adb778923cfae7541fa855da8e034df601` |
| `design/DI_SECURITY_RBAC_v2.3.md` | UC02 Security/RBAC reconciliation | `4409e15431754c2d1ab38be763c9b6e234c7ba42` |
| `docs/UC02_EXCEL_MASTER_ALIGNMENT.md` | Owner-confirmed FORM+EXCEL administration for Document Types, Extraction Profiles and Requirement Profiles | `1c1a16d684610387363c9508d5e90c46647602cd` |

### 5.3 UC02 DI rules now fixed

#### Audit storage hierarchy

For Audit Core-originated evidence, D5 generic Subject path is superseded by the trusted Audit business hierarchy:

```text
Project
 -> Dealer
   -> Dealer Outlet
     -> Customer
       -> Documents
```

Audit Core supplies trusted immutable IDs/context. DI constructs object keys. Browser never supplies the storage path.

A DI Subject may participate in more than one Audit business context, so DI stores an immutable Audit storage context separately from Subject identity.

#### Human admin versus machine integration

- DI human-admin operations receive the same Security human JWT initiated by SuperAdmin.
- DI performs live Security authorization according to the Security v2 contract.
- Normal Audit Core -> DI document processing uses `ServiceIntegration` with the DI audience.
- DI has no Clerk integration.

#### Project provisioning/purge

DI uses the canonical Security Tenant ID; it creates no second Tenant ID.

Phase-1 purge must be idempotent/resumable, stop or invalidate ongoing Tenant work, delete object bytes before deleting metadata needed to find the bytes, remove Tenant-owned DI state, verify zero-state, and return durable operation status/receipt.

#### Excel administration

Owner-confirmed additional Excel path:

```text
Document Types       -> FORM + EXCEL
Extraction Profiles  -> FORM + EXCEL
Requirement Profiles -> FORM + EXCEL
```

The controlled import flow is staging -> validation -> preview -> explicit SuperAdmin confirmation -> existing DI DRAFT/version lifecycle -> separate publish where that domain already publishes.

Excel does not automatically invent WEF for a DI domain that does not already have an approved effective-date concept.

The existing D25 Python schema-registry authority must be explicitly reconciled in implementation design for Extraction Profile Excel imports; it must not be silently contradicted.

---

## 6. Cross-module authority matrix for UC02

| Concern | Authority |
|---|---|
| Human credential authentication / Clerk integration | Security only |
| Global USER | Security |
| Canonical Tenant/Project ID | Security |
| Operating role and functional permission | Security |
| Project business projection | Audit Core |
| Dealer / Dealer Outlet | Audit Core |
| Role business scope | Audit Core |
| Product Master / Price / Discount / Audit business masters | Audit Core |
| Product Master effective resolver | Audit Core — latest WEF wins |
| DI Document Types / Extraction Profiles / Requirement Profiles | DI |
| DI configuration Excel processing | DI authority, surfaced/orchestrated through Audit Core |
| DI document bytes / extraction / Subject registry | DI |
| Audit storage business context | Audit Core supplies trusted context; DI persists/constructs storage key |
| Project Readiness aggregation | Audit Core |
| Project activation | Audit Core readiness + Security Tenant activation |
| Project rollback orchestration | Audit Core |
| DI purge | DI |
| Audit Core data deletion | Audit Core |
| Security Tenant hard delete | Security, last |
| Global USER deletion | Separate Security USER lifecycle; not part of Project rollback |

---

## 7. Canonical UC02 administrative call pattern

```text
Browser
  | Security-issued human JWT
  v
Audit Core
  |-- live functional/admin authorization through Security
  |
  |-- Security human-admin API
  |      same human JWT
  |
  |-- DI human-admin API
  |      same human JWT
  |
  |-- normal DI integration
         ServiceIntegration JWT, aud=di
```

Do not replace a human admin with ServiceIntegration merely because Audit Core is making the downstream HTTP call.

---

## 8. Project creation recovery invariant

Audit Core must persist a durable create/provisioning operation so retry does not create a second Security Tenant.

Conceptually the operation tracks at least:

```text
request/idempotency identity
Security Tenant step
canonical tenant_id
Audit Core Project step
DI provisioning step
overall status
last failure/correlation
```

Exact table and endpoint shape belongs to implementation/OpenAPI work; do not invent alternative business semantics.

---

## 9. Project deletion recovery invariant

Audit Core must persist the overall Project-delete operation outside the Tenant-owned delete graph far enough to survive deletion of the Tenant's business data.

DI must likewise persist/resume its purge status sufficiently to survive browser/caller timeout.

Never mark whole-Project deletion complete until:

1. DI zero-state is verified;
2. Audit Core zero-state is verified according to the approved rollback definition;
3. Security Tenant deletion succeeds last.

---

## 10. Master administration decisions closed on 21-Aug-2026

### 10.1 Product Master precedence

**Closed:** latest WEF wins.

For Project P and effective date T:

```text
choose the applicable PUBLISHED Product Master version
with maximum WEF such that WEF <= T
```

Overlap remains allowed in Phase 1.

### 10.2 DI Excel support

**Closed:** add Excel as well as forms for:

- Document Types;
- Extraction Profiles;
- Requirement Profiles.

Do not remove the existing native form/API lifecycle.

---

## 11. Items deliberately not invented / still implementation-design work

The following are not owner-level questions unless implementation inspection exposes a genuine conflict:

- exact server-generated Tenant/Dealer/Outlet code format — reuse existing validation/conventions;
- exact new physical table names/indexes/migration numbers;
- exact machine-readable OpenAPI schemas;
- exact workbook column lists — derive them from each owning domain's approved model/template;
- exact DI import-staging table/route names;
- exact table-by-table Audit Core and DI purge order — derive from approved physical schemas and FKs;
- exact same-WEF Product Master tie handling beyond the confirmed latest-WEF rule — do not silently invent a winner;
- how D25 Python extraction schema authority is technically reconciled with Extraction Profile Excel administration — must be explicitly designed before implementation;
- any new permission key — current designs do not silently add one.

---

## 12. Files intentionally not changed by this handoff work

No code files were changed.

No SQL/migration files were changed.

No machine-readable OpenAPI/YAML files were changed.

No permission-catalogue JSON/YAML files were changed.

No Web implementation file was changed.

---

## 13. Recommended resume order

A new session should resume in this order:

1. Read this handoff completely.
2. Read Security v2.0 baseline, then Security v2.1 UC02 revision and implementation design.
3. Read Audit Core v2.1 baseline, then v2.2 solution/API/physical/auth revisions and the latest `AUDIT_CORE_UC02_MASTER_RESOLUTION_ALIGNMENT.md`.
4. Read DI `DI_DECISIONS.md`, then v2.3 architecture/LLD/data/RBAC and `UC02_EXCEL_MASTER_ALIGNMENT.md`.
5. Inspect current `dev` implementation in all three modules before proposing code.
6. Produce/update machine-readable API and physical implementation designs only after design review approval.
7. Do not implement code unless explicitly authorized.

---

## 14. Design checkpoint

At this handoff point the important UC02 owner decisions are sufficiently defined for detailed implementation/API/schema planning:

- Security Tenant is canonical Project identity;
- Audit Core is the browser-facing Project-admin orchestrator;
- same human SuperAdmin JWT is preserved across downstream human-admin calls;
- ServiceIntegration remains the machine path;
- real Dealer -> Dealer Outlet hierarchy is retained;
- operating role and business scope remain separately owned;
- every active Outlet needs a PC before activation;
- Product Master is Project-specific in Phase 1;
- Product Master **latest WEF wins**;
- Excel import is staged/previewed/confirmed before authoritative version creation;
- DI Document Types, Extraction Profiles and Requirement Profiles support **FORM + EXCEL**;
- Audit Core-originated DI storage follows Project -> Dealer -> Dealer Outlet -> Customer -> Documents;
- whole-Project rollback is DI first, Audit Core second, Security Tenant last;
- global USER identities are not deleted by Project rollback.

The next step is implementation-design reconciliation, not additional business-rule invention.