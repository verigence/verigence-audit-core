# UC03 C0 — Shared Foundation / Project Context — Checkpoint Status

**Checkpoint:** `C0 — Shared Foundation / Project Context`  
**Status:** `AUTOMATED + DEV DEPLOYMENT EVIDENCE COMPLETE / HUMAN UAT PENDING`  
**Date:** 2026-08-23  
**Branch:** `planning/uc-003-booking-delivery-audit`  
**Formal checkpoint closure:** **NOT CLOSED**  
**C1 readiness:** **BLOCKED pending human UAT**

---

## 1. SHA convention

The validated checkpoint heads below were captured immediately before the C0 closure-evidence documentation write.

| Module | Validated C0 application head |
|---|---|
| Audit Core | `71bea92822d3de836faea8eb250dacab81cf4c4c` |
| DI | `c899beb03c5fcbc84ffd41ed832451674b246668` |
| Web/Android | `0cbb5794bee4d494c9ee45229484591233a91818` |

The status/handoff documentation commit necessarily advances the Audit Core branch beyond its validated application head. This does **not** change the C0 runtime implementation. Before C1 starts, inspect the live branch heads and use this note as the tested C0 baseline record.

---

## 2. C0 implementation completed

### Audit Core

Implemented:

- `GET /v1/me/projects` from active Project and `business_assignments` projection, returning operating role and Dealer/Outlet scope per Project;
- identity-only Security v2 human JWT handling for tenant-scoped C0 reads, with synchronous Security authorization before Audit Core business-scope enforcement;
- `GET /v1/tenants/{tenantId}/uc03/landing-metrics`;
- `GET /v1/tenants/{tenantId}/uc03/work-items`;
- `ALL | BOOKING | DELIVERY` filtering;
- maximum page size 10 and cursor/keyset paging;
- Project-timezone date filtering;
- C0 date precedence:
  - Booking: source Booking date -> Booking first-start event date -> Booking record date;
  - Delivery: actual-delivered date -> Delivery first-start event date -> Delivery record date;
- C0 landing metrics:
  - **Bookings In Progress** = `BOOKING_STARTED | BOOKING_IN_PROGRESS`;
  - **Delivery In Progress** = `DELIVERY_STARTED | DELIVERY_IN_PROGRESS`;
  - **Needs Attention** = distinct cases with at least one `OPEN | ACKNOWLEDGED` finding;
  - **Audit Flags** = count of `OPEN | ACKNOWLEDGED` findings;
- `journey_stage_states` projection foundation;
- append-only `journey_workflow_events` foundation;
- C0 OpenAPI and integration/authorization tests.

Deliberate C0 placeholders remain:

- `proposalReadyCount = 0` because extraction proposal persistence is C1 scope;
- `nextActionCode = null` because concrete Booking/Delivery command policy starts in later checkpoints.

### Web/Android

Implemented:

- Project context gate after login;
- no-assignment safe state;
- one-Project automatic selection;
- multiple-Project chooser;
- Project switcher;
- operating role bound to selected Project;
- Project switch clearing operational Project state and tenant-scoped query state;
- Project landing metrics;
- `Latest Bookings & Deliveries` latest-10 list;
- All/Bookings/Deliveries filter;
- date/date-range filter;
- cursor Previous/Next controls;
- mobile-first C0 layout;
- approved Verigence lockup;
- C0 operational route protection so legacy mutation/read flows are not exposed as UC03 Booking/Delivery functionality before C1/C2.

### DI

No C0 runtime change was required.

DI branch head remains `c899beb03c5fcbc84ffd41ed832451674b246668`.

### Security

No UC03 Security branch or Security code change was introduced.

C0 automated Project-discovery and authorization tests exercise the Audit Core `business_assignments` projection and Security-v2 authorization integration. Human UAT with real assigned users is still pending and is not represented as passed.

---

## 3. Material implementation files

### Audit Core

- `api/openapi-v1.yaml`
- `migrations/versions/0010_uc03_c0_foundation.py`
- `src/audit_core/db.py`
- `src/audit_core/main.py`
- `src/audit_core/security_authorization.py`
- `src/audit_core/uc03_authorized_work_items.py`
- `src/audit_core/uc03_project_context.py`
- `src/audit_core/uc03_work_items.py`
- `tests/test_security_authorization.py`
- `tests/test_uc03_landing_metrics.py`
- `tests/test_uc03_project_context.py`
- `tests/test_uc03_work_items.py`

### Web/Android

- `.github/workflows/uc03-c0-native-validation.yml`
- `src/App.tsx`
- `src/components/ProjectContextGate.tsx`
- `src/domain/models.ts`
- `src/features/uc03/projectContext.ts`
- `src/layout/AppShell.tsx`
- `src/main.tsx`
- `src/pages/DashboardPage.tsx`
- `src/pages/LoginPage.tsx`
- `src/services/audit-core/uc03.ts`
- `src/store/projectContextStore.ts`
- `src/store/sessionStore.ts`
- `src/styles/uc03-c0.css`

Temporary branch-safe DEV deployment workflows used to prove C0 were removed after successful deployment so they cannot accidentally redeploy or encode C0-only assumptions during later checkpoints. Their completed Actions runs remain the deployment evidence.

---

## 4. Database migration evidence

Implemented migration:

`0010_uc03_c0_foundation.py`

Final-head Audit Core CI applied a fresh Postgres migration chain through:

`0009_uc02_project_refs -> 0010_uc03_c0_foundation`

and verified the C0 foundation objects before running the full test suite.

No historical backfill result is claimed as part of C0.

---

## 5. API contract

C0 APIs present:

```text
GET /v1/me/projects
GET /v1/tenants/{tenantId}/uc03/landing-metrics
GET /v1/tenants/{tenantId}/uc03/work-items
```

