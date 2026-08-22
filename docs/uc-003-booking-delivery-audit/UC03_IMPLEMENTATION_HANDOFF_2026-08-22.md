# Verigence UC03 — Booking & Delivery Audit — Implementation Handoff

**Document ID:** `VUC03-HO-001`  
**Date:** 2026-08-22  
**Status:** IMPLEMENTATION HANDOFF / SEQUENTIAL EXECUTION CONTRACT  
**Use case:** UC03 — Booking & Delivery Audit  
**Primary modules:** Audit Core, DI, Web/Android  
**Security:** only if a concrete missing capability is proven  

---

## 1. Purpose

This handoff is the execution contract for implementing UC03 after the planning/design phase.

UC03 is the primary operational audit capability. It must be implemented in **small, sequential checkpoints** so each checkpoint can be completed, tested and accepted before the next begins.

The implementation must not be split into multiple Booking/Delivery/Audit feature branches.

### Branch rule — frozen

There will be **one UC03 implementation branch per repository only**:

```text
verigence-audit-core
work/uc-003-booking-delivery-audit

verigence-di
work/uc-003-booking-delivery-audit

verigence-web
work/uc-003-booking-delivery-audit
```

There will be no separate `booking`, `delivery`, `audit`, `foundation`, mobile or Web feature branches for UC03.

All checkpoints are implemented sequentially on the same UC03 branch in each repository.

Security gets a UC03 branch only if implementation proves that the existing Security/Audit Core Project-role synchronization is insufficient:

```text
verigence-security
work/uc-003-project-context-authz
```

Do not create that Security branch speculatively.

---

## 2. Canonical planning sources

Canonical business/technical design lives on:

```text
verigence-audit-core
planning/uc-003-booking-delivery-audit
```

Required documents, in reading order:

1. `UC03_SOLUTION_DESIGN_v1.1.md`
2. `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`
3. `UC03_RULE_FLAG_CATALOG_v1.0.md`
4. `UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`
5. `UC03_RECONCILIATION_DECISIONS_v1.0.md`
6. `UC03_IMPLEMENTATION_DESIGN_v0.1.md`
7. `UC03_IMPLEMENTATION_HANDOFF_2026-08-22.md` — this document

DI supporting source:

```text
verigence-di
planning/uc-003-booking-delivery-audit

docs/uc-003-booking-delivery-audit/UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md
```

Web/Android supporting sources:

```text
verigence-web
planning/uc-003-booking-delivery-audit

docs/uc-003-booking-delivery-audit/UC03_UX_FLOW_CONTRACT_v0.1.md
docs/uc-003-booking-delivery-audit/UC03_UX_REVIEW_NOTES_v0.2.md
docs/uc-003-booking-delivery-audit/UC03_ANDROID_WEB_MOCKUPS_v0.1.html
```

`UC03_UX_REVIEW_NOTES_v0.2.md` overrides any conflicting behavior in the first static mockup.

---

## 3. Frozen planning baselines

The planning branches were created from these baselines:

| Repository | Frozen planning baseline | Planning branch |
|---|---|---|
| Audit Core | `dev@082cc2ada5cd934bf0707ccae945667feb3f6e37` | `planning/uc-003-booking-delivery-audit` |
| DI | `dev@c97b3f3e5f8577160c88af1080496808189206fb` | `planning/uc-003-booking-delivery-audit` |
| Web/Android | `dev@2c98f753ed1428c0d5f7a0b7144169d528a5bb78` | `planning/uc-003-booking-delivery-audit` |

Before creating implementation branches, compare each current `dev` branch with the frozen planning baseline. If `dev` has advanced, reconcile UC03 with those changes first. Do not blindly branch from the old SHA if newer approved work exists.

---

## 4. Non-negotiable business invariants

