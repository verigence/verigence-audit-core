# Verigence UC03 — Rule & Flag Catalog

**Document ID:** `VUC03-RF-001`  
**Version:** `1.0`  
**Status:** DRAFT / PROVISIONAL BUSINESS RULE CATALOG  
**Date:** 2026-08-22  
**Parent design:** `VUC03-SD-002 / UC03_SOLUTION_DESIGN_v1.1.md`  
**Workflow contract:** `VUC03-WF-002 / UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`

---

## 1. Purpose

This document defines the UC03 Phase-1 rule and audit-flag model for Booking and Delivery.

It separates:

1. **business progression** — what the dealer actually did;
2. **capture/completion policy** — whether Verigence has completed the configured stage work;
3. **audit rules** — validations/reconciliations against evidence, master data and observations;
4. **flags** — auditable exceptions raised by machine or human actors;
5. **review/resolution** — TL/PM/Executive actions on individual flags.

The non-negotiable rule is:

> **No UC03 audit rule is allowed to reject or roll back a real `DELIVERY_STARTED` or `DELIVERY_COMPLETED` event solely because the dealer is non-compliant.**

A rule may keep a Verigence Booking/Audit checkpoint `IN_PROGRESS`, raise a flag, escalate a flag, or require review. It does not stop reality from being recorded.

---

## 2. Source traceability

The catalog is grounded in:

- `PC Evidence Capture Process — Booking and Delivery` (PCP), especially Booking steps/Gate 1, Delivery steps/Gate 2, dynamic document triggers, timing and exception sections;
- `SPR_Tool_Process_SubProcess_Activity_Details.xlsx` (SPR), especially process rows 5-40 and validation rows 67-82;
- UC03 business decisions recorded in `VUC03-SD-002`.

Where the source is incomplete or inconsistent, the rule is marked **PROVISIONAL** rather than silently resolved.

Source wording that described a gate as blocking the process is reinterpreted under the current UC03 decision as either:

- a Verigence stage-completion guard; or
- an audit flag/escalation condition.

It is never a blocker to recording a real dealer Delivery event.

---

## 3. Rule configuration model

Each rule version should conceptually contain:

```text
rule_key
rule_version_id
stage_code                  BOOKING | DELIVERY | POST_DELIVERY
name
description
source_reference
trigger_family
applicability_expression
input_fact_keys
input_document_keys
evaluator_key
rule_parameters
severity_default
flag_type_code
flag_message_template
effect_policy
review_policy
active_from
active_to
lifecycle_status            DRAFT | PUBLISHED | RETIRED
```

### 3.1 Trigger families

Phase 1:

```text
ON_FIELD_CHANGE
ON_DOCUMENT_RESULT
ON_STAGE_EVENT
ON_CHECKPOINT_EVALUATION
ON_MANUAL_REQUEST
```

Scheduled/Post-Delivery triggers are out of scope.

### 3.2 Effect policy

Allowed effect families:

```text
VALIDATION_ONLY
FLAG_ONLY
BOOKING_COMPLETION_GUARD
AUDIT_COMPLETION_GUARD
ESCALATE_FLAG
```

There is deliberately **no** `BLOCK_DELIVERY`, `ABORT_JOURNEY`, or `ROLLBACK_BUSINESS_EVENT` effect.

`BOOKING_COMPLETION_GUARD` means Verigence may keep Booking `IN_PROGRESS` rather than asserting `BOOKING_CLOSED / PROCEED_TO_DELIVERY`; if the dealer nevertheless starts Delivery, Workflow Rule `WF-001` records that progression and raises the appropriate flag.

---

## 4. Severity model

Initial severity vocabulary:

```text
INFO
LOW
MEDIUM
HIGH
CRITICAL
```

Severity values in this catalog are **defaults**, not immutable business logic. They must be versioned/configurable.

A severity may influence:

- ordering/visual priority;
- escalation;
- review requirement;
- Audit State completion policy where explicitly configured.

Severity never controls whether a real Delivery event can be recorded.

---

## 5. Flag model

UC03 extends the existing Audit Core Finding model rather than adding a second anomaly database.

Required UC03 semantics:

```text
stage_code
origin_kind                 MACHINE | HUMAN
origin_actor_id             nullable
origin_role_snapshot        PC | TL | PM | EXECUTIVE | SYSTEM
rule_key                    nullable
rule_version_id             nullable
severity
finding_status              OPEN | ACKNOWLEDGED | RESOLVED | VOIDED
finding_type_code
title
description
expected_summary
observed_summary
resolution_reason
correlation_id
created_at
```