No C1 Booking mutation API is recorded as C0 completion.

---

## 6. Final-head automated evidence

### Audit Core final validated head

SHA: `71bea92822d3de836faea8eb250dacab81cf4c4c`

GitHub Actions:

- workflow: `CI`
- run ID: `32594620868`
- run number: `538`
- result: **SUCCESS**
- package build: **PASS**
- Ruff: **PASS**
- fresh Postgres/Alembic migration through `0010_uc03_c0_foundation`: **PASS**
- pytest: **141 passed, 1 warning in 10.88s**

The warning is a Starlette/httpx deprecation warning and did not fail the suite.

### Web final validated head

SHA: `0cbb5794bee4d494c9ee45229484591233a91818`

GitHub Actions:

- workflow: `Web CI`
- run ID: `32594624429`
- run number: `272`
- result: **SUCCESS**
- TypeScript typecheck: **PASS**
- production build: **PASS**

### Android final validated head

Web/Android SHA: `0cbb5794bee4d494c9ee45229484591233a91818`

GitHub Actions:

- workflow: `UC03 C0 Android Validation`
- run ID: `32594624444`
- run number: `4`
- result: **SUCCESS**
- native DEV Web asset build: **PASS**
- Capacitor Android generation/sync: **PASS**
- native configuration verification: **PASS**
- Gradle `lintDebug`: **PASS**
- Gradle `assembleDebug`: **PASS**
- APK verification: **PASS**
- artifact upload: **PASS**

APK evidence:

- artifact ID: `9481252665`
- artifact name: `verigence-uc03-c0-android-debug`
- APK SHA-256: `0c287c03ce28af417ea5b01f8662215d8f276cc7eb9d0eb0ef477552f6c9ef30`
- artifact ZIP SHA-256: `20900a7c6101041dc21a78c577f2fcc85cc871193fba0ed0d78cbc7d7c84b09b`

---

## 7. DEV deployment evidence

### Audit Core Railway DEV

Exact deployed application SHA:

`ffa334fcd0791a51e9b83221ceafc1603fd05d49`

Validation/deployment workflow:

- run ID: `32594431799`
- result: **SUCCESS**

Railway deployment:

- deployment ID: `3614f0e0-1472-47fd-9af6-a64f795931f8`
- deployment status: **SUCCESS**

Smoke evidence:

- service health: **PASS**
- unauthenticated `/v1/me/projects`: expected HTTP `401`
- unauthenticated `/v1/tenants/uc03-c0-smoke/uc03/landing-metrics`: expected HTTP `401`
- unauthenticated `/v1/tenants/uc03-c0-smoke/uc03/work-items`: expected HTTP `401`
- approved Web DEV-origin CORS preflight: **PASS**

The final validated Audit Core head `71bea928...` is exactly one cleanup commit after the deployed SHA; that commit only removed the temporary C0 DEV validation/deployment workflow. No application/runtime file changed between the deployed SHA and the final validated application head.

### Web Cloudflare DEV

Exact deployed application SHA:

`771d01396caa178d721f615fb1bbd36cae653a4c`

Validation/deployment workflow:

- run ID: `32594494424`
- result: **SUCCESS**

Cloudflare DEV:

- Worker: `verigence-web-dev`
- version ID: `8a5d5ef0-dc46-4e84-87d1-310303c1cfc5`
- DEV endpoint: `https://verigence-web-dev.jbrconsulting-it.workers.dev`

Deployed asset/hash smoke verified:

- `/v1/me/projects` contract marker;
- `/uc03/landing-metrics` contract marker;
- `/uc03/work-items` contract marker;
- `Delivery In Progress` wording;
- approved Verigence logo;
- Security DEV proxy path.

The final validated Web head `0cbb5794...` is exactly one cleanup commit after the deployed SHA; that commit only removed the temporary C0 Web DEV deployment workflow. No application/runtime file changed between the deployed SHA and the final validated application head.

---

## 8. Human UAT — PENDING

**No human UAT pass is claimed.**

The following C0 acceptance behavior still requires human verification against DEV, including representative real Project assignments:

- zero-Project user-safe state;
- one-Project automatic entry to landing;
- multiple-Project chooser;
- a user whose operating role differs by Project;
- Project switching with no stale prior-Project data visible;
- four landing metrics displaying correctly for the selected Project/scope;
- latest list showing at most 10 rows;
- All/Bookings/Deliveries filtering;
- date/date-range behavior in the Project timezone;
- Previous/Next paging;
- `Delivery In Progress` wording;
- approved Verigence logo;
- Android phone, Android tablet and desktop Web usability for the C0 landing/context flow.

Human UAT outcome must be appended to this status note before C0 is marked formally closed.

---

## 9. Known issues and deferred work

### Known C0 defects

No known automated C0 defect remains from the completed CI/deployment evidence above.

### Still pending

- human C0 UAT;
- formal C0 closure/approval.

### Explicitly deferred

- C1 Booking mutations/capture/extraction proposal behavior;
- C2 Delivery mutations/evidence behavior;
- C3 audit/review hardening;
- DI Booking/Delivery runtime profile changes;
- VIN/chassis rule implementation;
- Post-Delivery reconciliation;
- final Phase-1 promotion to `dev`.

---

## 10. Checkpoint readiness

Automated tests/builds: **PASS**  
Fresh migration verification: **PASS**  
Audit Core DEV deployment/smoke: **PASS**  
Web DEV deployment/smoke: **PASS**  
Android debug build/artifact: **PASS**  
Human UAT: **PENDING**  

Therefore:

**C0 is not formally closed. C1 must not start until the pending human UAT is completed and its outcome is recorded.**
