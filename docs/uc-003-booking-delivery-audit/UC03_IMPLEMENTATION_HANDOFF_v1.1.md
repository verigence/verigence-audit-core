# Verigence UC03 — Booking & Delivery Audit — Implementation Handoff

**Document ID:** `VUC03-HO-002`  
**Version:** `1.1`  
**Status:** APPROVED EXECUTION STRUCTURE / C0 AUTOMATED + DEV EVIDENCE COMPLETE / HUMAN UAT PENDING  
**Date:** 2026-08-23  
**Supersedes:** `UC03_IMPLEMENTATION_HANDOFF_v1.0.md`  
**Canonical implementation design:** `UC03_IMPLEMENTATION_DESIGN_v0.1.md`

---

## 1. Execution decision

UC03 will be implemented **sequentially on one existing UC03 branch per touched repository**.

The unified branch is:

```text
planning/uc-003-booking-delivery-audit
```

This branch already contains the approved UC03 planning/design package and now becomes the single UC03 execution branch for continuity.

No second implementation branch will be created.

Specifically, do **not** create:

```text
work/uc-003-booking-delivery-audit
work/uc-003-booking
work/uc-003-delivery
work/uc-003-audit
work/uc-003-foundation
```

or any equivalent sub-feature branch.

The three repositories necessarily retain their own Git branches, but all use the same UC03 branch name and the same sequential checkpoint model:

```text
verigence-audit-core / planning/uc-003-booking-delivery-audit
verigence-di         / planning/uc-003-booking-delivery-audit
verigence-web        / planning/uc-003-booking-delivery-audit
```

Security gets no UC03 branch unless a concrete missing Security capability is proven.

---

# 2. Why the work is still split into checkpoints

The implementation is split for **execution control**, not for Git branching.

```text
SINGLE UC03 BRANCH
       |
       +-- C0 Shared Foundation / Project Context
       |
       +-- C1 Booking
       |
       +-- C2 Delivery
       |
       +-- C3 Audit / Review / Hardening
       |
       +-- Full DEV/UAT
       |
       +-- Promote UC03 to dev
```

A checkpoint is a bounded implementation package with its own acceptance gate.

A later checkpoint must not start until the earlier checkpoint is closed, except for the minimum shared API/schema preparation that is genuinely required by the current checkpoint.

This is the main protection against context loss, scope drift and accidental business assumptions during UC03.

---

# 3. Frozen business invariants

The following are implementation rules, not suggestions.

1. One immutable internal `journey_id` spans Booking and Delivery.
2. PC-facing UI uses Booking and Delivery terminology; it does not say Journey.
3. Verigence records what the dealer actually does. Audit incompleteness/non-compliance may raise flags but may not reject or roll back a real Delivery Start or Delivery Completed event solely for audit reasons.
4. Booking business status, Delivery business status, Audit State and Audit Status remain separate concepts.
5. Per stage Audit State is:

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

6. Per stage Audit Status is:

```text
NOT_EVALUATED
NO_FLAGS
FLAGS_RAISED
```

7. `FLAGS_RAISED` is historical/sticky once a valid stage flag has existed; later resolution does not rewrite history to `NO_FLAGS`.
8. Machine and human anomalies use the same canonical flag/finding register.
9. PC/TL/PM/Executive may raise flags. TL and PM normally review/resolve. Executive has all Phase-1 flag privileges. Permission policy must remain configurable and server enforced.
10. Booking statuses are:

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
BOOKING_CLOSED
BOOKING_CANCELLED
DUPLICATE_BOOKING
```

11. Delivery statuses are exactly:

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
```

There is no Delivery Closed state.
12. Booking may remain `BOOKING_IN_PROGRESS` after Delivery begins. Audit Core accepts Delivery progression and raises the configured incomplete-Booking-at-Delivery machine flag.
13. VIN/chassis reconciliation is Rule Engine logic in Audit Core, never Web/Android logic.
14. DI owns document intelligence, extraction, confidence and provenance only. DI does not own business-stage status or audit result.
15. Post-Delivery reconciliation is structurally reserved but out of UC03 Phase 1 runtime scope.
16. Android phone is the primary PC design target, Android tablet second, desktop Web third.
17. The approved bundled Verigence logo/lockup is used in production; static mockup placeholders are never copied.

