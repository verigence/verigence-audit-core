# Verigence Audit Core — Implementation Plan

**Document ID:** VAC-IMP-001  
**Version:** 1.0  
**Status:** ACTIVE PLANNING BASELINE  
**Date:** 2026-08-15  
**Applies to:** VAC-SD-003 v2.1, VAC-DB-002, VAC-API-001, VAC-ERR-001

## 1. Purpose

This plan turns the approved/candidate Audit Core design package into small implementation increments. It is deliberately lean: each task has one clear outcome, one small acceptance gate, and only the testing needed to protect real product risk.

The plan does **not** treat design documents as implemented code. Implementation status is tracked separately in `docs/AUDIT_CORE_PROGRESS_TRACKER.md`.

## 2. Governing implementation rules

1. **No guessing.** An unresolved business rule remains configurable/open until approved.
2. **Risk-based verification.** High-risk areas such as Tenant isolation, authorization, DI orchestration and workflow durability receive deeper tests; simple CRUD does not receive unnecessary ceremony.
3. **No direct user-to-DI access.** Web/Mobile calls Audit Core only.
4. **Audit does not control dealer operations.** No implementation may block, approve, reject, stop or cancel actual dealer delivery/business activity.
5. **Observed business state is separate from Audit state/outcome.**
6. **No baseline destructive delete.** Public APIs contain no DELETE operations; runtime DB role must not receive destructive business-table access.
7. **Published master versions are immutable.**
8. **Committed workflow tasks cannot be lost.**
9. **Security remains identity/permission authority.** Audit Core does not create a competing identity system.
10. **Completion requires evidence.** A task becomes COMPLETE only after its stated acceptance evidence exists.

## 3. Status model

| Status | Meaning |
|---|---|
| `NOT STARTED` | No implementation work completed. |
| `IN PROGRESS` | Implementation is being developed. |
| `CODE COMPLETE` | Code/configuration exists and local/basic automated checks pass. |
| `VERIFIED` | Stated acceptance scenario has passed in the appropriate test/dev environment. |
| `COMPLETE` | Verified with no known gap against the task definition. |
| `BLOCKED` | Cannot proceed because an explicit dependency is unresolved. |

A task SHALL NOT move directly from `IN PROGRESS` to `COMPLETE`.

## 4. Completion evidence

Evidence should be minimal and concrete, for example:

- commit/file path;
- passing test name/suite;
- migration execution result;
- API contract test result;
- CI run;
- dev-environment verification result.

Screenshots/manual sign-off are required only where automation does not reasonably prove the acceptance condition.

---

## 5. Implementation increments

### P0 — Freeze implementation inputs

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| P0-01 | Approve v2.1 design package | Owner-approved VAC-SD-003, VAC-DB-002, VAC-API-001 and VAC-ERR-001 status | Manifest marks package approved/baselined for implementation | None |
| P0-02 | Confirm runtime/tooling | Recorded application runtime/framework, DB migration tool and test runner | Choice is documented once; repository can be scaffolded without assumption | P0-01 |

### A — Repository and CI foundation

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| A-01 | Scaffold Audit Core service | Minimal runnable service with configuration layout | Service starts locally and exposes only a health endpoint | P0-02 |
| A-02 | Add CI quality gate | Build/lint/unit-test pipeline using selected tooling | Clean commit passes CI | A-01 |
| A-03 | Add environment/config validation | Startup validation for required runtime settings | Missing required setting fails fast with safe error | A-01 |

### B — PostgreSQL foundation

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| B-01 | Convert VAC-DB-002 into migration baseline | Executable initial migration(s) for approved schema | Fresh development DB applies successfully from zero | P0-01, P0-02 |
| B-02 | Enforce Tenant RLS runtime pattern | Tenant session helper + non-owner runtime-role setup | Cross-Tenant DB test is denied and runtime role has no BYPASSRLS | B-01 |
| B-03 | Verify no-delete DB privileges | Runtime grants exclude DELETE on protected business/audit/workflow/master tables | Privilege test proves runtime cannot hard-delete protected rows | B-01 |
| B-04 | Verify master immutability | Tests for published version and published child-row mutation guards | Published version mutation fails; DRAFT mutation succeeds | B-01 |

