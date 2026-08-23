# Verigence UC03 — Booking & Delivery Audit — Consolidated Solution Design

**Document ID:** `VUC03-SD-002`  
**Version:** `1.1`  
**Status:** DRAFT FOR BUSINESS & IMPLEMENTATION REVIEW  
**Date:** 2026-08-22  
**Supersedes:** `VUC03-SD-001 v1.0` for current UC03 planning decisions  
**Primary owner:** Audit Core  
**Affected modules:** Audit Core, DI, Web/Android  
**Security:** impact assessment only unless a concrete permission gap is found  
**Post-Delivery reconciliation:** reserved structurally; out of Phase-1 scope

---

## 0. Purpose

UC03 is the primary and largest operational audit use case for Verigence. It follows one vehicle-sale case from Booking through Delivery using one immutable internal `journey_id`, while the user-facing product speaks in business language: **Booking**, **Delivery**, **Audit Flags**, **Review**, and **Completed**.

The central rule is unchanged:

> **Verigence records the dealer's real business process; it never rejects or aborts that real progression merely because audit prerequisites are incomplete or a rule has failed.**

If Delivery starts before Booking prerequisites are completed, Verigence records Delivery and raises an audit flag that snapshots the incomplete Booking conditions at that moment.

---

## 1. Source basis and precedence

UC03 is grounded in the supplied business material:

- `PC Evidence Capture Process — Booking and Delivery` — Booking/Delivery sequence, document applicability, capture timing, exceptions, extraction delay and human-vs-document capture principles.
- `Web Capture Field List.xlsx` — complete 123-field inventory and capture-mode split.
- `SPR_Tool_Process_SubProcess_Activity_Details.xlsx` — operational process/rule candidates, duplicate, validation, payment and exception controls.
- existing Audit Core, DI and Web/Android runtime/design baseline.

Current UC03 business decisions supersede conflicting earlier source assumptions. In particular:

1. audit conditions do not block real dealer progression;
2. VIN reconciliation belongs to the Rule Engine and may raise/escalate a flag, but the app still records a real Delivery event;
3. PC, TL, PM and Executive may raise flags;
4. TL and PM are the normal review/resolution roles; Executive has all Phase-1 flag privileges;
5. Post-Delivery reconciliation is out of scope for Phase 1;
6. the document catalogue is provisional until testing/UAT;
7. PC UI uses Booking/Delivery terminology, not Journey terminology;
8. Delivery has no `DELIVERY_CLOSED`, `DELIVERY_SUCCESS`, or `DELIVERY_FAILURE` state in Phase 1.

---

## 2. Frozen repository baselines

| Module | Repository | Baseline | UC03 planning branch |
|---|---|---|---|
| Audit Core | `verigence-audit-core` | `dev@082cc2ada5cd934bf0707ccae945667feb3f6e37` | `planning/uc-003-booking-delivery-audit` |
| DI | `verigence-di` | `dev@c97b3f3e5f8577160c88af1080496808189206fb` | `planning/uc-003-booking-delivery-audit` |
| Web/Android | `verigence-web` | `dev@2c98f753ed1428c0d5f7a0b7144169d528a5bb78` | `planning/uc-003-booking-delivery-audit` |

No UC03 production code, DDL migration, native implementation or mockup implementation is authorized by this planning document.

---

## 3. Terminology boundary

Internally, Audit Core retains the existing Journey aggregate and immutable `journey_id` because existing Booking, Delivery, evidence, findings and audit structures already attach to it.

PC-facing UI SHALL NOT show `Journey`, `Journey Workspace` or `Journey Stage` as the user's operating concept.

PC-facing language is:

- Create Booking
- Booking Started
- Booking In Progress
- Booking Closed
- Booking Cancelled
- Duplicate Booking
- Delivery Started
- Delivery In Progress
- Delivery Completed
- Audit Flags
- Review

The same internal ID follows the case from Booking into Delivery.

---

## 4. Non-negotiable business invariants

### INV-01 — one immutable internal case

Booking and Delivery are stages/projections of the same aggregate. They are not independent cases.

