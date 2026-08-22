# Verigence UC03 — Booking & Delivery Audit — Implementation Handoff

**Document ID:** `VUC03-HO-001`  
**Version:** `1.0`  
**Status:** IMPLEMENTATION HANDOFF / SEQUENTIAL EXECUTION  
**Date:** 2026-08-22  
**Canonical implementation design:** `UC03_IMPLEMENTATION_DESIGN_v0.1.md`

---

## 1. Purpose

This document is the execution contract for UC03.

UC03 is intentionally implemented as **small sequential checkpoints on one UC03 branch per touched repository**.

There are no separate Booking, Delivery, Audit, Foundation, mobile, or review branches.

The implementation sequence is:

```text
UC03 branch
   |
   +-- Checkpoint 0: Shared Foundation + Project Context
   |
   +-- Checkpoint 1: Booking
   |
   +-- Checkpoint 2: Delivery
   |
   +-- Checkpoint 3: Audit / Review / Hardening
   |
   +-- DEV/UAT
   |
   +-- merge UC03 -> dev
```

A later checkpoint MUST NOT begin until the current checkpoint passes its acceptance gate, except for narrowly necessary contract preparation.

This structure is designed to prevent scope drift, context loss, hidden coupling, and partial implementation assumptions.

---

# 2. Non-negotiable UC03 business invariants

1. One immutable internal `journey_id` follows the case from Booking through Delivery.
2. PC-facing UI uses Booking and Delivery terminology, not Journey terminology.
3. Verigence records the dealer's real process; audit conditions never abort or refuse a real Delivery progression event solely because prerequisites are incomplete or non-compliant.
4. Booking and Delivery business status are separate from Audit State and Audit Status.
5. Each stage has independent:
   - Business Status;
   - Audit State: `NOT_STARTED | IN_PROGRESS | COMPLETE`;
   - Audit Status: `NOT_EVALUATED | NO_FLAGS | FLAGS_RAISED`.
6. `FLAGS_RAISED` is historical/sticky at stage level once a valid flag has existed.
7. Machine and human flags use one canonical register.
8. PC/TL/PM/Executive may raise flags; TL/PM normally review and resolve; Executive has all Phase-1 flag privileges. Authorization remains configurable/server-enforced.
9. Delivery business lifecycle is exactly:

```text
DELIVERY_STARTED
      -> DELIVERY_IN_PROGRESS
      -> DELIVERY_COMPLETED
```

There is no Delivery Closed state.

10. Booking may still be `BOOKING_IN_PROGRESS` when Delivery starts; the Delivery event is accepted and Audit Core raises the appropriate machine flag.
11. VIN/chassis reconciliation belongs to Audit Core Rule Engine, never client logic.
12. DI owns extraction/provenance only; DI does not own Booking/Delivery state, rule outcome, or audit decision.
13. Post-Delivery reconciliation is out of Phase-1 implementation scope.
14. Android phone is the primary PC UX target, Android tablet second, desktop Web third.
15. Existing approved Verigence brand asset must be reused; mockup logo placeholders are not implementation assets.

---

# 3. Repositories and branch rule

## 3.1 Repositories

| Repository | Responsibility |
|---|---|
| `verigence-audit-core` | authoritative workflow, state, rules, flags, project context, aggregate reads |
| `verigence-di` | document classification/extraction/confidence/provenance |
| `verigence-web` | Web + Capacitor Android UX |
| `verigence-security` | no UC03 change unless a concrete missing authorization/context capability is proven |

## 3.2 Existing planning branches

The current UC03 planning/design work is on:

```text
planning/uc-003-booking-delivery-audit
```

in Audit Core, DI and Web.

Frozen planning baselines:

| Module | Frozen baseline |
|---|---|
| Audit Core | `dev@082cc2ada5cd934bf0707ccae945667feb3f6e37` |
| DI | `dev@c97b3f3e5f8577160c88af1080496808189206fb` |
| Web/Android | `dev@2c98f753ed1428c0d5f7a0b7144169d528a5bb78` |