### C — Security, errors and request context

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| C-01 | Implement Security JWT verification | JWKS-based Security token validator and request principal context | Valid token succeeds; invalid issuer/audience/signature/expiry fails closed | A-01 |
| C-02 | Enforce Tenant and permission checks | Tenant-match and effective-permission middleware/service | Tenant mismatch and missing permission return catalogue errors | C-01 |
| C-03 | Implement common error handling | Typed exceptions + central mapper using VAC-ERR-001 | Representative validation/auth/not-found/conflict/system errors match contract | A-01 |
| C-04 | Implement correlation and safe structured logging | Correlation propagation + structured request/dependency/error logs | Correlation ID appears end-to-end; sensitive-data test/log review shows no prohibited payloads | A-01, C-03 |

### D — Project landscape and assignments

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| D-01 | Implement Project projection | Project GET/PATCH for the Security Tenant | Exactly one Project context exists per Tenant | B-02, C-02 |
| D-02 | Implement Dealer and Outlet APIs | Dealer/Outlet create/read/update/inactivate | Tenant/Dealer hierarchy enforced; no DELETE route | D-01 |
| D-03 | Implement dealership staff references | Outlet-scoped dealership staff records | Staff can be referenced by journey/booking without Security identity | D-02 |
| D-04 | Implement Verigence business assignments | PC/TL/PM/CRM/Executive Dealer/Outlet coverage records | Business-scope authorization test allows assigned scope and denies unassigned scope | D-02, C-02 |

### E — Versioned masters

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| E-01 | Implement product catalogue reads/management | OEM/Model/Variant/Colour/SKU domain APIs required by Audit Core | Product hierarchy can resolve a sellable configuration | B-01, C-02 |
| E-02 | Implement Price List version lifecycle | Draft/create/publish/retire + item management | Published price version is immutable and effective version can be resolved by date | B-04, E-01 |
| E-03 | Implement Discount Scheme version lifecycle | Draft/create/publish/retire + applicability/benefits | Published discount version is immutable; unresolved formulas are not hard-coded | B-04, E-01 |
| E-04 | Implement document/control/policy version lifecycles | Requirement Profile, Audit Control and Project Policy versions | Published versions immutable and Journey can reference exact effective versions | B-04 |

### F — Customer and Journey

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| F-01 | Implement Customer APIs | Outlet-scoped customer create/read/update | Customer belongs to valid Tenant/Dealer/Outlet hierarchy | D-02 |
| F-02 | Implement protected customer matching | Project-wide duplicate/match query using approved protected match keys | Matching works across Dealers/Outlets without logging raw protected identifiers | F-01, C-04 |
| F-03 | Implement Journey APIs | Customer Journey create/read/update with separate audit fields | Journey links correct Project/Dealer/Outlet/Customer and does not expose dealer-control commands | F-01, E-04 |

### G — Internal DI façade

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| G-01 | Verify Audit Core→DI authentication mechanism | Approved internal Security-mediated service-auth path | Audit Core can call DI without user/client direct DI access or private bypass | C-01 |
| G-02 | Implement DI anti-corruption client | Internal DI client for Subject, upload, status, facts and permitted verification calls | Audit domain code does not parse DI wire contracts directly | G-01 |
| G-03 | Implement evidence upload façade | Audit Core multipart upload → DI → Audit Core evidence record | Client receives `evidenceId`; raw binary is not persisted in Audit Core | G-02, F-03 |
| G-04 | Implement ingestion recovery/idempotency | Persisted evidence-ingestion operation with retry/recovery | Replayed upload does not duplicate evidence; DI-accepted/outer-failure scenario is recoverable | G-03 |
| G-05 | Implement evidence facts/read façade | Evidence status/facts refresh and Audit Core fact projection | Public API exposes Audit Core evidence IDs/facts and no DI IDs | G-02, G-03 |

### H — Vehicle-sale Journey process data

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| H-01 | Implement Booking and product selection | Booking + Journey product APIs | Booking can be recorded with SC reference and product snapshot | F-03, E-01 |
| H-02 | Implement commercials and discounts | Standard-vs-Actual commercial lines and discount applications | Values preserve provenance and exact master-version reference | H-01, E-02, E-03 |
| H-03 | Implement Payments and Finance | Payment/verification-event/finance data APIs | Multiple payments supported; Audit Core records findings rather than blocking dealer transaction | F-03 |
| H-04 | Implement Insurance, VAS and Trade-In | Insurance/add-on/trade-in APIs | Process data persists independently under the Journey; open formulas remain unimplemented/configured | F-03 |
| H-05 | Implement Vehicle, Registration and Delivery | Vehicle/registration/delivery APIs + delivery status history | Actual delivery status uses configured business code and is independent of audit state/outcome | F-03 |

