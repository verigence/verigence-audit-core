# UC03 Attribute Review & Cross-Source Audit View

**Date:** 2026-08-30  
**Status:** IMPLEMENTATION BASELINE  
**Scope:** UC03 V1/V2 extracted-field mapping, Booking Review, post-Delivery source comparison, evidence drill-down and typed business projection

## 1. Decision

UC03 uses one explicit business-attribute mapping for both V1 and V2. Extraction transport may differ, but once DI returns a canonical fact the Audit Core/UI model is the same.

The design has two user views:

1. **Booking Attribute Review** — available immediately after Booking submission and progressively populated while DI extraction finishes.
2. **Cross-Source Audit View** — available after Delivery submission and shows every available source value side-by-side for the same business attribute.

Both views reuse one **Evidence Viewer** that opens the exact source document/page and overlays DI's `evidenceRegion` bounding box.

## 2. Non-negotiable ownership / no-duplication rule

Document Intelligence owns:

- original document content;
- machine extracted values;
- canonical field identity;
- confidence;
- page number;
- evidence/bounding region;
- source fact version/history;
- competing values from multiple documents.

Audit Core MUST NOT create another raw extracted-field source of truth.

Audit Core owns:

- UC03 business/Excel attribute semantics;
- explicit source precedence/resolution rules;
- typed operational business records where an approved owner already exists;
- human corrections;
- review/verification state;
- audit events/findings;
- reference-only provenance identifying which DI fact was used for a resolved business attribute.

The new `auditcore.journey_attribute_resolutions` table therefore stores source references only. It intentionally does **not** store extracted value, confidence, page number or bounding box.

The legacy V1 `journey_document_extracted_fields` table is retained for compatibility but is now treated as a human-correction ledger. New V1 review writes store only `modified_value` plus DI source-fact references; unchanged machine values are no longer copied into it.

## 3. Common V1/V2 attribute mapping

Implementation: `src/audit_core/uc03_attribute_mapping.py`.

Rules:

- mapping is explicit; no fuzzy/English-label guessing is permitted;
- an unknown DI field is returned under `unmappedFields` and remains visible to reviewers;
- `SUPPORTED` mappings participate in resolution;
- `PROVISIONAL` mappings may be displayed but are not committed into business tables;
- source priority is evaluated before confidence;
- confidence breaks ties only between candidates at the same source-priority level;
- document ID/field key provide a deterministic final tie break;
- a null candidate remains visible as evidence but cannot beat a non-null candidate.

Initial implemented mapping:

| Excel # | Attribute | DI keys | Current precedence | Mapping | Typed write |
|---:|---|---|---|---|---|
| 2 | Customer Name | `customer_name`, `pan_name`, `aadhaar_name` | PAN → Aadhaar → Booking Form/Docket | SUPPORTED | Legal Name only for PAN/Aadhaar after PC confirmation |
| 3 | Customer Number | `customer_phone` | Booking Form/Docket | SUPPORTED | Existing Customer typed owner |
| 6 | Mail ID | `customer_email` | Booking Form/Docket | PROVISIONAL | No automatic write |
| 7 | PAN | `pan_number` | PAN | SUPPORTED | Review-only until an approved typed owner exists |
| 9 | SC Name | `sales_person` | Booking Form/Docket | PROVISIONAL | No automatic write |
| 20 | Model | `vehicle_model` | Booking Form/Docket → Invoice | SUPPORTED | Review-only; Product Master/SKU resolution is required before write |
| 22 | Variant | `vehicle_variant` | Booking Form/Docket → Invoice | SUPPORTED | Review-only; Product Master/SKU resolution is required before write |
| 23 | Color | `vehicle_color` | Booking Form/Docket → Invoice | SUPPORTED | Review-only; Product Master/SKU resolution is required before write |
| 35 | Ex Showroom | `ex_showroom_price` | Booking Form/Docket → Cost Sheet → Invoice | PROVISIONAL | No automatic write |
| 36 | Registration Type (amount) | `road_tax_registration` | Booking Form/Docket | PROVISIONAL | No automatic write |
| 44 | Insurance | `insurance_amount` | Insurance evidence → Booking Form/Docket | PROVISIONAL | No automatic write |
| — | Booking Reference | `booking_reference_number` | Booking Form/Docket | SUPPORTED | `auditcore.bookings.booking_reference` |
| — | Actual Booking Date | `booking_date` | Booking Form/Docket | SUPPORTED | `auditcore.bookings.booking_date` |