### INV-02 — audit never blocks reality

A failed rule, missing document, unreadable evidence, incomplete prerequisite, non-intimation, payment discrepancy, VIN mismatch or open flag SHALL NOT cause Audit Core to reject the recording of a real dealer progression event solely because it is non-compliant.

Commands may still be rejected for authorization, identity, idempotency, concurrency or impossible sequence reasons.

### INV-03 — progression with incomplete prior work creates a flag

If Delivery starts while Booking is not normally closed for Delivery, Audit Core records `DELIVERY_STARTED`, snapshots the incomplete Booking prerequisites, raises a machine flag, and leaves Booking in its existing state until its own work is completed or otherwise concluded.

### INV-04 — business status, Audit State and Audit Status are separate

A Delivery may be physically completed while its Delivery Audit State remains `IN_PROGRESS`. A Booking may be closed and still have `FLAGS_RAISED`.

### INV-05 — machine and human flags share one register

PC, TL, PM, Executive and machine rules all create records in the same canonical flag/finding model, with origin and actor metadata.

### INV-06 — extraction is proposal-based

DI-extracted values arrive as proposals with provenance. They never silently overwrite a PC-entered or previously accepted value.

### INV-07 — owning domains remain authoritative

UC03 does not duplicate all 123 fields into a new generic authoritative table. Existing typed Audit Core domains remain the source of truth; UC03 adds a normalized resolver/read model where rules or UI need one consistent view.

### INV-08 — Android phone/tablet first

The PC experience is designed first for Android phone, then Android tablet, then desktop Web.

### INV-09 — technical language stays out of the UI

Raw backend errors, provider names, internal IDs and implementation messages are not user-facing.

---

## 5. Stage model

Phase 1 contains two active business stages and one reserved future stage:

```text
BOOKING
DELIVERY
POST_DELIVERY   [reserved only; reconciliation out of scope]
```

Booking and Delivery are allowed to overlap.

Example valid state:

```text
Booking Business Status  = BOOKING_IN_PROGRESS
Booking Audit State      = IN_PROGRESS
Booking Audit Status     = FLAGS_RAISED

Delivery Business Status = DELIVERY_STARTED
Delivery Audit State     = IN_PROGRESS
Delivery Audit Status    = NOT_EVALUATED
```

---

## 6. Booking business status

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
BOOKING_CLOSED
BOOKING_CANCELLED
DUPLICATE_BOOKING
```

### BOOKING_STARTED

The Booking exists and PC work has started.

### BOOKING_IN_PROGRESS

Booking capture, documents, extraction review or prerequisite work remains open.

This state may continue after Delivery starts. That condition raises an audit flag but does not block Delivery.

### BOOKING_CLOSED

The Booking phase has been formally concluded. Closure has one of two dispositions:

```text
PROCEED_TO_DELIVERY
NO_DELIVERY
```

Normal ready-for-Delivery closure may require the configured Booking completion policy to be satisfied because this command is Verigence asserting that its Booking stage work is complete. This is different from refusing a later real Delivery event.

### BOOKING_CANCELLED

The underlying Booking ended before Delivery for a configured cancellation reason.

### DUPLICATE_BOOKING

The record is identified as a duplicate. The status becomes `DUPLICATE_BOOKING` and a duplicate flag is raised automatically.

### Default Booking close/cancel reasons

The reason catalogue is configuration-driven. Phase-1 defaults:

```text
CUSTOMER_CANCELLED
FINANCE_NOT_APPROVED
VEHICLE_UNAVAILABLE
CUSTOMER_SHIFTED_DEALER
DEALER_CANCELLED
DUPLICATE_BOOKING
OTHER
```

The UI uses a dropdown plus Remarks. `OTHER` requires remarks by default. Duplicate Booking always raises a flag.

---

## 7. Delivery business status — revised v1.1

Phase-1 Delivery business status is now deliberately limited to:

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
```

There is **no** `DELIVERY_CLOSED` state.

Before `DELIVERY_STARTED`, Delivery Not Started is a derived UI condition rather than a persisted enum.

### DELIVERY_STARTED