Evidence/fact links use the existing finding-evidence direction.

### 5.1 Human-raised flags

Human flags do not require a machine rule key.

Minimum human flag categories should be configurable rather than hard-coded. Initial categories:

```text
PHYSICAL_OBSERVATION
DOCUMENT_EXCEPTION
PAYMENT_EXCEPTION
CUSTOMER_IDENTITY_CONCERN
COMMERCIAL_EXCEPTION
PROCESS_NON_COMPLIANCE
DELIVERY_EXCEPTION
OTHER
```

Human flag input:

```text
stage
category
severity
summary
remarks
evidence links optional
```

The UI must not force users to imitate machine rule terminology.

---

## 6. Flag authority matrix — Phase-1 defaults

| Action | PC | TL | PM | Executive |
|---|---:|---:|---:|---:|
| Raise | Yes | Yes | Yes | Yes |
| Add remark | Yes | Yes | Yes | Yes |
| Add evidence | Yes | Yes | Yes | Yes |
| Review/Acknowledge | No default | Yes | Yes | Yes |
| Resolve | No default | Yes | Yes | Yes |
| Reopen | No default | Yes | Yes | Yes |
| Void/Reclassify | No default | Configurable | Configurable | Yes |
| View within authorized scope | Yes | Yes | Yes | Yes |

These are policy defaults. Authorization must be permission-driven and changeable without rewriting the Workflow Manager.

---

## 7. Audit State completion direction

Audit State is independent of business status and flag result.

Initial policy direction:

### Booking Audit State = COMPLETE

When:

- the configured Booking audit/capture completion set has been addressed;
- required machine evaluations for the stage have run or have an explicit unavailable/error disposition;
- required PC remarks/answers for the stage are complete.

Open/resolved flags do not automatically prevent `COMPLETE` unless a published rule explicitly carries `AUDIT_COMPLETION_GUARD`.

### Delivery Audit State = COMPLETE

May occur before or after `DELIVERY_COMPLETED`, but in the normal case follows completion of applicable Delivery audit work.

Physical Delivery completion itself does not auto-complete the audit.

This policy remains configurable and must be finalized before implementation DDL/API freeze.

---

## 8. Workflow-level rules — frozen

| Rule key | Stage | Trigger | Condition | Default effect | Default severity | Source |
|---|---|---|---|---|---|---|
| `WF_BOOKING_INCOMPLETE_AT_DELIVERY_START` | Booking | `ON_STAGE_EVENT: DELIVERY_STARTED` | Booking not closed `PROCEED_TO_DELIVERY` | `FLAG_ONLY` + snapshot outstanding prerequisites | HIGH | UC03 decision |
| `WF_DUPLICATE_BOOKING` | Booking | duplicate command/detection | booking marked/matched duplicate | set Duplicate Booking status + flag | HIGH | UC03 decision + SPR 14-15 |
| `WF_DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE` | Delivery | `ON_STAGE_EVENT: DELIVERY_COMPLETED` | Delivery audit completion set is incomplete | `FLAG_ONLY` or configured requirement flags; Audit remains In Progress | HIGH | UC03 decision |
| `WF_LATE_EVIDENCE_AFTER_DELIVERY` | Delivery | evidence/correction after physical Delivery | evidence timestamp > Delivery Completed timestamp | provenance marker; optional flag by policy | MEDIUM PROVISIONAL | UC03 design-derived |

`WF_LATE_EVIDENCE_AFTER_DELIVERY` is a design-derived governance rule, not explicitly stated in the source; its severity/flag behavior remains provisional. Timestamp provenance itself is mandatory.

---

## 9. Booking capture and identity rules

