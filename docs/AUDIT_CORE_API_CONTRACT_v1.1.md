# Verigence Audit Core — API Contract UC02 Revision

**Document ID:** VAC-API-002  
**Version:** 1.1  
**Status:** DRAFT FOR IMPLEMENTATION REVIEW  
**Date:** 2026-08-21  
**Base contract:** `VAC-API-001 v1.0` / `docs/AUDIT_CORE_API_CONTRACT_v1.0.md`  
**Solution design:** `VAC-SD-004 v2.2`  
**Machine-readable OpenAPI:** not changed by this design-only revision

> This document adds/changes only the API semantics required for UC02 Project Onboarding & Administration. Existing VAC-API-001 Journey/evidence/audit/task APIs remain valid unless explicitly superseded below. No YAML/OpenAPI file or application code is changed by this document.

---

## 1. Common contract rules

Existing v1.0 rules for `Authorization`, `X-Correlation-ID`, `Idempotency-Key`, optimistic concurrency and `application/problem+json` remain.

UC02 adds these rules:

1. User-facing UI terminology is **Project**; internal path scoping continues to use canonical `{tenantId}`.
2. Project/Dealer/Outlet/Role Mapping/Master administration is human-admin initiated.
3. Audit Core validates the Security human JWT and obtains current Security authorization/administrative context.
4. When Audit Core calls a downstream Security or DI **administrative** endpoint, it forwards the same human Bearer token.
5. `ServiceIntegration` is used only for normal machine integration/background work and Security `/authorization/check`, not as a substitute for the human administrator.
6. UC02 Phase 1 introduces SuperAdmin hard-delete administration as an explicit exception to the v1.0 no-public-DELETE baseline.
7. Normal Journey/evidence/workflow APIs remain non-destructive according to VAC-API-001.

The exact Audit Core error codes for new UC02 failures must be added to the approved error catalogue during the later API implementation artifact update. This Markdown revision does not invent error-code numbers.

---

## 2. Project creation — new platform-scope Audit Core entry point

A Tenant-scoped path cannot create the first Project because `{tenantId}` does not exist until Security creates it.

Add:

```text
POST /v1/projects
```

Headers:

```text
Authorization: Bearer <Security-issued human SuperAdmin JWT>
Idempotency-Key: required
X-Correlation-ID: optional/recommended
```

Request:

```json
{
  "projectName": "Hyundai West Audit Project",
  "oemId": "<existing OEM UUID>",
  "productCategoryId": "<existing Product Category UUID>",
  "effectiveStartDate": "2026-09-01",
  "effectiveEndDate": null,
  "timezoneName": "Asia/Kolkata",
  "regionCode": "<existing/approved region value or null>"
}
```

No request fields for:

- `tenantId`;
- Tenant Code;
- Dealer Code;
- Outlet Code;
- DI storage path.

### 2.1 Orchestration semantics

Audit Core:

1. authorizes the human SuperAdmin;
2. persists/claims the idempotent provisioning operation;
3. calls Security Tenant create with the same human JWT;
4. obtains canonical `tenantId`;
5. creates the Audit Core Project projection using that ID;
6. invokes/ensures DI Project/Tenant provisioning through the approved DI admin contract using the same human JWT where a DI admin operation is required;
7. records the provisioning result.

A retry with the same idempotency key and same semantic request returns/resumes the same logical operation. Same key + conflicting request returns the existing platform conflict behavior.

### 2.2 Response

Canonical result contains:

```json
{
  "operationId": "uuid",
  "tenantId": "uuid",
  "projectName": "Hyundai West Audit Project",
  "projectStatus": "CONFIGURING",
  "provisioningStatus": "READY | IN_PROGRESS | RECOVERY_REQUIRED",
  "currentStep": "SECURITY | AUDIT_CORE | DI | COMPLETE"
}
```

HTTP handling:

- `201 Created` when all required create/provisioning work completed during the request;
- `202 Accepted` when a durable provisioning operation exists but completion/recovery is still in progress;
- normal problem response when no usable provisioning operation can be established.

Do not report `READY` unless Security, Audit Core and required DI provisioning have succeeded.

### 2.3 Provisioning status/retry

```text
GET  /v1/project-provisioning-operations/{operationId}
POST /v1/project-provisioning-operations/{operationId}/retry
```

