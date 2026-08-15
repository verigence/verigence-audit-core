# Verigence Audit Core — Requirements Correction Addendum

**Document ID:** VAC-REQ-ADD-001  
**Version:** 1.1  
**Status:** APPROVED BUSINESS CORRECTIONS — supplements and overrides conflicting statements in `VAC-REQ-001 v1.0`  
**Date:** 2026-08-15  
**Applies to:** `docs/AUDIT_CORE_REQUIREMENTS_BASELINE_v1.0.md`

## 1. Purpose

This addendum records explicit project-owner corrections made after the initial requirements baseline. It does not replace the 104-activity process capture or the supplied workbook evidence. Where this addendum conflicts with `VAC-REQ-001 v1.0`, this addendum is authoritative.

## 2. Corrected foundational requirements

### VAC-CORR-001 — One Security Tenant equals one Audit Project

Exactly one Verigence Security Tenant represents exactly one Audit Project.

```text
Security Tenant = Audit Project
```

Audit Core SHALL NOT model multiple Audit Projects underneath one Security Tenant.

A Project remains a business entity/projection in Audit Core for project metadata, OEM/product context, dates, thresholds and operating configuration, but the Project key is the Security `tenant_id`; a second independent Project authorization boundary is not introduced.

### VAC-CORR-002 — Business hierarchy

The required business hierarchy is:

```text
Project (= Security Tenant)
  -> Dealer
      -> Dealer Outlet / Location
          -> Customer
              -> Customer / Audit Journey
```

The Journey covers the end-to-end audited vehicle-sale lifecycle.

### VAC-CORR-003 — Journey process areas are peers, not children of Booking

Booking is the process kick-off, but it is not the aggregate root for all later business processes.

The Customer/Audit Journey SHALL coordinate the relevant process areas, including:

- Booking and booking classification;
- Commercials and discounts;
- Payments and payment verification;
- Finance / DO / PO where applicable;
- Insurance;
- Accessories / RSA / EW / Service Package / other VAS;
- Trade-In;
- Vehicle/VIN and registration facts;
- Delivery readiness, physical verification and delivery completion;
- Documents/evidence;
- Audit controls, observations/findings and review;
- CRM follow-up and escalations where triggered.

These process areas may evolve in parallel and SHALL NOT be forced into one simplistic linear Booking status.

### VAC-CORR-004 — Customer placement and identity

A Customer is a business record under the Dealer Outlet context and is referenced by one or more Customer/Audit Journeys as permitted by the final repeat-customer policy.

A Journey belongs to exactly one Customer and one Dealer Outlet.

Project-wide duplicate/match detection SHALL still be possible across Dealers and Dealer Outlets using protected/normalized identity match keys and evidence-derived facts.

**Open decision:** whether a repeat purchase at the same Dealer Outlet must reuse an existing Customer record or may create a new Customer context that is then linked as a match. The solution SHALL not make duplicate detection dependent on this decision.

### VAC-CORR-005 — Master data versioning is mandatory

Price lists, discount schemes, document requirement profiles, audit control/rule sets and other material business configuration used to reach an audit decision SHALL be versioned/effective-dated.

Published versions SHALL be immutable. A change creates a new version; it SHALL NOT rewrite the configuration against which an historical Journey was evaluated.

Each Journey/evaluation SHALL retain the effective version/snapshot needed to reproduce the audit decision.

### VAC-CORR-006 — Durable workflow/tasks are mandatory

Audit Core requires a durable workflow/task capability. Once a task is committed it SHALL NOT be lost because of:

- API/service restart;
- worker crash;
- deployment;
- transient database/network failure;
- duplicate event delivery;
- mobile retry/replay;
- scheduler restart.

Business state changes and directly resulting workflow task creation SHALL commit atomically in the same PostgreSQL transaction where practical.

The durable workflow design SHALL include at minimum:

- persistent workflow instance/state;
- persistent human/system tasks;
- append-only task transition/history events;
- idempotency keys/effect keys;
- atomic task claim/concurrency control;
- retry with persisted next-attempt time;
- stale-running/lease recovery for worker-driven tasks;
- dead-letter/failed state after a configured retry budget;
- transactional outbox for integration side effects;
- inbox/deduplication for future inbound asynchronous events;
- explicit cancellation with actor/reason rather than silent deletion.

The implementation may remain inside Audit Core initially; a separate BPM/workflow product is not a requirement for v1.

### VAC-CORR-007 — PC versus TL/PM responsibility separation

The Process Consultant (PC) is primarily responsible for field/process execution and evidence capture: receiving the file, capturing/uploading documents/photos, recording legitimate operational metadata, performing configured field checks, adding observations/remarks and submitting work.

Formal verification/validation authority SHALL sit with TL and PM according to the configured process/exception path. PC SHALL NOT automatically receive final verification permissions merely because the PC captured the evidence.

This separation applies to Audit Core permissions and DI document-verification permissions.

### VAC-CORR-008 — Dealership participant boundary

The dealership Sales Executive/Sales Consultant initiates the operational process by handing the booking file to the PC. Dealership personnel are business participants/reference master data in the current scope and do not require direct Verigence application dependency unless a later approved requirement adds limited dealer logins.

### VAC-CORR-009 — Dealer Outlet versus Security Location

Dealer Outlet is an Audit Core business entity. It MAY reference a Security Location for geo/schedule/access enforcement, but the solution SHALL NOT assume the two are the same entity until that mapping is explicitly approved.

This preserves the ability to reuse Security access controls without coupling business hierarchy to Security location implementation.

## 3. Requirements retained unchanged

All other process requirements captured from the supplied SPR process workbook and current-tool workbook remain in force unless explicitly superseded by a later approved addendum. This includes the eight process areas, Daily PC/TL Activity Tracker, PC Daily Activity Notepad, document/evidence requirements, Standard-versus-Actual comparisons, duplicate detection, CRM triggers, EOD/daily operations, trade-in, delivery, payment and analytics requirements.

## 4. Open business decisions carried forward

The following remain unresolved and SHALL NOT be guessed in design or code:

1. Satellite monthly-volume threshold value and whether classification is automatic or manually approved.
2. PM versus PMO terminology/role distinction.
3. Per-car Total Discount / Above Scheme formula.
4. PO / DO / Refund realised-payment logic.
5. Insurance Calculator integration method and OEM-specific rules.
6. Trade-in ageing/resale threshold (60 vs 90 days in source material).
7. Dedicated trade-in Sales field/business meaning where source material is ambiguous.
8. Deal-level Short/Excess formula/label.
9. Notification provider/channel implementation.
10. Repeat-customer record reuse policy described in VAC-CORR-004.
11. Exact Dealer Outlet ↔ Security Location cardinality/mapping policy.
