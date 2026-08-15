# Verigence Audit Core — Implementation Progress Tracker

**Document ID:** VAC-TRK-001  
**Version:** 1.0  
**Status:** ACTIVE  
**Created:** 2026-08-15  
**Implementation plan:** `docs/AUDIT_CORE_IMPLEMENTATION_PLAN_v1.0.md` / VAC-IMP-001

## 1. Tracker rules

This file is the operational source of truth for Audit Core implementation progress.

Allowed task states:

`NOT STARTED -> IN PROGRESS -> CODE COMPLETE -> VERIFIED -> COMPLETE`

`BLOCKED` may be used at any non-complete stage when a named dependency prevents work.

Rules:

1. Design/document creation alone does not complete an implementation task.
2. A task cannot move from `IN PROGRESS` directly to `COMPLETE`.
3. `COMPLETE` requires the task's acceptance condition plus concrete evidence.
4. Evidence must point to something real: commit/path, test, CI run, migration result or dev verification.
5. Do not mark neighboring tasks complete by inference.
6. Use deeper verification only for higher-risk areas; avoid duplicate/wasteful gates.
7. When blocked by an unresolved business decision, mark `BLOCKED`; do not invent the rule.

## 2. Current position

**Implementation tasks:** 48  
**COMPLETE:** 10  
**VERIFIED:** 0  
**CODE COMPLETE:** 0  
**IN PROGRESS:** 0  
**BLOCKED:** 0  
**NOT STARTED:** 38  
**Implementation completion:** 20.8%

Repository/CI foundation and the PostgreSQL foundation are complete. Runtime configuration fails safely when `APP_ENV` is absent; VAC-DB-002 is an immutable Alembic baseline; Tenant RLS, no-delete privileges and published-master immutability are verified. C-01 now validates Security-issued JWTs through JWKS and produces a Tenant/permission principal context. C-02 Tenant and permission enforcement is the next eligible task.

## 3. Increment summary

| Increment | Scope | Tasks | Complete | Status |
|---|---|---:|---:|---|
| P0 | Freeze implementation inputs | 2 | 2 | COMPLETE |
| A | Repository and CI foundation | 3 | 3 | COMPLETE |
| B | PostgreSQL foundation | 4 | 4 | COMPLETE |
| C | Security, errors and request context | 4 | 1 | IN PROGRESS |
| D | Project landscape and assignments | 4 | 0 | NOT STARTED |
| E | Versioned masters | 4 | 0 | NOT STARTED |
| F | Customer and Journey | 3 | 0 | NOT STARTED |
| G | Internal DI façade | 5 | 0 | NOT STARTED |
| H | Vehicle-sale Journey process data | 5 | 0 | NOT STARTED |
| I | Audit controls, findings and review | 3 | 0 | NOT STARTED |
| J | Durable Audit workflow | 4 | 0 | NOT STARTED |
| K | Daily operations, CRM and escalations | 3 | 0 | NOT STARTED |
| L | API/observability/release verification | 4 | 0 | NOT STARTED |

## 4. Detailed tracker

