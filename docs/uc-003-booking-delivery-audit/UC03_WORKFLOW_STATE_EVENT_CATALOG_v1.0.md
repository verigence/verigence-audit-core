# Verigence UC03 — Workflow Manager State & Event Catalog

**Document ID:** `VUC03-WF-001`  
**Version:** `1.0`  
**Status:** DRAFT FOR BUSINESS & IMPLEMENTATION REVIEW  
**Date:** 2026-08-22  
**Parent design:** `VUC03-SD-001` / `UC03_SOLUTION_DESIGN_v1.0.md`

---

## 1. Purpose

This document is the authoritative UC03 state/event model for Booking and Delivery workflow management.

It translates the UC03 business design into explicit states, commands, events, transition guards, audit side effects and idempotency rules.

The core rule is:

> **A real dealer business event is recorded even when audit prerequisites are incomplete. Audit incompleteness creates flags; it does not reject reality.**

Post-Delivery recurring/reconciliation workflow is out of Phase-1 scope.

---

## 2. Aggregate identity

One immutable internal `journey_id` identifies the case across Booking and Delivery.

The aggregate contains independent stage projections:

```text
journey_id
  |
  +-- BOOKING
  |     business_status
  |     audit_state
  |     audit_status
  |
  +-- DELIVERY
  |     business_status
  |     audit_state
  |     audit_status
  |
  +-- POST_DELIVERY [reserved]
        audit_state
        audit_status
```

PC-facing UI uses Booking/Delivery terminology and does not display `journey_id` as a user concept.

---

## 3. State dimensions

### 3.1 Booking Business Status

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
BOOKING_CLOSED
BOOKING_CANCELLED
DUPLICATE_BOOKING
```

### 3.2 Delivery Business Status

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
DELIVERY_CLOSED
```

No persisted Delivery status is required before `DELIVERY_STARTED`; “Not Started” is a derived UI condition.

### 3.3 Audit State — per stage

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

### 3.4 Audit Status — per stage

```text
NOT_EVALUATED
NO_FLAGS
FLAGS_RAISED
```

`FLAGS_RAISED` is sticky historical truth. Once a non-voided stage flag has been raised, resolving all flags does not return the stage to `NO_FLAGS`.

### 3.5 Existing Finding/Flag status direction

To minimize unnecessary schema divergence from the existing Audit Core Finding model, Phase-1 should prefer the current canonical statuses:

```text
OPEN
ACKNOWLEDGED
RESOLVED
VOIDED
```

Review is represented through review events and/or `ACKNOWLEDGED`; a new `UNDER_REVIEW` persisted enum is not required unless implementation review demonstrates a real gap.

A future `REOPEN` command may move `RESOLVED -> OPEN` while preserving append-only history.

---

## 4. Command versus event

A **command** expresses actor intent and may be rejected for authorization, identity, concurrency or sequence-integrity reasons.

An **event** is an immutable fact accepted by Audit Core.

Example:

```text
Command: Start Delivery
  -> validate actor/scope/idempotency
  -> append DELIVERY_STARTED event
  -> update Delivery projection
  -> evaluate progression rules
  -> possibly append FLAG_RAISED event
```

A rule result occurs **after or alongside durable acceptance** of the real business event; it does not erase it.

---

## 5. Event envelope

Every persisted workflow event should conceptually carry:

```text
event_id
journey_id
tenant_id
event_type
stage_code
source_kind           HUMAN | MACHINE | SOURCE_SYSTEM
actor_id              nullable for MACHINE
actor_role_snapshot   nullable
idempotency_key       where command-driven
correlation_id        nullable
safe_payload          schema-constrained
occurred_at_utc
recorded_at_utc
```

`safe_payload` must never contain bearer tokens/secrets and must avoid unnecessary PII.

Exact storage schema is deferred to implementation design.

---

## 6. Booking commands/events

### B01 — Start Booking

**Command:** `START_BOOKING`  
**Event:** `BOOKING_STARTED`

Allowed when:

- Journey exists and has no Booking stage status; or
- Journey is being created through the approved PC-create exception and the create operation atomically establishes the Booking stage.

