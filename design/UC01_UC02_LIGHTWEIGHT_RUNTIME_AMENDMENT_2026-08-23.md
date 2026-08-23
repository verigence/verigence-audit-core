# UC01 / UC02 Lightweight Runtime Amendment

Date: 2026-08-23
Status: implementation amendment from `dev`

This amendment applies the cross-module Security runtime policy to Audit Core without changing the UC02 business workflow or introducing UC03 journey-master caching.

## Runtime rules

1. Keep Web lazy loading. Do not create a large bootstrap response merely to reduce request count.
2. Reuse the Security human bearer token supplied by Web. Validate it on every protected request.
3. Reuse the backend JWT validator/JWKS client so signature verification is local during normal operation; JWKS network retrieval is for initial key acquisition/key rotation, not every API call.
4. Reuse backend ServiceIntegration tokens while valid rather than issuing a machine token for each authorization call. Authorization decisions themselves are not made authoritative by browser state.
5. Read-only reference/master data uses authenticated-human access unless a stronger read restriction is explicitly documented. SuperAdmin remains required for administrative mutations and the cross-Tenant Project administration directory.
6. Do not cache all UC02 master result sets. Journey-hot master caching is a UC03 concern and is introduced only when the journey flow demonstrates repeated use.
7. Audit Core uses one cached SQLAlchemy Engine. PostgreSQL pooling is hardened centrally with pre-ping and bounded pool/connect/statement timeouts. No generic transaction replay is added.

## Approved lightweight read paths

The following GET paths may use authenticated-human validation without a live `/security/v1/platform/admin-context` SuperAdmin attestation:

- `/v1/project-reference-data`
- `/v1/tenants/{tenant_id}/project-masters`
- `/v1/tenants/{tenant_id}/project-masters/{owner_module}/{master_key}/versions`
- `/v1/tenants/{tenant_id}/project-masters/DI/{master_key}/template`

`GET /v1/projects` is intentionally not in this list. It is a cross-Tenant Project Administration directory and therefore requires SuperAdmin attestation. The attestation is reused for the existing short server-side window; the Project directory itself is read from Audit Core in one cross-Tenant database query after that authorization succeeds.

Staged-import detail/error reports and every state-changing Project Master operation remain on the stronger administrative boundary.

## Project creation screen request budget

The Project creation screen is governed by the following request-path budget:

- Project directory: one lazy `GET /v1/projects` on screen entry; Project selection/form state changes do not trigger another directory read.
- Project reference data: one lazy `GET /v1/project-reference-data` on screen entry; changing Project form fields does not retrigger the reference-data read.
- Project create: one `POST /v1/projects` for the explicit create action.
- After successful create, Web uses the create response to preserve/select the new Project in the local Project directory state. It does not immediately issue a redundant `GET /v1/projects` merely because `tenantId` changed.
- Loading the full newly-created Project aggregate is allowed once because the create response is not the edit/version contract used by the Project Details screen.

The browser may therefore make more than one legitimate Audit Core API request while creating a Project, but it must not create a request storm from React renders, callback identity changes, focus/reconnect, or Tenant selection changes.

## JWKS network-call assurance

Audit Core owns one process-cached `SecurityTokenValidator`, which owns one `PyJWKClient`. The JWKS set cache is explicitly enabled with a bounded lifespan. For a normal Project creation screen interaction using the same currently-valid signing key (`kid`):

- the first protected Audit Core request after process/JWKS-cache initialization may cause one JWKS network fetch;
- all additional protected requests on the Project creation screen validate the human JWT locally from the same cached JWKS set;
- therefore the normal screen burst causes **at most one JWKS network fetch per Audit Core process/JWKS cache window**, not one JWKS call per API request.

A second JWKS network fetch is permitted only for the security-correct exceptional path where the cached set has expired or Security has rotated to an unknown signing key. This exception is deliberate key-rotation behavior, not a Project-screen request burst.