The dealer Delivery process has begun or the PC has reached/identified the Delivery event.

It is valid even if Booking remains `BOOKING_STARTED` or `BOOKING_IN_PROGRESS`.

### DELIVERY_IN_PROGRESS

Material Delivery capture is underway: documents, photos, payment verification, witness answers, manual observations or Delivery-stage audit work.

### DELIVERY_COMPLETED

The physical Delivery event has happened and the Delivery business stage has reached its terminal business status for Phase 1.

`DELIVERY_COMPLETED` does **not** imply:

- Delivery audit complete;
- no flags;
- all documents available;
- payment verified;
- VIN rule passed;
- TL/PM review complete.

A valid record is:

```text
Delivery Business Status = DELIVERY_COMPLETED
Delivery Audit State     = IN_PROGRESS
Delivery Audit Status    = FLAGS_RAISED
```

After physical Delivery completion, Verigence may continue evidence collection, rule evaluation, remarks and review without inventing a second Delivery business terminal state.

---

## 8. Audit State — independently per stage

For Booking, Delivery and reserved Post Delivery:

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

Audit State answers: **How far has Verigence's audit work for this stage progressed?**

- `NOT_STARTED` — no material stage audit activity.
- `IN_PROGRESS` — capture/evidence/evaluation work is active or incomplete.
- `COMPLETE` — the configured stage audit completion policy has been satisfied.

Audit State is not a compliance result. `COMPLETE + FLAGS_RAISED` is valid.

The initial Phase-1 design direction is that Audit State completion is based on required audit work/evaluation completion, not on every flag being resolved. Flag review/resolution remains its own lifecycle. The detailed completion criteria are finalized in the Rule/Flag Catalog and implementation design.

---

## 9. Audit Status — independently per stage

```text
NOT_EVALUATED
NO_FLAGS
FLAGS_RAISED
```

Audit Status answers: **What did the stage audit discover?**

`FLAGS_RAISED` is sticky historical truth. Resolving all flags does not relabel the stage `NO_FLAGS`.

Example:

```text
Audit State   = COMPLETE
Audit Status  = FLAGS_RAISED
Open Flags    = 0
Resolved      = 3
```

---

## 10. Workflow Manager

Workflow Manager is an Audit Core capability, not a browser state machine.

It coordinates:

```text
Business command/event
        |
        v
authorization / scope / idempotency / concurrency
        |
        v
persist real event and project business state
        |
        v
evaluate stage/progression rules
        |
        v
raise/update flags
        |
        v
refresh Audit State / Audit Status / aggregate read model
```

A rule result never rolls back an already accepted real-world progression event.

The detailed command/event contract is `VUC03-WF-002 / UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`.

---

## 11. Workflow event families

### Business progression

```text
BOOKING_STARTED
BOOKING_CLOSED
BOOKING_CANCELLED
BOOKING_MARKED_DUPLICATE
DELIVERY_STARTED
DELIVERY_COMPLETED
```

`DELIVERY_IN_PROGRESS` is normally a projection transition caused by first material Delivery work; an explicit event may be retained for audit history if implementation design finds value.

### Capture/evidence

Examples:

```text
DOCUMENT_UPLOADED
DOCUMENT_ANSWERED_YES
DOCUMENT_ANSWERED_NO
DOCUMENT_MARKED_NA
DOCUMENT_PROCESSING_UPDATED
EXTRACTION_PROPOSAL_AVAILABLE
EXTRACTION_ACCEPTED
EXTRACTION_CORRECTED
MANUAL_FIELD_RECORDED
PHOTO_CAPTURED
PAYMENT_RECORDED
WITNESS_RESPONSE_RECORDED
```

### Audit/flag

```text
AUDIT_STAGE_STARTED
AUDIT_EVALUATED
AUDIT_STAGE_COMPLETED
FLAG_RAISED
FLAG_REVIEWED
FLAG_RESOLVED
FLAG_REOPENED
FLAG_VOIDED
FLAG_REMARK_ADDED
FLAG_EVIDENCE_ADDED
```

---

