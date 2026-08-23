# UC03 C0 — Shared Foundation / Project Context — Checkpoint Status

**Checkpoint:** `C0 — Shared Foundation / Project Context`  
**Status:** `AUTOMATED + DEV DEPLOYMENT EVIDENCE COMPLETE / HUMAN UAT DEFERRED`  
**Date:** 2026-08-23  
**Branch:** `planning/uc-003-booking-delivery-audit`  
**Formal checkpoint closure:** **NOT CLOSED**  
**C1 engineering readiness:** **AUTHORIZED TO PROCEED under explicit human-owner sequencing direction**

---

## 1. Sequencing decision

C0 human UAT has **not** been passed and no human-UAT pass is claimed in this record.

On **23-Aug-2026**, the human owner explicitly directed that UC03 engineering proceed checkpoint-by-checkpoint on the unified planning branch (`C0`, then `C1`, then `C2`) while the human DEV/UAT execution is consolidated at the end of the UC03 cycle. Therefore:

- C0 remains formally open because human UAT is pending;
- pending C0 human UAT does **not** block C1/C2 engineering under this direction;
- proceeding with C1/C2 does **not** imply that C0 human UAT passed;
- final UC03 DEV/UAT must include the deferred C0 scenarios together with the later checkpoint scenarios before Phase-1 promotion.

This sequencing statement supersedes the earlier C0 note that said C1 could not start before human UAT.

---

## 2. C0 validated baseline

The validated C0 application heads captured before the original closure-evidence documentation write were:

| Module | Validated C0 application head |
|---|---|
| Audit Core | `71bea92822d3de836faea8eb250dacab81cf4c4c` |
| DI | `c899beb03c5fcbc84ffd41ed832451674b246668` |
| Web/Android | `0cbb5794bee4d494c9ee45229484591233a91818` |

Later C1/C2 work advances the unified planning branches; these SHAs remain the historical tested C0 baseline rather than the current branch heads.

---

## 3. C0 implementation completed

### Audit Core

Implemented:

- `GET /v1/me/projects` from active Project and `business_assignments` projection, returning operating role and Dealer/Outlet scope per Project;
- identity-only Security v2 human JWT handling for tenant-scoped C0 reads, with synchronous Security authorization before Audit Core business-scope enforcement;
- `GET /v1/tenants/{tenantId}/uc03/landing-metrics`;
- `GET /v1/tenants/{tenantId}/uc03/work-items`;
- `ALL | BOOKING | DELIVERY` filtering;
- maximum page size 10 and cursor/keyset paging;
- Project-timezone date filtering;
- Booking date precedence: source Booking date -> Booking first-start event date -> Booking record date;
- Delivery date precedence: actual-delivered date -> Delivery first-start event date -> Delivery record date;
- C0 landing metrics for Bookings In Progress, Delivery In Progress, Needs Attention and Audit Flags;
- `journey_stage_states` projection foundation;
- append-only `journey_workflow_events` foundation;
- C0 OpenAPI and integration/authorization tests.

C0 deliberately left `proposalReadyCount = 0` and `nextActionCode = null`; concrete proposal and command behavior belongs to later UC03 checkpoints.

### Web/Android

Implemented:

- Project context gate after login;
- zero-assignment safe state;
- one-Project automatic selection and multiple-Project chooser;
- Project switcher and selected-Project operating role;
- clearing of operational/query state on Project switch;
- Project landing metrics and latest-10 Booking/Delivery work list;
- All/Bookings/Deliveries and date/date-range filters;
- cursor Previous/Next controls;
- mobile-first layout and approved Verigence lockup;
- C0 route protection so legacy mutation/read flows were not represented as UC03 Booking/Delivery functionality before later checkpoints.

### DI / Security

No C0 DI runtime change and no UC03 Security code branch were required. C0 authorization tests exercised the Audit Core business-assignment projection and Security-v2 authorization boundary. Human validation with representative assigned users remains deferred.

---

## 4. Database and contract evidence

C0 migration: `0010_uc03_c0_foundation.py`.

The final C0 Audit Core CI applied a fresh Postgres migration chain through `0009_uc02_project_refs -> 0010_uc03_c0_foundation` before the full test suite.

C0 API contract:

```text
GET /v1/me/projects
GET /v1/tenants/{tenantId}/uc03/landing-metrics
GET /v1/tenants/{tenantId}/uc03/work-items
```

No C1 mutation is claimed as part of the C0 baseline.

---

## 5. C0 automated evidence

### Audit Core

Validated C0 SHA: `71bea92822d3de836faea8eb250dacab81cf4c4c`

- CI run ID `32594620868`, run `538`: **SUCCESS**;
- package build: **PASS**;
- Ruff: **PASS**;
- fresh migration through `0010_uc03_c0_foundation`: **PASS**;
- pytest: **141 passed, 1 warning**.

The warning was a non-failing Starlette/httpx deprecation warning.

### Web

Validated C0 SHA: `0cbb5794bee4d494c9ee45229484591233a91818`

- Web CI run ID `32594624429`, run `272`: **SUCCESS**;
- TypeScript typecheck: **PASS**;
- production build: **PASS**.

### Android

Validated C0 Web/Android SHA: `0cbb5794bee4d494c9ee45229484591233a91818`

- Android validation run ID `32594624444`, run `4`: **SUCCESS**;
- native Web asset build, Capacitor sync, native config, `lintDebug`, `assembleDebug`, APK verification and artifact upload: **PASS**;
- historical C0 artifact ID `9481252665`;
- historical artifact name `verigence-uc03-c0-android-debug`;
- APK SHA-256 `0c287c03ce28af417ea5b01f8662215d8f276cc7eb9d0eb0ef477552f6c9ef30`.

The workflow was later generalized for subsequent UC03 checkpoints; the historical Actions run remains valid C0 evidence.

---

## 6. C0 DEV deployment evidence

### Audit Core Railway DEV

Deployed C0 application SHA: `ffa334fcd0791a51e9b83221ceafc1603fd05d49`

- validation/deployment run ID `32594431799`: **SUCCESS**;
- Railway deployment ID `3614f0e0-1472-47fd-9af6-a64f795931f8`: **SUCCESS**;
- service health: **PASS**;
- expected unauthenticated 401 behavior on C0 protected reads: **PASS**;
- approved Web DEV-origin CORS preflight: **PASS**.

The original final validated C0 Audit Core head was one cleanup-only commit after that deployed SHA.

### Web Cloudflare DEV

Deployed C0 application SHA: `771d01396caa178d721f615fb1bbd36cae653a4c`

- validation/deployment run ID `32594494424`: **SUCCESS**;
- Worker `verigence-web-dev`;
- Cloudflare version ID `8a5d5ef0-dc46-4e84-87d1-310303c1cfc5`;
- C0 deployed contract markers, wording, logo and Security proxy: **PASS**.

The original final validated C0 Web head was one cleanup-only commit after that deployed SHA.

---

## 7. Human UAT — DEFERRED / PENDING

**No human UAT pass is claimed.** Under the 23-Aug-2026 sequencing direction, these scenarios are retained for the consolidated end-of-UC03 DEV/UAT cycle:

- zero-Project safe state;
- one-Project automatic entry;
- multiple-Project chooser;
- a user whose operating role differs by Project;
- Project switching with no stale prior-Project data;
- selected-Project landing metrics and latest-list behavior;
- All/Bookings/Deliveries and date/date-range filtering in Project timezone;
- Previous/Next paging;
- `Delivery In Progress` wording;
- approved Verigence branding;
- Android phone/tablet and desktop Web usability for Project context/landing.

The human outcome must be recorded before C0 is formally closed and before final Phase-1 promotion.

---

## 8. Checkpoint readiness

Automated tests/builds: **PASS**  
Fresh migration verification: **PASS**  
Audit Core DEV deployment/smoke: **PASS**  
Web DEV deployment/smoke: **PASS**  
Android debug build/artifact: **PASS**  
Human UAT: **DEFERRED / PENDING**  
Formal C0 closure: **NOT CLOSED**  
C1 engineering under approved sequencing: **AUTHORIZED**  

Therefore:

**C0 remains formally open because human UAT is pending. Per the explicit 23-Aug-2026 human-owner sequencing direction, C1/C2 engineering may proceed on the unified planning branch while C0 human UAT is deferred to the consolidated end-of-UC03 DEV/UAT cycle. This does not constitute a C0 UAT pass or formal C0 closure.**