---

# 4. Repositories and frozen source references

| Module | Repository | UC03 branch | Original frozen `dev` baseline |
|---|---|---|---|
| Audit Core | `verigence-audit-core` | `planning/uc-003-booking-delivery-audit` | `082cc2ada5cd934bf0707ccae945667feb3f6e37` |
| DI | `verigence-di` | `planning/uc-003-booking-delivery-audit` | `c97b3f3e5f8577160c88af1080496808189206fb` |
| Web/Android | `verigence-web` | `planning/uc-003-booking-delivery-audit` | `2c98f753ed1428c0d5f7a0b7144169d528a5bb78` |
| Security | `verigence-security` | none initially | current Security v2 `dev` source of truth |

The UC03 branches now intentionally contain both planning history and, after implementation begins, sequential implementation commits.

Do not rebase these branches back onto older baselines. Before beginning C0, reconcile any newer required `dev` changes explicitly rather than silently overwriting UC03 design history.

---

# 5. Canonical UC03 documents

Read these before implementation:

1. `UC03_SOLUTION_DESIGN_v1.1.md`
2. `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`
3. `UC03_RULE_FLAG_CATALOG_v1.0.md`
4. `UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`
5. `UC03_RECONCILIATION_DECISIONS_v1.0.md`
6. `UC03_IMPLEMENTATION_DESIGN_v0.1.md`
7. `UC03_IMPLEMENTATION_HANDOFF_v1.1.md`

Supporting module documents:

### DI

`docs/uc-003-booking-delivery-audit/UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md`

### Web/Android

- `UC03_UX_FLOW_CONTRACT_v0.1.md`
- `UC03_UX_REVIEW_NOTES_v0.2.md`
- `UC03_ANDROID_WEB_MOCKUPS_v0.1.html`

`UC03_UX_REVIEW_NOTES_v0.2.md` overrides conflicting first-pass mockup details.

---

# 6. Checkpoint execution rule

Every checkpoint uses the same closure discipline:

```text
inspect current branch HEADs
        ->
implement only current checkpoint
        ->
module tests/typecheck/build
        ->
cross-module contract tests
        ->
DB migration verification if applicable
        ->
DEV deployment
        ->
Android build if mobile behavior changed
        ->
checkpoint UAT
        ->
write checkpoint closure note
        ->
start next checkpoint
```

No implementation work from a future checkpoint should be added merely because a nearby file is already open.

---

# 7. C0 — Shared Foundation + Project Context

C0 must stay small. Its purpose is to establish common context/read/workflow primitives that Booking, Delivery and Audit all depend on.

It is part of the same UC03 branch.

## 7.1 Audit Core

Implement:

- current-user Project discovery from active `business_assignments` and active `projects`;
- `GET /v1/me/projects`;
- role/scope returned per Project;
- 0/1/multiple Project behavior support;
- `GET /v1/tenants/{tenantId}/uc03/work-items`;
- default latest 10 cases;
- `ALL | BOOKING | DELIVERY` filtering;
- date/date-range filtering;
- fixed maximum page size 10;
- cursor/keyset paging;
- Project timezone for business-date filtering;
- base `journey_stage_states` projection;
- immutable `journey_workflow_events` foundation;
- aggregate version/idempotency conventions for later commands.

Existing code to inspect/reuse before adding new modules:

```text
src/audit_core/business_assignments.py
src/audit_core/projects.py
src/audit_core/journeys.py
src/audit_core/workflow.py
src/audit_core/workflow_reliability.py
src/audit_core/idempotency.py
src/audit_core/authorization.py
src/audit_core/security_integration.py
src/audit_core/main.py
api/openapi-v1.yaml
```

New UC03-focused files are allowed where repository conventions justify them, but filenames must be confirmed against the codebase at implementation time rather than blindly following design examples.