1. One immutable internal `journey_id` spans Booking and Delivery.
2. PC-facing UI uses **Booking** and **Delivery** terminology; it does not expose “Journey Workspace”.
3. Dealer progression is observed and recorded; Verigence audit logic does not abort dealer progression.
4. Audit incompleteness/non-compliance may raise flags and keep Audit State `IN_PROGRESS`, but cannot reject a real Delivery Start/Completed event solely for non-compliance.
5. Booking and Delivery business status are separate from per-stage Audit State and Audit Status.
6. Machine and human flags use one canonical finding/flag register.
7. PC/TL/PM/Executive may raise flags. TL/PM normally review/resolve. Executive has all Phase-1 flag privileges. Enforcement is permission/policy driven, not React role conditionals.
8. VIN/chassis reconciliation belongs to Audit Core Rule Engine, never Android/Web.
9. DI owns document intelligence only — classification/extraction/confidence/provenance — not Booking/Delivery business state or audit outcome.
10. Post-Delivery reconciliation is structurally reserved but out of UC03 Phase-1 implementation scope.
11. Android phone is the primary PC UX; Android tablet second; desktop Web uses the same business workflow/components.
12. Extraction is asynchronous: upload first, continue PC work, per-document progress, proposals-not-overwrites.
13. Approved Verigence logo/lockup must reuse the existing runtime asset `src/assets/verigenceLockup.ts`; mockup logo rendering is non-normative.
14. Technical/internal backend error text must never be shown directly to users.

---

## 5. Frozen state model

### Booking business status

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
BOOKING_CLOSED
BOOKING_CANCELLED
DUPLICATE_BOOKING
```

Booking close uses a configurable reason dropdown plus remarks.

Working reason list:

```text
Customer Cancelled
Finance Not Approved
Vehicle Unavailable
Customer Shifted Dealer
Dealer Cancelled
Duplicate Booking
Other
```

Duplicate Booking must set `DUPLICATE_BOOKING` and raise a flag.

### Delivery business status

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
```

There is **no Delivery Closed state**.

`DELIVERY_COMPLETED` records the real physical delivery event. Delivery Audit State may remain `IN_PROGRESS` afterward.

### Audit State — independently per stage

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

### Audit Status — independently per stage

```text
NOT_EVALUATED
NO_FLAGS
FLAGS_RAISED
```

`FLAGS_RAISED` is historical/sticky: resolving all flags does not rewrite the fact that the stage had flags.

---

## 6. Project-context UX — frozen

PC/TL/PM may work across multiple Projects and may have different operating roles per Project.

After login:

```text
0 Projects  -> safe no-assignment screen
1 Project   -> auto-select, no selector screen
>1 Projects -> Choose Project screen
```

Multi-Project users get `Switch Project` without sign-out.

Project is represented by `tenant_id` in the current architecture.

Preferred source for current-user operational Projects:

```text
Security actor identity
+
Audit Core business_assignments
+
Audit Core projects
```

Proposed endpoint:

```http
GET /v1/me/projects
```

Do not reuse the existing SuperAdmin `/v1/projects` administration endpoint for PC/TL/PM.

---

## 7. Project landing — frozen

The UC03 landing is business-focused and Project-scoped.

Primary metrics direction:

```text
Bookings In Progress
Delivery In Progress
Needs Attention
Audit Flags
```

`Delivery Today` is explicitly wrong and must not be implemented.

Main list:

```text
Latest Bookings & Deliveries
```

Default behavior:

- latest 10 authorized cases only;
- selected Project scope;
- latest activity descending;
- each row/card shows latest Booking and Delivery information for the same case;
- filters: All / Bookings / Deliveries;
- date selector/date range;
- 10 per page;
- Previous/Next pagination;
- Project timezone controls date boundaries;
- no duplicate row for one `journey_id` when both Booking and Delivery match a date range.

Proposed endpoint:

```http
GET /v1/tenants/{tenantId}/uc03/work-items
```

---

# 8. Sequential implementation strategy

UC03 is executed in **four functional checkpoints plus one hardening checkpoint**, on the same UC03 branch in each repository.

The split is deliberately small enough to stay mentally bounded while preserving cross-module integration.

```text
F0  Shared Foundation
    ↓
B1  Booking
    ↓
D1  Delivery
    ↓
A1  Audit & Review
    ↓
H1  Hardening / UAT / Android package
```

### Execution rule

**Do not start the next checkpoint until the previous checkpoint passes its acceptance gate.**

A checkpoint is not considered complete merely because backend code compiles. Audit Core + DI where applicable + Web/Android + tests must close together.

---

# 9. Checkpoint F0 — Shared Foundation

## Goal

Establish Project context and the minimum UC03 infrastructure needed by both Booking and Delivery, without implementing Booking/Delivery document capture yet.

