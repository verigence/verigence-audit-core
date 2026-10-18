# Verigence Audit Core — Pending Issues Register

**Document ID:** VAC-ISS-001  
**Version:** 1.3  
**Status:** ACTIVE  
**Created:** 2026-08-16  
**Updated:** 2026-10-18  
**Owner:** Audit Core / cross-module integration  
**Related tracker:** `docs/AUDIT_CORE_PROGRESS_TRACKER.md` / VAC-TRK-001

## 1. Purpose

This register records unresolved items that remain after Audit Core implementation completion and DEV deployment. It separates external integration blockers from business/design inputs that were intentionally not guessed during implementation.

An item is closed only when the stated acceptance condition has concrete evidence.

## 2. Cross-module deployment and integration blockers

| ID | Priority | Issue | Current evidence | Impact | Owner / next module | Acceptance condition | Status |
|---|---|---|---|---|---|---|---|
| XMOD-SEC-01 | P0 | Security DEV must expose the approved OAuth authentication/token service, including `POST /oauth/token`. | **Resolved 2026-08-16.** Security source commit `7f206e508a5d16c8ec4a77d3ead519042bfc7b68` passed CI run `31935073699` and was deployed to Railway as deployment `c56c9777-6e9d-43a0-8c64-c22da73e6dd8`. DEV verification run `31935073694` passed: JWKS live; unauthenticated `/oauth/token` returned OAuth `invalid_client` rather than 404; controlled authorization-code USER token, `client_credentials` SERVICE token and delegated token-exchange token were issued and JWKS-validated; Clerk login redirect was live. | The prior Security-server `/oauth/token` blocker is removed. | Security | Security OAuth/authentication service live in DEV with positive USER, SERVICE and delegated issuance plus negative invalid-client verification. | **CLOSED** |
| XMOD-AC-SEC-01 | P0 | Audit Core's actual confidential OAuth client credential must be registered in Security and synchronized into the deployed Audit Core Railway service. | **Resolved 2026-08-16.** Audit Core commit `fd5732cc5897d3173f4e312a83bb7a34b6482733` bootstrapped and retained a managed `audit-core` client secret only in Audit Core Railway, set `SECURITY_CLIENT_ID=audit-core` and the Security token URL, and exposed only a SHA-256 verifier. Security commit `cb8cd6475206ee7cdf02915e4a69dffe5f77a7f0` added verifier-based confidential-client authentication and least-privilege `di.document.read` / `di.document.upload` registration; CI run `31954157904` and binding/deploy run `31954157872` passed. Audit Core workflow run `31953891024` then passed with the real Railway-held credential: JWKS-validated SERVICE token issued with `sub=audit-core`, Tenant `dev-auth-test`, `actor_type=SERVICE`, scope `di.document.read`; a delegated token-exchange call using that same real client identity and an invalid SERVICE subject was authenticated and failed closed with `invalid_grant`, proving no SERVICE-to-USER fallback. The raw secret was not copied into Security or committed. | The actual Audit Core module identity can now authenticate to live Security and obtain a least-privilege SERVICE token. | Audit Core + Security deployment configuration | Register `audit-core` with approved DI scopes; retain the managed confidential-client credential in Audit Core Railway; configure the Security token URL; prove a real Audit Core-authenticated token request without exposing the secret. | **CLOSED** |
| XMOD-DI-01 | P0 | DI DEV is not currently available as a verified live dependency. | Audit Core deployment preflight could not resolve the previously recorded `di-api-dev.verigence.app` host. DI `dev` head `10afac88879e6ff3146319f8f14677817784b6b5` had failing DI CI and Railway DEV deployment runs on 2026-08-15. | Audit Core DI-backed evidence operations cannot be enabled against a verified DEV endpoint. | DI | DI DEV CI is green, Railway deployment is successful, a canonical DEV base URL is confirmed, and `/health` returns HTTP 200. | **NEXT BLOCKER** |
| XMOD-AC-01 | P0 | `DI_BASE_URL` is intentionally not configured in the deployed Audit Core service. | Final Audit Core deployment evidence explicitly reports `DI=NOT_CONFIGURED_PENDING_DI_DEV`. | Prevents accidental calls to a stale or invented DI endpoint; DI-backed operations remain fail-closed. | Audit Core | After XMOD-DI-01 is closed, configure the verified DI DEV base URL in Railway and redeploy/verify Audit Core without changing the approved integration contract. | WAITING ON DI |
| XMOD-E2E-01 | P0 | Real cross-module Audit Core → Security → DI smoke test has not yet been executed. | Security is live; the actual `audit-core` confidential-client binding is now live and its SERVICE token plus delegated fail-closed path are verified. A positive delegated USER flow is already proven in Security's controlled DEV smoke and Security regression tests, but the final Audit Core → Security → DI DEV operation still requires a verified DI DEV endpoint and a real Security USER subject in the end-to-end test. | Full cross-module operational readiness is not yet proven. | Audit Core + Security + DI | Using the real Audit Core confidential-client identity and a real Security-issued user flow, execute at least one approved Audit Core DI-backed operation in DEV; verify tenant/permission enforcement, successful DI call, error translation/fail-closed behavior, correlation/audit evidence, and no direct client-to-DI bypass. | WAITING ON XMOD-DI-01 + DI BINDING |
| OPS-URL-01 | P2 | Railway-generated Audit Core public hostname contains `production` although the deployed runtime is DEV. | Final deployment URL is `https://verigence-audit-core-production.up.railway.app`; runtime variables were configured with `APP_ENV=dev` in Railway DEV environment `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`. | Non-functional naming ambiguity could cause operator confusion. | Audit Core / platform ops | Confirm naming policy and, if required, assign a DEV-specific canonical/custom hostname without changing runtime behavior. | OPEN — NON-BLOCKING |