## 7.2 Database

Current migration series reaches `0009` on the frozen UC03 branch.

Implemented C0 migration:

```text
0010_uc03_c0_foundation.py
```

Expected semantic scope:

- stage-state projection;
- immutable workflow event stream;
- tenant/RLS/constraint/index support;
- no destructive replacement of existing Journey/domain history.

## 7.3 Web/Android

Implement:

- Project context gate after login;
- no-assignment user-safe state;
- one Project auto-selection;
- multiple Project chooser;
- Project switcher;
- operating role bound to selected Project;
- clear Dealer/Outlet/current-case/query-cache state when Project changes;
- Project-scoped landing metrics:
  - Bookings In Progress;
  - Delivery In Progress;
  - Needs Attention;
  - Audit Flags;
- `Latest Bookings & Deliveries` list;
- latest 10 default;
- date/date-range filter;
- All/Bookings/Deliveries filter;
- Previous/Next page control;
- approved `src/assets/verigenceLockup.ts` identity treatment.

Existing Web structure to reuse:

```text
src/App.tsx
src/assets/verigenceLockup.ts
src/components/
src/domain/
src/features/
src/layout/
src/pages/
src/services/
src/store/
src/styles/
src/theme/
```

## 7.4 DI

No runtime change expected in C0.

## 7.5 Security

Verify that existing UC02 role-mapping synchronization makes Audit Core `business_assignments` reliable for Project discovery.

Do not change Security if that verification passes.

C0 implementation/testing did not expose a missing Security backend capability. No UC03 Security branch or Security code change was introduced.

## 7.6 C0 acceptance gate

C0 is closed only when:

- one Project auto-enters landing;
- multiple Projects show selection;
- a user's operating role can differ by Project;
- Project switch cannot expose stale prior-Project data;
- latest list returns at most 10 transactions;
- date filtering uses Project timezone;
- landing uses `Delivery In Progress`, not `Delivery Today`;
- UI uses approved Verigence logo;
- no Booking/Delivery mutation flow has been prematurely implemented.

## 7.7 C0 execution status — 23-Aug-2026

**Checkpoint status:** AUTOMATED + DEV DEPLOYMENT EVIDENCE COMPLETE; HUMAN UAT PENDING. C0 is **not formally closed** and C1 Booking is **not authorized to start** until the remaining human UAT is explicitly accepted.

Validated C0 implementation heads captured immediately before this closure-evidence documentation update:

| Module | Validated C0 head | Evidence |
|---|---|---|
| Audit Core | `71bea92822d3de836faea8eb250dacab81cf4c4c` | Final-head PR CI run `32594620868` passed package build, Ruff, fresh Postgres migration through `0010_uc03_c0_foundation`, and `141 passed` tests. |
| DI | `c899beb03c5fcbc84ffd41ed832451674b246668` | No C0 runtime change; branch head retained. |
| Web/Android | `0cbb5794bee4d494c9ee45229484591233a91818` | Final-head Web CI run `32594624429` passed typecheck/build; Android validation run `32594624444` passed native configuration, `lintDebug`, `assembleDebug`, APK verification and artifact upload. |

Deployment evidence:

- Audit Core exact deployed application SHA: `ffa334fcd0791a51e9b83221ceafc1603fd05d49`.
  - branch-safe validation/deployment run: `32594431799`;
  - Railway DEV deployment ID: `3614f0e0-1472-47fd-9af6-a64f795931f8`;
  - deployed smoke passed `/health`, unauthenticated presence checks for `/v1/me/projects`, `/v1/tenants/{tenantId}/uc03/landing-metrics`, `/v1/tenants/{tenantId}/uc03/work-items`, and approved DEV Web-origin CORS.
  - final validated Audit Core head `71bea928...` is one cleanup commit after the deployed SHA; the only difference is removal of the temporary C0 branch deployment workflow, with no application/runtime file change.