| Rule key | Trigger | Condition / evaluation | Effect | Default severity | Traceability |
|---|---|---|---|---|---|
| `BK_REQUIRED_CAPTURE_COMPLETE` | checkpoint | configured mandatory Booking fields addressed | `BOOKING_COMPLETION_GUARD` | HIGH | PCP §3.2 |
| `BK_DOCKET_PRESENT` | document/checkpoint | Booking Docket requirement answered with acceptable evidence | `BOOKING_COMPLETION_GUARD`; flag if absent | HIGH | PCP §3.2; SPR 1,5 |
| `BK_DOCKET_READABLE` | document result | docket cannot be processed/read to configured standard | flag; repeated-failure escalation | HIGH | PCP §8 |
| `BK_PAN_PRESENT` | document/checkpoint | PAN requirement not satisfied | completion guard + flag | HIGH | PCP §3.2 |
| `BK_MIN_BOOKING_PROOF_PRESENT` | document/checkpoint | minimum Booking amount proof not satisfied and no applicable approved exception | completion guard + flag | HIGH | PCP §3.2; SPR 3,7-8 |
| `BK_CONDITIONAL_DOCS_ADDRESSED` | checkpoint | every applicable conditional Booking document has valid answer/evidence/NA reason according to policy | completion guard | HIGH | PCP §3.2, §5 |
| `BK_PRICE_LIST_SELECTED` | field/checkpoint | no Price List selected | completion guard + validation | HIGH | PCP §3.1-3.2 |
| `BK_PRICE_LIST_EFFECTIVE` | field/checkpoint | selected Price List does not cover Booking date | completion guard + flag | HIGH | PCP §3.1-3.2 |
| `BK_PRICE_VARIANCE` | field/evaluation | standard vs actual non-zero variance | flag unless supported/explained by applicable discount evidence/policy | MEDIUM/HIGH PROVISIONAL | PCP §3.1-3.2; SPR 75-77 |
| `BK_PAN_FORMAT` | field change | PAN fails approved format | validation + flag by policy | MEDIUM | SPR 68 |
| `BK_CONTACT_FORMAT` | field change | contact is not exactly valid numeric format under approved rule | validation + flag | MEDIUM | SPR 67,74 |
| `BK_GST_REQUIRED_CORPORATE` | field/checkpoint | customer Corporate and GST missing | completion guard/flag by profile | HIGH | SPR 71; PCP §5 |
| `BK_DUPLICATE_STRONG_IDENTITY` | field/evaluation | configured PAN/Aadhaar/GST/mobile identity match meets duplicate threshold | duplicate candidate flag; human verification; may lead to Duplicate Booking status | HIGH | SPR 14-15 |
| `BK_DUPLICATE_LASTNAME_PINCODE` | field/evaluation | Last Name + Pincode candidate match | manual-verification flag only | MEDIUM | SPR 15 |
| `BK_TERRITORY_REGISTRATION_CONSISTENCY` | field change | territory categorization inconsistent with registration/dealership-state rule | flag/validation | MEDIUM | SPR 79 |
| `BK_EXCHANGE_RC_AUTHORIZATION` | checkpoint | exchange/trade-in applies and RC ownership/supporting transfer/authorization requirement not satisfied | completion guard + flag | HIGH | SPR 8,31; PCP §5 |

Duplicate matching details beyond the source-supported candidates remain implementation-rule configuration, not hard-coded client logic.

---

## 10. Dynamic document applicability rules

| Rule key | Stage | Condition | Adds/changes requirements | Traceability |
|---|---|---|---|---|
| `DOC_ALWAYS_BOOKING` | Booking | every Booking | Booking Docket, PAN, Aadhaar, Address Proof, Minimum Booking Amount proof | PCP §5 / Figure 2 |
| `DOC_ALWAYS_DELIVERY` | Delivery | every Delivery | NDC, Tax Invoice DMS, Tax Invoice Tally, Insurance Cover Note, Gate Pass, Customer ID, Customer Ledger, Cost Sheet, Docket audit form, Car pictures | PCP §5 / Figure 2 |
| `DOC_EXCHANGE` | Both | Exchange Taken = Yes | RC/Transfer/Authorization; trade-in RC/valuation docs | PCP §5 |
| `DOC_CORPORATE` | Both | Corporate customer/discount | GST Certificate, Corporate ID, Purchase Order | PCP §5 |
| `DOC_FINANCED` | Delivery | DO exists or Finance Type = In House | Bank approval letter, Delivery Order | PCP §5 |
| `DOC_REGISTRATION_DEALER` | Delivery | registration by dealer | Registration Invoice, RTO Challan, debit note for insurance/registration | PCP §5 |
| `DOC_ACCESSORIES` | Delivery | accessories taken/billed | Accessory Invoice DMS, Accessory Invoice Tally | PCP §5 |
| `DOC_THIRD_PARTY_PAYMENT` | Delivery | any receipt not paid by customer | third-party declaration, Payment Receipts Tally | PCP §5 |

