# Verigence UC03 — Booking & Delivery Audit Solution Design

**Document ID:** `VUC03-SD-001`  
**Version:** `1.0`  
**Status:** DRAFT FOR BUSINESS & IMPLEMENTATION REVIEW  
**Date:** 2026-08-22  
**Primary owner:** Audit Core  
**Affected modules:** Audit Core, DI, Web/Android  
**Security:** impact assessment only unless a concrete permission gap is found  
**Mockups:** deliberately deferred until this design is approved

---

## 0. Executive summary

UC03 is the principal operational audit use case for Verigence. It follows one vehicle-sale case from Booking through Delivery using one immutable internal `journey_id`, while allowing the user-facing product to speak only in business language such as **Booking**, **Delivery**, **Audit Flags**, and **Review**.

The central design rule is:

> **Verigence records the dealer's real business process; Verigence does not stop, abort, or reject that business process because an audit prerequisite or audit rule failed.**

If a dealer proceeds to Delivery while Booking prerequisites remain incomplete, Verigence SHALL record Delivery and SHALL raise one or more audit flags describing exactly what remained incomplete at the time of progression.

UC03 therefore separates four concerns that must never be collapsed into one status:

1. **Booking/Delivery business status** — what operational work has started, is in progress, completed, closed or cancelled.
2. **Audit State** — whether the audit work for a stage has not started, is in progress, or is complete.
3. **Audit Status** — whether a stage has not been evaluated, has no flags, or has raised flags.
4. **Flags/Anomalies** — individual machine- or human-raised audit exceptions with their own lifecycle, evidence, review and resolution history.

Post-Delivery reconciliation is structurally reserved but **out of Phase-1 scope**.

---

## 1. Source basis and precedence

### 1.1 Business source material

UC03 is based on the supplied business materials:

- **PC Evidence Capture Process — Booking and Delivery** — 26 documents / 123 fields, Booking and Delivery capture process, document applicability, timing, exceptions and extraction interaction principles.
- **Web Capture Mockups** — upload-first flow, per-document processing state, progressive extraction, extracted proposals, bulk acceptance, failure handling and Delivery checklist presentation.
- Current Verigence Web, Audit Core and DI runtime/design baseline.

### 1.2 Current business decisions that supersede earlier source assumptions

The following UC03 decisions are authoritative even where earlier process material differs:

1. Audit conditions do **not** block the dealer business process.
2. A VIN mismatch is a rule-engine flag/escalation condition; it does **not** cause the application to refuse to record a real Delivery event.
3. Humans may raise flags: PC, TL, PM and Executive.
4. Executive has all Phase-1 flag privileges.
5. Post-Delivery reconciliation controls are out of scope for this implementation phase.
6. The final document catalogue is provisional and will be reviewed during testing/UAT.
7. VIN reconciliation logic belongs to the Audit Core Rule Engine, not Web/Android.
8. PC-facing UI must use Booking/Delivery terminology, not the internal term Journey.

Where this document conflicts with older vehicle-sale Journey/audit design, this UC03 design is the intended direction for UC03 and the conflicting older behavior must be reconciled during implementation design before code is changed.

---

## 2. Repository and planning baseline

UC03 planning is isolated from current development by exact baseline commit.

| Module | Repository | Baseline branch | Baseline commit | UC03 planning branch |
|---|---|---|---|---|
| Audit Core | `verigence-audit-core` | `dev` | `082cc2ada5cd934bf0707ccae945667feb3f6e37` | `planning/uc-003-booking-delivery-audit` |
| DI | `verigence-di` | `dev` | `c97b3f3e5f8577160c88af1080496808189206fb` | `planning/uc-003-booking-delivery-audit` |
| Web/Android | `verigence-web` | `dev` | `2c98f753ed1428c0d5f7a0b7144169d528a5bb78` | `planning/uc-003-booking-delivery-audit` |

Security is not given a UC03 branch at this stage. Existing Security remains authoritative for identity and functional authorization. A Security change is allowed only if the final UC03 permission matrix exposes a concrete missing capability.

No UC03 production code, DDL migration or UI implementation is authorized by this planning document.

---

## 3. Terminology boundary

### 3.1 Internal architecture term

Audit Core continues to use the existing internal concept **Journey** and the immutable `journey_id` because current evidence, findings, booking, delivery and review models already attach to it.

### 3.2 User-facing term

For PC users, the product SHALL NOT display:

- Journey
- Journey Workspace
- Journey Stage

The PC sees business terms:

- Create Booking
- Booking Started
- Booking In Progress
- Booking Closed
- Booking Cancelled
- Duplicate Booking
- Delivery Started
- Delivery In Progress
- Delivery Completed
- Delivery Closed
- Audit Flags
- Review

The same internal `journey_id` follows the case from Booking into Delivery.

---

## 4. Non-negotiable UC03 invariants

### INV-01 — one immutable internal case identity

One `journey_id` follows the case from Booking through Delivery. Booking and Delivery are stages/projections of the same aggregate, not separate customer cases.

### INV-02 — audit never aborts dealer progression

A failed rule, missing document, unreadable evidence, incomplete prerequisite, non-intimation, payment discrepancy, VIN mismatch or open flag SHALL NOT cause Audit Core to reject the recording of a real dealer business event solely because it is non-compliant.

Business progression may be rejected only for technical/identity/authorization/sequence integrity reasons, for example:

- Journey does not exist;
- actor has no scope/permission;
- duplicate idempotent command conflicts semantically;
- a logically impossible command is issued against a terminal cancelled/duplicate record without an approved reopen path.

