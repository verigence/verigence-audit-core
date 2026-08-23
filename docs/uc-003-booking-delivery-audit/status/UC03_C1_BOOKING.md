# UC03 C1 — Booking Audit — Checkpoint Status

**Checkpoint:** `C1 — Booking Audit`  
**Status:** `ENGINEERING + AUTOMATED + DEV VALIDATION COMPLETE / HUMAN UAT DEFERRED`  
**Date:** 2026-08-23  
**Branch:** `planning/uc-003-booking-delivery-audit`  
**Formal human-UAT closure:** **NOT CLOSED**  
**C2 engineering readiness:** **AUTHORIZED**

---

## 1. Sequencing decision

The human owner directed UC03 engineering to be completed checkpoint-by-checkpoint (`C0`, `C1`, `C2`) on the unified planning branch, while human DEV/UAT is consolidated at the end of the UC03 cycle.

Accordingly, C0 and C1 human UAT remain **DEFERRED / PENDING** and no human pass is claimed here. C1 engineering, automated validation and branch-safe DEV validation are complete, so C2 engineering may proceed. Final Phase-1 promotion still requires the consolidated human DEV/UAT cycle.

---

## 2. C1 functional scope completed

### Audit Core

Implemented:

- Booking Start / Booking In Progress;
- normal `close-ready`, Close No Delivery, Cancel and Mark Duplicate;
- idempotency and optimistic `If-Match` aggregate-version handling;
- immutable Booking workflow events and versioned Booking document requirement snapshots;
- `YES | NO | NA | UNANSWERED` document assessments;
- dynamic conditional-document applicability;
- typed PC capture into existing Customer / Booking / Registration / Trade-In domains;
- `EXCHANGE_TAKEN` as typed Trade-In detail (`details.exchangeTaken`), not an invented business-status code;
- human-token Booking evidence upload through Audit Core to DI;
- progressive document processing state;
- extraction proposals with source evidence, canonical fact, fact version, confidence and value-source provenance;
- proposal accept/correct preserving the immutable machine original;
- PC human audit flags plus the mandatory HIGH duplicate finding and append-only finding history;
- explicit completion-blocking semantics so an ordinary open flag does not automatically stop dealer operations;
- Booking completion/checkpoint calculation and aggregate Web/Android workspace;
- strict UC03 extraction refresh consuming only DI mappings reconciled as SUPPORTED.

C1 deliberately does **not** create a generic 123-field business-value store. Accepted/corrected values land in existing typed business domains; proposal records preserve machine provenance.

### DI publication boundary

- `booking_form`: `customer_name`, `customer_phone`, `vehicle_model`, `vehicle_variant`, `vehicle_color`;
- `pan_card`: `pan_number`, `pan_name`;
- equivalent supported aliases consumed by Audit Core remain explicit rather than inferred;
- PROVISIONAL/TBD mappings remain unpublished/disabled;
- machine value/confidence/source/fact-version envelope is preserved.

No Aadhaar extraction/raw-retention assumption is introduced. No source-precedence rule is inferred from processing order.

### Web / Android

Implemented:

- Start Booking / Open Booking from the Project work list;
- dedicated `/bookings/:journeyId` operational workspace;
- evidence-first upload while PC capture continues concurrently;
- progressive processing state and extraction proposals;
- bulk clean-proposal acceptance and individual accept/correct;
- dynamic Exchange-driven document checklist;
- PC audit flags, completion blockers and all Booking conclusions;
- responsive phone/tablet/desktop layout;
- background/focus/reconnect refresh;
- stable upload idempotency key derived from Booking, requirement and file fingerprint so resume/focus does not blindly resubmit evidence.

---

## 3. Database and canonical API contract

C1 migration: `0011_uc03_booking_capture`. `0012` remains available for Delivery/C2.

The canonical C1 contract is frozen in `api/openapi-v1.yaml` and covers Booking start/conclusions, typed capture, proposal accept/correct, extraction refresh, processing status, flags, workspace, Booking evidence upload and versioned Booking document assessments.

OpenAPI freeze run `32615962320`: **SUCCESS**. The one-shot helper workflow/script removed themselves after committing the contract.

---

## 4. Automated validation evidence

### Audit Core

Clean application/cleanup head before the status-only commits: `187b76bd469b6fe99c2d257a7b0476cc34c9f836`.

CI run `32616313311` (run 602): **SUCCESS**.

- package build: **PASS**;
- Ruff: **PASS**;
- fresh Alembic migration through `0011_uc03_booking_capture`: **PASS**;
- pytest: **156 passed, 1 non-failing deprecation warning**.

Temporary C1 formatter, contract-freeze and DEV-validation workflows are not retained in the clean branch.

### DI

Clean head: `d6d851b94c388038e0cfbcab949248776c63d8ff`.

Final clean CI run `32616320456` (run 199): **SUCCESS** for backend lint, typecheck and tests. Frontend placeholder check succeeded/skipped as designed.