- Web exact deployed application SHA: `771d01396caa178d721f615fb1bbd36cae653a4c`.
  - branch-safe Web DEV validation/deployment run: `32594494424`;
  - Cloudflare DEV version ID: `8a5d5ef0-dc46-4e84-87d1-310303c1cfc5`;
  - deployed asset hashes/markers for Project discovery, landing metrics, work list, `Delivery In Progress` wording and approved Verigence logo passed; Security proxy smoke passed.
  - final validated Web head `0cbb5794...` is one cleanup commit after the deployed SHA; the only difference is removal of the temporary C0 Web deployment workflow, with no application/runtime file change.
- Android final-head artifact:
  - workflow run `32594624444`;
  - artifact `verigence-uc03-c0-android-debug`, artifact ID `9481252665`;
  - APK SHA-256 `0c287c03ce28af417ea5b01f8662215d8f276cc7eb9d0eb0ef477552f6c9ef30`;
  - artifact ZIP digest `sha256:20900a7c6101041dc21a78c577f2fcc85cc871193fba0ed0d78cbc7d7c84b09b`.

Human UAT is deliberately **PENDING**, not inferred from CI. A human must still exercise the deployed C0 flow with representative accounts/data for:

- zero-, one- and multiple-Project behavior;
- different operating roles across Projects;
- Project switching with no stale Dealer/Outlet/case/query data exposure;
- landing metrics and latest-10 list presentation;
- All/Bookings/Deliveries filtering, date-range behavior and Previous/Next paging;
- phone/tablet/Desktop usability and approved Verigence identity treatment.

Detailed C0 evidence is recorded in `docs/uc-003-booking-delivery-audit/status/UC03_C0_FOUNDATION.md`.

The documentation commit(s) that add this evidence necessarily advance the Audit Core branch beyond the validated C0 application head above. They are documentation-only; before C1 begins, inspect the live branch head as required by Section 6.

---

# 8. C1 — Booking

C1 is the first complete vertical business slice.

## 8.1 Booking business behavior

Implement:

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
BOOKING_CLOSED
BOOKING_CANCELLED
DUPLICATE_BOOKING
```

Booking conclusion uses a configurable close-reason dropdown plus remarks.

Working reasons:

- Customer Cancelled;
- Finance Not Approved;
- Vehicle Unavailable;
- Customer Shifted Dealer;
- Dealer Cancelled;
- Duplicate Booking;
- Other.

Duplicate Booking must produce both the duplicate business status and a canonical Audit Flag.

## 8.2 Audit Core C1 scope

Implement:

- Booking workflow commands/events;
- Booking stage state projection;
- Booking Audit State/Audit Status;
- dynamic Booking document applicability;
- `YES | NO | NA | UNANSWERED` assessments;
- applicability reason/history;
- Booking capture routed into existing typed domains;
- DI proposal acceptance/correction provenance;
- Booking rule execution;
- machine Booking flags;
- human Booking flag creation;
- Booking close/cancel/duplicate commands;
- Booking checkpoint/completeness read model;
- Booking portion of aggregate UC03 workspace;
- case-level processing-status read model.

Existing modules to inspect/reuse:

```text
src/audit_core/bookings.py
src/audit_core/commercials.py
src/audit_core/payments_finance.py
src/audit_core/insurance_tradein.py
src/audit_core/evidence.py
src/audit_core/evidence_read.py
src/audit_core/di_client.py
src/audit_core/versioned_masters.py
src/audit_core/findings.py
src/audit_core/audit_evaluation.py
src/audit_core/audit_events.py
```

## 8.3 Database

Proposed C1 migration, only if needed after reuse review:

```text
0011_uc03_booking_capture.py
```

Possible semantic scope:

- document assessment structure;
- extraction proposal/acceptance provenance structure;
- Booking close/disposition fields where not represented safely by workflow events/projection;
- indexes/RLS.

Do not create a generic 123-field authoritative table.

## 8.4 DI C1 scope

Implement reconciled Booking profiles only.

Rules:

- `SUPPORTED` mappings may be configured;
- `PROVISIONAL` requires explicit reconciliation before production publication;
- `TBD` remains disabled;
- preserve original machine value/confidence/source;
- processing order is never data-source precedence;
- no Aadhaar extraction/raw-retention by assumption.

## 8.5 Web/Android C1 scope

Mobile-first Booking experience:

- Create/Open Booking;
- Booking Started/In Progress;
- upload first;
- per-document extraction progress;
- PC-only fields remain usable while extraction runs;
- progressive extraction proposals;
- bulk accept clean proposals;
- individual accept/edit low-confidence or variance proposals;
- dynamic document checklist;
- Booking flags;
- manual PC flag;
- Booking verification/checkpoint summary;
- Booking conclusion dropdown + remarks;
- duplicate Booking behavior;
- phone/tablet/Desktop layout from shared components.

## 8.6 C1 acceptance gate

Must pass end-to-end:

```text
Web/Android
  -> Audit Core
  -> DI
  -> Audit Core
  -> Web/Android