## 12. Flags / anomalies

UC03 extends the existing Journey-scoped Audit Core Finding architecture rather than creating a second disconnected anomaly system.

Each flag must be able to preserve:

```text
journey_id
stage_code                BOOKING | DELIVERY | POST_DELIVERY
origin_kind               MACHINE | HUMAN
origin_actor_id            nullable for MACHINE
origin_role_snapshot       PC | TL | PM | EXECUTIVE | SYSTEM
rule_key                   nullable for pure human observation
rule_version_id            nullable
severity
finding/flag status
expected_summary
observed_summary
source/evidence links
created_at
review/resolution history
correlation_id
```

Phase-1 authority defaults:

| Action | PC | TL | PM | Executive |
|---|---:|---:|---:|---:|
| Raise | Yes | Yes | Yes | Yes |
| Add remark/evidence | Yes | Yes | Yes | Yes |
| Review/Acknowledge | No default | Yes | Yes | Yes |
| Resolve | No default | Yes | Yes | Yes |
| Reopen | No default | Yes | Yes | Yes |
| Void/Reclassify | No default | Configurable | Configurable | Yes |

This matrix must remain policy/permission driven, not hard-coded in React.

The detailed rule and flag definitions are maintained in `VUC03-RF-001 / UC03_RULE_FLAG_CATALOG_v1.0.md`.

---

## 13. Rule Engine boundary

Audit Core owns business rule evaluation.

DI owns document intelligence: upload/storage integration, quality/readability where supported, classification, extraction, confidence and provenance.

Web/Android owns capture UX only.

VIN/chassis reconciliation is explicitly a Rule Engine concern. The Android/Web app captures/provides observed values and renders the result; it does not implement matching logic.

Rule trigger families for Phase 1:

```text
ON_FIELD_CHANGE
ON_DOCUMENT_RESULT
ON_STAGE_EVENT
ON_CHECKPOINT_EVALUATION
ON_MANUAL_REQUEST
```

Scheduled Post-Delivery triggers are out of scope.

---

## 14. Document applicability and evidence

Document requirements are dynamic. The checklist is generated from Journey attributes rather than showing every possible document with habitual `NA` answers.

A document requirement assessment can conceptually record:

```text
journey_id
stage_code
requirement_key
profile_version_id
applicability_state
applicability_reason
answer                   YES | NO | NA | UNANSWERED
evidence_id              nullable
answered_by_actor_id     nullable
answered_by_role         nullable
remarks                  nullable
version_no
```

`NO` is a valid audit answer. It may raise a flag; it is not a UI/system failure.

The current provisional document catalogue contains 29 numbered entries from the supplied applicability diagram although the source prose repeatedly refers to 26 documents. That discrepancy is intentionally retained for UAT reconciliation rather than silently corrected.

The complete provisional catalogue and 123-field inventory are maintained in `VUC03-FM-001 / UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`.

---

## 15. 123-field ownership principle

The source field inventory contains exactly 123 fields with the following capture split:

```text
PC types            36
Extracted           57
System / computed   27
Upload                3
TOTAL               123
```

UC03 will not make the browser authoritative for these fields.

Ownership direction:

- `PC types` -> validated human input persisted by the owning Audit Core domain;
- `Extracted` -> DI proposal with source/confidence, explicitly accepted/corrected into the owning Audit Core domain;
- `System / computed` -> Audit Core/master/calculation-derived value;
- `Upload` -> Audit Core evidence association with DI document processing/storage boundary.

