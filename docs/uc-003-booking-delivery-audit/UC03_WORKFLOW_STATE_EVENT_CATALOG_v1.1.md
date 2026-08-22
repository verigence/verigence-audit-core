# Verigence UC03 — Workflow Manager State & Event Catalog

**Document ID:** `VUC03-WF-002`  
**Version:** `1.1`  
**Status:** DRAFT FOR BUSINESS & IMPLEMENTATION REVIEW  
**Date:** 2026-08-22  
**Parent design:** `VUC03-SD-002 / UC03_SOLUTION_DESIGN_v1.1.md`  
**Supersedes:** `VUC03-WF-001 v1.0`

---

## 1. Purpose

This is the authoritative Phase-1 state/event contract for UC03 Booking and Delivery workflow management.

The core rule is:

> **A real dealer business event is recorded even when audit prerequisites are incomplete. Audit incompleteness creates flags; it does not reject reality.**

Delivery has one terminal Phase-1 business state: `DELIVERY_COMPLETED`. There is no `DELIVERY_CLOSED`, `DELIVERY_SUCCESS`, or `DELIVERY_FAILURE` state.

Post-Delivery recurring/reconciliation workflow is out of Phase-1 scope.

---

## 2. Aggregate identity

One immutable internal `journey_id` identifies the case across Booking and Delivery.

```text
journey_id
  |
  +-- BOOKING
  |     business_status
  |     closure_disposition
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

The PC-facing product uses Booking/Delivery terminology and does not expose Journey as the operating concept.

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

### 3.2 Booking closure disposition

For `BOOKING_CLOSED` only:

```text
PROCEED_TO_DELIVERY
NO_DELIVERY
```

### 3.3 Delivery Business Status

```text
DELIVERY_STARTED
DELIVERY_IN_PROGRESS
DELIVERY_COMPLETED
```

No persisted `NOT_STARTED` or `CLOSED` value is required. Not Started is derived from absence of a Delivery stage status.

### 3.4 Audit State — each stage

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

### 3.5 Audit Status — each stage

```text
NOT_EVALUATED
NO_FLAGS
FLAGS_RAISED
```

`FLAGS_RAISED` is sticky historical truth.

### 3.6 Existing Flag/Finding status direction

Prefer current Audit Core semantics unless implementation review identifies a gap:

```text
OPEN
ACKNOWLEDGED
RESOLVED
VOIDED
```

An explicit future reopen event may move `RESOLVED -> OPEN` while preserving history.

---

## 4. Command versus event

A command is actor intent and may be rejected for:

- authorization/scope;
- nonexistent or wrong identity;
- semantic idempotency conflict;
- optimistic concurrency conflict;
- impossible sequence against a terminal state.

An event is an accepted immutable fact.

Audit non-compliance is **not** a reason to reject a real Delivery progression event.

Example:

```text
Command: START_DELIVERY
  -> validate actor/scope/idempotency
  -> append DELIVERY_STARTED
  -> update Delivery projection
  -> evaluate Booking-at-Delivery rules
  -> append FLAG_RAISED where required
