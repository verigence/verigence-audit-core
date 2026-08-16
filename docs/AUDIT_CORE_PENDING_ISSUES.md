# Verigence Audit Core — Pending Issues Register

**Document ID:** VAC-ISS-001  
**Version:** 1.1  
**Status:** ACTIVE  
**Created:** 2026-08-16  
**Updated:** 2026-08-16  
**Owner:** Audit Core / cross-module integration  
**Related tracker:** `docs/AUDIT_CORE_PROGRESS_TRACKER.md` / VAC-TRK-001

## 1. Purpose

This register records unresolved items that remain after Audit Core implementation completion and DEV deployment. It separates external integration blockers from business/design inputs that were intentionally not guessed during implementation.

An item is closed only when the stated acceptance condition has concrete evidence.

## 2. Cross-module deployment and integration blockers

| ID | Priority | Issue | Current evidence | Impact | Owner / next module | Acceptance condition | Status |
|---|---|---|---|---|---|---|---|
| XMOD-SEC-01 | P0 | Security DEV must expose the approved OAuth authentication/token service, including `POST /oauth/token`. | **Resolved 2026-08-16.** Security source commit `7f206e508a5d16c8ec4a77d3ead519042bfc7b68` passed CI run `31935073699` and was deployed to Railway as deployment `c56c9777-6e9d-43a0-8c64-c22da73e6dd8`. DEV verification run `31935073694` passed: JWKS live; unauthenticated `/oauth/token` returned OAuth `invalid_client` rather than 404; controlled authorization-code USER token, `client_credentials` SERVICE token and delegated token-exchange token were issued and JWKS-validated; Clerk login redirect was live. | The prior Security-server `/oauth/token` blocker is removed. | Security | Security OAuth/authentication service live in DEV with positive USER, SERVICE and delegated issuance plus negative invalid-client verification. | **CLOSED** |
| XMOD-AC-SEC-01 | P0 | Audit Core's actual confidential OAuth client credential must be registered in Security and synchronized into the deployed Audit Core Railway service. | Security DEV is live and verified with a controlled client `security-dev-test`, but no evidence yet proves that the deployed Audit Core service possesses a matching registered `audit-core` client credential. The Audit Core repository's temporary Railway inspection workflow could not inspect that service because the repository does not currently have a usable Railway Actions credential. | Audit Core cannot yet be claimed to obtain a real SERVICE/delegated token using its own module identity, even though Security can issue those token types. | Audit Core + Security deployment configuration | Register `audit-core` with the approved DI scopes in Security; store the same managed confidential-client credential in Audit Core Railway; configure the Security token URL; make a real Audit Core-authenticated `client_credentials` and/or delegated-token request successfully; do not expose the secret. | **OPEN — NEXT CROSS-MODULE BINDING** |
| XMOD-DI-01 | P0 | DI DEV is not currently available as a verified live dependency. | Audit Core deployment preflight could not resolve the previously recorded `di-api-dev.verigence.app` host. DI `dev` head `10afac88879e6ff3146319f8f14677817784b6b5` had failing DI CI and Railway DEV deployment runs on 2026-08-15. | Audit Core DI-backed evidence operations cannot be enabled against a verified DEV endpoint. | DI | DI DEV CI is green, Railway deployment is successful, a canonical DEV base URL is confirmed, and `/health` returns HTTP 200. | BLOCKED — AFTER CLIENT BINDING |
| XMOD-AC-01 | P0 | `DI_BASE_URL` is intentionally not configured in the deployed Audit Core service. | Final Audit Core deployment evidence explicitly reports `DI=NOT_CONFIGURED_PENDING_DI_DEV`. | Prevents accidental calls to a stale or invented DI endpoint; DI-backed operations remain fail-closed. | Audit Core | After XMOD-DI-01 is closed, configure the verified DI DEV base URL in Railway and redeploy/verify Audit Core without changing the approved integration contract. | WAITING ON DI |
| XMOD-E2E-01 | P0 | Real cross-module Audit Core → Security → DI smoke test has not yet been executed. | Security's own DEV USER/SERVICE/delegated OAuth smoke test is now green, but Audit Core's actual OAuth client binding and DI DEV availability are still outstanding. | Full cross-module operational readiness is not yet proven. | Audit Core + Security + DI | Using the real Audit Core confidential-client identity and a real Security-issued user flow, execute at least one approved Audit Core DI-backed operation in DEV; verify tenant/permission enforcement, successful DI call, error translation/fail-closed behavior, correlation/audit evidence, and no direct client-to-DI bypass. | WAITING ON XMOD-AC-SEC-01 + XMOD-DI-01 |
| OPS-URL-01 | P2 | Railway-generated Audit Core public hostname contains `production` although the deployed runtime is DEV. | Final deployment URL is `https://verigence-audit-core-production.up.railway.app`; runtime variables were configured with `APP_ENV=dev` in Railway DEV environment `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`. | Non-functional naming ambiguity could cause operator confusion. | Audit Core / platform ops | Confirm naming policy and, if required, assign a DEV-specific canonical/custom hostname without changing runtime behavior. | OPEN — NON-BLOCKING |