CI must contain a regression test that validates the same signed human token repeatedly through one real `PyJWKClient` instance and proves the JWKS fetch function is invoked exactly once.

## Project directory source of truth

A successfully provisioned Project persisted in `auditcore.projects` must remain discoverable in Project Administration independently of a transient Security Tenant-directory read. Accordingly:

- `GET /v1/projects` is a SuperAdmin-only cross-Tenant control-plane read;
- the directory is sourced from persisted Audit Core Projects in one SQL query;
- it does not call Security `/security/v1/platform/tenants`;
- it does not execute one database query per Security Tenant;
- live Security Tenant status is not required to render the Project selector; Security remains authoritative for authentication, SuperAdmin authorization and Tenant mutations.

This removes the previous intersection failure mode where an existing Audit Core Project could disappear from the selector because the Security Tenant directory was unavailable, incomplete, or temporarily inconsistent.

## Database configuration

Production PostgreSQL connections use:

- `pool_pre_ping=True`
- pool checkout timeout: 5 seconds
- connect timeout: 5 seconds
- statement timeout: 10 seconds

The timeout settings are configured once at Engine creation. API handlers do not implement their own DB retry loops.

## UC01 note

UC01 pending approvals already use a separate lazy query. The Security v2 USER directory owns server-side `userStatus`, `limit` and `offset` filtering. The runtime objective is to keep that request small and avoid introducing additional token/JWKS network work.

## Defect register — UC01 / UC02 request-path performance and continuity

These defects are treated as one cross-screen request-path cleanup, not as isolated fixes that depend on manual screenshots for every screen.

### DEF-PERF-001 — ProjectSelector re-fetch loop

Closure requirement: Project directory loading depends only on the authenticated session/request trigger that genuinely requires a reload. Tenant selection, Dealer/Outlet state and Project form state must not cause a Project directory refresh. Duplicate development mounts must share an in-flight directory request rather than producing duplicate network calls.

### DEF-PERF-002 — Existing Project directory can return empty despite persisted Projects

Closure requirement: a successfully provisioned Project persisted in Audit Core remains discoverable and selectable on the next visit. The Project directory must not depend on intersecting a live Security Tenant list with per-Tenant Audit Core lookups.

### DEF-PERF-003 — `/v1/projects` causes excessive Security and per-Tenant calls

Closure requirement: one logical Project directory read performs one SuperAdmin authorization decision (subject to the short existing server-side attestation reuse) and one Audit Core Project directory SQL query. It performs zero Security `/platform/tenants` directory calls and zero N+1 per-Tenant Project queries.

### DEF-PERF-004 — Dealer addition performs unnecessary follow-up reads and Security calls

Closure requirement: use the Dealer create response to update local page state where safe; do not immediately re-read data that is already known. Dealer list/read access uses the minimum correct authorization boundary and does not create repeated live Security calls for ordinary page rendering.

### DEF-PERF-005 — Outlet addition performs unnecessary follow-up reads and Security calls

Closure requirement: one Outlet addition results in the mutation plus only the minimum data refresh actually required. Use the returned Outlet state directly where safe and remove repeated live Security work from ordinary reads.

### DEF-PERF-006 — UC01 / UC02 complete request-surface audit

The Web-to-Audit-Core-to-Security request path for UC01 landing/pending activities and every UC02 step is checked for:

- unstable React effects that refetch on unrelated renders;
- duplicate or immediately redundant GETs after successful mutations;
- repeated `admin-context` calls during one user interaction;
- unnecessary SuperAdmin requirements on ordinary read-only data;
- repeated `/platform/tenants` directory reads;
- N+1 backend service or DB calls;
- avoidable ServiceIntegration token issuance;
- repeated JWKS network retrieval despite a stable signing key;
- calls that can be satisfied by the response already returned from the preceding mutation.

Acceptance principle: a user should not have to report the same request-storm pattern screen by screen. The complete UC01/UC02 request surface is corrected as one performance/continuity pass before feature expansion continues.
