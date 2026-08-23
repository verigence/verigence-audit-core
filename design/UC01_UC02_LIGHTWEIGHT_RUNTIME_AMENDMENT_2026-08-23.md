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