If an attribute changes, the requirements are recalculated, newly applicable requirements are recorded, and the user is told what changed and why.

---

## 11. Document quality and answer rules

| Rule key | Trigger | Condition | Effect | Default severity | Source |
|---|---|---|---|---|---|
| `DOC_REQUIRED_MISSING` | checkpoint/document answer | applicable required requirement unanswered/missing | flag; completion impact according to stage policy | HIGH | PCP §§3-5 |
| `DOC_REQUIRED_ANSWER_NO` | document answer | applicable document explicitly answered No | flag with PC remark/evidence context; never treated as app failure | HIGH/MEDIUM by doc | PCP §5.4 |
| `DOC_NA_NOT_ALLOWED` | document answer | NA attempted where profile forbids NA | validation; answer rejected as invalid input | MEDIUM | PCP §4.2/§5 |
| `DOC_NA_REASON_REQUIRED` | document answer | NA allowed but configured reason missing | completion guard/validation | MEDIUM | PCP §3.2 |
| `DOC_UNREADABLE` | document result | DI/verification indicates unreadable/failed extraction quality | flag/retry path | MEDIUM/HIGH | PCP §8 |
| `DOC_UNREADABLE_REPEAT` | document result | configured repeated read attempts reached escalation threshold | escalate flag to TL; manual-entry/evidence path remains available | HIGH | PCP §8 (two attempts) |
| `DOC_CLASSIFICATION_MISMATCH` | document result | uploaded evidence class differs from required document type | flag/validation | MEDIUM PROVISIONAL | design-derived from DI classification boundary |

The source states escalation after two failed unreadable attempts; the exact DI status mapping must be confirmed against DI implementation contracts.

---

## 12. Delivery capture rules

| Rule key | Trigger | Condition / evaluation | Effect | Default severity | Traceability |
|---|---|---|---|---|---|
| `DL_AADHAAR_PRESENT_FORMAT` | field/checkpoint | Aadhaar missing or not configured 12-digit form | Delivery audit completion guard + flag | HIGH | PCP §4.2 |
| `DL_INTIMATION_ANSWERED` | checkpoint | Delivery intimation question unanswered | audit completion guard | HIGH | PCP §4.1-4.2 |
| `DL_NOT_INTIMATED` | field change | Delivery Intimated = No | machine/human flag; reason required; continue Delivery | HIGH | PCP §4.1, §8; SPR 21-22 |
| `DL_NON_INTIMATION_REASON_REQUIRED` | field change/checkpoint | Intimated = No and reason missing | audit completion guard | HIGH | PCP §4.2 |
| `DL_DOCUMENTS_ADDRESSED` | checkpoint | every applicable Delivery document has an allowed answer | audit completion guard; missing/No flags generated by document rules | HIGH | PCP §4.2 |
| `DL_CAR_PHOTO_SET` | checkpoint | required VIN/exterior/interior/odometer photo set incomplete | audit completion guard + flag | HIGH | PCP §4.2 |
| `DL_VIN_RECONCILIATION` | photo/fact evaluation | observed VIN/chassis does not satisfy published reconciliation rule against invoice | CRITICAL flag + escalation; still record real Delivery progression | CRITICAL | PCP §4.1-4.2; algorithm intentionally deferred |
| `DL_PAYMENT_VERIFICATION_COMPLETE` | checkpoint | one or more applicable receipts not verified with realized amount | audit completion guard + flags | HIGH | PCP §4.1-4.2 |
| `DL_FLAG_REMARKS_COMPLETE` | checkpoint | required PC remarks missing for raised observations/flags assigned to PC response | audit completion guard | MEDIUM/HIGH | PCP §4.2 |
| `DL_DELIVERY_BEFORE_BOOKING_DATE` | field/stage evaluation | Delivery date/time earlier than Booking date/time | flag | HIGH | SPR 78 |
| `DL_PHYSICAL_COMPLETION_WITH_CAPTURE_GAPS` | `DELIVERY_COMPLETED` | Delivery audit completion set incomplete | record Delivery Completed; raise configured gap flags; Audit State remains In Progress | HIGH | UC03 decision |

The source used VIN mismatch as a “stop” instruction. UC03 supersedes that workflow effect: it is a critical flag/escalation rule but not a refusal to record a real Delivery event.