### INV-03 — progression with incomplete earlier-stage work creates a flag

If Delivery starts while Booking remains `BOOKING_IN_PROGRESS`, Audit Core records Delivery and raises a machine flag snapshotting the incomplete Booking prerequisites at that point.

The flag remains in history even if the missing Booking work is later completed.

### INV-04 — audit result is separate from business status

A Booking may be closed and still have `FLAGS_RAISED`. A Delivery may be completed while its Delivery Audit State remains `IN_PROGRESS`.

### INV-05 — flags are first-class auditable records

Machine and human flags use the same canonical register and preserve origin, stage, actor/role, rule/version where applicable, evidence, remarks, review and resolution history.

### INV-06 — no silent extraction overwrite

DI-extracted values arrive as proposals. A value already provided/accepted by a user is not silently overwritten by a later extraction result.

### INV-07 — source-of-truth data remains in owning domains

UC03 SHALL NOT create a second generic store containing duplicate authoritative copies of all 123 fields merely for workflow convenience. Existing typed Audit Core domains remain authoritative; a resolver/read model may normalize those values for rules and UI.

### INV-08 — mobile/tablet first for PC

The primary PC interaction is Android phone/tablet. Desktop Web remains supported but does not drive the PC workflow design.

### INV-09 — technical/internal messages stay out of UI

Backend names, raw provider errors, internal IDs, stack/error details and implementation terminology SHALL NOT be displayed to users.

---

## 5. UC03 stage model

UC03 contains three logical stages:

```text
BOOKING
DELIVERY
POST_DELIVERY  [reserved; Phase-1 business processing out of scope]
```

Each stage owns independent business/audit projections.

### 5.1 Stage independence

Stages are allowed to overlap.

Example:

```text
Booking Business Status  = BOOKING_IN_PROGRESS
Booking Audit State      = IN_PROGRESS
Booking Audit Status     = FLAGS_RAISED

Delivery Business Status = DELIVERY_STARTED
Delivery Audit State     = IN_PROGRESS
Delivery Audit Status    = NOT_EVALUATED
```

This is valid and is expected when the dealer progresses before Booking prerequisites are complete.

---

## 6. Booking business-status model

### 6.1 Status catalogue

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
BOOKING_CLOSED
BOOKING_CANCELLED
DUPLICATE_BOOKING
```

### 6.2 Meaning

#### BOOKING_STARTED

The Booking exists in Verigence and PC work has started. It may have arrived from DMS/import or been created by the PC under the approved exception path.

#### BOOKING_IN_PROGRESS

Booking capture/document/prerequisite work is still open.

This status may remain active even after `DELIVERY_STARTED`. If that happens, Audit Core raises an audit flag but continues both stages.

#### BOOKING_CLOSED

The Booking stage has been formally concluded.

There are two closure dispositions:

```text
PROCEED_TO_DELIVERY
NO_DELIVERY
```

`PROCEED_TO_DELIVERY` is the normal closure after the Booking prerequisite policy is satisfied.

`NO_DELIVERY` is used when the Booking is concluded for a business reason and the dealer will not proceed to Delivery.

The physical implementation of `closure_disposition` is deferred to implementation design; the semantic distinction is required.

#### BOOKING_CANCELLED

An explicit cancellation has been recorded. A cancellation reason and optional remarks are preserved.

#### DUPLICATE_BOOKING

The record has been identified as a duplicate. The status becomes `DUPLICATE_BOOKING` and Audit Core SHALL raise a duplicate-booking flag automatically.

The duplicate record is terminal for Phase 1 unless a future approved reopen/merge capability is designed.

### 6.3 Booking close/cancel reason catalogue

Phase-1 configurable defaults:

```text
CUSTOMER_CANCELLED
FINANCE_NOT_APPROVED
VEHICLE_UNAVAILABLE
CUSTOMER_SHIFTED_DEALER
DEALER_CANCELLED
DUPLICATE_BOOKING
OTHER
```

UI presents a dropdown plus a Remarks field.

Reason behavior:

| Selected reason | Resulting status | Automatic flag |
|---|---|---|
| Customer Cancelled | `BOOKING_CANCELLED` | No, unless another rule independently raises one |
| Dealer Cancelled | `BOOKING_CANCELLED` | Configurable |
| Duplicate Booking | `DUPLICATE_BOOKING` | Yes — duplicate flag |
| Finance Not Approved | `BOOKING_CLOSED` / `NO_DELIVERY` | Rule-configurable |
| Vehicle Unavailable | `BOOKING_CLOSED` / `NO_DELIVERY` | Rule-configurable |
| Customer Shifted Dealer | `BOOKING_CLOSED` / `NO_DELIVERY` | Rule-configurable |
| Other | `BOOKING_CLOSED` / `NO_DELIVERY` | Review policy configurable; remarks mandatory |

The reason catalogue SHALL be configuration/master-driven rather than hard-coded in React.

---

## 7. Delivery business-status model

### 7.1 Status catalogue

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
DELIVERY_CLOSED
```

Before Delivery starts, absence of a Delivery stage/status is interpreted as “not started”; no additional persisted business enum is required by this design.

### 7.2 Meaning

#### DELIVERY_STARTED

The dealer Delivery process has begun or the PC has reached the delivery event.

Delivery may start while Booking remains `BOOKING_IN_PROGRESS`.

#### DELIVERY_IN_PROGRESS

Delivery capture/document/photo/payment/witness work is actively being performed.

#### DELIVERY_COMPLETED

The physical dealer delivery event has occurred.

