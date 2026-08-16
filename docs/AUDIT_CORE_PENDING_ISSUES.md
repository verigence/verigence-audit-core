# Verigence Audit Core — Pending Issues Register

**Document ID:** VAC-ISS-001  
**Version:** 1.0  
**Status:** ACTIVE  
**Created:** 2026-08-16  
**Owner:** Audit Core / cross-module integration  
**Related tracker:** `docs/AUDIT_CORE_PROGRESS_TRACKER.md` / VAC-TRK-001

## 1. Purpose

This register records unresolved items that remain after Audit Core implementation completion and DEV deployment. It separates external integration blockers from business/design inputs that were intentionally not guessed during implementation.

An item is closed only when the stated acceptance condition has concrete evidence.

## 2. Cross-module deployment and integration blockers

| ID | Priority | Issue | Current evidence | Impact | Owner / next module | Acceptance condition | Status |
|---|---|---|---|---|---|---|---|
| XMOD-SEC-01 | P0 | Security DEV does not expose the approved OAuth token-exchange endpoint at `POST /oauth/token`. | Audit Core deployment preflight on 2026-08-16 reached Security DEV successfully; `/.well-known/jwks.json` returned HTTP 200, while unauthenticated `POST /oauth/token` returned HTTP 404. | Audit Core can validate Security-issued user JWTs, but cannot obtain the delegated/service token required for downstream DI calls. | Security | Deploy the approved token-exchange implementation; endpoint is routable and returns the contractually correct auth/validation response instead of 404; positive issuance is proven with a real approved client/service identity. | BLOCKED — MOVE TO SECURITY NEXT |
| XMOD-DI-01 | P0 | DI DEV is not currently available as a verified live dependency. | Audit Core deployment preflight could not resolve the previously recorded `di-api-dev.verigence.app` host. DI `dev` head `10afac88879e6ff3146319f8f14677817784b6b5` had failing DI CI and Railway DEV deployment runs on 2026-08-15. | Audit Core DI-backed evidence operations cannot be enabled against a verified DEV endpoint. | DI | DI DEV CI is green, Railway deployment is successful, a canonical DEV base URL is confirmed, and `/health` returns HTTP 200. | BLOCKED — AFTER SECURITY |
| XMOD-AC-01 | P0 | `DI_BASE_URL` is intentionally not configured in the deployed Audit Core service. | Final Audit Core deployment evidence explicitly reports `DI=NOT_CONFIGURED_PENDING_DI_DEV`. | Prevents accidental calls to a stale or invented DI endpoint; DI-backed operations remain fail-closed. | Audit Core | After XMOD-DI-01 is closed, configure the verified DI DEV base URL in Railway and redeploy/verify Audit Core without changing the approved integration contract. | WAITING ON DI |
| XMOD-E2E-01 | P0 | Real cross-module Audit Core → Security → DI smoke test has not yet been executed. | Audit Core unit/controlled integration and release tests are green, but DEV Security OAuth and DI are not both live, so a real end-to-end dependency test is not yet possible. | Full cross-module operational readiness is not yet proven. | Audit Core + Security + DI | Using a real Security-issued identity/token flow, execute at least one approved Audit Core DI-backed operation in DEV; verify tenant/permission enforcement, successful DI call, error translation/fail-closed behavior, correlation/audit evidence, and no direct client-to-DI bypass. | WAITING ON XMOD-SEC-01 + XMOD-DI-01 |
| OPS-URL-01 | P2 | Railway-generated Audit Core public hostname contains `production` although the deployed runtime is DEV. | Final deployment URL is `https://verigence-audit-core-production.up.railway.app`; runtime variables were configured with `APP_ENV=dev` in Railway DEV environment `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`. | Non-functional naming ambiguity could cause operator confusion. | Audit Core / platform ops | Confirm naming policy and, if required, assign a DEV-specific canonical/custom hostname without changing runtime behavior. | OPEN — NON-BLOCKING |

## 3. Audit Core DEV deployment evidence

The following is already complete and is not a pending issue:

- Audit Core source commit: `3de3dd821f1b1f514a972b00b25ee3d45bc17300`.
- Audit Core CI run `31930752365`: SUCCESS.
- Railway deployment: `33863763-c351-47d4-a47a-8302ec59f71d`: SUCCESS.
- Public health check: `https://verigence-audit-core-production.up.railway.app/health`: PASS at deployment verification.
- Neon runtime configuration: configured.
- Security JWKS dependency: reachable and HTTP 200.
- Runtime database transactions use the approved `audit_core_runtime` role boundary.

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

1. Resolve `XMOD-SEC-01` in Security.
2. Resolve `XMOD-DI-01` in DI.
3. Configure Audit Core DI runtime dependency (`XMOD-AC-01`).
4. Execute and evidence the real cross-module DEV smoke test (`XMOD-E2E-01`).
5. Address `OPS-URL-01` when platform naming is cleaned up; it is not a functional blocker.

Business/design inputs in section 4 remain on their own approval path and must not be silently folded into the cross-module deployment work.