```

Minimum proven scenarios:

- create/start Booking;
- extraction processing while PC continues work;
- clean proposal accept;
- corrected proposal preserves machine original;
- conditional document applicability;
- duplicate status + flag;
- normal close;
- cancellation/no-delivery reason + remarks;
- user-safe extraction failures;
- Android background/resume does not duplicate upload or lose processing state.

Do not begin Delivery until C1 is closed.

---

# 9. C2 — Delivery

## 9.1 Delivery business behavior

Exactly:

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
```

No Delivery Close.

## 9.2 Critical non-blocking scenario

If Dealer starts Delivery while Booking remains incomplete:

```text
record DELIVERY_STARTED
keep Booking audit/capture state truthful
raise configured MACHINE flag
continue Delivery capture
```

Do not return a business rejection solely because Booking prerequisites are incomplete.

## 9.3 Audit Core C2 scope

Implement:

- Delivery Start command/event;
- Delivery Complete command/event;
- Delivery stage projection;
- Delivery Audit State/Audit Status;
- automatic incomplete-Booking-at-Delivery rule/flag;
- Delivery intimation facts;
- dynamic Delivery document assessments;
- vehicle/photo evidence linkage;
- payment facts/rules;
- registration/delivery typed-domain integration;
- VIN Rule Engine integration;
- non-intimation flag;
- Delivery rules/flags;
- Delivery portion of aggregate workspace;
- physical Delivery Completed while audit may remain In Progress.

Existing modules to inspect/reuse:

```text
src/audit_core/vehicle_delivery.py
src/audit_core/payments_finance.py
src/audit_core/evidence.py
src/audit_core/evidence_read.py
src/audit_core/findings.py
src/audit_core/audit_evaluation.py
src/audit_core/workflow.py
```

## 9.4 Database

Proposed C2 migration only if required:

```text
0012_uc03_delivery_capture.py
```

Prefer existing vehicle/delivery/payment structures; migration should add only missing UC03 audit/workflow metadata.

## 9.5 DI C2 scope

Configure reconciled Delivery extraction/document handling only.

DI may supply VIN/chassis/identifier source facts where supported, but never decides business match/compliance.

## 9.6 Web/Android C2 scope

Implement:

- Delivery Started/In Progress/Completed;
- visible warning when Booking remains incomplete;
- primary path remains Continue Delivery;
- Delivery document checklist;
- touch-friendly Yes/No/NA;
- Android camera/photo evidence;
- VIN/photo presentation;
- payment verification presentation;
- Delivery flag list;
- Delivery Completed confirmation;
- Delivery Completed + Audit State IN_PROGRESS display;
- tablet/Desktop adaptations from shared components.

## 9.7 C2 acceptance gate

Minimum proven scenarios:

- clean Delivery start;
- Delivery start with Booking incomplete;
- automatic flag + accepted Delivery progression;
- missing/No document answer produces flag without blocking app;
- non-intimation produces flag and continues;
- VIN mismatch produces configured flag but physical Delivery remains recordable;
- payment mismatch/unverified flag;
- camera evidence attaches to correct requirement;
- Delivery Completed can coexist with Audit IN_PROGRESS;
- late evidence preserves true timestamp;
- Booking flow regression pack still passes.