This is intentionally not presented as a completed mapping of all 57 workbook rows marked `Extracted`. The existing source-mapping artifact contains SUPPORTED, PROVISIONAL and TBD rows. A field is added to the common resolver only when the canonical DI key and source relationship are explicit. Unknown rows are not invented merely to make the screen look complete.

## 4. Customer-name audit semantics

The existing identity amendment remains authoritative:

- `customers.display_name` is **Entered Name** and remains immutable after Journey creation;
- PAN `pan_name` and Aadhaar `aadhaar_name` are identity-authoritative sources for **Legal Name**;
- Booking Form/Docket `customer_name` may be shown in the audit comparison but MUST NOT overwrite Entered Name or establish Legal Name;
- when PC confirms Review, an identity-authoritative selected source can update `customers.legal_name`;
- if it materially conflicts with an already verified Legal Name, Audit Core sets `legal_name_status='CONFLICT'` and does not silently overwrite the previous Legal Name.

## 5. Booking Attribute Review

Lifecycle:

`Booking submit → Review opens → DI continues asynchronously → Review refreshes → PC inspects values/evidence → PC confirms Review`

The primary screen is attribute-centric rather than document-centric.

Columns:

- Attribute / Excel field number;
- Resolved value;
- Confidence score;
- selected Document Type/source;
- review state;
- **View evidence**.

Confidence rules:

- numeric confidence is displayed because PC/TL explicitly needs it during Review;
- `<92%` is marked `Needs Review`;
- `>=92%` is `Ready` for confidence purposes;
- a missing value/confidence is not treated as ready.

The old document-wise list remains as a secondary evidence inventory; it is not the primary Review interaction.

If some documents are still extracting, Booking remains completed. Review shows `Processing` and automatically refreshes after two minutes while open. Confirmation is disabled until extraction is no longer pending and no document has failed processing.

## 6. PC confirmation and business-table update

Endpoint:

`POST /v2/tenants/{tenantId}/journeys/{journeyId}/booking/review/confirm`

The browser does not send resolved values back to Audit Core. Audit Core re-reads current DI facts and re-runs the common resolver server-side before committing anything.

The command uses `If-Match` and idempotency. It then:

1. verifies Booking capture is completed and PC verification is `PENDING`;
2. requires no pending/failed Booking document processing;
3. applies only `SUPPORTED` attributes that have an approved typed-domain owner;
4. records a reference-only `journey_attribute_resolutions` row for each supported resolved attribute;
5. marks Booking PC verification `VERIFIED`;
6. writes a safe workflow event containing attribute keys/counts only, never PII values.

Current approved typed writes are intentionally limited:

- Customer Number → existing Customer operational representation;
- Booking Reference → Booking;
- Actual Booking Date → Booking;
- Customer Name → Legal Name only when selected evidence is PAN/Aadhaar.

The following are deliberately **review-only** at this stage even when their extraction mapping is supported:

- PAN number — no approved typed owner has been established in the current UC03 model;
- Model/Variant/Color — free-text extraction must not bypass Product Master/SKU resolution.

A `PROVISIONAL` mapping is never written automatically.

This rule prevents Review from becoming a back door that mutates the business model with ungoverned free text.

## 7. Evidence Viewer

The Review API supplies:

- `documentId`;
- `canonicalFieldId`;
- `fieldKey`;
- `sourceFactVersion`;
- value;
- confidence;
- `pageNo`;
- `evidenceRegion`.

The Web application fetches the original document through an authorized Audit Core content proxy:

`GET /v2/tenants/{tenantId}/journeys/{journeyId}/review/documents/{documentId}/content`

Audit Core verifies that the DI document is linked to the Journey before streaming it. The proxy avoids depending on browser access to a DI/R2 URL.

For PDFs the existing `PdfPageReview` renderer opens the requested page and draws the normalized box. Images use the same `NORMALIZED_1000` coordinates directly on the image.

No bounding-box copy is stored in Audit Core.

## 8. Post-Delivery Cross-Source Audit View

Endpoint:

`GET /v2/tenants/{tenantId}/journeys/{journeyId}/audit/source-comparison`

Availability is gated by Delivery submission (`capture_completed_at_utc`).

The API reads current Booking + Delivery DI document facts and returns one logical row per mapped attribute:

`Attribute | Resolved Value | <actual source document 1> | <actual source document 2> | ... | Audit Result`

The UI uses actual document/source labels as column headings. It does not create fixed `source_1/source_2/source_3` columns in the database.

Result states:

- `MATCH` — two or more non-null source values are equivalent after harmless normalization;
- `MISMATCH` — multiple available source values differ;
- `SINGLE_SOURCE` — one source currently supplies the value;
- `NOT_AVAILABLE` — no source has supplied a value.

Every source cell is clickable and opens the Evidence Viewer on that exact document/page/box.

The table is a live audit projection from DI, not a duplicated Audit Core table.

## 9. V1 compatibility

V1 and V2 may obtain DI facts through different capture/review paths, but both use the same `uc03_attribute_mapping` registry for known business projection.

The V1 generic-review flow has been corrected so that:

- unchanged DI extraction values are no longer stored in `journey_document_extracted_fields`;
- only human-modified values are retained there as Audit Core-owned corrections;
- known typed projection uses the common attribute mapping rather than the old generic `_PROPOSAL_CAPTURE_MAP` route;
- Booking Form `customer_name` no longer has a path to overwrite Entered Name;
- PAN/Aadhaar legal-name semantics are handled through the explicit identity rule.

Legacy rows already present in `journey_document_extracted_fields` are not deleted or rewritten by this change.

## 10. Source resolution vs Final Source of Truth

This Review resolver is deliberately conservative.

The workbook and earlier V2 design include a broader `Final Source of truth` concept spanning later Delivery evidence. During Booking, a Delivery-only final source may not exist yet. Therefore:

- Booking Review resolves from sources currently available at the Booking stage according to the explicit common mapping;
- the post-Delivery view recomputes across Booking + Delivery sources;
- Delivery-only sources do not create false Booking-stage missing errors;
- future rule expansion must use the approved source-truth matrix rather than guessing aliases.

## 11. Data model

### `auditcore.journey_attribute_resolutions`

Purpose: record which DI fact was used when a supported attribute Review was confirmed.

Stored:

- Journey/stage/attribute key;
- Excel field number where defined;
- mapping status/version;
- DI document ID;
- optional legacy Evidence ID;
- DI canonical field ID;
- field key;
- source fact version;
- document type;
- resolution rule;
- owning Audit Core domain/reference where a typed write occurred;
- confirming actor/time.

Not stored:

- extracted value;
- confidence;
- page number;
- bounding box;
- document content.

Runtime has SELECT/INSERT only. A verified resolution is not silently updated later.

## 12. Failure semantics

- DI metadata/field contract failure → Review returns dependency-unavailable; no guessed value is shown.
- DI Decimal confidence serialized as a JSON numeric string is accepted by the Audit Core adapter.
- pending DI extraction → Booking remains completed; Review shows Processing.
- failed document processing → Review remains visible but PC confirmation is blocked until follow-up.
- unknown DI field → shown in `unmappedFields`; never mapped by similarity.
- stale Booking version → confirmation returns version conflict and user refreshes.
- DI document not linked to Journey → content proxy returns not-found.

## 13. Security / privacy

- document content is fetched only after normal UC03 Journey authorization and linkage validation;
- raw PII values are not placed into workflow safe payloads;
- raw DI facts are not copied to Audit Core;
- human corrections remain auditable;
- Entered Name immutability is preserved.

## 14. Implementation files

Audit Core:

- `src/audit_core/uc03_attribute_mapping.py`
- `src/audit_core/uc03_attribute_resolution.py`
- `src/audit_core/uc03_document_review_v2.py`
- `src/audit_core/di_client.py`
- `src/audit_core/uc03_pc_generic_review.py`
- `migrations/versions/0037_uc03_attribute_resolution_refs.py`

Web:

- `src/pages/BookingReviewV2Page.tsx`
- `src/pages/AuditReviewPage.tsx`
- `src/features/uc03/AttributeEvidenceViewer.tsx`
- `src/features/uc03/AuditSourceComparisonTable.tsx`
- `src/services/audit-core/uc03DocumentReviewV2.ts`
- `src/styles/uc03-attribute-audit-review.css`

## 15. Validation gates

Before merge/deployment:

1. Audit Core unit tests pass, including Decimal confidence compatibility and attribute resolver precedence.
2. Web TypeScript/build passes.
3. Booking V2 manual smoke demonstrates:
   - Booking submit opens Attribute Review;
   - extracted values populate progressively;
   - confidence/document type are visible;
   - evidence click opens correct page/box;
   - confirmation updates only approved business owners and sets PC verification VERIFIED.
4. Delivery smoke demonstrates Source Comparison appears only after Delivery submission and every available source value can be opened as evidence.
5. Database check confirms no new unchanged DI raw values are written into `journey_document_extracted_fields` and `journey_attribute_resolutions` contains references only.