This status describes reality. It does not mean the Verigence Delivery audit is complete or clean.

A valid state is:

```text
Delivery Business Status = DELIVERY_COMPLETED
Delivery Audit State     = IN_PROGRESS
Delivery Audit Status    = FLAGS_RAISED
```

#### DELIVERY_CLOSED

The Delivery stage has been formally concluded in Verigence after the applicable Phase-1 Delivery audit/capture workflow has completed according to policy.

`DELIVERY_CLOSED` does not mean “success” or “failure”. UC03 Phase 1 has no `DELIVERY_SUCCESS` / `DELIVERY_FAILURE` business outcome.

---

## 8. Post-Delivery scope

Post-Delivery is reserved structurally because later releases will add reconciliation/periodic controls.

Phase-1 explicitly excludes:

- D+7 payment monitoring;
- D+12 DO monitoring;
- weekly trade-in checks;
- 90-day ageing logic;
- CRM reconciliation;
- monthly controls;
- scheduled Post-Delivery rule engine.

Audit stage structures may support `POST_DELIVERY` as a stage code, but no Phase-1 PC workflow or recurring processor is implemented.

---

## 9. Audit State and Audit Status

Each stage — Booking, Delivery, and reserved Post Delivery — has its own independent Audit State and Audit Status.

### 9.1 Audit State

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

Meaning:

- `NOT_STARTED` — no stage audit work has begun.
- `IN_PROGRESS` — evidence/capture/evaluation/review work is active or incomplete.
- `COMPLETE` — the configured stage audit workflow has reached its completion condition.

Audit completion does **not** imply no flags.

### 9.2 Audit Status

```text
NOT_EVALUATED
NO_FLAGS
FLAGS_RAISED
```

Meaning:

- `NOT_EVALUATED` — no committed evaluation result exists yet for the stage.
- `NO_FLAGS` — the stage has been evaluated and no non-voided flag has ever been raised by that evaluation/history.
- `FLAGS_RAISED` — one or more non-voided flags have been raised for the stage.

### 9.3 Historical semantics

`FLAGS_RAISED` is historical stage-level truth, not merely “open flags > 0”.

Example:

```text
Audit State   = COMPLETE
Audit Status  = FLAGS_RAISED
Open Flags    = 0
Resolved      = 3
```

The UI may display “3 flags resolved”, but SHALL NOT relabel the stage `NO_FLAGS` because that would erase the fact that exceptions existed.

---

## 10. Workflow Manager — conceptual architecture

The Workflow Manager is an Audit Core capability that coordinates business-stage events, audit stage projection, rule triggers and flag creation.

It is not a browser state machine.

```text
                         INTERNAL JOURNEY
                               |
             +-----------------+-----------------+
             |                                   |
          BOOKING                             DELIVERY
             |                                   |
   Business Status                         Business Status
   Audit State                             Audit State
   Audit Status                            Audit Status
   Requirements                            Requirements
   Evidence                                Evidence
   Rules                                   Rules
   Flags                                   Flags
             \                                   /
              +---------------+-----------------+
                              |
                         EVENT LEDGER
                              |
                         RULE ENGINE
                              |
                         FLAG REGISTER
```

Web/Android sends commands/events to Audit Core and renders the resulting aggregate. It does not independently decide transitions or compliance.

---

## 11. Workflow Manager — state/event model

### 11.1 Event classes

Events are divided into four classes.

#### A. Business progression events

```text
BOOKING_STARTED
BOOKING_CLOSED
BOOKING_CANCELLED
BOOKING_MARKED_DUPLICATE
DELIVERY_STARTED
DELIVERY_COMPLETED
DELIVERY_CLOSED
```

#### B. Capture/evidence events

```text
DOCUMENT_REQUIRED
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

These may be stored in specialized authoritative tables and projected into the Journey event timeline rather than requiring one giant generic event payload.

#### C. Audit workflow events

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

#### D. System/rule events

```text
RULE_EVALUATED
BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY
DOCUMENT_APPLICABILITY_CHANGED
DUPLICATE_BOOKING_DETECTED
```

Exact persisted event names may be normalized during implementation design; the business semantics above are mandatory.

---

## 12. Business transition table

### 12.1 Booking transitions

| Command/event | Allowed current Booking state | Result | Audit behavior |
|---|---|---|---|
| `BOOKING_STARTED` | no Booking stage yet | `BOOKING_STARTED` | Booking Audit State remains `NOT_STARTED` until audit/capture begins |
| first material capture/evidence work | `BOOKING_STARTED` | `BOOKING_IN_PROGRESS` | Booking Audit State -> `IN_PROGRESS` |
| prerequisites completed / normal stage close | `BOOKING_STARTED` or `BOOKING_IN_PROGRESS` | `BOOKING_CLOSED` + disposition `PROCEED_TO_DELIVERY` | evaluate Booking rules; flags do not prevent close if policy defines prerequisites as complete |
| close with no-delivery business reason | `BOOKING_STARTED` or `BOOKING_IN_PROGRESS` | `BOOKING_CLOSED` + disposition `NO_DELIVERY` | preserve incomplete items; rules may raise flags; remarks/reason recorded |
| explicit cancellation | `BOOKING_STARTED` or `BOOKING_IN_PROGRESS` | `BOOKING_CANCELLED` | preserve all existing flags/evidence/history |
| mark duplicate | `BOOKING_STARTED` or `BOOKING_IN_PROGRESS` | `DUPLICATE_BOOKING` | raise mandatory duplicate flag |
| `DELIVERY_STARTED` | `BOOKING_CLOSED` (`PROCEED_TO_DELIVERY`) | Booking unchanged | normal path |
| `DELIVERY_STARTED` | `BOOKING_IN_PROGRESS` | Booking remains `BOOKING_IN_PROGRESS` | **must record Delivery and raise incomplete-at-delivery flag** |

### 12.2 Delivery transitions

| Command/event | Allowed Delivery state | Result | Audit behavior |
|---|---|---|---|
| `DELIVERY_STARTED` | no Delivery stage | `DELIVERY_STARTED` | Delivery Audit State may move to `IN_PROGRESS` when capture begins |
| first material Delivery capture | `DELIVERY_STARTED` | `DELIVERY_IN_PROGRESS` | continue evidence/rules |
| `DELIVERY_COMPLETED` | `DELIVERY_STARTED` or `DELIVERY_IN_PROGRESS` | `DELIVERY_COMPLETED` | always record physical delivery; unresolved audit prerequisites create/retain flags |
| `DELIVERY_CLOSED` | `DELIVERY_COMPLETED` | `DELIVERY_CLOSED` | stage audit completion policy evaluated; flags may remain historically present |

### 12.3 No audit-based rejection

The following pattern is prohibited:

```text
DELIVERY_STARTED
 -> Booking incomplete
 -> HTTP 422 / refuse event
