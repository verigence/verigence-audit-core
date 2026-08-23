# UC03 C1 — Booking Audit — Checkpoint Status

**Checkpoint:** `C1 — Booking Audit`  
**Status:** `ENGINEERING + AUTOMATED + DEV VALIDATION COMPLETE / FINAL CLEAN ANDROID ARTIFACT RUN IN PROGRESS / HUMAN UAT DEFERRED`  
**Date:** 2026-08-23  
**Branch:** `planning/uc-003-booking-delivery-audit`  
**Formal human-UAT closure:** **NOT CLOSED**  
**C2 engineering readiness:** **AUTHORIZED after final clean Android artifact evidence is recorded**

---

## 1. Sequencing decision

The human owner directed UC03 engineering to be completed checkpoint-by-checkpoint (`C0`, `C1`, `C2`) on the unified planning branch, while human DEV/UAT is consolidated at the end of the UC03 cycle.

Therefore:

- C0 human UAT remains deferred/pending and has **not** been passed;
- C1 human UAT also remains deferred/pending and no human pass is claimed here;
- engineering, automated validation and branch-safe DEV validation may complete independently of the deferred human-UAT checklist;
- final Phase-1 promotion still requires the consolidated human DEV/UAT cycle.

---

## 2. C1 functional scope completed

### Audit Core

Implemented:

- Booking Start / Booking In Progress;
- normal `close-ready` completion;
- Close No Delivery with configured reason and remarks;
- Cancel with configured reason and remarks;
- Mark Duplicate with mandatory HIGH duplicate finding and append-only finding history;
- idempotency and optimistic `If-Match` aggregate-version handling;
- immutable Booking workflow events;
- versioned Booking document requirement snapshots;
- `YES | NO | NA | UNANSWERED` document assessments;
- dynamic conditional-document applicability;
- typed PC capture into existing Customer / Booking / Registration / Trade-In business domains;
- `EXCHANGE_TAKEN` persisted as typed Trade-In detail (`details.exchangeTaken`) rather than an invented business-status code;
- human-token Booking evidence upload through Audit Core to DI;
- progressive document processing state;
- extraction proposals with source evidence, source canonical fact, source fact version, confidence and value source;
- proposal accept/correct while preserving the immutable machine original as provenance;
- human PC audit flags;
- explicit completion-blocking semantics so an ordinary open flag does not automatically stop dealer operations;
- Booking completion/checkpoint calculation;
- aggregate Booking Web/Android workspace read model;
- strict UC03 extraction refresh consuming only DI mappings reconciled as SUPPORTED.

C1 deliberately does **not** introduce a generic 123-field business-value store. Accepted/corrected values land in the existing typed business domains; proposal rows preserve machine provenance.

### DI

C1 publication boundary:

- `booking_form`: `customer_name`, `customer_phone`, `vehicle_model`, `vehicle_variant`, `vehicle_color`;
- `pan_card`: `pan_number`, `pan_name`;
- equivalent supported aliases consumed by Audit Core remain explicit rather than inferred;
- PROVISIONAL/TBD mappings remain unpublished/disabled;
- machine value/confidence/source/fact-version envelope remains intact for Audit Core proposal provenance.

No Aadhaar extraction or raw-retention assumption is introduced. No source-precedence rule is inferred from document-processing order.

### Web / Android

Implemented:

- work-list actions for Start Booking / Open Booking;
- dedicated `/bookings/:journeyId` operational route;
- evidence-first Booking workspace;
- document upload and PC capture while extraction processes concurrently;
- progressive document processing and proposal refresh;
- bulk clean-proposal acceptance plus individual accept/correct;
- dynamic Exchange-driven document checklist;
- PC human audit flags;
- completion blockers and all Booking conclusions;
- responsive phone/tablet/desktop layout;
- background/focus/reconnect refresh;
- stable upload idempotency key derived from Booking, requirement and file fingerprint so resume/focus does not blindly resubmit the document.

---

## 3. Database and API contract

C1 migration: `0011_uc03_booking_capture`.

`0012` remains available for Delivery/C2.

Final clean Audit Core CI applies a fresh Postgres/Alembic chain through `0011_uc03_booking_capture` before tests.

The canonical C1 API contract is frozen in `api/openapi-v1.yaml` and includes:

```text
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/booking/start
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/booking/close-ready
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/booking/close-no-delivery
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/booking/cancel
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/booking/mark-duplicate
PUT  /v1/tenants/{tenant_id}/journeys/{journey_id}/capture/{field_key}
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/extraction-proposals/{proposal_id}/accept
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/extraction-proposals/{proposal_id}/correct
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/booking/extraction/refresh
GET  /v1/tenants/{tenant_id}/journeys/{journey_id}/processing-status
GET  /v1/tenants/{tenant_id}/journeys/{journey_id}/flags
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/flags
GET  /v1/tenants/{tenant_id}/journeys/{journey_id}/uc03-workspace
POST /v1/tenants/{tenant_id}/journeys/{journey_id}/stages/BOOKING/documents/{requirement_key}/evidence
GET  /v1/tenants/{tenant_id}/journeys/{journey_id}/stages/BOOKING/documents
PUT  /v1/tenants/{tenant_id}/journeys/{journey_id}/stages/BOOKING/documents/{requirement_key}
```

C1 OpenAPI freeze run `32615962320`: **SUCCESS**. The one-shot contract helper workflow/script removed themselves after the contract commit.

---

## 4. Automated validation evidence

### Audit Core — clean final engineering head before this status record

