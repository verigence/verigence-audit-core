# Verigence Platform Architecture & Code Review Remediation Register

**Scope:** `verigence-security`, `verigence-di`, `verigence-audit-core`  
**Owner:** Platform architecture / engineering  
**Purpose:** Single source of truth for review observations, remediation decisions, implementation tracking, and validation evidence across the three modules.  
**Implementation policy:** Items in this register are observations or candidates only until explicitly selected for implementation. This document must not be treated as approval to change application code.

---

## 1. How to use this register

Each observation gets a stable ID. When an item is selected for implementation, update the same row with the implementation PR/commit and validation evidence rather than creating a separate tracker.

### Priority

- **P0** — security/data integrity/availability issue requiring immediate action
- **P1** — significant performance, reliability, security-hardening, or architectural issue
- **P2** — important maintainability, contract, resilience, or operational improvement
- **P3** — optimization / improvement with limited immediate risk
- **Preserve** — reviewed design that should not be changed without a new reason

### Status

- **OPEN** — observation captured; no implementation decision yet
- **REVIEW** — design/solution options being evaluated
- **APPROVED** — remediation explicitly approved for implementation
- **IN PROGRESS** — implementation underway
- **VALIDATING** — implementation complete; evidence still required
- **CLOSED** — implementation validated and accepted
- **NO CHANGE** — design reviewed and intentionally retained

---

## 2. Open remediation register

| ID | Module | Priority | Status | Observation | Risk / Why it matters | Proposed direction | Implementation | Validation |
|---|---|---:|---|---|---|---|---|---|
| **AC-SEC-001** | Audit Core / Security integration | **P1** | **OPEN** | `dependencies.py` obtains the Bearer JWT and creates/uses `SecurityTokenValidator`; review indicates the validator is not application-scoped and a new `PyJWKClient` may be created per request. | Repeated JWKS-client construction can add unnecessary latency and dependency pressure to every authenticated API request and may prevent effective JWKS caching/reuse. | Verify the actual object lifecycle first. If confirmed, use a single application-scoped `SecurityTokenValidator` / `PyJWKClient` with normal key refresh on `kid` changes. Do not change authorization semantics. | — | — |
| **AC-DB-001** | Audit Core / Database | **P1** | **OPEN** | `db.py` executes `SET LOCAL ROLE audit_core_runtime` on every DB transaction/connection. | Correct runtime-role configuration is an environment/deployment dependency. A missing or incomplete role membership can make otherwise healthy application paths fail at runtime. | Keep the role-switching security model, but make role creation/membership/`SET ROLE` capability an explicit migration/deployment contract and validate it during environment deployment. | — | — |
| **PLAT-AUTH-001** | Security ↔ Audit Core ↔ DI | **P1** | **OPEN** | Audit Core's delegated DI integration requires the system-wide permissions `di.subject.create`, `di.document.upload`, `di.document.read`, and `di.document.fields.read`. | If the confidential client/delegation allow-list is incomplete, normal evidence ingestion or extraction read-back fails even when user permissions and DI are healthy. | Treat these four permissions as the canonical Audit Core → DI delegated integration contract. Capture them in permanent configuration/tests rather than relying only on environment-specific runtime settings. | — | — |
| **PLAT-CONTRACT-001** | Audit Core ↔ DI | **P2** | **OPEN** | Audit Core's `di_client.py` is an HTTP contract boundary to DI. Recent integration testing exposed differences between older flat responses and the newer DI `ApiResponse` envelope / slim document representation. | Undocumented response-shape drift can surface as `DI_CONTRACT_ERROR` even when DI processing is successful. | Define/version the Audit Core ↔ DI API contract and maintain compatibility tests at the integration boundary. Prefer fixing/adapting the client contract layer rather than changing DI extraction logic to satisfy Audit Core. | — | — |
| **PLAT-OBS-001** | Cross-module | **P2** | **OPEN** | Correlation IDs are extracted/propagated in Audit Core, but the end-to-end observability contract across Security → Audit Core → DI → worker is not yet recorded in this register. | Difficult root-cause analysis for latency/failure across asynchronous and delegated calls. | Define the mandatory correlation/trace fields that must propagate across Security token exchange, DI API calls, worker processing, and audit events. | — | — |
| **AC-OAUTH-001** | Audit Core / Security integration | **P2** | **REVIEW** | `SecurityOAuthClient` performs confidential-client OAuth calls to Security for client credentials/token exchange. | Token-exchange behavior sits on a hot integration path and may be sensitive to unnecessary repeated token acquisition, expiry handling, and dependency outages. | Review current token reuse/caching, expiry handling, retry/backoff, and failure semantics before deciding whether any change is needed. | — | — |

---

## 3. Reviewed design to preserve / no change

These observations are recorded so future remediation work does not accidentally redesign components that were reviewed positively.