## Audit Core scope

Implement/reconcile:

- current-user Project read model (`/v1/me/projects`);
- active `business_assignments` projection query;
- Project role/scope resolution;
- latest-10 UC03 work-list API with date filters and cursor paging;
- stage-state persistence foundation;
- workflow event persistence foundation;
- aggregate version/optimistic concurrency foundation;
- idempotent command infrastructure reuse/extension;
- UC03 workspace/read-model skeleton;
- safe errors;
- authorization/scope isolation;
- OpenAPI changes.

Existing Audit Core integration points to reuse/review include:

```text
src/audit_core/business_assignments.py
src/audit_core/projects.py
src/audit_core/journeys.py
src/audit_core/workflow.py
src/audit_core/workflow_reliability.py
src/audit_core/idempotency.py
src/audit_core/security.py
src/audit_core/security_integration.py
src/audit_core/main.py
api/openapi-v1.yaml
```

New UC03-specific service/API modules may be introduced only where they improve separation; do not duplicate the existing domain services.

## Database scope

First UC03 migrations start after the existing migration chain (current planning baseline contains `0001`–`0009`). Exact numeric filenames must be resolved against current `dev` at implementation start.

Conceptual first migration set:

```text
<next>_uc03_stage_workflow_foundation.py
<next>_uc03_stage_workflow_rls_indexes.py
```

Includes at minimum semantic structures for:

- `journey_stage_states`;
- `journey_workflow_events`;
- required index/RLS/append-only protections.

Do not invent timestamps during backfill.

## DI scope

None beyond integration smoke checks in F0.

## Web/Android scope

Implement:

- `AuthenticatedIdentityContext` / existing auth reconciliation;
- `SelectedProjectContext`;
- conditional Project chooser;
- automatic single-Project selection;
- Project switcher;
- tenant-query cache invalidation on Project switch;
- Project-scoped latest-10 landing;
- All/Bookings/Deliveries filter;
- date/date-range filter;
- 10-item paging;
- cards on phone, responsive rows/cards on tablet/Web;
- approved `verigenceLockup` asset;
- user-safe empty/error/loading states.

No Booking/Delivery capture mutations yet.

## F0 acceptance gate

Must pass before Booking starts:

1. one Project auto-enters;
2. multiple Projects show chooser;
3. same user can hold different roles by Project;
4. switch Project clears stale tenant/dealer/outlet/case data;
5. no Project gives safe empty state;
6. work list max 10;
7. cursor next/previous stable;
8. Booking filter works;
9. Delivery filter works;
10. Project timezone date boundary works;
11. user cannot see unauthorized Project rows;
12. approved logo renders on Android phone/tablet/Web;
13. no raw backend errors in UI.

Close F0 with a commit/taggable checkpoint before B1.

---

# 10. Checkpoint B1 — Booking

## Goal

Deliver the complete Booking vertical slice from Booking Start through Booking conclusion, including DI extraction and Booking-stage flags.

## Audit Core scope

Implement/reconcile:

- Booking `STARTED` / `IN_PROGRESS` / `CLOSED` / `CANCELLED` / `DUPLICATE_BOOKING`;
- Booking workflow events;
- Booking close reason catalogue + remarks;
- Duplicate Booking flag behavior;
- Booking-stage Audit State/Status;
- applicable Booking document requirements;
- dynamic applicability recalculation;
- document assessments (`YES/NO/NA/UNANSWERED` where policy allows);
- Booking capture orchestration over existing typed domains;
- DI evidence/proposal integration;
- accept/correct proposal provenance;
- Booking rules/evaluations;
- machine flags;
- human PC flags;
- Booking completion/checkpoint summary;
- Booking portion of UC03 workspace.

Reuse existing typed domains wherever possible:

```text
src/audit_core/bookings.py
src/audit_core/commercials.py
src/audit_core/payments_finance.py
src/audit_core/insurance_tradein.py
src/audit_core/evidence.py
src/audit_core/evidence_read.py
src/audit_core/di_client.py
src/audit_core/audit_evaluation.py
src/audit_core/findings.py
src/audit_core/versioned_masters.py
```

Do not create a generic duplicate authoritative table for all 123 fields.

## Database scope

Conceptual migration group:

```text
<next>_uc03_document_assessments.py
<next>_uc03_extraction_proposals.py
<next>_uc03_findings_extensions.py
```

Exact split can be reduced/combined after migration review, but semantics must match Implementation Design.

## DI scope

Implement only reconciled Booking extraction mappings from:

```text
UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md
```

Requirements:

- support `SUPPORTED` mappings;
- reconcile `PROVISIONAL` before production use;
- never silently configure `TBD` mappings;
- preserve document-specific provenance;
- preserve original machine fact after human correction;
- return confidence and processing state;
- no Aadhaar raw extraction/retention by assumption.

## Web/Android scope

Implement Booking-first PC experience:

- Create/Open Booking;
- Booking Started/In Progress status;
- document upload first;
- per-document processing states;
- PC-only fields usable immediately while DI processes;
- extraction proposals arrive progressively;
- clean-value bulk accept;
- low-confidence/variance review;
- accept/correct interaction;
- dynamic document checklist;
- Booking Audit State/Status;
- machine/human flags visible without stopping work;
- Booking conclusion sheet with reason dropdown + remarks;
- Duplicate Booking flow;
- Android-first touch layout;
- tablet and desktop adaptations of same components.

## B1 acceptance gate

At minimum:

- create/start Booking;
- extraction runs while PC continues capture;
- clean bulk accept;
- corrected proposal retains machine provenance;
- unreadable document retry is user-safe;
- dynamic conditional documents appear correctly;
- duplicate -> `DUPLICATE_BOOKING` + flag;
- normal Booking close works;
- no-delivery close reason + remarks works;
- Booking can remain In Progress without blocking later Delivery start;
- machine and PC flags both persist with actor/origin;
- Android phone/tablet/Web all usable;
- no silent extraction overwrites.

Do not start D1 until B1 is accepted.

---

# 11. Checkpoint D1 — Delivery

## Goal

Deliver the complete Delivery vertical slice while preserving the rule that dealer progression is never blocked by audit non-compliance.

## Audit Core scope

Implement/reconcile:

- `DELIVERY_STARTED`;
- `DELIVERY_IN_PROGRESS`;
- `DELIVERY_COMPLETED`;
- Delivery-stage Audit State/Status;
- automatic machine flag when Delivery starts while Booking prerequisites/audit completion conditions remain incomplete according to published policy;
- real Delivery event accepted even when Booking is incomplete;
- Delivery intimation capture;
- applicable Delivery documents;
- document assessments;
- vehicle/photo evidence linkage;
- payment verification/reconciliation inputs;
- VIN/chassis facts supplied to Rule Engine;
- VIN reconciliation result generated only by Rule Engine;
- Delivery machine/human flags;
- Delivery rules/evaluations;
- Delivery portion of UC03 workspace;
- Delivery Completed event with Audit State allowed to remain `IN_PROGRESS`.

Reuse/reconcile existing modules:

```text
src/audit_core/vehicle_delivery.py
src/audit_core/payments_finance.py
src/audit_core/evidence.py
src/audit_core/evidence_read.py
src/audit_core/audit_evaluation.py
src/audit_core/findings.py
src/audit_core/workflow.py
```

There is no Delivery Close command or state.

## DI scope

Implement only reconciled Delivery document extraction profiles.

DI may supply source-specific VIN/chassis identifier facts where supported; DI must not decide business match/pass/fail.

## Web/Android scope

Implement:

- Delivery Started/In Progress/Completed;
- warning/flag presentation when Booking remains incomplete;
- primary action remains Continue Delivery;
- Delivery document checklist;
- Yes/No/NA interactions according to requirement policy;
- Android camera flow for vehicle/VIN/interior/odometer/etc. evidence;
- evidence upload/retry idempotency;
- payment verification capture;
- Delivery flags;
- Delivery Completed screen while audit may remain In Progress;
- phone-first, tablet, desktop responsive views.

## D1 acceptance gate

At minimum:

- Delivery Start accepted while Booking remains In Progress;
- automatic flag records incomplete Booking at Delivery start;
- Booking historical flag remains after later Booking completion;
- missing Delivery document can raise flag without aborting Delivery;
- non-intimated Delivery can be recorded + flagged;
- VIN rule mismatch raises configured flag but does not prevent recording actual Delivery Completed;
- payment mismatch/unverified raises flag;
- Android camera evidence links to correct requirement;
- Delivery Completed can coexist with Delivery Audit `IN_PROGRESS`;
- late evidence keeps its true timestamp;
- no Delivery Closed state appears anywhere.