```

Required pattern:

```text
DELIVERY_STARTED
 -> persist Delivery start
 -> snapshot Booking incomplete prerequisites
 -> raise machine flag
 -> continue Delivery workflow
```

---

## 13. Progression-with-exception pattern

Canonical scenario:

```text
10:00  Booking = BOOKING_IN_PROGRESS
       Missing: Payment Proof, PAN verification

10:30  Dealer starts Delivery

10:30  Audit Core records:
       Delivery = DELIVERY_STARTED

10:30  Audit Core raises:
       BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY
       origin = MACHINE
       stage = BOOKING
       snapshot = [Payment Proof, PAN verification]

11:15  Payment Proof uploaded
11:17  PAN extraction accepted
11:20  Booking prerequisites complete
11:20  Booking = BOOKING_CLOSED

Historical flag remains and can be reviewed/resolved.
```

The system records what happened when it happened; later correction does not rewrite history.

---

## 14. Checkpoint model

The source process uses Gate 1 and Gate 2. UC03 retains their information value but changes their semantics.

They are **audit/capture checkpoints**, not business-process barriers.

A checkpoint may report:

```text
IN_PROGRESS
COMPLETE
COMPLETE_WITH_FLAGS
```

These are UI/read-model conditions, not necessarily persisted enums.

### 14.1 Booking checkpoint

Default source-derived Booking checks include:

- mandatory Booking capture fields;
- Booking Docket present/readable;
- Customer PAN present/readable;
- Minimum Booking Amount proof present/readable;
- applicable conditional documents addressed;
- effective price list selected;
- non-zero variance supported or flagged.

If the dealer starts Delivery before the checkpoint is complete, Delivery proceeds and a flag is raised.

### 14.2 Delivery checkpoint

Default source-derived Delivery checks include:

- Aadhaar captured/validated according to configured rule;
- applicable documents answered;
- required car photographs captured;
- VIN reconciliation rule evaluated;
- payments captured/verified according to configured rule;
- raised observations have required PC remarks;
- delivery intimation captured.

A failed checkpoint does not undo or prevent a real `DELIVERY_COMPLETED` event.

---

## 15. Document applicability model

The checklist SHALL be dynamic.

Documents that cannot apply are hidden from normal PC workflow rather than shown as meaningless NA rows.

Applicability inputs may include:

- exchange/trade-in;
- corporate customer / corporate discount;
- finance / DO;
- registration by dealer;
- accessories;
- third-party payer;
- future configured attributes.

### 15.1 Assessment states

For each applicable requirement:

```text
UNANSWERED
YES
NO
NA   [only when the requirement permits NA]
```

### 15.2 Semantics

- `YES` generally requires linked evidence where configured.
- `NO` is a valid audit answer; it may raise a flag when the document should exist.
- `NA` is allowed only where the versioned requirement permits it and SHALL record who chose it and when.
- Attribute changes may add new requirements later; the system records why and when the checklist changed.

### 15.3 Phase-1 catalogue

A provisional UC03 document catalogue will be built from the supplied process material.

The source material contains a count/numbering inconsistency (“26 documents” while the visible numbering reaches 29). UC03 SHALL NOT silently invent a reconciliation. The initial catalogue will be versioned and explicitly marked **PROVISIONAL — UAT REVIEW REQUIRED**.

---

## 16. Field ownership and resolved facts

The source process describes 123 fields split among PC input, document extraction and masters/system values.

UC03 implementation SHALL first create a field ownership matrix:

```text
field_key
business label
stage
owning Audit Core domain
source type
DI canonical field if extracted
master source if applicable
manual role if applicable
rule dependencies
PII/masking rule
```

### 16.1 No duplicate generic source of truth

Current typed Audit Core domains — Booking, Commercials, Payments, Finance, Insurance, Trade-in, Vehicle, Registration and Delivery — remain authoritative where they already own a field.

A Journey Fact Resolver/read model may present normalized keys to the Rule Engine, for example:

```text
booking.actual_ex_showroom_price
master.standard_ex_showroom_price
delivery.invoice_vin
delivery.observed_vin
payment.total_realised
```

The resolver must preserve provenance back to the owning record/evidence/master version.

---

## 17. Extraction and proposal model

DI processing has a non-trivial delay, so the PC interaction SHALL be designed around useful parallel work.

### 17.1 Required behavior

1. Upload documents first.
2. Continue PC-only Booking/Delivery inputs while extraction runs.
3. Show per-document status, not one global spinner.
4. Show real processing state / elapsed time where available; do not invent fake percentages.
5. Allow completed documents to populate proposals progressively.
6. Extracted values are proposals, not silent overwrites.
7. Permit bulk acceptance of clean values.
8. Force targeted review of low-confidence/variance/conflict values.
9. One failed document does not make successfully processed documents appear failed.
10. Manual fallback is available according to field/document policy.
11. Retry/escalation policy remains configurable; the source default of escalation after repeated failed reads will be evaluated during rule/catalogue design.

### 17.2 Browser polling direction

Phase-1 direction is one cheap Journey/Booking-scoped Audit Core processing-status endpoint.

Web/Android SHALL NOT poll DI directly.

Preferred behavior:

- one request per open Booking/Delivery, not per document;
- adaptive short polling while pending;
- stop when `pending == 0`;
- pause when app/tab is backgrounded;
- immediate refresh on focus/reconnect;
- ETag/version support where practical;
- transport hidden behind a client hook/service so SSE can be introduced later without changing screens.

---

## 18. Rule Engine design

Audit Core owns UC03 business audit rules.

DI owns extraction/quality facts; Web/Android owns presentation; neither owns business compliance logic.

### 18.1 Rule evaluator catalogue direction

Audit Core currently has versioned audit controls and evaluations. UC03 extends the evaluator catalogue beyond simple snapshot matching.

Planned evaluator families:

```text
REQUIRED_PRESENT
CONDITIONAL_REQUIRED
VALUE_EQUALS
VALUE_NOT_EQUALS
FORMAT_VALID
NUMERIC_VARIANCE
DATE_RANGE
DOCUMENT_PRESENT
DOCUMENT_READABLE
DOCUMENT_ANSWER
VIN_RECONCILIATION
PAYMENT_RECONCILIATION
DUPLICATE_DETECTION
MASTER_COMPARISON
COUNT_MINIMUM
MANUAL_OBSERVATION
STAGE_PROGRESSION_WITH_INCOMPLETE_PREREQUISITES
```

Exact evaluator names/DSL are implementation-design concerns; rule versioning and ownership are mandatory.

### 18.2 Rule trigger catalogue

```text
ON_FIELD_CHANGE
ON_DOCUMENT_RESULT
ON_STAGE_EVENT
ON_CHECKPOINT_EVALUATION
ON_MANUAL_REQUEST
```

Scheduled Post-Delivery triggers are deferred.

### 18.3 Rule result

A rule evaluation produces an auditable evaluation record.

A failing/result-requiring-attention evaluation may create or update a flag according to the rule version.

Rules SHALL NOT directly mutate a dealer business status to failure or refuse a real progression event.

### 18.4 VIN

VIN/chassis reconciliation is a Rule Engine concern.

Web/Android captures/displays observed inputs and evidence. DI may extract relevant VIN/chassis facts. Audit Core resolves and evaluates them.

The 8-character versus 17-character reconciliation question remains a rule/catalogue decision; it must not be embedded in client code.

---

## 19. Flag / anomaly model

UC03 uses one canonical flag register, extending/reusing the existing Journey-scoped Audit Core Finding capability rather than introducing a competing exception system.

### 19.1 Required flag attributes

Conceptually:

```text
flag_id / existing audit_finding_id
journey_id
stage_code                 BOOKING | DELIVERY | POST_DELIVERY
origin_kind                MACHINE | HUMAN
origin_actor_id            nullable for MACHINE
origin_role_code           PC | TL | PM | EXECUTIVE | SYSTEM
rule_key                    nullable for purely human observation
rule_version_id             nullable
finding_type_code
severity
status
title
description
expected_summary
observed_summary
blocking_indicator          informational only; never means stop dealer process
detected_at
correlation_id
created_at
updated_at
```

Existing evidence/fact linkage remains valuable and should be retained.

### 19.2 Flag lifecycle

Phase-1 direction:

```text
OPEN
UNDER_REVIEW
RESOLVED
VOIDED
```

`REOPENED` is an event/action that can return a resolved flag to an active state if policy permits.

Exact persisted states will be finalized in implementation design against existing `audit_findings` status values.

### 19.3 Append-only flag events

Conceptual events:

```text
RAISED
REMARK_ADDED
EVIDENCE_ADDED
REVIEW_STARTED
REVIEWED
RESOLVED
REOPENED
VOIDED
RECLASSIFIED
```

History is append-only; current status is a projection.

---

## 20. Human/machine flag authority

### 20.1 Phase-1 role matrix

| Action | PC | TL | PM | Executive |
|---|---:|---:|---:|---:|
| Raise flag | Yes | Yes | Yes | Yes |
| Add remark/evidence | Yes | Yes | Yes | Yes |
| Review | No by default | Yes | Yes | Yes |
| Resolve | No by default | Yes | Yes | Yes |
| Reopen | No by default | Yes | Yes | Yes |
| Void/reclassify | No by default | Policy | Policy | Yes |
| View | Business-scope based | Business-scope based | Project-scope based | Project-scope / all approved scope |

Executive has all Phase-1 flag privileges as a business decision.

### 20.2 Flexibility requirement

This matrix SHALL be policy/permission-driven. It must not be implemented as scattered UI role checks such as `if role === 'TL'` across screens.

Web may hide unavailable actions for usability, but Audit Core/Security authorization remains authoritative.

The final Security permission impact will be assessed after the Audit Core command/API design. Existing Security is not redesigned pre-emptively.

---

## 21. Module ownership

### 21.1 Audit Core — primary owner

Audit Core owns:

- Journey aggregate/internal ID;
- Booking and Delivery business statuses;
- stage Audit State;
- stage Audit Status;
- append-only workflow/business event history;
- document applicability rules/profile resolution;
- stage checkpoint/readiness projection;
- typed business domains;
- resolved-fact view for rules;
- rule execution/evaluations;
- machine/human flag register;
- flag review/resolution history;
- aggregate Booking/Delivery workspace API;
- cross-module orchestration to DI.

### 21.2 DI — evidence intelligence owner

DI owns:

- document storage/integration contract already approved;
- document processing status;
- quality/readability processing where supported;
- document classification;
- extraction;
- canonical document facts;
- confidence;
- accepted/corrected extraction provenance according to DI contract.

DI SHALL NOT own:

- Booking/Delivery statuses;
- Audit State/Status;
- Booking prerequisite rules;
- VIN business reconciliation result;
- price variance compliance;
- duplicate-booking business outcome;
- flag review/resolution.

### 21.3 Web/Android — experience owner

Web/Android owns:

- PC My Work presentation;
- Create/Open Booking UX;
- Booking capture UX;
- Delivery capture UX;
- document upload/camera interactions;
- extraction progress/proposal presentation;
- flags presentation/raise/remark actions;
- TL/PM/Executive review surfaces;
- user-safe wording;
- adaptive/mobile behavior.

Web/Android SHALL NOT calculate authoritative audit compliance or business-state transitions locally.

---

## 22. Conceptual Audit Core data-model delta

This is solution-level, not approved DDL.

### 22.1 Stage projection

A canonical one-row-per-stage projection is required conceptually:

```text
journey_stage_state
  tenant_id
  journey_id
  stage_code                 BOOKING | DELIVERY | POST_DELIVERY
  business_status            nullable where stage has no business status yet
  audit_state                NOT_STARTED | IN_PROGRESS | COMPLETE
  audit_status               NOT_EVALUATED | NO_FLAGS | FLAGS_RAISED
  started_at_utc
  completed_at_utc nullable
  closed_at_utc nullable
  version_no