| ID | Module | Priority | Status | Reviewed design | Decision |
|---|---|---:|---|---|---|
| **AC-AUTH-001** | Audit Core | **Preserve** | **NO CHANGE** | `authorization.py`: `require_tenant()` enforces `principal.tenant_id == tenant_id`; `require_permission()` checks `principal.permissions`. | Keep the authorization layer small and centralized. Avoid duplicating permission logic in business endpoints without a specific need. |
| **AC-JWT-001** | Audit Core / Security | **Preserve** | **NO CHANGE** | `security.py`: JWT verification requires `exp`, `iss`, `aud`, `sub`, `tenant_id`, and `permissions` and validates using Security JWKS. | Preserve claim validation semantics. Performance remediation, if approved, should change validator lifecycle/caching only. |
| **AC-AUDIT-001** | Audit Core | **Preserve** | **NO CHANGE** | `audit_events.py`: sequence number + SHA-256 hash chain over canonical JSON; `SELECT FOR UPDATE` on the chain head; outbox table. | Preserve tamper-evident audit-chain and outbox design. Any future change requires dedicated integrity testing. |
| **AC-WF-001** | Audit Core | **Preserve** | **NO CHANGE** | `workflow.py`: lease-based worker claiming, heartbeat, retry scheduling, stale lease recovery, `FOR UPDATE SKIP LOCKED`. | Retain the existing workflow engine. Do not introduce another workflow/orchestration product unless a concrete requirement cannot be met by this design. |
| **AC-EVID-001** | Audit Core | **Preserve** | **NO CHANGE** | `evidence.py`: idempotent evidence ingestion and saga/state-machine lifecycle (`RECEIVED` → `DI_SUBMITTING` → `DI_ACCEPTED` → `LINKED`) with Security token exchange and DI integration. | Extend this lifecycle when new evidence states are required; do not replace it with ad-hoc upload orchestration. |
| **AC-OBS-001** | Audit Core | **Preserve** | **NO CHANGE** | `observability.py`: correlation ID extraction/propagation. | Retain and extend consistently across module boundaries. |
| **AC-API-001** | Audit Core | **Preserve** | **NO CHANGE** | `main.py`: production disables OpenAPI/docs/redoc; 18 routers registered. | Keep production API-documentation exposure policy unless an explicit operational requirement changes it. |
| **AC-CONTRACT-001** | Audit Core | **Preserve** | **NO CHANGE** | `contract_guards.py`: response/contract guard installation. | Preserve contract enforcement; add compatibility/version tests around external module boundaries rather than bypassing guards. |

---

## 4. Module sections

### 4.1 Security

Current cross-module review items affecting Security:

- **AC-SEC-001** — JWKS validator/client lifecycle from Audit Core.
- **PLAT-AUTH-001** — canonical delegated DI permission set for the `audit-core` confidential client.
- **AC-OAUTH-001** — token exchange/client-credential lifecycle and resilience review.

Add Security-specific code-review findings here when available. Do not infer issues that were not observed by review or testing.

### 4.2 Document Intelligence (DI)

Current cross-module review items affecting DI:

- **PLAT-CONTRACT-001** — Audit Core ↔ DI response contract/version compatibility.
- **PLAT-OBS-001** — end-to-end correlation/trace propagation into DI worker processing.

**Important operating assumption:** DI document extraction/business logic should not be changed merely because an upstream E2E test fails. Deployment/configuration parity, environment variables, storage/database/JWKS configuration, and API-contract compatibility must be ruled out first.

Add DI-specific code-review findings here when available.

### 4.3 Audit Core

Current Audit Core findings are captured in **AC-SEC-001**, **AC-DB-001**, **AC-OAUTH-001**, and the Preserve/No Change section above.

---

## 5. Cross-module architectural boundaries

The following boundaries are the default ownership model when evaluating future remediation work:

- **Business evidence lifecycle** → Audit Core
- **Document intelligence lifecycle** → DI
- **Identity / authentication / authorization** → Security
- **Binary persistence** → object storage / storage subsystem

A remediation should not move ownership across these boundaries unless that architectural change is explicitly reviewed and approved.

---

## 6. Implementation selection checklist

Before changing code for any row in this register:

1. Confirm the observation against the current target branch/runtime.
2. Identify whether the issue is code, configuration, migration, deployment, or API-contract related.
3. Record the proposed remediation in this document.
4. Obtain explicit implementation approval.
5. Implement on the owning module only unless a cross-module contract genuinely requires coordinated changes.
6. Add regression/contract tests before deployment where practical.
7. Validate the exact failure mode that motivated the change.
8. Update the row with PR/commit, validation evidence, and final status.

---

## 7. Change log

| Date | Change |
|---|---|
| 2026-08-17 | Initial cross-module register created from the Security/DI/Audit Core architecture and code-review observations. No application-code remediation implemented as part of this document change. |