| ID | Task | Status | Evidence / verification | Blocker / note |
|---|---|---|---|---|
| P0-01 | Approve v2.1 design package | COMPLETE | Project-owner approval recorded 2026-08-15; `DESIGN_BASELINE_MANIFEST.md` marked APPROVED / BASELINED FOR IMPLEMENTATION in commit `f4f4bcabe720c50c36ff967fda269b9ec82746e5` and verified by commit diff | None |
| P0-02 | Confirm runtime/tooling | COMPLETE | Project-owner confirmation recorded 2026-08-15; `docs/AUDIT_CORE_RUNTIME_TOOLING_v1.0.md` committed as the approved runtime/tooling/hosting baseline in commit `823ff236aabb95e3e05b9389017299d7da527e0a` and verified by repository read | None |
| A-01 | Scaffold Audit Core service | COMPLETE | Service scaffold committed in `a1601fb0f417708ebf2d4918882e89b6f4e7e219`; `pytest -q` passed 2 tests; Uvicorn startup verified `/health` = 200 with `{"status":"ok"}`, while `/` and `/docs` = 404; committed files re-read from `main` | None |
| A-02 | Add CI quality gate | COMPLETE | CI workflow and Ruff gate introduced in `05b766cd740e93e02d88037557124b152fed998e`; lint corrections completed through `aa4ae93915aaa109f2c6f69a19e6d17f8d8ebf13`; GitHub Actions run `31878194470` passed build, Ruff lint and `pytest -q` unit tests on `main` | None |
| A-03 | Add environment/config validation | COMPLETE | `src/audit_core/config.py`, startup validation in `src/audit_core/main.py`, and `tests/test_config.py` implemented through commits `0c41b851e302ecf080535e24b694d0b35bb8dee1`, `9fd9c9ea0ec5fd48e4d3a896377e4c283305b6df`, and `80e3fc7f4fa4458a0eba24e8697c17cee5aa11ba`; GitHub Actions run `31878482855` passed after test-environment lint correction `56a1e50e2a7df10cdbef5b77c2aea0daeebbb4bc`; missing `APP_ENV` is covered by a negative startup test without secret values in the error | None |
| B-01 | Convert VAC-DB-002 into migration baseline | COMPLETE | Alembic config/environment and frozen migration `0001_vac_db_002_baseline.py` implemented; migration verifies the source Git blob before execution and strips source transaction wrappers; PostgreSQL driver format-token handling corrected in `e2d45396c558de63d9e4e7f1f70b477ec19f41f1`; GitHub Actions run `31878639884` applied the migration from a fresh PostgreSQL 16 database and passed build/lint/tests | None |
| B-02 | Enforce Tenant RLS runtime pattern | COMPLETE | `0002_runtime_role_rls.py` creates non-login, non-owner, non-superuser, non-BYPASSRLS `audit_core_runtime`; `src/audit_core/db.py` sets transaction-local validated tenant context; `tests/test_database_security.py` verifies runtime-role properties, same-Tenant visibility, cross-Tenant invisibility and cross-Tenant INSERT rejection; GitHub Actions run `31878786632` passed fresh migrations and all security tests | None |
| B-03 | Verify no-delete DB privileges | COMPLETE | Runtime role grants omit and explicitly revoke DELETE; `tests/test_database_security.py` verifies `has_table_privilege(..., 'DELETE') = false` and a runtime-role DELETE attempt fails with permission denied; GitHub Actions run `31878786632` passed | None |
| B-04 | Verify master immutability | COMPLETE | `tests/test_database_security.py` publishes a project-policy master version and verifies a subsequent content mutation is rejected by the VAC-DB-002 immutability trigger; GitHub Actions run `31878786632` passed | None |
| C-01 | Implement Security JWT verification | COMPLETE | `src/audit_core/security.py` implements JWKS-based JWT validation and immutable principal context using approved `tenant_id` and `permissions[]` claims; `tests/test_security.py` covers valid token plus invalid issuer, audience, signature and expiry; implementation landed through commits `a154913ac21db6097abc6f399f5bd9b2c04939a8`, `793ed669a5f2af38d560e90cad1f0038373953f9`, `ea8113874473e9b10632d492f5724d1ef246f836` and lint correction `d480abe613008e6d7b2719f3557f70d6aefd72dd`; GitHub Actions run `31879035125` passed build, lint, fresh DB migration and tests | Current `verigence-security` repository contains no implementation contract beyond README; validator follows the approved Audit Core Security JWT/JWKS contract and accepts issuer/audience/JWKS endpoint as configuration inputs |
| C-02 | Enforce Tenant and permission checks | NOT STARTED | — | Depends C-01 |
| C-03 | Implement common error handling | NOT STARTED | — | Must map VAC-ERR-001 |
| C-04 | Implement correlation and safe structured logging | NOT STARTED | — | No sensitive payload logging |
| D-01 | Implement Project projection | NOT STARTED | — | One Tenant = one Project |
| D-02 | Implement Dealer and Outlet APIs | NOT STARTED | — | No DELETE route |
| D-03 | Implement dealership staff references | NOT STARTED | — | Dealer staff are reference participants |
| D-04 | Implement Verigence business assignments | NOT STARTED | — | Dealer/Outlet coverage, not competing identity/RBAC |
| E-01 | Implement product catalogue | NOT STARTED | — | OEM/Model/Variant/Colour/SKU |
| E-02 | Implement Price List version lifecycle | NOT STARTED | — | Published immutable |
| E-03 | Implement Discount Scheme version lifecycle | NOT STARTED | — | Do not invent unresolved formulas |
| E-04 | Implement document/control/policy version lifecycles | NOT STARTED | — | Published immutable |
| F-01 | Implement Customer APIs | NOT STARTED | — | Outlet-scoped business entity |
| F-02 | Implement protected customer matching | NOT STARTED | — | Cross-Dealer/Outlet match without raw-ID logging |
| F-03 | Implement Journey APIs | NOT STARTED | — | Journey is audit correlation, not dealer workflow control |
| G-01 | Verify Audit Core→DI authentication mechanism | NOT STARTED | — | Explicit Security/service-auth dependency; no bypass |
| G-02 | Implement DI anti-corruption client | NOT STARTED | — | DI internal only |
| G-03 | Implement evidence upload façade | NOT STARTED | — | Client sees Audit Core evidenceId only |
| G-04 | Implement ingestion recovery/idempotency | NOT STARTED | — | Critical partial-failure/replay task |
| G-05 | Implement evidence facts/read façade | NOT STARTED | — | No public DI identifiers/routes |
| H-01 | Implement Booking and product selection | NOT STARTED | — | Booking starts Journey but is not universal root |
| H-02 | Implement commercials and discounts | NOT STARTED | — | Standard vs Actual + provenance |
| H-03 | Implement Payments and Finance | NOT STARTED | — | Audit records exceptions; does not block transaction |
| H-04 | Implement Insurance, VAS and Trade-In | NOT STARTED | — | Open formulas stay open/configured |
| H-05 | Implement Vehicle, Registration and Delivery | NOT STARTED | — | Actual delivery status separate from audit state |
| I-01 | Implement control evaluation framework | NOT STARTED | — | Reproducible version/evidence snapshot |
| I-02 | Implement findings and evidence linkage | NOT STARTED | — | Finding must not mutate dealer business status |
| I-03 | Implement PC submit and TL/PM review | NOT STARTED | — | SEND_BACK is audit work state, not delivery/business state |
| J-01 | Implement workflow/task persistence | NOT STARTED | — | Critical durable-work task |
| J-02 | Make task creation atomic with audit transitions | NOT STARTED | — | Critical no-lost-task invariant |
| J-03 | Implement worker retry and lease recovery | NOT STARTED | — | Critical crash recovery |
| J-04 | Implement task idempotency and dead-letter handling | NOT STARTED | — | Critical duplicate/retry protection |
| K-01 | Implement Daily/EOD records | NOT STARTED | — | Persisted state only |
| K-02 | Implement CRM interactions | NOT STARTED | — | CRM work uses durable tasks |
| K-03 | Implement Escalations | NOT STARTED | — | Escalation does not control dealer process |
| L-01 | Align implementation to OpenAPI | NOT STARTED | — | No undocumented public DI/DELETE/business-control routes |
| L-02 | Add key operational metrics/traces | NOT STARTED | — | Align to Observability baseline; avoid sensitive dimensions |
| L-03 | Run critical end-to-end audit journey | NOT STARTED | — | Audit Core only from client perspective |
| L-04 | Run critical security/reliability suite | NOT STARTED | — | Tenant, Executive no-delete, DI recovery, workflow recovery |

## 5. Completion evidence standard by task type

| Task type | Minimum evidence for COMPLETE |
|---|---|
| Simple API/domain task | Code + meaningful automated test(s) + successful contract/dev execution |
| Database task | Migration applied from fresh DB + relevant integrity/security test |
| Security task | Positive and negative authorization tests |
| DI integration | Real integration/controlled integration test including error translation; no direct client DI path |
| Durable workflow | Persistence plus the specific crash/retry/idempotency/concurrency acceptance test named by the task |
| Master versioning | Publish/retire success plus attempted post-publish mutation failure |
| Release/E2E | Passing CI/dev scenario tied to the stated acceptance flow |

## 6. Decisions/open items that must not be guessed

These do not block unrelated tasks but block any task that requires the actual value/rule:

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

## 7. Update discipline

When implementation work occurs, update only the affected task rows and increment summary. Record the evidence in the same update. Do not bulk-mark an increment complete because one endpoint, migration or test passes.

If new process inputs add scope, add a new tightly defined task or revise an unstarted task. Do not silently expand a task already marked CODE COMPLETE/VERIFIED/COMPLETE.