### I — Audit controls, findings and review

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| I-01 | Implement control evaluation framework | Version-selected reviewed evaluator execution + stored evaluation snapshot | Evaluation references exact control/master/evidence inputs used | E-04, G-05, H-02 |
| I-02 | Implement findings and evidence linkage | Findings, remarks and finding-evidence APIs | Finding traces to evaluation/evidence and does not mutate actual dealer status | I-01 |
| I-03 | Implement PC submit and TL/PM review | Audit state transitions + immutable review decisions | BREACH/NO_BREACH/SEND_BACK change audit state/outcome only | I-02, C-02 |

### J — Durable Audit workflow

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| J-01 | Implement workflow/task persistence | Workflow instance/task/event repositories and task commands | Task creation/claim/start/complete/cancel persists with immutable history | B-01, C-02 |
| J-02 | Make task creation atomic with audit transitions | Transaction boundary for audit state/review + task + audit event + outbox | Failure before commit leaves neither partial state nor missing resulting task | J-01, I-03 |
| J-03 | Implement worker retry and lease recovery | Retry schedule, lease/heartbeat, stale-task recovery | Simulated worker loss returns eligible task to processing without duplicate effect | J-01 |
| J-04 | Implement task idempotency and dead-letter handling | Effect-key guard + retry exhaustion/dead-letter visibility | Duplicate effect cannot create duplicate active task; exhausted task remains visible | J-03 |

### K — Daily operations, CRM and escalations

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| K-01 | Implement Daily/EOD records | Daily run/items/activity/notepad APIs | PC/TL daily activity can be recorded and completed without hidden in-memory state | D-04, J-01 |
| K-02 | Implement CRM interactions | CRM interaction records + durable CRM tasks | Triggered CRM work is persisted as a task and outcome is retained | J-02 |
| K-03 | Implement Escalations | Journey/project escalation records + durable assignment | Escalation is traceable, assigned and does not alter actual dealer process status | J-02 |

### L — API contract, observability and release verification

| ID | Task | Deliverable | Acceptance | Dependency |
|---|---|---|---|---|
| L-01 | Align implementation to OpenAPI | Implemented routes/schemas match `api/openapi-v1.yaml` | Contract test passes with no undocumented public DELETE or DI route | D–K applicable APIs |
| L-02 | Add key operational metrics/traces | Request, DI dependency, workflow queue/retry and error metrics/traces | Metrics/traces carry correlation and Tenant-safe dimensions without sensitive payloads | C-04, G-02, J-03 |
| L-03 | Run critical end-to-end audit journey | Project→Dealer→Outlet→Customer→Journey→evidence→audit→review | Scenario completes through Audit Core only; actual delivery status and audit outcome remain independent | D–K |
| L-04 | Run critical security/reliability suite | Tenant isolation, permission denial, Executive no-delete, DI retry, workflow recovery | All critical risk tests pass in development CI/environment | B-02, B-03, C-02, G-04, J-04 |

---

## 6. Intentionally deferred until a real requirement exists

These are **not** implementation tasks for the current baseline:

- separate BPM/workflow product;
- message broker solely for architectural purity;
- destructive delete/purge APIs;
- direct Web/Mobile access to DI;
- dealer-user login model;
- unapproved delivery-status values;
- unapproved discount/Short-Excess/PO-DO-refund formulas;
- complex data warehouse/BI platform inside Audit Core;
- speculative microservice decomposition.

## 7. External/open dependencies

The tracker shall mark tasks `BLOCKED` rather than guess when any of these are unresolved at implementation time:

- v2.1 package approval;
- runtime/framework/tooling choice;
- Audit Core→DI service-auth mechanism from Security;
- business values/formulas explicitly listed as open in VAC-SD-003;
- final delivery-status code set where required for real data;
- Observability implementation endpoint/provider details if not yet available.

## 8. Change control

New process inputs may add tasks, but existing task status must not be rewritten to imply work occurred. Material architecture changes first update the requirements/design/API/schema baseline and then adjust this plan and the tracker.