Retry is human SuperAdmin administration and reuses the original operation/idempotent receipts; it does not create a new Project/Tenant.

---

## 3. Project read/update

Existing:

```text
GET   /v1/tenants/{tenantId}/project
PATCH /v1/tenants/{tenantId}/project
```

is expanded to the complete UC02 Project representation.

Response fields include at minimum:

```text
tenantId
projectName
oemId
productCategoryId
effectiveStartDate
effectiveEndDate
timezoneName
regionCode
projectStatus
versionNo
createdAtUtc
updatedAtUtc
```

PATCH uses `If-Match`/version concurrency where the current mutable-resource pattern applies.

After Journeys or dependent published masters exist:

Allowed:

- Project Name;
- Effective End Date;
- Timezone;
- Region / Geography.

Rejected without a separately approved migration/rebaseline process:

- OEM;
- Product Category;
- Effective Start Date.

Audit Core determines dependency existence from its own data; it does not ask the UI to decide whether a field is safe to change.

---

## 4. Dealer APIs

Retain:

```text
POST   /v1/tenants/{tenantId}/dealers
GET    /v1/tenants/{tenantId}/dealers
GET    /v1/tenants/{tenantId}/dealers/{dealerId}
PATCH  /v1/tenants/{tenantId}/dealers/{dealerId}
```

### 4.1 Create/update change

`dealerCode` is not a required caller input for UC02. Audit Core generates the internal Dealer Code using the existing-compatible convention.

Business fields are limited to the existing Dealer model and approved UC02 screen. No latitude/longitude is added to Dealer.

### 4.2 Dependency preview and hard delete

Add:

```text
GET    /v1/tenants/{tenantId}/dealers/{dealerId}/deletion-impact
DELETE /v1/tenants/{tenantId}/dealers/{dealerId}
```

DELETE:

- human SuperAdmin only;
- `Idempotency-Key` required;
- Tenant/Dealer parent relationship validated;
- dependency preflight performed;
- may reject when Outlet/Customer/Journey/operational descendants make direct Dealer deletion unsafe;
- never silently deletes another Project's records;
- ordinary operating roles cannot call it.

The impact response returns dependency categories/counts needed for confirmation. It does not expose unrelated Tenant data.

---

## 5. Dealer Outlet APIs

Retain nested paths:

```text
POST   /v1/tenants/{tenantId}/dealers/{dealerId}/outlets
GET    /v1/tenants/{tenantId}/dealers/{dealerId}/outlets
GET    /v1/tenants/{tenantId}/dealers/{dealerId}/outlets/{outletId}
PATCH  /v1/tenants/{tenantId}/dealers/{dealerId}/outlets/{outletId}
```

### 5.1 Create/update request fields

UC02 Outlet representation includes:

```json
{
  "outletName": "Andheri East",
  "outletClassification": "ONSITE",
  "addressText": "Andheri Kurla Road, Andheri East, Mumbai",
  "city": "Mumbai",
  "stateRegion": "Maharashtra",
  "postalCode": "400059",
  "googlePlaceId": "<nullable Google Place ID>",
  "latitude": 19.1176,
  "longitude": 72.8631,
  "monthlyVehicleVolume": null
}
```

Rules:

- `outletCode` is server-generated, not required from caller;
- `googlePlaceId` nullable;
- latitude/longitude nullable;
- manual address without Maps is accepted;
- missing map/place data alone is not validation failure/readiness blocker;
- `ONSITE | SATELLITE` uses the existing Outlet classification domain.

### 5.2 Delete

Add:

```text
GET    /v1/tenants/{tenantId}/dealers/{dealerId}/outlets/{outletId}/deletion-impact
DELETE /v1/tenants/{tenantId}/dealers/{dealerId}/outlets/{outletId}
```

Same SuperAdmin/idempotency/dependency/isolation rules as Dealer delete.

An Outlet with operational Customer/Journey/evidence dependencies may be rejected for direct deletion; the whole-Project rollback remains the supported broad rollback path.

---

## 6. Employee candidate view — no membership write

The UC02 Employees screen needs an Audit Core facade because the browser does not call Security directly for this workflow.

Add:

```text
GET /v1/tenants/{tenantId}/role-mapping-candidates?q=<text>&limit=<n>
```

Audit Core composes a UI-safe view from Security's approved global USER administration/search capability.