Detailed publication-boundary CI run `32615064040`: **SUCCESS** with **216 passed, 39 skipped, 1 warning**, including the UC03 Booking profile tests.

Temporary DI DEV-validation workflow is not retained.

### Web

Clean head: `feda6325eb9d37f15b4c73e95c328b171c9c35f8`.

Web CI run `32616317723` (run 294): **SUCCESS** — TypeScript typecheck and production build passed.

Temporary Web DEV-validation workflow is not retained.

### Android — final clean branch

Permanent workflow: `UC03 Android Validation`.

Final clean run `32616317719` (run 7), head `feda6325eb9d37f15b4c73e95c328b171c9c35f8`: **SUCCESS**.

- native Web build/typecheck: **PASS**;
- Capacitor Android generation/sync: **PASS**;
- native manifest/plugin configuration verification: **PASS**;
- Gradle `lintDebug` + `assembleDebug`: **PASS** (`BUILD SUCCESSFUL`, 357 actionable tasks);
- APK verification: **PASS**;
- APK SHA-256: `cd6c9c880e25f20ca0f68590abceb2500174aa13ebeb14c6090ca94d82fde0b7`;
- artifact name: `verigence-uc03-c1-android-debug`;
- artifact ID: `9487081496`;
- artifact size: `7,950,181` bytes;
- uploaded artifact ZIP digest: `sha256:e22e04066428540d774ac59794a214f545e5b4084edf66d72464742d10df8857`;
- retention through 2026-09-06.

---

## 5. Branch-safe DEV deployment evidence

No planning branch was merged into `dev` to obtain this evidence.

### Audit Core Railway DEV

Deployed C1 runtime SHA: `647e7a95752bb65f90e9acbc9db4466dd05281b4`  
Validation/deployment run: `32616175038`  
Railway deployment: `d87ecb55-2721-4238-8711-fb84615f5a48` — **SUCCESS**.

Evidence: exact C1 runtime route contract, approved Railway DEV target with `APP_ENV=dev`, public health, Start/Close Ready/extraction-refresh/workspace route probes, and approved Web-origin CORS all **PASS**.

### DI Railway DEV

Deployed C1 runtime SHA: `d0f12e8a695cabfd29eca6348ff0ab56ea3fdb7a`  
Validation/deployment run: `32616135187`.

- `di-api` deployment `2289fba7-501d-41f5-95f2-484d9f5a12b6`: **SUCCESS**;
- `di-worker` deployment `b14df829-f932-4d18-829d-005e8797db8d`: **SUCCESS**;
- strict publication-boundary precheck: **PASS**;
- persisted API/worker config parity: **PASS**;
- worker startup + EOD scheduler topology: **PASS**;
- `https://di-api-dev.up.railway.app/health`: **PASS**;
- `https://di-api-dev.up.railway.app/ready`: **PASS**.

### Web Cloudflare DEV

Deployed C1 runtime SHA: `af4497087b98b5d0fbd28287b47d55240dde4156`  
Validation/deployment run: `32616238344`  
Worker: `verigence-web-dev`  
Cloudflare version: `1b4ea31c-667b-453d-be61-ec36ac95350a`.

Evidence:

- exact C1 source contract and production build: **PASS**;
- generated Booking chunk: `BookingWorkspacePage-AvkKakj1.js`;
- deployed lazy Booking chunk contains `uc03-workspace`: **PASS**;
- deployed lazy Booking chunk contains `booking/extraction/refresh`: **PASS**;
- Security reverse-proxy smoke: **PASS**.

---

## 6. Human UAT — DEFERRED / PENDING

No human C1 UAT pass is claimed. The consolidated end-of-UC03 DEV/UAT cycle must exercise at minimum:

- Start Booking and continue capture while evidence processes;
- progressive proposals, accept and correction with preserved machine provenance;
- Exchange Taken YES/NO dynamic applicability;
- duplicate conclusion and HIGH duplicate finding;
- ordinary human flag without unintended business blocking;
- normal `close-ready`, No Delivery and Cancel flows;
- safe extraction failure/retry;
- Android background/resume/reconnect without duplicate processing;
- phone/tablet/desktop usability.

---

## 7. Checkpoint readiness

Audit Core clean CI: **PASS**  
DI clean CI: **PASS**  
Web clean CI: **PASS**  
Final clean Android APK/artifact: **PASS**  
Audit Core DEV deployment/smoke: **PASS**  
DI API + worker DEV deployment/smoke: **PASS**  
Web DEV deployment/lazy-chunk smoke: **PASS**  
Canonical C1 OpenAPI: **PASS**  
Human UAT: **DEFERRED / PENDING**  

**C1 engineering, automated validation and branch-safe DEV validation are complete. C2 engineering is authorized to proceed on the unified planning branch. C1 formal human-UAT closure remains pending until the consolidated end-of-UC03 DEV/UAT cycle.**
