# UC01 / UC02 Lightweight Runtime Amendment

Date: 2026-08-23
Status: implementation amendment from `dev`

This amendment applies the cross-module Security runtime policy to Audit Core without changing the UC02 business workflow or introducing UC03 journey-master caching.

## Runtime rules

1. Keep Web lazy loading. Do not create a large bootstrap response merely to reduce request count.
2. Reuse the Security human bearer token supplied by Web. Validate it on every protected request.
3. Reuse the backend JWT validator/JWKS client so signature verification is local during normal operation; JWKS network retrieval is for initial key acquisition/key rotation, not every API call.
4. Reuse backend ServiceIntegration tokens while valid rather than issuing a machine token for each authorization call. Authorization decisions themselves are not made authoritative by browser state.
5. Read-only reference/master data uses authenticated-human access unless a stronger read restriction is explicitly documented. SuperAdmin remains required for administrative mutations.
6. Do not cache all UC02 master result sets. Journey-hot master caching is a UC03 concern and is introduced only when the journey flow demonstrates repeated use.
7. Audit Core uses one cached SQLAlchemy Engine. PostgreSQL pooling is hardened centrally with pre-ping and bounded pool/connect/statement timeouts. No generic transaction replay is added.

## Approved lightweight read paths

The following GET paths may use authenticated-human validation without a live `/security/v1/platform/admin-context` SuperAdmin attestation:

- `/v1/project-reference-data`
- `/v1/tenants/{tenant_id}/project-masters`
- `/v1/tenants/{tenant_id}/project-masters/{owner_module}/{master_key}/versions`
- `/v1/tenants/{tenant_id}/project-masters/DI/{master_key}/template`

Staged-import detail/error reports and every state-changing Project Master operation remain on the stronger administrative boundary.

## Database configuration

Production PostgreSQL connections use:

- `pool_pre_ping=True`
- pool checkout timeout: 5 seconds
- connect timeout: 5 seconds
- statement timeout: 10 seconds

The timeout settings are configured once at Engine creation. API handlers do not implement their own DB retry loops.

## UC01 note

UC01 pending approvals already use a separate lazy query. The Security v2 USER directory owns server-side `userStatus`, `limit` and `offset` filtering. The runtime objective is to keep that request small and avoid introducing additional token/JWKS network work.

## Open defect register — UC01 / UC02 request-path performance and continuity

These defects must be treated as one cross-screen request-path cleanup, not as isolated fixes that depend on manual screenshots for every screen.

### DEF-PERF-001 — ProjectSelector re-fetch loop

`ProjectSelector` currently includes callback props such as `onSelectionChange` in its effect dependency list while the parent recreates those callbacks on render. Routine Project Administration state changes can therefore retrigger `GET /v1/projects` repeatedly.

Expected correction: Project directory loading must depend only on stable data inputs that genuinely require a reload. Dealer/outlet/form state changes must not cause a Project directory refresh.

### DEF-PERF-002 — Existing Project directory can return empty despite persisted Projects

DEV evidence shows persisted Audit Core Projects while the Web Project selector can still display an empty directory. `/v1/projects` currently obtains the Security Tenant directory and then performs per-Tenant Audit Core Project lookups, returning only the intersection. This makes Project continuity dependent on multiple services and per-Tenant lookups.

Expected correction: an existing successfully provisioned Project must remain discoverable and selectable on the next visit. The Project directory path must not silently lose persisted Projects because of unnecessary cross-service joining or request cancellation/race behaviour.

### DEF-PERF-003 — `/v1/projects` causes excessive Security and per-Tenant calls

One logical Project directory read currently expands into live SuperAdmin/admin-context work, a Security `/platform/tenants` read, and per-Tenant Audit Core DB lookups. Repeated Web fetches multiply this into large Security-call bursts.

Expected correction: keep the Project list lazy and small, but make one logical Project-list request lightweight. Remove avoidable repeated Security attestations and N+1 per-Tenant query behaviour while preserving the required administrative access boundary.

### DEF-PERF-004 — Dealer addition performs unnecessary follow-up reads and Security calls

After `POST /dealers`, Web currently reloads Dealers and can immediately load Outlets for the newly created Dealer, even though the create response already contains the Dealer and a new Dealer cannot yet have Outlets. Dealer read endpoints also currently use live SuperAdmin validation, causing additional Security traffic.

Expected correction: use the Dealer create response to update local page state where safe; do not immediately re-read data that is already known. Dealer list/read access must use the minimum correct authorization boundary and must not create repeated live Security calls for ordinary page rendering.

### DEF-PERF-005 — Outlet addition performs unnecessary follow-up reads and Security calls

Outlet creation/listing follows the same heavy pattern: administrative mutation is followed by avoidable refresh calls, while ordinary Dealer/Outlet reads can invoke live SuperAdmin checks. Combined with parent re-renders, this produces request bursts disproportionate to one user action.

Expected correction: one Outlet addition should result in the mutation plus only the minimum data refresh actually required. Use the returned Outlet state directly where safe and remove repeated live Security work from ordinary reads.

### DEF-PERF-006 — UC01 / UC02 must be audited as complete request surfaces

The same class of defects may exist outside Project Details, Dealers and Outlets. The Web-to-Audit-Core-to-Security request path for UC01 landing/pending activities and every UC02 step must be reviewed proactively for:

- unstable React effects that refetch on unrelated renders;
- duplicate or immediately redundant GETs after successful mutations;
- repeated `admin-context` calls during one user interaction;
- unnecessary SuperAdmin requirements on ordinary read-only data;
- repeated `/platform/tenants` directory reads;
- N+1 backend service or DB calls;
- avoidable ServiceIntegration token issuance;
- calls that can be satisfied by the response already returned from the preceding mutation.

Acceptance principle: a user should not have to report the same request-storm pattern screen by screen. The complete UC01/UC02 request surface must be checked and corrected as one performance/continuity pass before feature expansion continues.
