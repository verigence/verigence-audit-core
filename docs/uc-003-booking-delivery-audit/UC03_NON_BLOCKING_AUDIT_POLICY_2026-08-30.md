# UC03 Non-Blocking Audit Policy

Date: 2026-08-30
Status: Governing UC03 business rule
Applies to: Booking V2, Delivery V2, audit review and exception handling

## Invariant

**Audit must never stop the Booking or Delivery business process.**

UC03 observes business activity, captures evidence, compares source values, identifies exceptions and raises flags. It does not act as an authorization gate for Booking or Delivery completion.

The canonical sequence is:

`Business process continues → evidence/status is recorded → exception is identified → flag is raised → review/follow-up happens separately.`

## Non-blocking conditions

None of the following may prevent Booking or Delivery progression/submission:

- Expected or mandatory audit document missing.
- Optional/conditional supporting document missing.
- PAN/Aadhaar identity evidence missing.
- GST, Corporate or Trade-In applicability unresolved.
- GST and Corporate evidence both present despite their mutual-exclusion rule.
- Document classification pending, unknown or failed.
- Extraction pending or failed.
- Low extraction confidence.
- Two or more source documents returning different values for the same business attribute.
- A review item being unaccepted/rejected/unresolved.
- An open audit flag or exception.

These conditions must be retained as evidence/status and surfaced through exceptions/flags.

## Technical command protection is different

The UI/API may temporarily prevent duplicate execution of the same technical command while that command is in flight, for example:

- the same file upload is still being transmitted;
- the same delete request is being processed;
- the same submit request is already executing.

This is idempotency/concurrency protection, not an audit gate. Once the immediate command finishes or fails, the PC must retain the ability to continue the business process.

## Booking V2

- The upload screen shows the expected audit pack but does not require audit completeness to continue.
- Missing expected evidence becomes an exception/flag.
- Classification/extraction can continue after the PC moves forward.
- GST/Corporate/Trade-In evidence is inferred when available; unresolved conditions do not block Booking.
- GST and Corporate remain mutually exclusive as a business rule. Contradictory evidence is preserved and flagged rather than used to stop Booking.
- Review is a separate audit activity after Booking submission.

## Delivery V2

- Delivery uses the same direct-upload/classification/extraction pattern as Booking V2.
- Documents are presented as Invoices, Payment receipts and Other documents, with mandatory/optional labels describing expected evidence.
- The PC can submit Delivery regardless of missing, processing, low-confidence or contradictory evidence.
- Delivery submission records the evidence state and raises exceptions/flags where required.
- Review then consolidates extracted attributes across all available sources.

## Review and flags

Review may require a reviewer to make a decision before closing the **review task itself**, but an unfinished review task must never reverse, delay or invalidate the already-submitted Booking or Delivery business event.

Flags are observations requiring follow-up. A flag can be open, escalated and resolved independently of the Booking/Delivery lifecycle.

## Data ownership

- Document Intelligence owns documents, extracted facts, confidence and evidence coordinates.
- Audit Core owns business journey state, configured requirements, audit rules, references/lineage, review decisions and flags.
- Audit Core must not duplicate DI extraction payloads merely to implement this policy.

## UI wording

Prefer business wording such as:

- Documents received
- Documents being classified
- Documents uploaded
- Review values being prepared
- Some expected documents are missing — this will be flagged for audit
- Differences found across source documents — review required
- Delivery submitted — audit review can continue separately

Avoid language that implies the audit engine is authorizing the business transaction, such as `blocked by audit`, `failed audit gate` or `cannot complete due to audit`.