## 3. Deployment evidence already complete

### Audit Core DEV

- Audit Core source commit: `3de3dd821f1b1f514a972b00b25ee3d45bc17300`.
- Audit Core CI run `31930752365`: SUCCESS.
- Railway deployment: `33863763-c351-47d4-a47a-8302ec59f71d`: SUCCESS.
- Public health check: `https://verigence-audit-core-production.up.railway.app/health`: PASS at deployment verification.
- Neon runtime configuration: configured.
- Security JWKS dependency: reachable and HTTP 200.
- Runtime database transactions use the approved `audit_core_runtime` role boundary.

### Security DEV

- Security source commit: `7f206e508a5d16c8ec4a77d3ead519042bfc7b68`.
- Security CI run `31935073699`: SUCCESS.
- Railway project: `verigence-security` / `a6808842-2e90-44f8-9172-63a905b24b5c`.
- Railway service: `verigence-security` / `cfc90262-4b33-419d-a874-4592de9e8db1`.
- Railway deployment: `c56c9777-6e9d-43a0-8c64-c22da73e6dd8`: SUCCESS.
- DEV verification run `31935073694`: SUCCESS.
- Security auth/session/membership database schema: applied and verified.
- Clerk upstream OAuth application behind Security: configured.
- Retained DEV test user: `verigence.security.devtest@example.com`, Tenant `dev-auth-test`, role `PC` (intentionally not deleted).
- Live Security USER, SERVICE and delegated token issuance: PASS; JWT validation through Security JWKS: PASS.

## 4. Unresolved business/design inputs

These are inherited from VAC-TRK-001 and remain intentionally unresolved. They did not block unrelated implementation completion and must not be guessed:

- Satellite monthly-volume threshold and classification approval policy;
- PM versus PMO final terminology;
- exact normal-path versus exception-path TL/PM verification gate;
- actual dealership delivery-status code vocabulary;
- Total Discount / Above Scheme formula;
- PO/DO/Refund realised-payment logic;
- Insurance Calculator provider/rules;
- Trade-In 60 versus 90-day ageing threshold;
- dedicated Trade-In Sales field meaning where source is ambiguous;
- Short/Excess formula;
- notification provider/channel;
- repeat-customer reuse/link policy;
- Dealer Outlet ↔ Security Location cardinality.

## 5. Resolution order

1. `XMOD-SEC-01` — **CLOSED**: Security OAuth/authentication server is live and verified.
2. Resolve `XMOD-AC-SEC-01`: bind the actual Audit Core OAuth client credential to Security.
3. Resolve `XMOD-DI-01` in DI.
4. Configure Audit Core DI runtime dependency (`XMOD-AC-01`).
5. Execute and evidence the real cross-module DEV smoke test (`XMOD-E2E-01`).
6. Address `OPS-URL-01` when platform naming is cleaned up; it is not a functional blocker.

Business/design inputs in section 4 remain on their own approval path and must not be silently folded into the cross-module deployment work.