---

## 13. Payment rules in UC03 Phase 1

Payment capture/verification is part of Booking/Delivery and remains in UC03; recurring settlement monitoring after Delivery is out of scope.

| Rule key | Trigger | Condition | Effect | Default severity | Source |
|---|---|---|---|---|---|
| `PAY_MADE_BY_CUSTOMER_CAPTURED` | checkpoint | payer ownership question required but unanswered | audit completion guard | MEDIUM | PCP §4.1 |
| `PAY_THIRD_PARTY_DECLARATION` | field/document | Made by Customer = No and required declaration/support missing | flag + document requirement | HIGH | PCP §5; SPR 27 |
| `PAY_FINANCE_BANK_REQUIRED` | field/checkpoint | financed deal and Bank Name missing | validation/flag | HIGH | SPR 70 |
| `PAY_RECEIPT_AMOUNT_MATCH` | verification | realized amount/receipt amount mismatch under configured tolerance | flag | HIGH | PCP §4.1; field inventory |
| `PAY_UNVERIFIED_RECEIPT` | checkpoint | receipt not verified | flag + Delivery audit completion guard | HIGH | PCP §4.1-4.2 |
| `PAY_DSA_SOURCE` | payment evaluation | payment source traced to DSA under approved logic | flag | MEDIUM/HIGH PROVISIONAL | SPR 30 |
| `PAY_OFFSITE_CASH_NO_INTIMATION` | payment evaluation | off-site cash collection without prior intimation | flag | HIGH | SPR 34-35 |
| `PAY_SHORT_EXCESS` | evaluation | payment total differs from expected/net deal under published basis/tolerance | flag | HIGH PROVISIONAL | SPR 96-100; source notes unresolved details |

Rules for D+7/D+12/PO schedules or other ongoing settlement monitoring are out of Phase-1 scope unless they are evaluated entirely from facts already available during the active Booking/Delivery audit.

---

## 14. Insurance / accessory rules

| Rule key | Trigger | Condition | Effect | Default severity | Source |
|---|---|---|---|---|---|
| `INS_INHOUSE_FIELDS_COMPLETE` | field/checkpoint | Insurance type In-House and configured amount/company/agent fields incomplete | flag/validation | HIGH | SPR 38-40,69 |
| `INS_AGENT_DUPLICATE_SELF` | evaluation | Self Insurance agent code duplicates under published scope | flag | MEDIUM PROVISIONAL | SPR 80 |
| `INS_AGENT_DUPLICATE_DEALER` | evaluation | agent code duplicated within same dealer | flag | MEDIUM PROVISIONAL | SPR 81 |
| `INS_AGENT_SELF_INHOUSE_REUSE` | evaluation | same agent code used for Self and In-House under published scope | flag | HIGH PROVISIONAL | SPR 82 |
| `ACC_BILLED_FITTED_OBSERVATION` | witness response | accessories billed but PC observes not fitted | human/machine flag | HIGH | PCP §1.2 / §4.1 |

Nightly/reporting implementation from the old SPR workbook is not automatically preserved as a nightly batch; UC03 implementation design must choose an appropriate event/checkpoint trigger while preserving rule semantics.

---

## 15. Booking closure rules

Booking close is a Verigence workflow assertion, not a dealer progression event.

### Ready-for-Delivery close

Default completion policy includes:

- configured mandatory Booking capture addressed;
- required Booking document requirements addressed to the configured standard;
- effective Price List selected;
- required extraction/manual review states concluded or explicitly unavailable;
- required variance treatment completed;
- required Booking rule evaluation executed.

A flag may remain open and Booking may still close if policy says the required capture/evaluation work is complete. Audit Status remains `FLAGS_RAISED`.

### No-Delivery close/cancel

Missing normal prerequisites do not have to be fabricated merely to close a customer/dealer-abandoned Booking. The close/cancel event preserves the incomplete state and may run closure rules that raise flags.

### Duplicate

Duplicate is a dedicated terminal Booking status and always carries a duplicate flag.

---

## 16. Delivery completion rules

`DELIVERY_COMPLETED` records reality and has no audit prerequisite guard.

At Delivery completion Audit Core performs a snapshot evaluation of configured Delivery conditions and raises/updates flags.

Delivery Audit State remains independent and can stay `IN_PROGRESS` after physical Delivery.

There is no Delivery Close command/state.