```

Exact table placement may instead extend existing Booking/Delivery tables plus an audit-stage table; implementation design must select the least-duplicative option.

### 22.2 Journey event ledger

Conceptually:

```text
journey_events
  tenant_id
  journey_id
  event_id
  event_type
  stage_code
  actor_id nullable
  actor_role_snapshot nullable
  source_kind HUMAN | MACHINE | SOURCE_SYSTEM
  safe_event_payload jsonb
  correlation_id nullable
  occurred_at_utc
```

The payload must be schema-constrained per event family; this is not approval for arbitrary business JSON.

### 22.3 Booking closure metadata

Conceptually preserve:

```text
close_reason_code
close_remarks
closure_disposition
closed_by_actor_id
closed_at_utc
```

Physical location TBD.

### 22.4 Document assessment

A Journey-stage requirement assessment is needed conceptually:

```text
journey_document_assessment
  tenant_id
  journey_id
  stage_code
  requirement_key
  requirement_profile_version_id
  applicability_state
  applicability_reason
  answer                     UNANSWERED | YES | NO | NA
  evidence_id nullable
  answered_by_actor_id nullable
  answered_by_role nullable
  answered_at_utc nullable
  na_reason nullable
  version_no
```

### 22.5 Flags/findings extension

Prefer extension/reuse of existing `audit_findings` and `finding_evidence` rather than a second anomaly table.

Required design delta includes stage/origin/rule provenance and append-only flag events.

### 22.6 Rule evaluator extension

Reuse existing versioned `audit_controls` / `audit_control_versions` and `audit_evaluations` where structurally suitable; extend evaluator capability rather than creating an unrelated rules subsystem.

---

## 23. Aggregate workspace/read model

Web/Android should not reconstruct the UC03 workflow by coordinating a large number of independent endpoints and duplicating rules.

Audit Core should expose one aggregate read model, exact route naming TBD, conceptually:

```text
GET /v1/tenants/{tenantId}/journeys/{journeyId}/workspace
```

Response groups user-relevant information:

```text
identity/reference/customer/vehicle
booking business status
booking audit state/status
booking checkpoint
booking requirements/documents
booking extraction/proposal summary
booking flags