Do not begin C3 until C2 is closed.

---

# 10. C3 — Audit / Review / Hardening

C3 completes the cross-stage audit behavior; it does not add Post-Delivery reconciliation.

## 10.1 Audit Core C3 scope

Implement/complete:

- UC03 extensions to existing `audit_findings`;
- stage attribution;
- MACHINE/HUMAN origin;
- actor/role provenance;
- rule/version linkage;
- append-only finding events;
- remarks/evidence;
- acknowledge/review/resolve/reopen/void actions according to policy;
- TL/PM/Executive policy enforcement;
- configurable Audit State completion policy;
- sticky Audit Status logic;
- cross-stage flag summary;
- full UC03 history/timeline;
- migration/backfill report;
- performance/index tuning;
- authorization matrix tests;
- telemetry/operability.

Existing modules to inspect/reuse:

```text
src/audit_core/findings.py
src/audit_core/audit_review.py
src/audit_core/audit_evaluation.py
src/audit_core/audit_events.py
src/audit_core/escalations.py
src/audit_core/escalations_api.py
src/audit_core/authorization.py
src/audit_core/security_integration.py
src/audit_core/workflow_telemetry.py
```

## 10.2 Database

Proposed C3 migrations as needed:

```text
0013_uc03_audit_flag_events.py
0014_uc03_backfill_indexes.py
```

Do not split simply for neatness; split only where migration safety/review requires it.

## 10.3 Web/Android C3 scope

Implement/complete:

- Booking/Delivery Audit Flag register;
- PC/TL/PM/Executive flag creation;
- TL/PM review/resolve;
- Executive full Phase-1 flag actions;
- remarks/evidence/history;
- Audit State and Audit Status presentation;
- complete history timeline;
- final Android phone/tablet/Desktop UX sweep;
- user-safe wording only.

## 10.4 C3 acceptance gate

Minimum proven scenarios:

- machine flag provenance;
- PC manual flag;
- TL review/resolve;
- PM review/resolve;
- Executive full permitted path;
- `Audit State = COMPLETE` with `Audit Status = FLAGS_RAISED` where configured policy permits;
- resolved flags remain historically visible;
- stale version conflict never silently overwrites review state;
- idempotency replay never duplicates flag/event;
- Project/role isolation remains correct;
- sensitive/internal data remains hidden from ordinary UX.

---

# 11. OpenAPI/API implementation rule

Canonical contract file:

```text
verigence-audit-core/api/openapi-v1.yaml
```

Contract changes are committed in the same checkpoint as code, not as cleanup afterward.

Expected API groups:

### C0

```text
GET /v1/me/projects
GET /v1/tenants/{tenantId}/uc03/landing-metrics
GET /v1/tenants/{tenantId}/uc03/work-items
```

### C1

```text
Booking start/close/cancel/duplicate
Booking capture/document assessment
extraction proposal accept/correct
UC03 workspace
processing-status
```

### C2

```text
Delivery start
Delivery complete
Delivery evidence/document/capture additions
```

### C3

```text
flags list/create
remarks/evidence
review/acknowledge/resolve/reopen/void
history/timeline where separate read is needed
```

---

# 12. Migration discipline

Recommended sequence, all on the same UC03 branch:

```text
0010_uc03_c0_foundation.py
0011_uc03_booking_capture.py
0012_uc03_delivery_capture.py
0013_uc03_audit_flag_events.py
0014_uc03_backfill_indexes.py        only if needed
```

Every migration requires:

- tenant/RLS review;
- forward verification;
- downgrade/rollback analysis;
- index/query-plan review where relevant;
- no fabricated historical timestamps;
- explicit count/report for ambiguous backfill.

---

# 13. Checkpoint status notes

At the end of every checkpoint create/update a status document, for example:

```text
docs/uc-003-booking-delivery-audit/status/UC03_C0_FOUNDATION.md
docs/uc-003-booking-delivery-audit/status/UC03_C1_BOOKING.md
docs/uc-003-booking-delivery-audit/status/UC03_C2_DELIVERY.md
docs/uc-003-booking-delivery-audit/status/UC03_C3_AUDIT.md
```