## 3.3 Implementation branch policy

The implementation SHALL use **one UC03 implementation branch per repository**, all with the same logical name:

```text
work/uc-003-booking-delivery-audit
```

Each implementation branch is created once from the approved UC03 planning head for that repository.

No sub-feature branches are created for Booking, Delivery, Audit, Project Context, Android, DI, or review.

All implementation commits are sequential on the UC03 branch.

If the team elects to continue implementation directly on the existing `planning/uc-003-booking-delivery-audit` branch instead, that must be a deliberate branch-policy decision before code starts; do not mix semantics accidentally. The preferred execution name remains `work/uc-003-booking-delivery-audit` because planning-only history remains distinguishable from implementation history while still satisfying the one-branch UC03 rule.

---

# 4. Canonical design set

Implementation must begin by reading in this order:

1. `UC03_SOLUTION_DESIGN_v1.1.md`
2. `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`
3. `UC03_RULE_FLAG_CATALOG_v1.0.md`
4. `UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`
5. `UC03_RECONCILIATION_DECISIONS_v1.0.md`
6. `UC03_IMPLEMENTATION_DESIGN_v0.1.md`
7. this handoff

Cross-module supporting documents:

### DI

`verigence-di/docs/uc-003-booking-delivery-audit/UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md`

### Web/Android

- `UC03_UX_FLOW_CONTRACT_v0.1.md`
- `UC03_UX_REVIEW_NOTES_v0.2.md`
- `UC03_ANDROID_WEB_MOCKUPS_v0.1.html`

The UX Review Notes override any conflicting first-pass mockup detail.

---

# 5. Execution discipline

Every checkpoint follows this loop:

```text
1. inspect current UC03 branch head
2. implement only checkpoint scope
3. run module tests/typechecks/builds
4. run cross-module contract tests needed for checkpoint
5. deploy DEV if useful for validation
6. review against checkpoint acceptance list
7. document exact completed files/APIs/migrations
8. commit checkpoint closure note
9. only then start next checkpoint
```

Rules:

- no opportunistic future-checkpoint implementation;
- no redesign of Security unless a demonstrated gap blocks the checkpoint;
- no hard-coded business rules in React;
- no hidden client-side status transitions;
- no silent extraction overwrite;
- no raw technical/internal messages in user-facing UI;
- no Post-Delivery processor work in Phase 1.

---

# 6. Checkpoint 0 — Shared Foundation + Project Context

This is intentionally small. It exists because Booking, Delivery and Audit all depend on the same Project context and stage-state/event primitives.

It is **not a separate branch** and should be closed quickly before Booking begins.

## 6.1 Audit Core scope

Implement:

- current-user Project discovery from active `business_assignments` + `projects`;
- `GET /v1/me/projects`;
- selected-Project-safe role/scope read model;
- latest-10 UC03 work list;
- date/date-range filters;
- max 10 rows per page;
- cursor/keyset paging;
- Project timezone date semantics;
- base stage-state persistence;
- immutable workflow-event infrastructure;
- aggregate version/idempotency conventions required by later commands.

Existing code to inspect/reuse first:

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

Proposed new UC03 service/API files may be introduced only if they fit repository conventions; examples:

```text
src/audit_core/uc03_context.py
src/audit_core/uc03_read.py
src/audit_core/uc03_workflow.py
src/audit_core/uc03_api.py
```

Do not force these names if existing module patterns make another placement cleaner.

## 6.2 Migration scope

Audit Core migration starts after current `0009` migration.

Proposed first migration name:

```text
0010_uc03_workflow_foundation.py
```

Expected semantic contents:

- `journey_stage_states`;
- `journey_workflow_events`;
- indexes/constraints/RLS required for Project-scoped reads and immutable events;
- no destructive replacement of existing Journey/Booking/Delivery tables.

If implementation review shows one migration is too broad, split sequentially as `0010`, `0011`, etc., but keep them within the same UC03 branch/checkpoint.

## 6.3 Web/Android scope

Implement:

- conditional Project selection after login;
- 0 Project safe state;
- 1 Project auto-select;
- multiple Project chooser;
- Switch Project;
- selected Project + operating role context;
- clear Dealer/Outlet/case/query cache on Project switch;
- UC03 landing metrics:
  - Bookings In Progress;
  - Delivery In Progress;
  - Needs Attention;
  - Audit Flags;
- Latest Bookings & Deliveries list;
- latest 10 by default;
- All / Bookings / Deliveries filter;
- date selector/date range;
- Previous/Next paging;
- approved `src/assets/verigenceLockup.ts` brand asset.

Existing structures to inspect/reuse first:

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

## 6.4 DI scope

None, except contract awareness.

## 6.5 Security scope

Verify UC02 assignment synchronization is reliable enough for Audit Core `/me/projects`.

Do not change Security unless this verification fails.

## 6.6 Acceptance gate

Checkpoint 0 closes only when:

- one Project auto-select works;
- multiple Projects require selection;
- same user may show different operating role by Project;
- Project switch cannot leak prior Project state/data;
- latest list returns no more than 10 cases;
- Delivery metric says `Delivery In Progress`, never `Delivery Today`;
- date filter respects Project timezone;
- phone/tablet/Desktop use approved logo;
- no Booking/Delivery mutation is implemented yet.

---

# 7. Checkpoint 1 — Booking

Booking is the first complete business vertical slice.

Do not start Delivery-specific capture beyond the minimal event contract required to test the incomplete-Booking-at-Delivery behavior.

## 7.1 Booking business lifecycle

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
BOOKING_CLOSED
BOOKING_CANCELLED
DUPLICATE_BOOKING
```

Booking close reason is configuration-driven and requires remarks support.

Working reason catalogue:

- Customer Cancelled;
- Finance Not Approved;
- Vehicle Unavailable;
- Customer Shifted Dealer;
- Dealer Cancelled;
- Duplicate Booking;
- Other.

Duplicate Booking produces both:

```text
Business Status = DUPLICATE_BOOKING
Audit Flag = DUPLICATE_BOOKING
```

## 7.2 Audit Core Booking scope

Implement:

- Booking command handlers;
- Booking stage projection updates;
- Booking Audit State/Audit Status;
- dynamic Booking document applicability;
- Booking document assessment `YES | NO | NA | UNANSWERED`;
- document applicability reason/history;
- Booking capture writes routed to existing typed domains;
- extraction proposal acceptance/correction provenance;
- Booking rules from canonical Rule/Flag catalog;
- Booking close/cancel/duplicate behavior;
- incomplete Booking checkpoint summary;
- aggregate UC03 workspace Booking section;
- processing-status endpoint/snapshot needed by client.

Existing Audit Core modules to reuse/extend first:

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

## 7.3 DI Booking scope

Implement only reconciled Booking document/extraction profiles.

Requirements:

- use the 57-field mapping classifications;
- only `SUPPORTED` and approved `PROVISIONAL` mappings become active;
- `TBD` remains disabled;
- preserve source-specific facts;
- preserve machine confidence/provenance;
- never let processing order determine source precedence;
- no Aadhaar extraction/raw-retention by assumption.

## 7.4 Web/Android Booking scope

Implement mobile-first:

- Create Booking / open Booking;
- Booking Started/In Progress state;
- document upload first;
- per-document processing state;
- PC continues own fields during DI extraction;
- extraction proposal group;
- bulk accept clean values;
- individual accept/correct low-confidence/variance values;
- dynamic requirements;
- Booking flags summary;
- PC manual flag creation;
- Booking verification/checkpoint summary;
- Booking conclusion sheet with dropdown + remarks;
- duplicate Booking handling;
- phone + tablet + desktop responsive layouts.

## 7.5 Mandatory Booking tests

At minimum:

- Booking starts idempotently;
- extraction delay does not block PC work;
- clean proposal accepts correctly;
- corrected proposal preserves machine original;
- dynamic document requirement appears when driving fact changes;
- missing required item keeps Booking audit In Progress;
- Booking can close through approved close path;
- duplicate Booking creates status + flag;
- Booking Cancelled/no-delivery path records reason + remarks;
- no raw DI/provider error reaches UI;
- Android background/resume refreshes extraction safely.

## 7.6 Booking acceptance gate

Booking closes only when the complete Booking vertical slice works end to end on DEV:

```text
Web/Android -> Audit Core -> DI -> Audit Core -> Web/Android
```

and its test pack passes.

Only then begin Delivery.

---

# 8. Checkpoint 2 — Delivery

Delivery begins only after Booking checkpoint is signed off.

## 8.1 Delivery lifecycle

Exactly:

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
```