delivery business status
delivery audit state/status
delivery checkpoint
delivery requirements/documents/photos/payments/witness answers
delivery flags

review summary
history/timeline
processing summary
```

The PC UI labels this as Booking/Delivery; it does not expose “Journey Workspace”.

---

## 24. Command/API direction

Exact endpoints are deferred to implementation design, but the command boundary is required.

Illustrative commands:

```text
POST .../booking/start
POST .../booking/close
POST .../booking/cancel
POST .../booking/mark-duplicate

POST .../delivery/start
POST .../delivery/complete
POST .../delivery/close

POST .../flags
POST .../flags/{flagId}/review
POST .../flags/{flagId}/resolve
POST .../flags/{flagId}/remarks
```

Commands SHALL:

- be authorized server-side;
- be idempotent where retry could duplicate an event;
- use optimistic/version conflict handling where simultaneous users could race;
- append required event history;
- update projections transactionally where within one database;
- invoke rules after the business event is durably accepted, without allowing rule failure to erase the real event.

---

## 25. PC mobile/tablet interaction principles

Mockups are deferred, but the solution design fixes these UX constraints.

### 25.1 Device priority

```text
1. Android phone
2. Android tablet
3. Desktop Web
```

### 25.2 PC work concepts

PC navigation should be framed around:

```text
Create Booking
Bookings In Progress
Deliveries
Needs Attention
Completed
```

not Journey terminology.

### 25.3 Touch/mobile rules

- 44px+ primary touch targets;
- no hover-only actions;
- no dense desktop tables for primary PC work;
- card/checklist presentation;
- direct Android Camera integration for required vehicle photographs;
- persistent/save-resume server state;
- interruptions/app backgrounding must not lose work;
- extraction processing continues server-side after app navigation/close;
- user-safe error text only.

### 25.4 Status visibility

At a glance the PC should be able to see, per Booking/Delivery:

```text
Business Status
Audit State
Audit Status
Open Flags
Requirements complete / total
Documents processing
```

---

## 26. Delivery started before Booking closes — required UI/read-model behavior

This scenario is explicitly supported.

The UI should show something conceptually like:

```text
Booking
In Progress
2 prerequisites still open
1 flag raised when Delivery started