Effect:

```text
Booking Business Status = BOOKING_STARTED
Booking Audit State     = NOT_STARTED
Booking Audit Status    = NOT_EVALUATED
```

Idempotency:

- same semantic command/key returns existing result;
- a second different Booking start must not create another stage for the same Journey.

---

### B02 — Begin material Booking work

This is normally a derived transition rather than a separate visible button.

Trigger examples:

- first Booking document upload;
- first mandatory PC field save;
- first Booking document assessment;
- explicit audit-start action if implementation requires one.

Event/projection effect:

```text
BOOKING_STARTED -> BOOKING_IN_PROGRESS
Booking Audit State: NOT_STARTED -> IN_PROGRESS
```

Repeated capture actions do not repeatedly transition state.

---

### B03 — Normal Booking close / ready for Delivery

**Command:** `CLOSE_BOOKING_READY_FOR_DELIVERY`  
**Event:** `BOOKING_CLOSED`

Allowed from:

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
```

Required condition:

- configured Booking prerequisite/completion policy is satisfied.

Effect:

```text
Booking Business Status = BOOKING_CLOSED
closure_disposition      = PROCEED_TO_DELIVERY
```

Rule behavior:

- evaluate Booking rules/checkpoint;
- existing flags do not automatically prevent close if the completion policy has been met;
- compliance result is represented in Audit Status/flags, not business status.

Important distinction:

Refusing this command when the user falsely claims Booking prerequisites are complete is acceptable because this command declares Verigence stage closure.

Refusing a later real `DELIVERY_STARTED` event because Booking is still open is **not** acceptable.

---

### B04 — Close Booking with no Delivery

**Command:** `CLOSE_BOOKING_NO_DELIVERY`  
**Event:** `BOOKING_CLOSED`

Allowed from:

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
```

Required input:

```text
closeReasonCode
remarks              optional/mandatory according to reason policy
```

Effect:

```text
Booking Business Status = BOOKING_CLOSED
closure_disposition      = NO_DELIVERY
```

Default reasons mapping to this path:

```text
FINANCE_NOT_APPROVED
VEHICLE_UNAVAILABLE
CUSTOMER_SHIFTED_DEALER
OTHER
```

`OTHER` requires remarks in the default policy.

Audit behavior:

- preserve all incomplete requirements/evidence;
- run configured closure rules;
- flags may be raised;
- no audit failure converts the record to an artificial “Booking Failed” state.

---

### B05 — Cancel Booking

**Command:** `CANCEL_BOOKING`  
**Event:** `BOOKING_CANCELLED`

Allowed from:

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
```

Default reasons:

```text
CUSTOMER_CANCELLED
DEALER_CANCELLED
```

Effect:

```text
Booking Business Status = BOOKING_CANCELLED
```

All prior evidence, audit state/status and flags remain immutable/history-preserving.

Phase-1 treats `BOOKING_CANCELLED` as terminal unless an explicit reopen design is later approved.

---

### B06 — Mark Duplicate Booking

**Command:** `MARK_DUPLICATE_BOOKING`  
**Events:**

```text
BOOKING_MARKED_DUPLICATE
FLAG_RAISED
```

Allowed from:

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
```

Effect:

```text
Booking Business Status = DUPLICATE_BOOKING
Booking Audit Status    = FLAGS_RAISED
```

Mandatory machine/system flag:

```text
rule/flag key = DUPLICATE_BOOKING
stage         = BOOKING
origin        = MACHINE or HUMAN-triggered-system flag according to command source
```

The duplicate flag records any available canonical/original Booking reference linkage without exposing unnecessary internal IDs in the UI.

Phase-1 duplicate status is terminal.

---

## 7. Delivery commands/events

### D01 — Start Delivery

**Command:** `START_DELIVERY`  
**Event:** `DELIVERY_STARTED`

Allowed when:

- Journey/Booking identity exists;
- Booking is not terminal `BOOKING_CANCELLED` or `DUPLICATE_BOOKING` under Phase-1 sequence rules;
- Delivery has not already started, except idempotent replay.

Allowed Booking states include:

```text
BOOKING_CLOSED (PROCEED_TO_DELIVERY)
BOOKING_IN_PROGRESS
BOOKING_STARTED
```

Effect:

```text
Delivery Business Status = DELIVERY_STARTED
```

### D01-A — normal path

If Booking is closed/ready:

```text
record DELIVERY_STARTED
continue
```

### D01-B — exception path: Booking still incomplete

If Booking is `BOOKING_STARTED` or `BOOKING_IN_PROGRESS`:

```text
1. record DELIVERY_STARTED
2. snapshot incomplete Booking prerequisites
3. evaluate progression rule
4. raise BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY
5. leave Booking status unchanged
6. continue Delivery
```

The flag payload should identify the incomplete requirement keys and their state **as of Delivery start**.

Later completion of those Booking items does not delete or rewrite the flag.

---

### D02 — Begin material Delivery work

Derived on first meaningful Delivery capture, for example:

- document answer/upload;
- first vehicle photo;
- payment capture;
- witness answer;
- Delivery audit action.

Effect:

```text
DELIVERY_STARTED -> DELIVERY_IN_PROGRESS
Delivery Audit State: NOT_STARTED -> IN_PROGRESS
```

If capture begins in the same transaction as Delivery start, implementation may project directly to `DELIVERY_IN_PROGRESS` while preserving the `DELIVERY_STARTED` event.

---

### D03 — Complete physical Delivery

**Command:** `COMPLETE_DELIVERY`  
**Event:** `DELIVERY_COMPLETED`

Allowed from:

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
```

Effect:

```text
Delivery Business Status = DELIVERY_COMPLETED
```

This command records the physical dealer event and SHALL NOT be rejected solely because:

- Delivery documents are missing;
- photos are incomplete;
- VIN rule fails;
- payments are unverified;
- observations lack remarks;
- Delivery was not intimated;
- Booking prerequisites remain incomplete;
- audit flags are open.

Instead Audit Core:

```text
1. durably records DELIVERY_COMPLETED
2. snapshots relevant incomplete/failed conditions
3. evaluates configured Delivery rules/checkpoint
4. raises/updates flags
5. leaves Delivery Audit State independently IN_PROGRESS until its policy is complete
```

---

### D04 — Close Delivery stage

**Command:** `CLOSE_DELIVERY`  
**Event:** `DELIVERY_CLOSED`

Allowed from:

```text
DELIVERY_COMPLETED
```

Default Phase-1 guard:

```text
Delivery Audit State = COMPLETE
```

The existence of historical flags is **not** a default blocker to closing the stage.

Whether unresolved high/critical flags should require TL/PM review before Audit State can become `COMPLETE` belongs to the configurable stage-completion policy and will be finalized in the Rule/Flag Catalog.

Effect:

```text
Delivery Business Status = DELIVERY_CLOSED
```

There is no `DELIVERY_SUCCESS` or `DELIVERY_FAILURE` state in Phase 1.

---

## 8. Audit State transitions

### 8.1 Generic state machine per stage

```text
NOT_STARTED
    |
    | first material audit/capture/evaluation action
    v
IN_PROGRESS
    |
    | stage audit completion policy satisfied
    v
COMPLETE
```

### 8.2 No result semantics in Audit State

`COMPLETE` means workflow completion, not compliance success.

Valid examples:

```text
Audit State  = COMPLETE
Audit Status = NO_FLAGS
```

and:

```text
Audit State  = COMPLETE
Audit Status = FLAGS_RAISED
```

### 8.3 Reopen of a completed audit stage

Not approved in Phase-1 design yet.

If later required, it must be an explicit auditable event and cannot silently mutate `COMPLETE -> IN_PROGRESS` without history.

---

## 9. Audit Status transitions

Audit Status is derived/sticky:

```text
NOT_EVALUATED
   | first evaluation, zero flags
   v
NO_FLAGS

NOT_EVALUATED
   | first non-voided flag
   v
FLAGS_RAISED

NO_FLAGS
   | later flag raised
   v
FLAGS_RAISED

FLAGS_RAISED
   | flags resolved/voided
   v