Response contains only approved employee selector data required by UC02, for example:

```text
userId
displayName
email/current approved identifier fields
status
```

Only Security USERs eligible to be considered by the current Security lifecycle are returned according to Security's response/policy. Audit Core does not create a local Employee membership record from this GET.

The exact personally identifying fields returned must follow Security's approved user-list contract; this document does not invent additional PII.

---

## 7. Role Mapping — composite Security + Audit Core administration

Add:

```text
GET    /v1/tenants/{tenantId}/role-mappings
GET    /v1/tenants/{tenantId}/role-mappings/{userId}
PUT    /v1/tenants/{tenantId}/role-mappings/{userId}
DELETE /v1/tenants/{tenantId}/role-mappings/{userId}
```

PUT/DELETE require `Idempotency-Key` because the logical write spans Security and Audit Core.

### 7.1 PUT request

```json
{
  "operatingRole": "PC | TL | PM | CRM | Executive",
  "dealerIds": [],
  "outletIds": []
}
```

Validation:

- `PC`: one or more `outletIds`; each Outlet must belong to this Tenant; Dealer parent is derived/validated by Audit Core;
- `TL`: one or more `dealerIds`; Outlet list absent/empty because scope is all current Outlets below those Dealers;
- `PM`: no Dealer/Outlet IDs; Project-wide;
- `CRM`: zero Dealer IDs means Project-wide, otherwise one or more Dealer IDs; Outlet IDs absent/empty;
- `Executive`: no Dealer/Outlet IDs; Project-wide.

### 7.2 Write order and reconciliation

Audit Core:

1. calls Security set/replace operating role using the same human admin JWT;
2. replaces/creates Audit Core `business_assignments` consistent with the requested scope;
3. records the composite operation outcome;
4. on partial failure, returns recovery/partial-failure state and does not claim the requested mapping is complete.

A retry resumes/reconciles the same semantic operation.

### 7.3 DELETE

Role Mapping DELETE:

- removes Audit Core business assignment(s) for the target Project;
- removes the Security Tenant operating role using the same human JWT;
- never deletes the global USER;
- does not affect the USER's roles in another Tenant;
- is retry-safe across the two owning modules.

This is Project-assignment removal, not global USER deletion.

---

## 8. Project Masters catalogue facade

Add:

```text
GET /v1/tenants/{tenantId}/project-masters
```

Response is a module-owned descriptor list, not a hard-coded Web list.

Each descriptor contains only fields needed to drive the approved administration UI, conceptually:

```json
{
  "ownerModule": "AUDIT_CORE | DI",
  "masterKey": "<module-defined key>",
  "displayName": "<module-defined name>",
  "uploadMode": "EXCEL | FORM",
  "requiresWef": true,
  "templateVersion": "<nullable>",
  "currentVersionId": "<nullable>",
  "currentWef": "<nullable date>",
  "lifecycleStatus": "<module-defined supported lifecycle>"
}
```

Audit Core-owned descriptors are backed by the current versioned master/configuration domains plus UC02 Product Master. DI-owned descriptors are supplied by DI's approved admin catalogue contract. Audit Core does not invent DI master types.

---

## 9. Excel master import facade

For a descriptor with `uploadMode=EXCEL`, add:

```text
GET  /v1/tenants/{tenantId}/project-masters/{ownerModule}/{masterKey}/template
POST /v1/tenants/{tenantId}/project-masters/{ownerModule}/{masterKey}/imports
GET  /v1/tenants/{tenantId}/project-master-imports/{importId}
GET  /v1/tenants/{tenantId}/project-master-imports/{importId}/rows
GET  /v1/tenants/{tenantId}/project-master-imports/{importId}/error-report
DELETE /v1/tenants/{tenantId}/project-master-imports/{importId}
POST /v1/tenants/{tenantId}/project-master-imports/{importId}/confirm
```

### 9.1 Upload

`POST .../imports` uses `multipart/form-data`:

- `file` — `.xlsx`, required;
- `effectiveFrom` / WEF — required where descriptor says `requiresWef=true`;
- template/version metadata only as required by owning module contract.

For UC02 effective-dated Excel masters, WEF is mandatory and has no server default.

`Idempotency-Key` required.

Upload creates/stages an import only. It does not publish authoritative master data.

### 9.2 Import summary