```

---

## 5. Event envelope

Every persisted workflow event should conceptually carry:

```text
event_id
journey_id
tenant_id
event_type
stage_code
source_kind            HUMAN | MACHINE | SOURCE_SYSTEM
actor_id               nullable for MACHINE
actor_role_snapshot    nullable
idempotency_key        where command-driven
correlation_id         nullable
safe_payload           schema-constrained
occurred_at_utc
recorded_at_utc
```

`safe_payload` must not contain bearer tokens, secrets, or unnecessary PII.

---

## 6. Booking commands and transitions

### B01 — Start Booking

**Command:** `START_BOOKING`  
**Event:** `BOOKING_STARTED`

Effect:

```text
Booking Business Status = BOOKING_STARTED
Booking Audit State     = NOT_STARTED
Booking Audit Status    = NOT_EVALUATED
```

Repeated identical command is idempotent.

### B02 — Begin material Booking work

Usually derived from first meaningful capture action:

- first document upload;
- first mandatory PC field save;
- first document assessment;
- explicit audit start if implementation requires it.

Projection:

```text
BOOKING_STARTED -> BOOKING_IN_PROGRESS
Booking Audit State: NOT_STARTED -> IN_PROGRESS
```

### B03 — Close Booking ready for Delivery

**Command:** `CLOSE_BOOKING_READY_FOR_DELIVERY`  
**Event:** `BOOKING_CLOSED`

Allowed from:

```text
BOOKING_STARTED
BOOKING_IN_PROGRESS
```

Default guard:

- configured Booking completion/prerequisite policy is satisfied.

Effect:

```text
Booking Business Status = BOOKING_CLOSED
closure_disposition      = PROCEED_TO_DELIVERY
```

Existing audit flags do not automatically block this close if capture/completion policy is satisfied. Compliance is represented separately.

This command may legitimately be rejected when Verigence's own Booking completion policy is not satisfied because the command asserts that Booking work is ready/closed.

### B04 — Close Booking with no Delivery

**Command:** `CLOSE_BOOKING_NO_DELIVERY`  
**Event:** `BOOKING_CLOSED`

Input:

```text
closeReasonCode
remarks
```

Default mappings:

```text
FINANCE_NOT_APPROVED
VEHICLE_UNAVAILABLE
CUSTOMER_SHIFTED_DEALER
OTHER
```

Effect:

```text
Booking Business Status = BOOKING_CLOSED
closure_disposition      = NO_DELIVERY
```

`OTHER` requires remarks by default.

All incomplete capture/evidence remains in history and may create flags according to the rule catalogue.

### B05 — Cancel Booking

**Command:** `CANCEL_BOOKING`  
**Event:** `BOOKING_CANCELLED`

Default reasons:

```text
CUSTOMER_CANCELLED
DEALER_CANCELLED
```

Effect:

```text
Booking Business Status = BOOKING_CANCELLED
```

Prior evidence, Audit State/Status and flags remain preserved.

Phase 1 treats cancellation as terminal unless a later reopen design is approved.

### B06 — Mark Duplicate Booking

**Command:** `MARK_DUPLICATE_BOOKING`  
**Events:**

```text
BOOKING_MARKED_DUPLICATE
FLAG_RAISED
```

Effect:

```text
Booking Business Status = DUPLICATE_BOOKING
Booking Audit Status    = FLAGS_RAISED
```

A duplicate flag is mandatory and links any available safe source/candidate reference.

Phase 1 treats duplicate status as terminal for that duplicate record.

---

## 7. Delivery commands and transitions

### D01 — Start Delivery

**Command:** `START_DELIVERY`  
**Event:** `DELIVERY_STARTED`

Allowed when:

- the case identity exists;
- Booking is not terminal `BOOKING_CANCELLED` or `DUPLICATE_BOOKING` under Phase-1 sequence rules;
- Delivery has not already started except idempotent replay.

Allowed Booking states include:

```text
BOOKING_CLOSED / PROCEED_TO_DELIVERY
BOOKING_STARTED
BOOKING_IN_PROGRESS
```

Effect:

```text
Delivery Business Status = DELIVERY_STARTED
```

#### D01-A normal path

If Booking is closed ready for Delivery, record Delivery Start and continue.

#### D01-B exception path — Booking still incomplete

If Booking is `BOOKING_STARTED` or `BOOKING_IN_PROGRESS`:

```text
1. record DELIVERY_STARTED
2. snapshot incomplete Booking prerequisites at that instant
3. evaluate progression rule
4. raise BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY
5. leave Booking state unchanged
6. continue Delivery
```

Later Booking completion does not erase the historical flag.

### D02 — Begin material Delivery work

Usually derived from first meaningful Delivery action:

- Delivery document answer/upload;
- first vehicle photo;
- payment capture/verification;
- witness answer;
- manual Delivery observation;
- Delivery audit action.

Projection:

```text
DELIVERY_STARTED -> DELIVERY_IN_PROGRESS
Delivery Audit State: NOT_STARTED -> IN_PROGRESS
```

If first material work happens in the same command as Delivery Start, the projection may become `DELIVERY_IN_PROGRESS` immediately while preserving the `DELIVERY_STARTED` event in history.

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

This is the terminal Phase-1 Delivery business state.

The command SHALL NOT be rejected solely because:

- Booking prerequisites remain incomplete;
- Delivery documents are missing;
- a required document is answered No;
- document extraction failed/unreadable;
- photos are incomplete;
- VIN reconciliation raises a flag;
- payments are unverified or mismatched;
- witness/observation remarks are incomplete;
- Delivery was not intimated;
- audit flags are open.

Audit Core instead:

```text
1. durably records DELIVERY_COMPLETED
2. snapshots applicable incomplete/failed conditions
3. runs configured Delivery rules/checkpoint evaluation
4. raises/updates flags
5. leaves Delivery Audit State independently IN_PROGRESS until audit completion policy is met
```

There is no subsequent `CLOSE_DELIVERY` business command in Phase 1.

---

## 8. Activity allowed after Delivery Completed

`DELIVERY_COMPLETED` is terminal for Delivery **business status**, but not for the stage audit workflow.

After `DELIVERY_COMPLETED`, authorized users may still:

- upload/associate late evidence;
- accept/correct extraction proposals where policy permits;
- add PC/TL/PM/Executive remarks;
- review and resolve flags;
- run/re-run permitted stage rules;
- complete Delivery Audit State.

Any late evidence/correction must retain timestamps and provenance so the audit can distinguish evidence captured before versus after physical Delivery.

The system must never backdate late capture merely because it resolves an outstanding requirement.

---

## 9. Audit State transitions

Generic stage projection:

```text
NOT_STARTED
    |
    | first material audit/capture/evaluation action
    v