FLAGS_RAISED   [sticky historical status]
```

A complete removal/reset to `NO_FLAGS` is prohibited because it would erase historical exception truth.

UI separately displays:

```text
Open Flags
Acknowledged Flags
Resolved Flags
Voided Flags
```

---

## 10. Finding/Flag lifecycle and authority

### 10.1 Canonical status direction

```text
OPEN
  |
  | TL/PM/Executive reviews/acknowledges
  v
ACKNOWLEDGED
  |
  | authorized resolution
  v
RESOLVED

OPEN or ACKNOWLEDGED
  |
  | authorized invalidation
  v
VOIDED

RESOLVED
  |
  | explicit future/approved reopen action
  v
OPEN
```

Every transition appends a flag event.

### 10.2 Phase-1 role defaults

| Action | PC | TL | PM | Executive |
|---|---:|---:|---:|---:|
| Raise | Yes | Yes | Yes | Yes |
| Add remark/evidence | Yes | Yes | Yes | Yes |
| Acknowledge/review | No default | Yes | Yes | Yes |
| Resolve | No default | Yes | Yes | Yes |
| Reopen | No default | Yes | Yes | Yes |
| Void/reclassify | No default | Configurable | Configurable | Yes |

Authorization must be policy/permission driven, not client hard-coded.

---

## 11. Rule-trigger events

Phase-1 Rule Engine trigger families:

```text
ON_FIELD_CHANGE
ON_DOCUMENT_RESULT
ON_STAGE_EVENT
ON_CHECKPOINT_EVALUATION
ON_MANUAL_REQUEST
```

Examples:

| Trigger | Example rule |
|---|---|
| `ON_DOCUMENT_RESULT` | Required document unreadable / classification mismatch |
| `ON_FIELD_CHANGE` | Price variance, duplicate customer/deal indicators |
| `ON_STAGE_EVENT: DELIVERY_STARTED` | Booking prerequisites incomplete at Delivery |
| `ON_STAGE_EVENT: DELIVERY_COMPLETED` | Delivery capture completeness snapshot |
| `ON_CHECKPOINT_EVALUATION` | stage requirement summary |
| `ON_MANUAL_REQUEST` | user-triggered re-evaluation after corrected evidence |

Scheduled Post-Delivery triggers are out of scope.

---

## 12. Required automatic progression flags

At minimum the Workflow Manager must support these system-level flags independent of the detailed business rule catalogue:

### WF-001 — Booking prerequisites incomplete at Delivery start

Trigger:

```text
DELIVERY_STARTED
AND Booking Business Status != BOOKING_CLOSED(PROCEED_TO_DELIVERY)
```

Action:

```text
record Delivery start
raise flag with prerequisite snapshot
```

### WF-002 — Duplicate Booking

Trigger:

```text
MARK_DUPLICATE_BOOKING command
or future duplicate-detection rule
```

Action:

```text
Booking Status = DUPLICATE_BOOKING
raise duplicate flag
```

### WF-003 — Delivery completed with incomplete Delivery audit prerequisites

Trigger:

```text
DELIVERY_COMPLETED
AND Delivery checkpoint != complete
```

Action:

```text
record physical Delivery completion
raise/update one or more configured Delivery exception flags
continue Delivery audit IN_PROGRESS
```

These flags never roll back the accepted business event.

---

## 13. Document/capture events and workflow effects

Document processing is asynchronous.

### 13.1 Upload

`DOCUMENT_UPLOADED` may:

- move Audit State to `IN_PROGRESS`;
- register evidence in Audit Core and DI;
- start DI processing;
- trigger applicability/rule evaluation.

It does not mark a requirement complete until the requirement's configured acceptance criteria are met.

### 13.2 Processing result

`DOCUMENT_PROCESSING_UPDATED` / `EXTRACTION_PROPOSAL_AVAILABLE` may:

- create extracted fact proposals;
- trigger quality/readability rules;
- update the aggregate processing summary.

### 13.3 Accept/correct

`EXTRACTION_ACCEPTED` or `EXTRACTION_CORRECTED`:

- persists accepted value through the owning domain/provenance contract;
- may trigger dependent rules;
- never silently replaces another accepted user value.

### 13.4 Document answer NO

`DOCUMENT_ANSWERED_NO` is valid capture, not application failure.

It may trigger a required-document flag according to the document requirement version.

### 13.5 NA

`DOCUMENT_MARKED_NA` is accepted only if the requirement version allows NA and records actor/time/reason policy.

---

## 14. Checkpoint projection

Checkpoint is a derived/read-model concept.

Suggested values:

```text
IN_PROGRESS
COMPLETE
COMPLETE_WITH_FLAGS
```

It is calculated from:

- requirement completion;
- document assessments;
- accepted field state;
- relevant rule/evaluation state;
- stage policy.

Checkpoint does not determine whether Audit Core records a real dealer progression event.

---

## 15. Idempotency requirements

All externally retriable business commands require idempotency because Android/mobile connectivity can produce retries.

At minimum:

```text
START_BOOKING
CLOSE_BOOKING_READY_FOR_DELIVERY
CLOSE_BOOKING_NO_DELIVERY
CANCEL_BOOKING
MARK_DUPLICATE_BOOKING
START_DELIVERY
COMPLETE_DELIVERY
CLOSE_DELIVERY
RAISE_FLAG
RESOLVE_FLAG
```

Semantic rules:

1. same idempotency key + same semantic payload -> return original result;
2. same key + different semantic payload -> conflict;
3. replay must not append duplicate business/flag events;
4. command result should include current version/projection for client refresh.

---

## 16. Optimistic concurrency

UC03 permits multiple actors (PC, TL, PM, Executive) to interact with the same case.

State-changing commands should use one of:

- explicit expected `versionNo`;
- ETag / `If-Match`;
- equivalent existing Audit Core optimistic-lock convention.

A concurrency conflict may reject a stale command and require refresh. This is distinct from rejecting dealer progression due to non-compliance.

For high-value physical events (`DELIVERY_STARTED`, `DELIVERY_COMPLETED`), implementation should combine idempotency with server-side conflict resolution so mobile retry does not create uncertainty about whether the event was accepted.

---

## 17. Sequence integrity versus audit blocking

The system distinguishes two kinds of guard.

### 17.1 Allowed hard guards

Examples:

- unknown Journey;
- wrong tenant/outlet scope;
- unauthorized actor;
- duplicate terminal command with conflicting payload;
- start Delivery against a Phase-1 terminal duplicate/cancelled Booking where no approved reopen path exists;
- close Delivery before physical Delivery has been recorded.

### 17.2 Prohibited audit guards

Examples that must **not** reject real Delivery start/completion:

- missing Booking payment proof;
- unreadable PAN;
- price variance;
- Booking Audit State still in progress;
- missing Delivery document;
- VIN mismatch;
- non-intimation;
- short/unverified payment;
- open audit flag.

These produce audit outcomes/flags.

---

## 18. Derived timeline

The Workflow Manager should expose an ordered business/audit timeline without forcing the UI to display technical event codes.

Example:

```text
10:00 Booking started
10:03 Booking documents uploaded
10:04 Booking audit in progress
10:07 Price variance flag raised
10:30 Delivery started
10:30 Booking-incomplete-at-delivery flag raised
10:45 Missing payment proof uploaded
10:47 Booking prerequisites complete
10:48 Booking closed
12:05 Vehicle delivered
12:05 Delivery completed
12:06 VIN discrepancy flag raised
14:20 Team Lead reviewed flag
15:15 Additional evidence received
15:40 Flag resolved
16:10 Delivery audit complete
16:11 Delivery closed
```

User-facing timeline wording must be business-safe and may hide internal rule IDs while retaining them in the backend record.

---

## 19. State snapshots for flags

Flags raised because of timing/progression must retain an immutable snapshot of relevant state.

Example for `BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY`:

```text
observed_at = Delivery start timestamp
booking_business_status = BOOKING_IN_PROGRESS
incomplete_requirement_keys = [...]
missing_document_requirement_keys = [...]
pending_extraction_keys = [...]
related_evidence_ids = [...]
rule_version_id = ...
```

When the Booking is later completed, the flag's original observed snapshot is unchanged.

---

## 20. Audit-stage completion policy

Exact completion policy will be finalized in the Rule/Flag Catalog.

The Workflow Manager must support a configurable policy such as:

```text
required capture complete
AND required rule evaluations executed
AND required remarks supplied
AND required review actions completed
```

The policy SHALL NOT require `Audit Status = NO_FLAGS`.

Therefore:

```text
Audit State = COMPLETE
Audit Status = FLAGS_RAISED
```

is a first-class valid outcome.

---

## 21. Post-Delivery reserved behavior

No Phase-1 business events are defined for Post Delivery.

The schema/design should permit future stage events without changing the Booking/Delivery identity model.

Do not implement scheduled reconciliation, success/failure outcomes or recurring Post-Delivery controls under UC03 Phase 1.

---

## 22. Open workflow decisions

The following remain explicitly open for later design artifacts:

1. Whether normal ready-for-Delivery Booking close is automatic when prerequisites become complete or requires a PC confirmation command.
2. Exact stage Audit State `COMPLETE` criteria, especially treatment of unresolved high/critical flags.
3. Reopen policy for Booking/Delivery after `CLOSED`.
4. Behavior if physical Delivery is discovered after a Booking was already `BOOKING_CANCELLED` or `DUPLICATE_BOOKING`.
5. Exact close/cancel reason catalogue governance/master location.
6. Exact command/API route names.
7. Exact event storage physical schema.

No implementation may silently decide these items outside the approved UC03 design process.

---

## 23. Workflow Manager acceptance scenarios

The eventual implementation must prove at least:

1. Booking starts and moves to In Progress on first capture.
2. Booking closes normally when prerequisites are satisfied.
3. Booking closes with no Delivery using reason + remarks.
4. Customer cancellation produces Booking Cancelled.
5. Duplicate produces Duplicate Booking plus mandatory flag.
6. Delivery starts after clean Booking close.
7. Delivery starts while Booking In Progress; event succeeds and flag is raised.
8. Missing Booking items are completed after Delivery start; historical flag remains.
9. Delivery completes with missing Delivery prerequisites; physical event succeeds and flags are raised.
10. VIN mismatch raises flag but does not reject Delivery completion.
11. Machine and PC can both raise flags on same stage.
12. TL reviews/resolves; PM reviews/resolves; Executive can perform all flag actions.
13. PC cannot resolve under default Phase-1 policy.
14. Resolved flags do not change stage Audit Status back to No Flags.
15. Audit State can be Complete while Audit Status is Flags Raised.
16. Retried mobile command with same idempotency key does not duplicate event.
17. Stale conflicting command receives concurrency conflict without corrupting timeline.
18. Technical/backend details are not required in user-facing workflow state.

---

## 24. Canonical workflow summary

```text
BOOKING
  STARTED
     |
     v
  IN_PROGRESS -----------------------------+
     |                                      |
     | prerequisites complete               | dealer starts Delivery early
     v                                      |
  CLOSED (PROCEED_TO_DELIVERY)              |
                                            |
                                            v
                                      DELIVERY STARTED
                                            |
                               MACHINE FLAG: Booking incomplete
                                            |
                                            v
                                      DELIVERY IN_PROGRESS
                                            |
                                            v
                                      DELIVERY COMPLETED
                                            |
                            audit/capture may still be IN_PROGRESS
                                            |
                                            v
                                      DELIVERY CLOSED

Alternative Booking terminal paths:

BOOKING_IN_PROGRESS -> BOOKING_CANCELLED
BOOKING_IN_PROGRESS -> DUPLICATE_BOOKING + FLAG
BOOKING_IN_PROGRESS -> BOOKING_CLOSED (NO_DELIVERY, reason + remarks)

At every stage:

Audit State  = NOT_STARTED | IN_PROGRESS | COMPLETE
Audit Status = NOT_EVALUATED | NO_FLAGS | FLAGS_RAISED

Flags = MACHINE or HUMAN; reviewed/resolved independently.
```