## 3. Deployment evidence already complete

### Audit Core DEV

- Audit Core source deployment commit: `3de3dd821f1b1f514a972b00b25ee3d45bc17300`.
- Audit Core CI run `31930752365`: SUCCESS.
- Railway deployment: `33863763-c351-47d4-a47a-8302ec59f71d`: SUCCESS.
- Public health check: `https://verigence-audit-core-production.up.railway.app/health`: PASS at deployment verification.
- Neon runtime configuration: configured.
- Security JWKS dependency: reachable and HTTP 200.
- Runtime database transactions use the approved `audit_core_runtime` role boundary.
- Audit Core Security client binding commit: `fd5732cc5897d3173f4e312a83bb7a34b6482733`.
- Real Audit Core Security client verification run `31953891024`: SUCCESS after Security registration; SERVICE issuance/JWKS validation PASS; delegated invalid-subject fail-closed PASS.

### Security DEV

- Original Security auth deployment source commit: `7f206e508a5d16c8ec4a77d3ead519042bfc7b68`.
- Original Security CI run `31935073699`: SUCCESS.
- Railway project: `verigence-security` / `a6808842-2e90-44f8-9172-63a905b24b5c`.
- Railway service: `verigence-security` / `cfc90262-4b33-419d-a874-4592de9e8db1`.
- Original Railway deployment: `c56c9777-6e9d-43a0-8c64-c22da73e6dd8`: SUCCESS.
- Original DEV verification run `31935073694`: SUCCESS.
- Audit Core binding source commit: `cb8cd6475206ee7cdf02915e4a69dffe5f77a7f0`.
- Audit Core binding CI run `31954157904`: SUCCESS.
- Audit Core binding/deploy run `31954157872`: SUCCESS; hashed verifier configured and live health PASS.
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
2. `XMOD-AC-SEC-01` — **CLOSED**: the actual Audit Core OAuth client is bound and verified against live Security.
3. Resolve `XMOD-DI-01` in DI.
4. Configure Audit Core DI runtime dependency (`XMOD-AC-01`).
5. Execute and evidence the real cross-module DEV smoke test (`XMOD-E2E-01`).
6. Address `OPS-URL-01` when platform naming is cleaned up; it is not a functional blocker.

Business/design inputs in section 4 remain on their own approval path and must not be silently folded into the cross-module deployment work.

## 6. Reviewer role (`REVIEWER`) known issues

Recorded 2026-10-18 when the Reviewer operating role was approved for build. The Reviewer sits at one physical location, is mapped to several Dealer Outlets, reviews what a PC captured, corrects values Document Intelligence read incorrectly, and marks a Journey's Booking or Delivery documents reviewed. The Reviewer does not upload, holds no task queue, cannot close a Task, and sits in no role ladder. These items were accepted as known before the build rather than resolved first.