IN_PROGRESS
    |
    | configured stage audit completion policy satisfied
    v
COMPLETE
```

`COMPLETE` means configured audit work is complete, not that the stage is compliant.

Valid combinations:

```text
Audit State  = COMPLETE
Audit Status = NO_FLAGS
```

and:

```text
Audit State  = COMPLETE
Audit Status = FLAGS_RAISED
```

Initial design direction: open/resolved flag state is not automatically a blocker to Audit State completion. If a configured high/critical rule requires review before completion, that is expressed through stage completion policy, not by changing business status.

A completed Audit State reopen capability is not approved yet; if added, it must be explicit and auditable.

---

## 10. Audit Status transitions

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
   | all flags resolved/voided
   v
FLAGS_RAISED   [sticky historical truth]
```

UI separately reports open, acknowledged, resolved and voided counts.

---

## 11. Flag lifecycle and authority

Canonical lifecycle direction:

```text
OPEN
  |
  | review/acknowledge
  v
ACKNOWLEDGED
  |
  | authorized resolution
  v
RESOLVED

OPEN / ACKNOWLEDGED
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

Phase-1 policy defaults:

| Action | PC | TL | PM | Executive |
|---|---:|---:|---:|---:|
| Raise flag | Yes | Yes | Yes | Yes |
| Add remark/evidence | Yes | Yes | Yes | Yes |
| Review/Acknowledge | No default | Yes | Yes | Yes |
| Resolve | No default | Yes | Yes | Yes |
| Reopen | No default | Yes | Yes | Yes |
| Void/Reclassify | No default | Configurable | Configurable | Yes |

The matrix is permission/policy-driven, not UI hard-coded.

---

## 12. Rule-trigger events

Phase-1 trigger families:

```text
ON_FIELD_CHANGE
ON_DOCUMENT_RESULT
ON_STAGE_EVENT
ON_CHECKPOINT_EVALUATION
ON_MANUAL_REQUEST
```

Scheduled Post-Delivery triggers are explicitly out of scope.

Examples:

| Trigger | Example |
|---|---|
| `ON_FIELD_CHANGE` | format, duplicate, price/discount variance, payment match |
| `ON_DOCUMENT_RESULT` | missing/unreadable/document classification/extraction quality |
| `ON_STAGE_EVENT: DELIVERY_STARTED` | incomplete Booking prerequisites at Delivery |
| `ON_STAGE_EVENT: DELIVERY_COMPLETED` | snapshot incomplete Delivery audit prerequisites |
| `ON_CHECKPOINT_EVALUATION` | stage requirement completeness |
| `ON_MANUAL_REQUEST` | re-evaluate after corrected evidence |

Detailed rule keys are in `UC03_RULE_FLAG_CATALOG_v1.0.md`.

---

## 13. Mandatory workflow-level automatic flags

### WF-001 — Booking prerequisites incomplete at Delivery Start

Trigger:

```text
DELIVERY_STARTED
AND Booking is not BOOKING_CLOSED / PROCEED_TO_DELIVERY
```

Action:

```text
record Delivery start
snapshot incomplete Booking requirement keys/states
raise flag
continue
```

### WF-002 — Duplicate Booking

Trigger:

```text
MARK_DUPLICATE_BOOKING
or approved duplicate-detection rule
```

Action:

```text
Booking Status = DUPLICATE_BOOKING
raise duplicate flag
```

### WF-003 — Delivery completed with incomplete audit prerequisites

Trigger:

```text
DELIVERY_COMPLETED
AND Delivery checkpoint/capture policy incomplete
```

Action:

```text
record Delivery completion
raise/update configured Delivery exception flags
leave Delivery Audit State IN_PROGRESS
```

These flags never roll back accepted business events.

---

## 14. Document and extraction workflow

Document processing is asynchronous.

### Upload

`DOCUMENT_UPLOADED` may:

- move stage Audit State to `IN_PROGRESS`;
- create/associate Audit Core evidence;
- start DI processing;
- trigger document applicability/rules.

### Processing

`DOCUMENT_PROCESSING_UPDATED` / `EXTRACTION_PROPOSAL_AVAILABLE` may:

- update per-document state;
- create extracted proposals;
- trigger quality/readability rules;
- refresh aggregate processing summary.

### Accept/correct

`EXTRACTION_ACCEPTED` / `EXTRACTION_CORRECTED` writes accepted data into the authoritative owning Audit Core domain with source/provenance linkage.

A later DI result never silently replaces an already accepted PC value.

### Failure

Extraction failure is local to the affected document. Other documents continue processing.

The source process indicates escalation after repeated unreadable attempts; the specific flag/policy is captured in the Rule/Flag Catalog.

---

## 15. Dynamic document applicability

A document requirement is evaluated from current case attributes.

If an attribute changes, newly applicable requirements are added and an auditable applicability-change event is recorded.

Example:

```text
Exchange Taken: NO -> YES
  -> exchange document requirements become applicable
  -> DOCUMENT_APPLICABILITY_CHANGED
  -> UI explains why requirements were added
