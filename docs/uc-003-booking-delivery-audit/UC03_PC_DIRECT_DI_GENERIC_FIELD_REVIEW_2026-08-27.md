# UC03 PC Direct DI Generic Field Review — 27-Aug-2026

## Decision

PC Booking Review follows the same direct Document Intelligence (DI) boundary already used for PC document upload.

- Web reads the source document and extraction result directly from DI using the human Security access token.
- Audit Core does not define, whitelist, or filter which DI fields are visible in Review.
- Every field returned by DI for the current document extraction is displayed dynamically.
- A new DI field must not require an Audit Core or Web field-list change merely to be visible.

## Simple review behaviour

The PC changes only values that are incorrect.

- An unchanged DI value has no per-field approval/decision state.
- A changed value carries only the modified value plus modification actor/time in Audit Core.
- There is no `decision` column and no `reviewed_by_role` column for this phase.
- The effective value is `COALESCE(modified_value, extracted_value)`.
- Confidence is retained from DI.

## Persistence

Audit Core stores the complete document extraction in `auditcore.journey_document_extracted_fields` when the PC saves the document review.

The minimum persisted lineage is:

- tenant / Journey / Evidence / DI document
- DI source fact reference and source fact version
- field key
- extracted value
- optional modified value
- confidence score
- modification actor/time only when modified

All fields are stored, including fields that do not currently have an Audit Core typed-domain destination.

## Existing typed Audit Core domains

Existing typed mappings remain in place where they already exist. After generic persistence, Audit Core may project a known field into the current Customer, Booking, Payment, Registration, Trade-In, or other typed record.

A field without a typed mapping is not an error and does not block document review. A typed projection failure must not roll back the already-persisted generic extraction review; the generic document record is the durable result of the PC save.

## Performance / reliability

The operational goal is a fast Review experience with no unnecessary blockers.

- Web preloads DI extraction and document content directly and in parallel for review-ready documents.
- Opening Review does not wait for Audit Core to decide which extraction fields are supported.
- PC saves the complete document in one Audit Core batch request; there are no per-field network writes.
- Unchanged fields require no click or decision.
- Only modifications create field-level correction audit events.
- Document completion remains a single `BOOKING_DOCUMENT_REVIEWED` event so existing PC verification can continue to require every current Booking document to be saved once.

## Rollout compatibility

The existing `/booking/direct-document-review` API is retained during rollout. The new Web flow uses `/booking/direct-document-review-fields`; this allows Audit Core and Web DEV deployments to roll independently without breaking an older client during the deployment window.