Conceptual fields:

```text
importId
ownerModule
masterKey
effectiveFrom
fileName
fileHash
status
rowsParsed
validRows
warningRows
errorRows
createdBy
createdAtUtc
```

The exact file hash algorithm uses the platform/owning-module approved hash convention; this contract does not invent a new one.

### 9.3 Parsed row preview

`GET .../rows` is paginated and returns:

```text
rowNumber
parsedData            owning-master columns only
validationStatus      VALID | WARNING | ERROR
messages[]
```

The UI can filter/page errors without re-uploading.

### 9.4 Confirm

```text
POST /v1/tenants/{tenantId}/project-master-imports/{importId}/confirm
```

Preconditions:

- import belongs to Tenant;
- parse/validation complete;
- no blocking row error remains under owning-module rules;
- caller explicitly confirms current staged result;
- import not already confirmed with a conflicting semantic action.

Confirm creates a `DRAFT` authoritative master version in the owning module. Publish remains separate.

### 9.5 Delete staging/draft import

`DELETE .../project-master-imports/{importId}` is Phase-1 SuperAdmin administration for unconfirmed/staging or otherwise owning-module-permitted DRAFT import state.

It does not hard-delete a PUBLISHED master version inside a continuing Project.

---

## 10. Master version read/publish/history

Facade:

```text
GET  /v1/tenants/{tenantId}/project-masters/{ownerModule}/{masterKey}/versions
POST /v1/tenants/{tenantId}/project-masters/{ownerModule}/{masterKey}/versions/{versionId}/publish
POST /v1/tenants/{tenantId}/project-masters/{ownerModule}/{masterKey}/versions/{versionId}/retire
```

Existing Audit Core domain-specific master APIs remain valid underlying business APIs where already defined.

Published versions are immutable.

Phase-1 overlap:

- may return a warning;
- does not block publish/activation solely due to overlap;
- resolver behavior remains owned by the specific master domain.

---

## 11. Product Master facade semantics

Product Master is an Audit Core-owned Project master in Phase 1.

Rules:

- one Project maintains its own effective-dated Product Master versions;
- no Phase-1 API to select/reuse another Project's Product Master;
- Product Excel rows use the canonical existing Audit Core product-reference fields/template; this contract does not invent additional product columns;
- validation resolves existing canonical product identities and permits the Product Master owning workflow to materialize genuinely new stable canonical product identities under the final physical-design rules;
- confirmed Project-effective Product Master version stores the set/snapshot needed for historical reproducibility;
- Price List/Discount validation checks references against the relevant Project Product Master effective context.

Phase 2 may add a pick/reuse-existing Product Master operation; there is no placeholder endpoint for it in Phase 1.

---

## 12. Project Readiness

Add:

```text
GET /v1/tenants/{tenantId}/project/readiness
```

Response:

```json
{
  "readyToActivate": false,
  "evaluatedAtUtc": "<timestamp>",
  "checks": [
    {
      "area": "ROLE_MAPPING",
      "checkKey": "<stable implementation key>",
      "severity": "BLOCKING | WARNING | INFO",
      "status": "PASS | FAIL | PENDING",
      "message": "<safe user-facing explanation>",
      "targetTask": "ROLE_MAPPING"
    }
  ]
}
```

The implementation owns stable `checkKey` values; this Markdown contract does not invent a catalogue before implementation review.

Blocking minimum:

- Project setup complete;
- background Security/Audit Core/DI provisioning ready;
- required Dealer/Outlet structure valid;
- every ACTIVE Outlet has at least one ACTIVE PC mapping;
- required role/business mappings complete according to Project setup policy;
- Project/module-required master versions are acceptable for activation;
- DI required Project/config/storage capability ready.

Warning only in Phase 1:

- missing optional Google Place ID/coordinates;
- master effective-period overlap.

---

## 13. Activate Project — Audit Core façade

Add:

```text
POST /v1/tenants/{tenantId}/project/activate
```

`Idempotency-Key` required.

Audit Core:

1. evaluates Project Readiness;
2. if a blocking check fails, returns a conflict/problem with the current readiness result/reference and does not call Security activation;
3. calls `POST /security/v1/platform/tenants/{tenantId}/activate` with the same human SuperAdmin JWT;
4. records/returns the resulting Project activation state;
5. never reports ACTIVE if Security activation failed.