```

`NO` is a legitimate document answer and may raise a flag. It does not mean the user failed to use the application.

---

## 16. Optimistic concurrency

State-changing commands should use expected version / ETag or equivalent optimistic concurrency.

If TL and PC act on stale projections, Audit Core rejects stale mutation with a safe conflict response and requires refresh. It must not silently overwrite newer state.

Idempotency and concurrency solve different problems and both are required.

---

## 17. Idempotency

Commands that create or progress workflow state require semantic idempotency.

At minimum:

```text
START_BOOKING
CLOSE_BOOKING_READY_FOR_DELIVERY
CLOSE_BOOKING_NO_DELIVERY
CANCEL_BOOKING
MARK_DUPLICATE_BOOKING
START_DELIVERY
COMPLETE_DELIVERY
RAISE_FLAG
RESOLVE_FLAG
```

Same key + same semantic request returns the existing outcome.

Same key + different request returns a conflict and must not execute.

---

## 18. UI projection rules

Web/Android never derives authoritative business state from visible field counts alone.

Audit Core returns the projection.

Example PC card:

```text
Booking #682604
Booking: In Progress
23 / 26 requirements complete
Audit: Flags Raised (2 open)

Delivery: Started
Audit: In Progress
```

If Delivery is completed while Delivery audit remains open:

```text
Delivery: Completed
Audit: In Progress
3 items still require audit work
2 flags open
```

The UI must not invent a Delivery Closed state to hide this distinction.

---

## 19. Post-Delivery

Reserved only. No recurring timers, weekly/monthly reconciliation or post-Delivery state commands are included in this Phase-1 workflow catalog.

---

## 20. Required implementation tests

At minimum:

1. Booking start is idempotent.
2. material Booking capture moves `STARTED -> IN_PROGRESS`.
3. normal Booking close requires configured completion policy.
4. Booking no-Delivery close preserves incomplete evidence/history.
5. cancellation records reason/remarks.
6. duplicate marks status and raises flag.
7. Delivery Start after normal Booking close succeeds.
8. Delivery Start during Booking In Progress succeeds and raises progression flag.
9. late Booking completion does not erase the progression flag.
10. Delivery material work moves `STARTED -> IN_PROGRESS`.
11. Delivery Completed succeeds despite missing docs/open flags/VIN mismatch/unverified payment.
12. Delivery Completed is terminal business status; no Delivery Close command exists.
13. audit work may continue after Delivery Completed.
14. Audit State can become Complete with historical Flags Raised when completion policy permits.
15. `FLAGS_RAISED` remains sticky after resolution.
16. PC/TL/PM/Executive raise authority follows policy.
17. TL/PM/Executive review/resolve follows policy.
18. stale concurrent writes fail safely.
19. repeated command idempotency does not duplicate events/flags.
20. user-facing responses contain no raw technical/provider errors.

---

## 21. Current decision state

Frozen:

- one internal case ID;
- Booking/Delivery terminology for PC;
- no audit-blocking of real progression;
- overlapping Booking/Delivery work;
- Booking statuses including close/cancel/duplicate;
- Delivery statuses only Started/In Progress/Completed;
- per-stage Audit State and Audit Status;
- sticky Flags Raised semantics;
- machine + human flag register;
- role-policy direction;
- VIN matching in Rule Engine;
- Post-Delivery out of scope.

Still to be finalized in detailed design/testing:

- final Booking normal-close trigger UX;
- exact Audit State completion policy for configured high/critical review rules;
- final document catalogue/extraction profiles;
- exact VIN reconciliation algorithm;
- final rule severities/escalation defaults where marked provisional.