Do not start A1 until D1 is accepted.

---

# 12. Checkpoint A1 — Audit & Review

## Goal

Complete the cross-stage flag review/resolution experience and audit history for PC/TL/PM/Executive.

## Audit Core scope

Implement/reconcile:

- extensions to existing `audit_findings` as UC03 flag register;
- `stage_code`;
- `origin_kind = MACHINE | HUMAN`;
- origin actor/role snapshot;
- rule/rule-version linkage;
- append-only finding events;
- remarks/evidence/review/resolution/reopen/void actions;
- configurable role/action policy;
- Audit State completion policy;
- sticky `FLAGS_RAISED` Audit Status;
- full UC03 history/timeline read model;
- audit permission enforcement;
- safe concurrency/idempotency around review actions.

Existing modules to reuse/reconcile:

```text
src/audit_core/findings.py
src/audit_core/audit_review.py
src/audit_core/audit_events.py
src/audit_core/escalations.py
src/audit_core/escalations_api.py
src/audit_core/authorization.py
src/audit_core/security_integration.py
```

## DI scope

No new business behavior. DI evidence/provenance remains viewable through Audit Core references where needed.

## Web/Android scope

Implement:

- case-level Audit Flags list;
- filter by Booking/Delivery stage;
- machine/human origin labels;
- PC/TL/PM/Executive raise flag;
- remarks/evidence attachment;
- TL/PM/Executive review;
- TL/PM/Executive resolution;
- reopen/void where policy permits;
- tablet-friendly master/detail review;
- desktop review workspace;
- phone review usable without dense tables;
- history/timeline showing business and audit events separately but chronologically.

## A1 acceptance gate

At minimum:

- PC human flag;
- TL review/resolve;
- PM review/resolve;
- Executive full Phase-1 flag actions;
- machine rule flag is distinguishable from human flag;
- actor role snapshot retained;
- Audit State `COMPLETE` + Audit Status `FLAGS_RAISED` is valid;
- after all flags resolved, Audit Status remains `FLAGS_RAISED`;
- stale review version does not overwrite newer action;
- same idempotency key does not duplicate event;
- full Booking/Delivery history is readable on Android/tablet/Web.

---

# 13. Checkpoint H1 — Hardening, UAT and Android package

## Goal

Validate the complete UC03 workflow on real environments/devices before promotion.

## Cross-module scope

- reconcile final 26-vs-29 document requirement decision from UAT;
- reconcile remaining DI `PROVISIONAL/TBD` mappings;
- freeze VIN normalization/match algorithm;
- freeze high/critical flag completion-review policy;
- confirm Booking/Delivery date-source precedence;
- confirm existing permission vs new permission keys;
- verify Project-role synchronization from UC02 is reliable;
- migration/backfill report for existing Journeys;
- performance check for latest-10 work list and workspace;
- security/scope regression;
- responsive regression;
- error-copy review;
- Android physical-device test;
- fresh Android Capacitor debug build;
- final DEV deployment;
- business/UAT sign-off.

## Required device/layout targets

At minimum:

```text
Android phone 360 px
Android phone 390 px
Android phone 430 px
Android tablet
Desktop Web
```

The Android build is required. iOS is not in UC03 scope.

---

# 14. Web is mandatory in every checkpoint

UC03 is not an “Android-only implementation with Web later”.

The UX priority is Android-first, but **Web is implemented and closed in the same checkpoint**.

| Checkpoint | Android/Web delivery |
|---|---|
| F0 | Project chooser/switch + latest-10 landing on phone/tablet/Web |
| B1 | Booking capture/extraction/documents/conclusion on phone/tablet/Web |
| D1 | Delivery documents/photos/payments/status on phone/tablet/Web |
| A1 | flag review/history on phone/tablet/Web |
| H1 | cross-device regression + Android native package + Web DEV validation |

The same business components and API contracts must be reused; do not fork two independent workflows.

---

# 15. API contract direction

OpenAPI must be updated before/with implementation, not after UI code guesses endpoints.

Planned UC03 API families:

```text
GET  /v1/me/projects
GET  /v1/tenants/{tenantId}/uc03/work-items
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/uc03-workspace
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/processing-status

POST /.../booking/start
POST /.../booking/close-ready
POST /.../booking/close-no-delivery
POST /.../booking/cancel
POST /.../booking/mark-duplicate

POST /.../delivery/start
POST /.../delivery/complete

PUT  /.../stages/{stage}/documents/{requirementKey}
PUT  /.../capture/{fieldKey}              where generic capture is retained
POST /.../extraction-proposals/{id}/accept
POST /.../extraction-proposals/{id}/correct

GET  /.../flags
POST /.../flags
POST /.../flags/{id}/remarks
POST /.../flags/{id}/acknowledge
POST /.../flags/{id}/resolve
POST /.../flags/{id}/reopen
POST /.../flags/{id}/void
```

Exact endpoint naming may be adjusted during OpenAPI implementation review, but client/server must converge on one published contract before each checkpoint closes.

---

# 16. Data model direction

Reuse existing Audit Core typed domains and existing findings/rules/evidence systems.

New/extended semantics include:

```text
journey_stage_states
journey_workflow_events
journey_document_assessments
capture/extraction proposal acceptance provenance
audit_findings extensions
audit_finding_events
audit_control/version evaluator/effect metadata extensions
```

Do not create a parallel generic database that duplicates Booking, Commercial, Payments, Finance, Insurance, Trade-In, Vehicle, Registration and Delivery source-of-truth tables.

---

# 17. Migration discipline

1. Migrations are additive first.
2. No destructive replacement of historical data.
3. Existing Journeys are backfilled only from reliable durable facts.
4. Unknown historic timestamps remain unknown; do not fabricate them.
5. Migration/backfill produces an ambiguity/unmapped report.
6. RLS and tenant scope must be included with new UC03 tables.
7. Append-only event tables must be protected from normal update/delete mutation.
8. Current `dev` migration head determines actual UC03 migration numbers at implementation start.

---

# 18. DI discipline

- Web/Android never calls DI directly.
- Audit Core is the workflow/integration boundary.
- DI machine facts remain immutable/provenance-bearing.
- Accepted/corrected values are represented distinctly.
- Processing order never implies source precedence.
- Retry/new evidence never silently destroys prior machine facts.
- VIN matching is not implemented in DI.
- Aadhaar extraction/raw retention is not introduced without explicit approved policy.

---

# 19. Web/Android discipline

- use existing approved Verigence shell, theme and logo;
- PC UI uses Booking/Delivery terms;
- no “Journey Workspace” label;
- phone cards, not dense desktop tables squeezed onto mobile;
- 44–48 px touch targets;
- camera capture adjacent to relevant Delivery requirement;
- extraction processing is non-blocking;
- app background/resume safely restarts processing refresh;
- no raw technical/backend errors;
- no silent duplicate upload on network retry;
- Project switch invalidates tenant-scoped client state;
- user sees Booking/reference/customer/vehicle, not internal Journey ID.

---

# 20. Implementation commit/checkpoint discipline

Even though there is one branch, use clear checkpoint commits.

Suggested commit prefixes:

```text
uc03(f0): ...
uc03(booking): ...
uc03(delivery): ...
uc03(audit): ...
uc03(hardening): ...
```

Do not mix unrelated next-checkpoint work into a checkpoint that is still failing tests.

At each checkpoint close:

1. run module unit/integration tests;
2. run cross-module contract smoke tests;
3. run Web typecheck/build;
4. run relevant responsive smoke tests;
5. deploy/check DEV where appropriate;
6. record the close commit SHAs in the UC03 progress/handoff update;
7. only then proceed.

---

# 21. Mandatory cross-cutting test scenarios

The Implementation Design currently defines 37 mandatory scenarios. They remain binding.

Highest-risk scenarios that must never be dropped:

- multi-Project user with different roles per Project;
- Project switch tenant isolation;
- latest 10 + stable paging;
- extraction continues while PC works;
- proposals do not overwrite silently;
- Duplicate Booking -> business status + flag;
- Delivery starts while Booking remains In Progress -> event accepted + machine flag;
- missing Delivery requirement -> flag, not app/process abort;
- VIN mismatch -> Rule Engine flag, actual Delivery still recordable;
- physical Delivery Completed while audit remains In Progress;
- machine + PC/TL/PM/Executive flags preserve origin;
- Audit State Complete + FLAGS_RAISED valid;
- sticky FLAGS_RAISED after resolution;
- stale concurrency conflict safe;
- idempotency replay safe;
- Android camera evidence maps to correct requirement;
- app background/resume during DI processing;
- raw backend error never shown to user;
- approved Verigence lockup renders consistently.