No Delivery Closed state exists.

## 8.2 Critical progression invariant

When dealer Delivery begins while Booking is incomplete:

```text
accept DELIVERY_STARTED
preserve Booking = IN_PROGRESS if still incomplete
raise MACHINE flag for incomplete Booking prerequisites at Delivery
continue Delivery capture normally
```

No audit rule may turn that real Delivery Start into a client/server business rejection.

## 8.3 Audit Core Delivery scope

Implement:

- Delivery Start/Complete commands;
- Delivery stage projection;
- Delivery Audit State/Audit Status;
- Delivery intimation capture;
- dynamic Delivery document assessments;
- Delivery/payment/registration/vehicle facts using existing typed domains;
- vehicle-photo evidence linkage;
- VIN Rule Engine input/result integration;
- payment verification rules;
- non-intimation rules;
- Delivery rule/flag execution;
- incomplete-Booking-at-Delivery automatic flag;
- Delivery workspace section;
- physical Delivery Completed while audit may remain In Progress.

Existing modules to inspect/reuse first:

```text
src/audit_core/vehicle_delivery.py
src/audit_core/payments_finance.py
src/audit_core/evidence.py
src/audit_core/evidence_read.py
src/audit_core/findings.py
src/audit_core/audit_evaluation.py
src/audit_core/workflow.py
```

## 8.4 DI Delivery scope

Only Delivery evidence/extraction that is reconciled in the canonical mapping.

DI supplies identifiers/facts; it does not decide VIN match/compliance.

## 8.5 Web/Android Delivery scope

Implement:

- Delivery Started/In Progress/Completed;
- visible notice when Booking audit remains incomplete;
- `Continue Delivery` remains available;
- Delivery document checklist;
- Yes/No/NA controls;
- Android camera/photo capture;
- VIN/photo evidence UX;
- payment capture/verification presentation;
- Delivery flags;
- Delivery Completed confirmation;
- screen remains usable when Delivery audit is still In Progress after physical completion;
- tablet/Desktop adaptations from same components.

## 8.6 Mandatory Delivery tests

At minimum:

- Delivery Start after clean Booking;
- Delivery Start while Booking incomplete;
- auto-flag raised, Delivery still accepted;
- No document answer raises configured flag without app failure;
- non-intimated Delivery records reason/flag and continues;
- VIN mismatch raises configured critical flag while Delivery remains recordable;
- payment mismatch raises flag;
- vehicle photos attach to correct requirement;
- Delivery Completed may coexist with Audit State IN_PROGRESS;
- late evidence after physical completion preserves true timestamp.

## 8.7 Delivery acceptance gate

Delivery closes as an implementation checkpoint only when:

- all business statuses behave correctly;
- non-blocking dealer progression invariant is proven by automated tests;
- Android phone UAT passes camera/document flow;
- tablet/Desktop layouts remain coherent;
- Booking behavior has not regressed.

Only then begin Audit/Review.

---

# 9. Checkpoint 3 — Audit / Review / Hardening

This checkpoint finalizes the cross-stage audit experience. It does not introduce Post-Delivery reconciliation.

## 9.1 Audit Core scope

Implement/complete:

- canonical machine/human flag provenance;
- `audit_findings` UC03 extensions;
- append-only finding events;
- manual flag creation;
- remarks/evidence on flags;
- acknowledge/review/resolve/reopen/void actions as allowed;
- TL/PM/Executive policy enforcement;
- configurable Audit State completion policy;
- sticky stage Audit Status behavior;
- cross-stage flag summaries;
- UC03 timeline/history;
- migration/backfill reporting;
- permission matrix verification;
- aggregate read performance/index tuning;
- operability/telemetry.

Existing modules to reuse/extend:

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

## 9.2 Web/Android scope

Implement/complete:

- Audit Flag register by Booking/Delivery stage;
- PC/TL/PM/Executive raise flow;
- TL/PM review/resolve flow;
- Executive full Phase-1 flag actions;
- history/timeline;
- stage Audit State/Audit Status presentation;
- no technical/internal messages;
- final Android/tablet/Desktop responsive sweep.

## 9.3 Mandatory Audit tests

At minimum:

- PC human flag;
- TL review/resolve;
- PM review/resolve;
- Executive full privilege path;
- Audit State COMPLETE + FLAGS_RAISED is valid when policy permits;
- resolved flags do not rewrite historical stage status to NO_FLAGS;
- stale aggregate version does not overwrite newer review state;
- repeated idempotency key does not duplicate events/flags;
- Project isolation and role switching remain correct;
- no unmasked Aadhaar/internal IDs/raw backend errors in ordinary UI.

## 9.4 Audit acceptance gate

Checkpoint closes when full UC03 E2E scenario pack passes on DEV and the cross-stage review history is internally consistent.

---

# 10. Migration plan

Do not create all migrations up front before the owning checkpoint is understood.

Recommended sequence on the same UC03 branch:

```text
0010_uc03_workflow_foundation.py        Checkpoint 0
0011_uc03_booking_capture.py            Checkpoint 1
0012_uc03_delivery_capture.py           Checkpoint 2
0013_uc03_audit_flag_events.py          Checkpoint 3
0014_uc03_backfill_indexes.py           Checkpoint 3 if needed
```

Names are proposed, not a license to create unnecessary tables. Reuse the existing physical model wherever possible.

Every migration must have:

- forward path;
- rollback/downgrade where safe;
- RLS/tenant isolation review;
- index review;
- data/backfill verification query;
- no fabricated historical timestamps.

---

# 11. API plan

Canonical OpenAPI file to update:

```text
verigence-audit-core/api/openapi-v1.yaml
```

Checkpoint 0 APIs:

```text
GET /v1/me/projects
GET /v1/tenants/{tenantId}/uc03/work-items
```

Checkpoint 1 APIs:

```text
Booking start/conclude/cancel/duplicate commands
Booking document assessment
capture/proposal accept/correct
UC03 workspace
processing-status
```

Checkpoint 2 APIs:

```text
Delivery start
Delivery complete
Delivery document/evidence/capture additions
```

Checkpoint 3 APIs:

```text
flags list/create
remarks/evidence
acknowledge/review/resolve/reopen/void
history/timeline as required
```

OpenAPI contract must be updated in the same checkpoint as implementation, not after the fact.

---

# 12. Commit/checkpoint discipline

Keep commits small and checkpoint-labelled.

Recommended prefix examples:

```text
uc03(foundation): ...
uc03(booking): ...
uc03(delivery): ...
uc03(audit): ...
uc03(android): ...
uc03(di): ...
uc03(test): ...
```

At each checkpoint end, commit/update a status note such as:

```text
docs/uc-003-booking-delivery-audit/status/UC03_CHECKPOINT_1_BOOKING.md
```

That note records:

- branch HEADs across touched repos;
- migrations applied;
- APIs completed;
- UI screens completed;
- tests passed/failed;
- DEV deployment reference;
- known issues;
- explicit deferred items;
- approval to start next checkpoint.

This is the primary mechanism for preventing context loss during a long UC03 implementation.

---

# 13. DEV and deployment sequence

Within each checkpoint:

```text
local/module tests
   -> contract tests
   -> DB migration verification
   -> Audit Core DEV deploy
   -> DI DEV deploy if DI changed
   -> Web DEV deploy
   -> Android debug APK when mobile behavior changed materially
   -> checkpoint UAT
```

Do not merge partial UC03 to `dev` merely to move faster unless an explicit integration decision is made.

The preferred approach is one coherent UC03 integration branch per repo, validated sequentially, then promoted to `dev` when UC03 Phase 1 is accepted.

---

# 14. Full Phase-1 acceptance scenarios

The canonical Implementation Design contains the complete mandatory E2E list. The handoff groups them as follows:

### Foundation

- Project auto-select/multi-select/no-assignment;
- role differs by Project;
- Project switch isolation;
- latest 10 + pagination/date/timezone;
- approved logo.

### Booking

- create/start;
- asynchronous extraction while PC works;
- proposals/correction;
- document applicability;
- duplicate;
- close/cancel with remarks;
- user-safe errors.

### Delivery

- clean Delivery start;
- Delivery start with incomplete Booking;
- non-intimation;
- No document answer;
- VIN mismatch rule result;
- payment issue;
- Android photo evidence;
- physical Delivery Completed while audit remains In Progress.

### Audit

- human/machine flags;
- PC/TL/PM/Executive privilege paths;
- resolve/reopen history;
- sticky stage Audit Status;
- concurrency/idempotency;
- history/provenance/privacy.

---

# 15. Explicitly out of scope for UC03 Phase 1

- Post-Delivery weekly/monthly reconciliation engine;
- D+7/D+12/D+90 schedulers;
- final Delivery Success/Failure status;
- iOS packaging/testing;
- redesign of Security v2;
- redesign of DI architecture;
- a second generic 123-field source-of-truth table;
- client-side VIN rule;
- client-side authorization enforcement;
- replacing the approved Verigence shell/brand system;
- per-document polling storms/SSE unless real performance evidence later requires it.

---

# 16. Open decisions that may be finalized during UAT

The following remain controlled, explicit decisions rather than hidden assumptions:

1. final 26-vs-29 business document catalogue reconciliation;
2. remaining `PROVISIONAL`/`TBD` DI extraction-source mappings;
3. exact VIN/chassis normalization/match algorithm;
4. exact high/critical flag review requirements before Audit State may become COMPLETE;
5. exact Booking/Delivery business-date precedence for date filters;
6. exact existing-vs-new permission key mapping;
7. confirmation that UC02 assignment synchronization is sufficient for `/me/projects`;
8. exact historical Journey backfill scope.

None of these may be guessed in React or DI profiles.

---

# 17. Definition of Done — UC03 Phase 1

UC03 Phase 1 is done only when:

- all four sequential checkpoints are closed;
- Booking and Delivery workflow/status models match canonical design;
- no dealer progression is blocked by audit non-compliance;
- machine and human flags share one canonical model;
- Project selection/switching works correctly for PC/TL/PM/Executive;
- latest-10/date-filter/paging work per Project;
- DI extraction proposals preserve provenance and never silently overwrite accepted data;
- Booking and Delivery mobile flows work on physical Android devices;
- Web/tablet layouts are usable and aligned with approved Verigence framework;
- approved Verigence logo asset is used everywhere;
- required automated tests and DEV/UAT scenario pack pass;
- migrations/backfill are validated;
- no technical/internal messages or sensitive unmasked identifiers are exposed to ordinary users;
- Post-Delivery remains out of Phase-1 runtime scope;
- final checkpoint status notes contain exact branch heads, deployed versions and known deferred decisions.

---

# 18. Immediate implementation start point after approval

Start only with **Checkpoint 0 — Shared Foundation + Project Context** on the single UC03 branch.

Do not start Booking capture until Checkpoint 0 acceptance criteria pass.

Then execute strictly:

```text
Checkpoint 0 Foundation
        ->
Checkpoint 1 Booking
        ->
Checkpoint 2 Delivery
        ->
Checkpoint 3 Audit / Review / Hardening
```

All on the same UC03 branch per repository.