Each note must contain:

- exact branch HEAD SHA for Audit Core/DI/Web;
- files materially changed;
- migrations applied;
- APIs added/changed;
- DI profiles added/changed;
- UI screens/components completed;
- tests and builds passed;
- DEV deployments/APK reference where applicable;
- known issues;
- explicitly deferred items;
- approval/readiness for next checkpoint.

The next checkpoint begins from those recorded heads.

---

# 14. Commit discipline

Keep commits small and checkpoint-labelled on the same branch.

Examples:

```text
uc03(c0): add project context read model
uc03(c0): add latest-10 work list
uc03(c1): add booking stage commands
uc03(c1): add extraction proposal acceptance
uc03(c2): add non-blocking delivery start
uc03(c2): add android vehicle photo capture
uc03(c3): add finding event history
uc03(c3): add TL PM review actions
```

No checkpoint-specific Git branch is needed.

---

# 15. Deployment sequence per checkpoint

```text
module unit/integration tests
        ->
contract tests
        ->
migration verification
        ->
Audit Core DEV deployment
        ->
DI DEV deployment when DI changed
        ->
Web DEV deployment
        ->
Android debug APK when relevant
        ->
checkpoint UAT
```

UC03 is promoted to `dev` only after the agreed Phase-1 acceptance point. Do not partially merge random future-checkpoint code into `dev` simply to reduce branch age.

If a production-critical hotfix lands on `dev` during the long UC03 work, reconcile it deliberately into the single UC03 branch and record that synchronization in the active checkpoint note.

---

# 16. Explicit Phase-1 out of scope

- Post-Delivery reconciliation;
- D+7/D+12/D+90 timers;
- weekly/monthly processors;
- Delivery Success/Failure;
- iOS;
- Security v2 redesign;
- DI architecture redesign;
- duplicate generic source-of-truth for all 123 fields;
- client-side VIN rule;
- client-side permission enforcement;
- replacement of existing Verigence product shell/brand identity.

---

# 17. Controlled open decisions

The following remain review/UAT decisions and must not be guessed in code:

1. final business reconciliation of the 26-vs-29 document catalogue;
2. remaining provisional/TBD DI source mappings;
3. final VIN/chassis normalization and match algorithm;
4. exact high/critical flag review policy before Audit State can become COMPLETE;
5. final Booking/Delivery date-source precedence for date filtering;
6. final existing-vs-new permission mapping;
7. confirmation that UC02 assignment synchronization fully supports `/me/projects`;
8. exact historical Journey backfill population.

---

# 18. UC03 Phase-1 Definition of Done

UC03 is complete only when:

- C0, C1, C2 and C3 are each formally closed;
- one selected Project context correctly drives PC/TL/PM/Executive operational work;
- latest-10 and date-filter paging works per Project;
- Booking lifecycle matches design;
- Delivery lifecycle is Started/In Progress/Completed only;
- dealer Delivery progression is never blocked solely by an audit exception;
- dynamic documents/extraction/proposals work without silent overwrites;
- machine and human flags share one auditable register;
- Audit State/Audit Status are stage-specific and historically correct;
- Android phone flow passes physical-device UAT;
- Android tablet and desktop Web remain usable and visually aligned;
- approved Verigence logo is used;
- required automated/E2E tests pass;
- migrations/backfill are verified;
- ordinary UI exposes no technical internal messages or inappropriate sensitive identifiers;
- Post-Delivery runtime remains out of scope;
- final checkpoint notes contain exact implementation heads and deferred decisions.

---

# 19. Immediate start after handoff approval

The first implementation work item is **C0 — Shared Foundation + Project Context**.

Do not implement Booking capture until C0 is closed.

Then proceed strictly on the same UC03 branches:

```text
C0 Foundation / Project Context
          ->
C1 Booking
          ->
C2 Delivery
          ->
C3 Audit / Review / Hardening
```

No additional UC03 implementation branches are to be created.