| ID | Priority | Issue | Current evidence | Impact | Owner / next module | Acceptance condition | Status |
|---|---|---|---|---|---|---|---|
| RVW-SEC-01 | P0 | Security must register the `REVIEWER` operating-role template and resolve its permission bundle. | The bundle is defined in `docs/AUDIT_CORE_DEFAULT_ROLE_BUNDLES_v1.1.md` §8 and introduces no new permission names. Audit Core sends `{"roleKey": <operatingRole>}` to Security's operating-role endpoint and requires the receipt to echo the same `roleKey` or it compensates and rolls the mapping back (`security_integration.py:286-300`; `role_mappings.py:602-606`). The endpoint exists on Security `dev` (`api/routes/v2_rbac.py:162`, backed by `OperatingRoleAssignmentService`), so the contract is in place; only the role itself is missing. Security `dev` defines operating roles in `security.role_definitions` and `security.platform_role_permission_defaults`, seeded by `migrations/0013_v2_operating_role_defaults.sql` — not as module catalogue templates. verigence-security PR #110 adds REVIEWER there. | No `REVIEWER` role mapping can be saved: `user_tenant_operating_roles.role_key` carries a CHECK listing the five existing roles, so the assignment is refused at the database, and a Reviewer token resolves no `audit.*` permission. The Audit Core and web work is deliverable but not usable end to end until this is closed. | Security | REVIEWER present in `role_definitions`, the role-key CHECK widened, the §8 bundle in `platform_role_permission_defaults`, existing tenants backfilled in `tenant_role_permissions`, and new-tenant seeding no longer restricted to the original five roles; then a role mapping saves and echoes `roleKey=REVIEWER` and a Reviewer token resolves `audit.journey.read` and `audit.journey.update` while withholding `audit.work.*` and `audit.evidence.upload`. | **OPEN — verigence-security PR #110 raised against `dev`** |
| RVW-AC-01 | P1 | `p2_document_queue.business_stage` is not a trustworthy stage source and is read inconsistently. | Nullable `varchar(40)` with no CHECK constraint (`migrations/versions/0114_uc03_p2_foundation.py:64`), written only when a local capture row exists and never cleared (`uc03_p2_worker.py:2168`, `:2199`). Two call sites disagree: `uc03_p2_journey360.py:1261` reads a null stage as `BOOKING`; `uc03_p2_api.py:2187` treats a null stage as matching nothing. | A stage-scoped control keyed on this column would block or permit by accident, intermittently. | Audit Core | The two inconsistent reads are reconciled, or the column is retired in favour of the capture stage. | OPEN — WORKED AROUND: Reviewer logic resolves stage from `document_capture_v2_documents.stage_code` and `journey_document_extracted_fields.stage_code`, both `NOT NULL`. `business_stage` is not introduced into new logic (owner decision 2026-10-18). |
| RVW-AC-02 | P1 | A page resync destroys a correction in place, attribution included. | Both field upserts conflict on a key containing `source_fact_version` and then `DO UPDATE SET modified_value=EXCLUDED.modified_value, is_modified=EXCLUDED.is_modified, modified_by_actor_id=EXCLUDED.modified_by_actor_id, modified_at_utc=EXCLUDED.modified_at_utc` (`uc03_di_core_persistence.py` `_LEGACY_UPSERT`, `_V2_UPSERT`). Resync is "a read of what DI already holds, no new reading" (`uc03_p2_api.py:1686-1688`), so the fact version normally does not advance and the existing row is overwritten rather than superseded. | Without compensation, the evidence that a correction was ever made is erased along with the value. | Audit Core | The append-only field change log is populated on every apply and every overwrite, so a destroyed correction remains reconstructible. | OPEN — COMPENSATED: change log, a page-scoped pre-resync warning, and resync refused for a stage already marked reviewed with a TL/PM override carrying a reason. The override is a deliberate change to what a PC may do on a reviewed stage. |
| RVW-AC-03 | P2 | A correction ranks below a newer DI fact version. | `load_reviewed_field_details` orders `source_fact_version DESC` before `reviewed_at_utc DESC` (`uc03_journey_reviewed_details.py:206-213`). | Where a resync does advance the fact version, the corrected row survives in the table but stops being the effective value. | Audit Core | Confirm whether an applied correction advances `source_fact_version`; if it does not, no further action is required. | OPEN — ACCEPTED: the pre-resync warning tells the Reviewer the change must be redone (owner decision 2026-10-18). |
| RVW-AC-04 | P2 | No separation of duties on a value change. | `proposed_by_actor_id` is written (`uc03_document_field_corrections.py:178`) and never read back for comparison; `submit_action` compares the actor to `assigned_actor_id` only when one is set (`uc03_p2_tasks.py:377-382`), so a TL may propose and then approve the same ≥90% correction. | The "second pair of eyes" on a high-confidence value change is a role gate, not a person gate, and is self-satisfiable. Pre-existing; the Reviewer role neither creates nor worsens it. | Audit Core | A proposer ≠ approver rule, if the owner wants one. | OPEN — PRE-EXISTING |
| RVW-AC-05 | P2 | Reviewer claim is advisory and does not gate the review. | Owner decision 2026-10-18: claim records and shows who owns a Journey but is not required in order to mark a stage reviewed, so there is no stale claim to release, reassign or expire. | Two Reviewers may work one Journey. Only a Reviewer who has personally opened every document on a stage can mark that stage reviewed, so the attestation stays personal. | Audit Core | Revisit once Reviewers have used it; tighten to a lock only if duplicated effort proves real. | OPEN — BY DESIGN |
| RVW-AC-06 | P2 | One operating role per actor. | Two active `business_assignments` rows with different `business_role_code` values raise `403 "Ambiguous operating role"` (`uc03_p2_access.py:111-116`), and `_mapping_from_rows` raises on inconsistent roles (`role_mapping_policy.py:28-29`). | Nobody can be both a PC and a Reviewer, so a Reviewer cannot cover an upload and a PC cannot temporarily review. | Audit Core | None; accepted by the owner on 2026-10-18. | OPEN — ACCEPTED |