Delivery
Started
Continue delivery capture
```

The UI must never present the previous Booking incompleteness as a reason the PC cannot capture the Delivery that is actually occurring.

---

## 27. Review model

### 27.1 PC

PC can:

- capture evidence/data;
- raise human flags;
- add remarks/evidence to relevant flags;
- respond to machine flags;
- continue Booking/Delivery work irrespective of open audit flags.

### 27.2 TL / PM

TL and PM can:

- review flags;
- request/record remarks/evidence according to policy;
- resolve/reopen/void where authorized;
- review stage audit completion.

### 27.3 Executive

Executive has all Phase-1 flag privileges and broad approved visibility.

Exact business scope remains subject to existing Security + Audit Core assignment rules.

---

## 28. Failure/retry principles

### 28.1 DI extraction failure

A DI processing failure is local to the affected document.

It SHALL NOT:

- erase other completed extraction;
- block manual work that policy allows;
- block recording real dealer progression.

It may:

- offer retry/retake;
- offer allowed manual fallback;
- raise or escalate an audit flag according to rule/configuration.

### 28.2 Dependency outage

Business command durability must be designed so an accepted Audit Core event is not silently lost because downstream evaluation/DI refresh is temporarily unavailable.

Where appropriate, follow-up evaluation/reconciliation can be retried from durable Audit Core state.

### 28.3 Client retry

All state-changing commands that can be retried by Android/Web due to unreliable connectivity need idempotency semantics.

---

## 29. Observability and auditability

For every important UC03 action, the platform should be able to answer:

- Which Booking/Delivery?
- Which tenant/dealer/outlet?
- Which stage?
- What business event occurred?
- Who/what caused it?
- What was the stage state before/after?
- Which rule version evaluated?
- Which evidence/facts were used?
- Which flag was raised?
- Who reviewed/resolved it?
- What remarks/evidence were added?
- What happened later?

No bearer token, secret or unnecessary PII belongs in operational event payloads/logs.

---

## 30. Phase-1 provisional document direction

The exact catalogue will be a separate UC03 design artifact after this solution design.

Source-derived logical groups include:

### Booking — always/source-derived

- Booking Docket / Sales Contract
- Customer PAN
- Customer Aadhaar
- Customer Address Proof
- Minimum Booking Amount Proof

### Delivery — always/source-derived

- No Dues Certificate
- Tax Invoice — DMS
- Tax Invoice — Tally
- Insurance Cover Note
- Gate Pass
- Customer ID / KYC re-verification
- Customer Ledger
- Cost Sheet / Case Detail Form
- Docket Audit Form
- Pictures of Car Being Delivered

### Conditional/source-derived

- RC / Transfer Letter / Authorization Letter
- Trade-in documents / valuation
- GST Certificate
- Corporate ID
- Purchase Order
- Bank Approval Letter
- Delivery Order
- Registration Invoice
- RTO Challan
- Debit Note — Insurance/Registration
- Accessory Invoice — DMS
- Accessory Invoice — Tally
- Declaration for Third-Party Payment
- Payment Receipts — Tally

This list is deliberately provisional because the source count and visible numbering do not reconcile cleanly. UAT review is required before final catalogue freeze.

---

## 31. Design decisions frozen in v1.0

| ID | Decision | Status |
|---|---|---|
| UC03-D001 | One internal Journey ID continues through Booking and Delivery | FROZEN |
| UC03-D002 | PC UI does not use the word Journey | FROZEN |
| UC03-D003 | Audit never aborts/refuses real dealer progression because of audit non-compliance | FROZEN |
| UC03-D004 | Booking may remain In Progress after Delivery starts | FROZEN |
| UC03-D005 | Delivery start with incomplete Booking creates a machine flag | FROZEN |
| UC03-D006 | Booking statuses: Started, In Progress, Closed, Cancelled, Duplicate Booking | FROZEN |
| UC03-D007 | Booking close/cancel uses configurable reason dropdown + remarks | FROZEN |
| UC03-D008 | Duplicate Booking status automatically raises a flag | FROZEN |
| UC03-D009 | Delivery statuses: Started, In Progress, Completed, Closed | FROZEN |
| UC03-D010 | No Delivery Success/Failure status in Phase 1 | FROZEN |
| UC03-D011 | Per-stage Audit State: Not Started / In Progress / Complete | FROZEN |
| UC03-D012 | Per-stage Audit Status: Not Evaluated / No Flags / Flags Raised | FROZEN |
| UC03-D013 | PC/TL/PM/Executive can raise flags | FROZEN |
| UC03-D014 | TL/PM review and resolve; Executive has all flag privileges | FROZEN |
| UC03-D015 | Role/action matrix must remain configurable | FROZEN |
| UC03-D016 | VIN reconciliation belongs to Rule Engine, not client | FROZEN |
| UC03-D017 | Document catalogue provisional until UAT/testing review | FROZEN |
| UC03-D018 | Post-Delivery reconciliation processes out of Phase-1 scope | FROZEN |
| UC03-D019 | PC UX designed Android phone/tablet first | FROZEN |
| UC03-D020 | Extraction delay handled by upload-first/progressive proposal UX | FROZEN |

---

## 32. Open design items — not blockers to mockup planning

These require later catalogue/implementation decisions but do not invalidate the architecture:

1. Exact physical table placement for stage projections and event ledger.
2. Exact final document catalogue/count.
3. Exact 123-field ownership map against current Audit Core schema.
4. Exact VIN 8/17-character reconciliation rule/version.
5. Exact rule severity catalogue and escalation thresholds.
6. Exact definition of when a stage Audit State can transition to `COMPLETE` while flags remain unresolved.
7. Exact behavior if a physically delivered deal is later discovered against a Booking previously marked cancelled/duplicate — likely requires an explicit reopen/new-record policy.
8. Exact Security permission additions, if any, after Audit Core APIs are fixed.

Each must be tracked in the later UC03 Decision Register / implementation handoff. None should be silently assumed in code.

---

## 33. Planned UC03 design artifacts

After approval of this Solution Design, create in order:

1. `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.0.md`
   - executable-level event/transition matrix;
   - idempotency and concurrency rules;
   - stage completion criteria.

2. `UC03_RULE_FLAG_CATALOG_v1.0.md`
   - rule keys;
   - triggers;
   - inputs;
   - severity;
   - flag behavior;
   - review/resolution policy.

3. `UC03_DOCUMENT_FIELD_MATRIX_v1.0.md`
   - provisional document catalogue reconciled against UAT;
   - 123-field ownership/source mapping;
   - DI canonical fields;
   - Audit Core domain ownership.

4. `UC03_MOBILE_WEB_MOCKUPS_v1.0`
   - Android phone first;
   - Android tablet second;
   - Desktop Web third;
   - PC, TL, PM and Executive workflows.

5. `UC03_IMPLEMENTATION_DESIGN_v1.0.md`
   - approved schema delta;
   - exact API contracts;
   - module sequencing;
   - migrations;
   - test architecture;
   - deployment/retry behavior.

6. `UC03_IMPLEMENTATION_HANDOFF_<date>.md`
   - exact repos/branches/SHAs;
   - files created/changed;
   - implemented/deferred scope;
   - test evidence;
   - deployment sequence;
   - rollback and Definition of Done.

---

## 34. Implementation increments after design approval

No code begins until the design/mockup package is approved.

Recommended increments:

### J1 — Workflow foundation

- stage projection;
- event ledger;
- Booking/Delivery command handling;
- Audit State/Status projection;
- aggregate workspace read model.

### J2 — Booking capture

- document applicability;
- Booking capture/evidence;
- DI extraction integration;
- proposal acceptance;
- Booking rules;
- machine/human flags;
- Booking close/cancel/duplicate.

### J3 — Delivery capture

- Delivery start/progression overlap;
- Delivery document checklist;
- Android photographs;
- VIN rule integration;
- payments/witness observations;
- Delivery rules/flags;
- Delivery completed/closed.

### J4 — Review

- TL/PM/Executive flag review/resolution;
- stage history/timeline;
- audit completion policy.

### J5 — Operational projections

After the canonical flow stabilizes, existing Findings, Review Queue, Payment Tracker, Daily Operations, Dashboard and Analytics should increasingly project from the UC03 records rather than inventing independent workflow state.

Post-Delivery recurring reconciliation remains deferred.

---

## 35. Definition of design-complete for UC03 Solution Design

This Solution Design is ready to proceed to mockups/catalogues when business review accepts:

- terminology boundary;
- no-abort invariant;
- Booking and Delivery status semantics;
- stage overlap;
- Audit State and Audit Status semantics;
- duplicate Booking behavior;
- close/cancel reason model;
- role/flag authority direction;
- Rule Engine ownership;
- module boundaries;
- Post-Delivery Phase-1 exclusion;
- mobile-first PC direction.

Approval of this document does **not** approve final document counts, final field mappings, database DDL or production APIs; those are deliberately governed by the next design artifacts.

---

## 36. Canonical UC03 summary

```text
                          ONE INTERNAL JOURNEY ID
                                   |
          +------------------------+------------------------+
          |                                                 |
       BOOKING                                           DELIVERY
          |                                                 |
 Business Status                                      Business Status
 Started                                               Started
 In Progress                                           In Progress
 Closed / Cancelled / Duplicate                        Completed
                                                      Closed
          |                                                 |
 Audit State                                           Audit State
 Not Started / In Progress / Complete                  Not Started / In Progress / Complete
          |                                                 |
 Audit Status                                          Audit Status
 Not Evaluated / No Flags / Flags Raised               Not Evaluated / No Flags / Flags Raised
          |                                                 |
          +--------------------- FLAGS ----------------------+
                                |
                 MACHINE or HUMAN (PC/TL/PM/EXEC)
                                |
                        REVIEW / RESOLUTION

Dealer progression is always recorded.
Audit exceptions are recorded alongside it.
The audit system observes reality; it does not force reality to wait for the audit system.
```