The existing field `Status` (#90) is not retained as a PC-controlled Booking/Delivered switch; it is replaced by Workflow Manager state.

The existing observation rows (#117-123) are remapped to the UC03 flag/review model rather than retained as a parallel observation subsystem.

---

## 16. Extraction latency and read model

The source process expects meaningful document-processing latency. UX shall therefore be upload-first and work-while-processing.

The browser never polls DI directly. Audit Core exposes a cheap Journey/Booking-scoped aggregate processing snapshot containing per-document status and proposal readiness.

Initial direction remains adaptive short polling:

- one endpoint per case, not one per document;
- stop when no processing remains;
- pause when hidden/backgrounded;
- poll immediately on focus/reconnect;
- transport hidden behind a frontend hook so SSE can be introduced later without redesigning screens.

---

## 17. Mobile-first UX direction before mockups

The mockup phase follows this order:

```text
Android phone
Android tablet
Desktop Web
```

The same business workflow and backend contract serve all three.

PC mockups will prioritize:

- My Work / Booking lists;
- Create/Open Booking;
- Booking capture while extraction runs;
- extracted proposal acceptance/correction;
- dynamic documents;
- machine and human flag capture;
- Booking close/cancel/duplicate flow;
- Delivery start even with incomplete Booking;
- automatic progression flag presentation;
- Delivery documents;
- Android camera/car photos;
- payments;
- witness questions;
- Delivery completion;
- ongoing audit work after Delivery completion;
- TL/PM/Executive review views;
- full history/timeline.

Desktop Web will adapt the same components rather than define a second workflow.

---

## 18. Post-Delivery

Post-Delivery is structurally reserved but not activated in Phase 1.

Out of scope:

- D+7/D+12 payment monitoring;
- weekly/monthly reconciliation;
- trade-in ageing processor;
- CRM recurring controls;
- scheduled post-Delivery rule execution.

No empty Post-Delivery PC UI is required in the Phase-1 mockups.

---

## 19. Cross-module ownership

### Audit Core — major owner

- immutable case/Journey identity;
- Booking and Delivery business state;
- per-stage Audit State and Audit Status;
- workflow commands/events;
- document applicability;
- evidence association;
- rule evaluation;
- machine/human flag register and lifecycle;
- aggregate Booking/Delivery workspace/read model;
- typed domain persistence and normalized fact resolver.

### DI — controlled support

- document intelligence;
- processing status;
- quality/readability where supported;
- classification;
- extraction;
- confidence;
- extraction provenance and correction lineage.

DI does not own Booking/Delivery state, Audit State/Status, rule outcome or flag resolution.

### Web/Android — major UX owner

- mobile/tablet/desktop presentation;
- PC capture workflow;
- Android camera interactions;
- extraction progress/proposals;
- document checklist;
- flag creation/remarks;
- review UI;
- user-safe messaging.

Web/Android does not own authoritative transitions or rule logic.

### Security — preserve existing authority

Security remains authoritative for identity and functional authorization. UC03 changes Security only if implementation design finds a concrete missing permission capability.

---

## 20. Current design package

The current UC03 planning source of truth is:

1. `UC03_SOLUTION_DESIGN_v1.1.md` — this consolidated solution design.
2. `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md` — explicit Workflow Manager contract.
3. `UC03_RULE_FLAG_CATALOG_v1.0.md` — rule/flag registry and authority/completion policies.
4. `UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md` — provisional document catalogue and complete field inventory.

Mockups come after review/reconciliation of these four documents.

---

## 21. Remaining controlled decisions before implementation design

The following are intentionally open or provisional:

1. exact automatic-vs-PC-confirmed normal Booking close UX;
2. final document catalogue reconciliation (source prose says 26, numbered diagram gives 29);
3. exact source-document-to-field extraction profile for every extracted field;
4. authoritative VIN 8-character/17-character reconciliation algorithm;
5. final severity and escalation defaults for candidate rules marked provisional;
6. final Audit State completion policy where capture is complete but review is still pending;
7. exact persistence shape/migration after current schema review;
8. permission catalogue impact after role-action contract is mapped to Security.

These are documented decisions, not permission to invent values in code.

---

## 22. Exit criteria before mockups

Mockup work begins once:

- Delivery lifecycle revision is accepted;
- Workflow Manager catalog is consistent with this design;
- Rule/Flag Catalog has no unresolved structural contradiction;
- all 123 fields are accounted for;
- provisional document catalogue is explicit;
- fields that must be replaced/remapped by UC03 are identified;
- Android-first information architecture can be derived without embedding rule logic in the client.

At that point the next deliverable is the complete **Android-first, tablet and desktop Web UC03 mockup pack**.