---

## 14. Whole-Project hard delete

### 14.1 Impact

```text
GET /v1/tenants/{tenantId}/project/deletion-impact
```

Returns a safe dependency summary for explicit confirmation, including owning-domain counts/status categories needed to understand the rollback impact. It does not enumerate secrets/raw document bytes.

### 14.2 Start/resume deletion

```text
DELETE /v1/tenants/{tenantId}/project
```

Headers:

```text
Authorization: Bearer <Security-issued human SuperAdmin JWT>
Idempotency-Key: required
```

Response:

```json
{
  "operationId": "uuid",
  "tenantId": "uuid",
  "status": "RUNNING | RECOVERY_REQUIRED | COMPLETED",
  "currentStep": "PREFLIGHT | DI_PURGE | AUDIT_CORE_DELETE | SECURITY_DELETE | VERIFY"
}
```

Normally returns `202 Accepted` until cross-module zero-state verification is complete.

### 14.3 Status path survives Tenant deletion

Because the Security Tenant is intentionally removed before overall completion is reported, status is not only available under a Tenant-scoped path.

Add:

```text
GET  /v1/project-deletion-operations/{operationId}
POST /v1/project-deletion-operations/{operationId}/retry
```

The operation receipt is retained outside the Project-owned delete cascade according to Audit Core administrative-operation retention policy. This revision does not invent a new retention duration.

### 14.4 Required order

```text
DI purge + verify
 -> Audit Core delete + verify
 -> Security Tenant delete LAST
 -> final verify
```

Security and DI admin calls use the same initiating human JWT.

Audit Core must not report completion after only an HTTP 2xx from downstream; the operation verifies the owning-module zero-state contract.

---

## 15. Phase-1 delete policy inside a continuing Project

Allowed administrative hard delete where dependency preflight permits:

- Dealer;
- Dealer Outlet;
- Role Mapping/business assignment removal;
- unconfirmed/staging/DRAFT master import data according to owning module lifecycle.

Not silently hard-deleted from a continuing active Project:

- published master version/history used for decision reproducibility;
- Customer/Journey/audit/evidence/workflow history through ordinary row-level UI Delete.

Whole-Project rollback may remove Project-scoped history through the explicit cross-module delete operation.

---

## 16. Authorization for UC02 control-plane endpoints

UC02 create/delete Project are SuperAdmin administrative operations.

The current approved Audit Core permission catalogue has no destructive Project-delete key; this Markdown revision therefore does not invent one.

Audit Core must use live Security authorization/administrative classification to prove the caller is the one active SuperAdmin for these control-plane operations. Exact administrative-attestation transport must be frozen in the Security/AuthZ implementation contract before code.

All ordinary existing Audit Core permission checks remain unchanged.

---

## 17. API test gates

Before the machine-readable OpenAPI is changed, contract tests must be planned for:

- duplicate `POST /v1/projects` retry;
- partial Security/Audit Core/DI provisioning;
- Project PATCH restricted-field conflict;
- Dealer/Outlet cross-Tenant IDs;
- optional Google Place/manual-address paths;
- Dealer/Outlet delete impact and dependency rejection;
- Role Mapping partial Security/Audit Core failure/retry;
- global USER preservation on Role Mapping remove;
- WEF missing for effective-dated Excel import;
- malformed template/row preview/error report;
- upload without confirm creates no authoritative version;
- Product/Price/Discount cross-reference validation;
- overlap warning-only behavior;
- readiness PC-per-ACTIVE-Outlet blocking rule;
- activation denied on blockers;
- active Project hard delete with downstream timeout/retry;
- Security Tenant deletion last;
- deletion operation status after Tenant row no longer exists;
- Project recreate after successful rollback;
- `ServiceIntegration` rejected where human admin is required.

---

## 18. Supersession

For UC02 this v1.1 contract supersedes VAC-API-001 only for:

- Project create/provisioning;
- Project administration/readiness/activation;
- Dealer/Outlet admin delete;
- separate Dealer Outlet geo/Place fields;
- Role Mapping administration;
- Project Masters staging/preview/confirm facade;
- Phase-1 Project rollback hard delete;
- updated human-admin downstream identity propagation.

All existing Journey/process/evidence/audit/task/analytics routes remain governed by VAC-API-001 unless a later explicit revision changes them.