---

## 17. Rule evaluation record

Every machine evaluation should preserve enough information to reproduce/explain the result:

```text
audit_evaluation_id
journey_id
stage_code
rule_version_id / audit_control_version_id
trigger_event_id
input/reference snapshot
expected snapshot/criteria
observed snapshot
evaluation_result
explanation
correlation_id
evaluated_at
```

Large raw documents are referenced, not duplicated into evaluation JSON.

The current Audit Core evaluator implementation only supports a narrow evaluator path. UC03 implementation will require an expanded controlled evaluator catalog; it must remain deterministic and versioned.

---

## 18. Evaluator families — design direction

Candidate evaluator keys:

```text
REQUIRED_PRESENT
CONDITIONAL_REQUIRED
FORMAT_VALID
VALUE_EQUALS
VALUE_NOT_EQUALS
NUMERIC_VARIANCE
DATE_IN_EFFECTIVE_RANGE
DATE_ORDER
DOCUMENT_ANSWER_STATE
DOCUMENT_QUALITY
COUNT_MINIMUM
PAYMENT_RECONCILIATION
DUPLICATE_IDENTITY
MASTER_COMPARISON
VIN_RECONCILIATION
COMPLETION_SET
```

These are implementation design directions, not all guaranteed Phase-1 code until mapped against the final rule set.

---

## 19. Flag deduplication / recurrence

Machine rules must not create a fresh duplicate open flag on every re-evaluation.

Design direction:

```text
same journey
+ same stage
+ same rule key/version
+ same logical condition instance
```

should update/link the current condition evaluation or append an event to the existing flag according to rule policy.

If the condition was resolved and later materially recurs, the system may reopen or create a new occurrence according to the published rule definition, preserving both histories.

Human flags are not automatically deduplicated unless an explicit user flow links them.

---

## 20. Flag review/resolution events

Flag history should support:

```text
RAISED
REMARK_ADDED
EVIDENCE_ADDED
ACKNOWLEDGED
RECLASSIFIED
RESOLVED
REOPENED
VOIDED
```

Each event records actor, role snapshot, timestamp and safe reason/remarks.

Resolution does not delete the original machine/human observation or source evidence.

---

## 21. UI semantics derived from rule/flag model

The UI should speak in user language:

```text
Audit Flags
Needs Attention
Reviewed
Resolved
```

It should not expose rule engine class names, raw evaluator payloads, database IDs or backend error codes.

Examples:

```text
Price differs from the applicable Price List
Expected ₹15,92,000
Observed ₹15,84,700
Difference ₹7,300
```

or:

```text
Booking prerequisites were incomplete when Delivery started
3 items were still pending at 14:12
```

The underlying rule key/version remains available in audit history/admin diagnostics, not as the primary PC label.

---

## 22. Rule catalog status legend

- **FROZEN** — semantics directly approved in UC03 decisions.
- **SOURCE-SUPPORTED** — present in supplied process/source; implementation mechanics may still need mapping.
- **PROVISIONAL** — source or severity/threshold is incomplete/inconsistent and must be reviewed during testing/design.
- **OUT OF SCOPE** — deliberately deferred to Post-Delivery/later phase.

All rules in sections 8-16 are Phase-1 candidates unless explicitly marked provisional/out of scope.

---

## 23. Explicitly out of scope in this catalog

Not included as active Phase-1 scheduled controls:

- D+7 payment monitoring;
- D+12 DO monitoring;
- weekly trade-in sale/status update rules;
- 60/90-day trade-in ageing;
- monthly reconciliation;
- recurring CRM control scheduler;
- final post-Delivery outcome scoring.

The old process sources may mention these. They are preserved for future design but do not enter UC03 Phase-1 runtime.

---

## 24. Reconciliation items before implementation freeze

1. confirm final severity and escalation policy per rule;
2. confirm which rule keys require TL/PM review before Audit State can be Complete;
3. reconcile final document profile and source-to-field extraction mapping;
4. finalize VIN 8/17-character algorithm;
5. finalize tolerance/expected basis for monetary variance and Short/Excess rules;
6. map candidate evaluator keys to concrete Audit Core implementation;
7. map flag permissions to Security/Audit Core permission catalog;
8. decide whether selected legacy nightly rules become event-driven, checkpoint-driven or remain later analytics-only.

No unresolved item authorizes the frontend to implement its own compliance logic.