---

# 22. Explicitly out of scope for Phase 1

- Post-Delivery weekly/monthly reconciliation engine;
- D+7/D+12/90-day scheduled obligations;
- CRM post-delivery reconciliation workflow;
- Delivery Success/Failure terminal states;
- Delivery Closed state;
- iOS packaging/validation;
- authentication redesign;
- new generic source-of-truth for all 123 fields;
- speculative Security changes;
- hard-coded 26-document UI count;
- client-side VIN reconciliation.

---

# 23. Open decisions to close during implementation/UAT

These are known and must remain visible:

1. final business reconciliation of 26 vs 29 document requirements;
2. remaining `PROVISIONAL/TBD` DI mappings;
3. exact VIN/chassis normalization/matching rule;
4. exact high/critical flag review requirements before Audit State becomes Complete;
5. final business-date source precedence for Booking/Delivery date filtering;
6. final reuse/new Security permission-key mapping;
7. confirm UC02 role mapping keeps `business_assignments` reliable for Project context;
8. exact backfill treatment of legacy Journeys.

No developer should silently decide these inside React, SQL, DI profiles or rule code.

---

# 24. Definition of Done — UC03 Phase 1

UC03 Phase 1 is complete only when all of the following are true:

### Project/work entry

- PC/TL/PM/Executive sees only authorized Projects;
- one Project auto-selects;
- multiple Projects can be selected/switched;
- current role is Project-specific;
- latest Booking/Delivery work list is correct, max 10 per page and date-filterable.

### Booking

- Booking status lifecycle works;
- close/cancel/duplicate reasons + remarks work;
- dynamic documents work;
- DI processing/proposals work;
- proposal provenance is preserved;
- Booking rules/flags work;
- PC can continue work during extraction.

### Delivery

- Delivery Started/In Progress/Completed works;
- no Delivery Closed state exists;
- incomplete Booking never prevents recording Delivery progression;
- exception is captured as flag;
- Delivery docs/photos/payments work;
- VIN decision comes from Rule Engine;
- Delivery Completed can coexist with Audit In Progress.

### Audit/review

- machine + human flags share one register;
- role/action policy is configurable;
- TL/PM/Executive review/resolution works;
- history/provenance is complete;
- Audit State and Audit Status are independent and correct.

### UX/runtime

- Android phone is production-usable for PC workflows;
- Android tablet is usable for PC/reviewer workflows;
- desktop Web implements the same complete UC03 business flow;
- approved Verigence brand asset is used;
- technical errors are not exposed;
- mobile extraction-delay UX is productive/non-blocking;
- real Android debug package builds successfully from the accepted UC03 code.

### Quality

- mandatory E2E scenarios pass;
- migration/backfill report reviewed;
- authorization/tenant isolation passes;
- responsive regression passes;
- DEV deployment passes;
- Android physical-device UAT passes;
- business UAT accepts Phase 1.

---

# 25. Resume instructions after context reset

To resume UC03 implementation safely:

1. Read this handoff first.
2. Read `UC03_IMPLEMENTATION_DESIGN_v0.1.md`.
3. Read the v1.1 Solution Design + Workflow catalog.
4. Read Rule/Flag + Document/123-field + Reconciliation documents.
5. Read DI extraction mapping.
6. Read Web `UC03_UX_REVIEW_NOTES_v0.2.md` and UX contract.
7. Check current `dev` heads in Audit Core, DI and Web against the frozen planning baselines.
8. Reconcile any newer approved work.
9. Create exactly one `work/uc-003-booking-delivery-audit` branch in each of Audit Core, DI and Web.
10. Start **F0 Shared Foundation only**.
11. Do not begin Booking until F0 acceptance gate is green.
12. Continue sequentially B1 -> D1 -> A1 -> H1.

This sequence is intentionally strict to reduce drift, accidental cross-module assumptions and implementation confusion.
