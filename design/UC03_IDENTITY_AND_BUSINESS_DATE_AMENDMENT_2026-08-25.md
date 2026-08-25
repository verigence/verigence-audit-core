# UC03 Customer Identity and Business Date Amendment

**Date:** 2026-08-25  
**Status:** Implementation baseline  
**Scope:** UC03 Booking capture, Document Intelligence hand-off, Audit Core persistence and audit semantics

## 1. Purpose

UC03 must preserve the difference between what a Process Coordinator initially captures and what source evidence later establishes. It must also distinguish the real-world Booking date from the date/time Verigence receives the Booking for audit.

These distinctions are required for delayed capture scenarios, including satellite outlets and Bookings completed when a Process Coordinator is not present.

## 2. Customer name model

### 2.1 Entered Name

- The name keyed by the Process Coordinator before `Add Details` / Journey creation.
- Stored in the existing `auditcore.customers.display_name` column for backward compatibility.
- Becomes immutable after Journey creation.
- Represents operational input, not verified identity.
- Must never be overwritten by DI extraction or later evidence.

UI label: **Entered Name** or, where context is unambiguous, **Customer Name**.

### 2.2 Legal Name

- The name printed on an approved identity document and validated by the Process Coordinator against that source document.
- Stored separately in `auditcore.customers.legal_name`.
- Initially null with status `PENDING`.
- Updated only from an accepted/corrected identity-document extraction proposal.
- Current supported identity sources are PAN (`pan_name`) and Aadhaar (`aadhaar_name`).
- The original DI machine value remains immutable in the capture proposal; a human correction is stored separately by the existing proposal decision model.

Legal-name status:

- `PENDING` — no identity name has been accepted yet.
- `VERIFIED` — an identity-document name has been accepted/corrected and is the current Legal Name.
- `CONFLICT` — a later validated identity document produces a materially different name from the existing Legal Name.

### 2.3 Name comparison

Comparison may normalize case, repeated whitespace and punctuation for equivalence checks. The system must not use aggressive/fuzzy matching to silently treat materially different names as identical.

Examples:

- `AMIT  KUMAR SHARMA` vs `Amit Kumar Sharma` -> equivalent.
- `Amit K Sharma` vs `Amit Kumar Sharma` -> not automatically equivalent; requires visible review.

### 2.4 Conflict handling

If PAN and Aadhaar establish materially different Legal Names:

- do not overwrite the previously verified Legal Name silently;
- set `legal_name_status = 'CONFLICT'`;
- retain both source proposals and evidence provenance;
- expose the conflict for Audit Review / finding logic.

The initial Entered Name remains unchanged regardless of the result.

### 2.5 Privacy

PII values must not be copied into workflow-event `safe_payload`. Events may record metadata such as `legalNameUpdated: true`, source evidence identifiers and decision type, while the actual value remains in the typed customer record and proposal/evidence records.

## 3. Booking date model

### 3.1 Actual Booking Date

- The date on which the dealer/customer actually made the Booking.
- Persisted in the existing `auditcore.bookings.booking_date` column.
- UI/API business name: **Actual Booking Date**.
- May originate from Booking evidence through DI and PC validation or from PC operational input when evidence does not provide it.
- Any later correction follows the existing audited capture/edit path.

### 3.2 Audit Captured At

- The timestamp at which Verigence creates the Journey after the PC selects `Add Details`.
- Reuses the existing immutable `auditcore.journeys.created_at_utc` timestamp.
- No duplicate capture-date column is introduced.
- UI/API business name: **Audit Captured At**.
- System generated and never editable by the Process Coordinator.

### 3.3 Capture Lag

Capture Lag is derived, not stored:

`project-local date(Audit Captured At) - Actual Booking Date`

This supports analytics such as same-day capture, one-day delay, 2-3 day delay, >3-day delay, and onsite-vs-satellite capture delay.

A prior Actual Booking Date is a valid business condition; it is not an error.

## 4. Business-effective configuration date

Decision-relevant configuration must ultimately be evaluated using **Actual Booking Date**, not merely the date on which Verigence received the case.

This applies to, at minimum:

- Project Policy Version
- Price List Version
- Document Requirement Profile Version
- date-effective audit/control configuration where applicable

The Journey may be created before Actual Booking Date is known. Therefore:

1. Journey creation may establish an initial/provisional configuration snapshot using the capture date so the workspace can open.
2. When Actual Booking Date is first accepted/captured, Audit Core must resolve the effective versions for that date.
3. Rebinding must occur before audit submission/closure and must not silently discard evidence, assessments or completed work.
4. If the effective document profile differs after evidence/assessment work already exists, Audit Core must reconcile requirements explicitly rather than deleting historical records.

The implementation must fail safely rather than apply a configuration that was not effective on the Actual Booking Date.

## 5. Source mapping

Canonical Audit Core capture concepts:

| DI source | DI field | Audit Core concept |
| --- | --- | --- |
| PAN | `pan_name` | `CUSTOMER_LEGAL_NAME` |
| Aadhaar | `aadhaar_name` | `CUSTOMER_LEGAL_NAME` |
| Booking Docket/Form | `booking_date` | `BOOKING_DATE` / Actual Booking Date |

Names extracted from Booking forms/dockets are not identity-authoritative Legal Name sources. They may remain visible as document facts but must not overwrite either Entered Name or Legal Name.

## 6. Audit invariants

1. Entered Name is retained exactly as the captured business input (after harmless whitespace normalization at creation) and is immutable after Journey creation.
2. Legal Name is evidence-derived and stored separately.
3. DI never directly mutates the typed customer record; an accepted/corrected Audit Core proposal is required.
4. Machine proposal values are never overwritten by human correction.
5. Actual Booking Date and Audit Captured At are separate concepts.
6. Audit Captured At is system generated and immutable.
7. Historical business rules use Actual Booking Date once that date is known.
8. Delayed capture is observable through derived Capture Lag and is not itself a workflow error.

## 7. UI contract

Booking workspace should show, when available:

- **Entered Name** — read-only.
- **Legal Name** — Pending / Verified / Conflict, with identity-document provenance.
- **Actual Booking Date** — captured/validated business value.
- **Audit Captured At** — read-only system timestamp.

Where Entered Name and Legal Name differ, both remain visible. The UI must never disguise the difference by replacing the Entered Name.

## 8. Compatibility

- Existing `customers.display_name` remains the entered name to avoid a breaking rename.
- Existing `bookings.booking_date` becomes the explicitly named Actual Booking Date.
- Existing `journeys.created_at_utc` becomes the explicitly named Audit Captured At.
- New storage is limited to Legal Name state/provenance required to preserve the audit distinction.
