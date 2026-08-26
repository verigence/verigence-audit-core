# UC03 — PC Booking Capture and Document Verification

## Decision

Booking Capture and PC Document Verification are separate lifecycle activities.

Booking business status is not changed when the PC submits Step 2 or completes document verification. The Booking continues to retain its business/source status. PC verification is tracked independently.

TL review is optional. It is a separate assurance activity and is not a prerequisite for Booking progression.

## PC journey

```text
STEP 1 — DOCUMENTS
Upload documents to DI
Audit Core retains linkage / evidence IDs only
DI extraction continues asynchronously
        |
        v
STEP 2 — BOOKING DETAILS
PC enters whatever information is available
Missing information remains unchanged / null
        |
        v
SUBMIT BOOKING CAPTURE
capture_completed_at_utc = now()
pc_verification_status = PENDING
Booking business status = unchanged
        |
        v
AUTOMATICALLY OPEN DOCUMENT REVIEW
        |
        +-- DI ready ------> Review extracted values
        |
        +-- DI not ready --> Inform PC and return to other work
                            Register this attempted review for a
                            lightweight two-minute browser recheck
```

There is no Step 3 inside the Booking Capture wizard.

## Status model

The existing Booking business status remains independent.

PC verification uses only:

```text
capture_completed_at_utc = null     -> capture not submitted
capture_completed_at_utc != null
pc_verification_status = PENDING     -> Review Pending
pc_verification_status = VERIFIED    -> PC Verified
```

No `BOOKING_COMPLETED` status is introduced by this flow.

## Review Pending landing/work-list semantics

A Booking is Review Pending when:

```text
stage_code = BOOKING
AND capture_completed_at_utc IS NOT NULL
AND pc_verification_status = PENDING
```

This does not assert that DI extraction is ready. DI readiness is checked only when the PC attempts Review, or by the lightweight browser recheck for a Booking whose Review was already attempted.

## DI readiness behaviour

When Review is opened, the application refreshes the current DI-backed extraction state once.

If documents are not ready, show:

> Documents are still being prepared.
>
> Document Intelligence is processing the uploaded documents. Please continue with your other work and check again later. While this application window remains open, we will recheck this Booking periodically.

The PC is not held on a spinner or extracting page.

Only Bookings for which the PC actually attempted Review and received a not-ready response are registered for the two-minute browser recheck. The application does not poll every Review Pending Booking.

If the application window is closed, no browser timer is expected to continue. The Review Pending queue remains the durable way to resume work later.

## PC review

The Review page presents the existing source-document/extracted-field comparison.

For each reviewable extracted value the PC can:

- confirm the DI value; or
- correct it.

DI remains unchanged. Audit Core persists the confirmed/corrected business value and the existing provenance/audit information.

After all currently reviewable extracted values are decided and the linked documents are no longer processing or failed:

```text
pc_verification_status = VERIFIED
```

This update does not change Booking business status.

## TL review

TL Review is a separate, optional assurance activity after PC verification.

It may be used to:

- inspect the Booking and evidence;
- raise Audit Flags;
- review or resolve Audit Flags; and
- inspect audit history.

TL Review is not mandatory and therefore no mandatory `TL_REVIEW_PENDING` state is introduced. Existing audit/flag records provide the assurance history when a TL chooses to review a Booking.

## Ownership boundary

### Audit Core

- Booking/customer/dealer/outlet/project business records
- PC-captured business values
- evidence/document linkage IDs
- `capture_completed_at_utc`
- `pc_verification_status`
- confirmed/corrected values and provenance
- Audit Flags and optional TL assurance history

### Document Intelligence

- original document
- original extraction
- extraction processing state
- extracted fields
- confidence
- page/region localization

Audit Core must not copy DI OCR payloads, bounding-box structures, or other DI-owned extraction data merely to determine Review Pending state.