Application/cleanup head: `187b76bd469b6fe99c2d257a7b0476cc34c9f836`.

CI run `32616313311` (run 602): **SUCCESS**.

- package build: **PASS**;
- Ruff: **PASS**;
- fresh Alembic migration through `0011_uc03_booking_capture`: **PASS**;
- pytest: **156 passed, 1 non-failing deprecation warning**.

Temporary C1 formatter, contract-freeze and DEV-validation workflows are not retained in the clean Audit Core branch.

### DI — clean final C1 branch

Clean head: `d6d851b94c388038e0cfbcab949248776c63d8ff`.

Final CI run `32616320456` (run 199): **SUCCESS** for backend lint, typecheck and tests. Frontend placeholder check succeeded/skipped as designed.

Detailed C1 publication-boundary run `32615064040`: **SUCCESS** with **216 passed, 39 skipped, 1 warning**, including the UC03 Booking profile tests.

Temporary DI DEV-validation workflow is not retained in the clean branch.

### Web — clean final C1 branch

Clean head: `feda6325eb9d37f15b4c73e95c328b171c9c35f8`.

Web CI run `32616317723` (run 294): **SUCCESS**.

- TypeScript typecheck: **PASS**;
- production build: **PASS**.

Temporary Web DEV-validation workflow is not retained in the clean branch.

### Android — final clean branch validation

Permanent workflow: `UC03 Android Validation`.

Final-clean run: `32616317719` (run 7), branch head `feda6325eb9d37f15b4c73e95c328b171c9c35f8`.

At the time this status record was first written, the run had passed native Web build, Capacitor generation/sync and native configuration verification and was executing Gradle `lintDebug + assembleDebug`. Final APK verification/artifact evidence must be appended before C1 engineering is marked fully complete.

---

## 5. Branch-safe DEV deployment evidence

No planning branch was merged into `dev` to obtain this evidence.

### Audit Core Railway DEV

Deployed C1 runtime SHA: `647e7a95752bb65f90e9acbc9db4466dd05281b4`.

Validation/deployment run: `32616175038`.

Railway deployment ID: `d87ecb55-2721-4238-8711-fb84615f5a48` — **SUCCESS**.

Evidence:

- exact C1 runtime route contract: **PASS**;
- approved Railway DEV target and persisted `APP_ENV=dev`: **PASS**;
- public `/health`: **PASS**;
- C1 Start / Close Ready / extraction-refresh / workspace route probes: **PASS**;
- approved Web DEV-origin CORS preflight including Authorization, Content-Type, Idempotency-Key, If-Match and trace/correlation headers: **PASS**.

### DI Railway DEV

Deployed C1 runtime SHA: `d0f12e8a695cabfd29eca6348ff0ab56ea3fdb7a`.

Validation/deployment run: `32616135187`.

- `di-api` deployment `2289fba7-501d-41f5-95f2-484d9f5a12b6`: **SUCCESS**;
- `di-worker` deployment `b14df829-f932-4d18-829d-005e8797db8d`: **SUCCESS**;
- strict C1 publication boundary precheck: **PASS**;
- persisted API/worker config parity: **PASS**;
- worker startup and EOD scheduler topology: **PASS**;
- public `https://di-api-dev.up.railway.app/health`: **PASS**;
- public `https://di-api-dev.up.railway.app/ready`: **PASS**.

### Web Cloudflare DEV

Deployed C1 runtime SHA: `af4497087b98b5d0fbd28287b47d55240dde4156`.

Validation/deployment run: `32616238344`.

Worker: `verigence-web-dev`  
Cloudflare version: `1b4ea31c-667b-453d-be61-ec36ac95350a`.

Evidence:

- exact C1 source contract: **PASS**;
- TypeScript + production build: **PASS**;
- generated Booking chunk: `BookingWorkspacePage-AvkKakj1.js`;
- deployed lazy Booking chunk contains `uc03-workspace`: **PASS**;
- deployed lazy Booking chunk contains `booking/extraction/refresh`: **PASS**;
- Security reverse-proxy smoke: **PASS**.

---

## 6. Human UAT — DEFERRED / PENDING

No human C1 UAT pass is claimed.

The consolidated end-of-UC03 DEV/UAT cycle must exercise at minimum:

- Start Booking from Project work list;
- continue PC capture while uploaded evidence is still processing;
- progressive extraction proposals as documents complete;
- accept a machine proposal;
- correct a machine proposal and verify original machine provenance remains visible/auditable;
- Exchange Taken = YES and NO dynamic document applicability;
- duplicate Booking conclusion and HIGH duplicate flag;
- ordinary human audit flag without unintended dealer-operation blocking;
- normal `close-ready` completion;
- Close No Delivery and Cancel reason flows;
- safe extraction failure/retry behavior;
- Android background/resume and reconnect without duplicate evidence processing;
- phone/tablet/desktop usability.

---

## 7. Current readiness

Audit Core clean CI: **PASS**  
DI clean CI: **PASS**  
Web clean CI: **PASS**  
Audit Core DEV deployment/smoke: **PASS**  
DI API + worker DEV deployment/smoke: **PASS**  
Web DEV deployment/lazy-chunk smoke: **PASS**  
Canonical C1 OpenAPI: **PASS**  
Final clean Android APK artifact: **IN PROGRESS**  
Human UAT: **DEFERRED / PENDING**  

Until the final clean Android artifact run is complete, C1 engineering is not marked fully closed in this record. Human UAT remains intentionally deferred even after that artifact evidence is appended.
