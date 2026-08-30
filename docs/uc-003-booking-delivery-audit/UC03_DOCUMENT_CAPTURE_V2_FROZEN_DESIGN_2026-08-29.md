# UC03 Document Capture V2 — Frozen Design

**Date:** 2026-08-29  
**Status:** FROZEN FOR IMPLEMENTATION — REVIEW UX AMENDED 2026-08-30  
**Scope:** New parallel Document Capture V2 path for Booking and Delivery  
**Legacy impact:** Existing capture contracts remain compatible. The common review mapping now also governs safe V1 typed projection.

> **2026-08-30 amendment:** Screen 3 / post-Delivery audit review is superseded where necessary by `UC03_ATTRIBUTE_AUDIT_REVIEW_DESIGN_2026-08-30.md`. In particular, numeric confidence is now intentionally visible in Review, the primary Booking Review is attribute-centric, and Audit Core does not duplicate unchanged DI machine facts.

## 1. Source material and precedence

This design is grounded in:

1. the original UC03 / document-intelligence design already present in the repositories;
2. the uploaded field/source workbook `b89992d0-d386-45d2-a1db-1e1be331979a.xlsx`;
3. the existing Audit Core document-requirement master;
4. the deployed DI Schema V2 extraction/lineage contract; and
5. the business decisions confirmed on 2026-08-29 and Review amendment confirmed on 2026-08-30.

For the workbook, only the generic left-hand **B:K** block is global policy. The right-hand **Basant Swain** block is a worked case and MUST NOT be seeded as global policy.

The generic workbook block contains 115 attributes. 66 have an explicit `Final Source of truth` value. The exact extracted generic matrix is versioned separately in `UC03_SOURCE_TRUTH_MATRIX_V2.json`.

## 2. Hard compatibility rule

Document Capture V2 is additive and parallel.

- Do not change an existing V1 endpoint request/response contract without an explicit compatibility amendment.
- Existing adapters/services may be reused exactly as-is.
- If different capture behavior is required, add a V2 API/module/adapter.
- Legacy Booking and Delivery must remain usable while V2 is piloted.
- V2 may be hidden/disabled without a database rollback.
- The common attribute mapping may be shared by V1 and V2 after DI extraction because it is a business/audit semantic layer, not a capture transport contract.

## 3. Business processes

There are only two business processes:

1. **Booking**
2. **Delivery**

Each process has an independently lockable document-capture phase.

- While a phase is OPEN, PC may hard-delete a garbage/wrong upload.
- Once that phase is COMPLETED, no hard delete is allowed for that phase's evidence.
- After completion, corrections use replacement/supersession and preserve history.

## 4. Booking V2 screens

### Screen 1 — Documents

Audit Core supplies the complete Booking requirement list: REQUIRED, CONDITIONAL and OPTIONAL.

Current required Booking documents from the Audit Core master are:

- Booking Docket / Booking Form
- PAN
- Aadhaar
- Minimum Booking Amount Payment Proof

The existing master also contains Booking conditional/optional evidence. Document Capture V2 additionally requires Corporate ID as a V2-only conditional requirement; this MUST NOT alter the legacy profile.

Screen behavior:

- Top of screen: one common upload area.
- PC may upload one file or multiple files together.
- PC does not select or declare a document type.
- Requirement rows are informational/non-clickable before a document is classified to that requirement.
- Browser uploads bytes directly to R2 using a short-lived presigned PUT URL.
- DI classifies the uploaded file.
- When DI classifies a file, the matching requirement row becomes active/highlighted.
- An active row allows **View**, **Delete** and **Upload Again** while Booking is OPEN.
- If DI returns UNKNOWN / cannot classify, the file satisfies no requirement and PC is asked to upload again.
- All applicable REQUIRED requirements must have successfully classified active documents before Screen 2 is enabled.
- Extraction is asynchronous and never blocks Screen 1 completion.

### Conditional/non-mandatory questions on Screen 1

GST, Corporate and Trade-In decisions are made on Screen 1 only.

If matching evidence is already classified, the applicable answer is inferred from the evidence and PC is not asked the same question.

If no matching evidence is present, PC is asked whether the condition applies.

If PC answers **Yes, applicable**:

- ask whether the document is available;
- if available, ask PC to upload it and require successful classification before treating that requirement as satisfied;
- if not available, record the declaration and allow PC to continue to Screen 2; an asynchronous audit finding is created/recomputed later.

If PC answers **No**, record NOT_APPLICABLE and allow continuation.

### Screen 2 — Booking Details

V2 Screen 2 keeps the existing Booking Details business fields/behavior except:

- GST option is not shown;
- Corporate / Corporate ID availability option is not shown;
- Trade-In option is not shown.

Those values come from Screen 1 declarations/evidence for V2.

The legacy Screen 2 remains unchanged.

Booking is submitted on Screen 2.

### Screen 3 — Booking Attribute Review V2

After Booking submit, navigate to the V2 Review.

The **primary Review is attribute-centric**, not document-centric. It uses the common explicit UC03 Excel/business-attribute mapping.

Show:

- Attribute / Excel field number;
- resolved extracted value;
- **numeric confidence score**;
- selected document type/source;
- `Ready` / `Needs Review` state;
- `View evidence` link.

Rules:

- Show extracted values progressively as DI completes extraction.
- A field with confidence **< 92%** is marked `Needs Review`.
- Confidence **>= 92%** does not create a confidence-only review task.
- Review may open while some documents are still extracting; it must clearly show processing state without blocking the already-completed Booking.
- PC/TL can click any selected source value and open the original document on the correct page with DI's `evidenceRegion` box highlighted.
- The older document-wise list remains a secondary evidence inventory.
- PC confirmation is blocked while extraction is pending or any document has failed processing.
- On confirmation, Audit Core re-reads DI server-side and writes only attributes with an explicitly approved typed owner. Raw DI facts are not copied.

## 5. Performance contract

Target PC interaction for Booking Screen 1 is approximately **10 seconds** under normal conditions.

The critical synchronous path is:

`select files -> direct R2 upload -> classify -> Audit Core requirement reconciliation -> enable Screen 2`

The following are explicitly outside the PC blocking path:

- field extraction;
- source-of-truth resolution;
- audit-rule recomputation;
- review population.

Multiple uploads must run concurrently. Classification is interactive-priority work. The UI polls one aggregate capture-state endpoint only while unresolved classifications exist; no WebSocket/SSE dependency is introduced.

## 6. Integrity and asynchronous recovery

The browser is never the source of truth for document custody or classification.

### Upload initiation

Audit Core V2 obtains a DI document ID and upload intent before bytes are sent. Audit Core stores that DI document ID immediately, so a browser crash cannot leave Audit Core unaware of an initiated document.

### Direct R2 upload

The browser performs the PUT directly to R2. DI verifies object existence/metadata before transitioning the V2 upload to STORED/FIT.

A browser `finalize` call is a latency optimization only. DI/Audit Core status reconciliation MUST be able to discover a successfully stored object and resume processing even if the browser finalize call is lost.

### Classification

Classification is a separate V2 queue/worker. Legacy DI classification behavior is not changed.

After V2 classification succeeds:

1. DI persists the observed/accepted type and classification evidence;
2. DI schedules the existing extraction pipeline using the accepted type as the hint;
3. the existing Schema V2 extraction pipeline performs extraction/lineage without a V1 contract change.

### Audit Core synchronization

Audit Core stores only linkage/capture state and DI document IDs; it does not duplicate DI extraction storage.

Audit Core periodically/read-time reconciles its active V2 links with DI V2 document status. Reconciliation is idempotent.

Review adds only reference-level provenance (`journey_attribute_resolutions`) when a supported attribute is confirmed. Value/confidence/page/box stay in DI.

## 7. Delete and replacement rules

### OPEN phase

Hard delete is permitted for V2 evidence created in the currently OPEN phase.

Hard delete removes:

- R2 object;
- DI document children/facts/artifacts for that document;
- DI document row;
- Audit Core V2 link;

and the requirement returns to unresolved/missing.

### COMPLETED phase

Hard delete is forbidden.

### Upload Again

Never delete the current valid document before the replacement is safely stored and correctly classified.

Replacement sequence:

1. keep current document active;
2. upload replacement;
3. verify STORED/FIT;
4. classify replacement to the expected requirement;
5. atomically make replacement active;
6. while phase is OPEN, hard-delete the old document; after phase completion, supersede instead.

## 8. Attribute-source matrix and Final Source of Truth

DI preserves every extracted value with its document lineage. Audit Core V2 exposes a logical attribute/source matrix rather than fixed `source_1..source_5` database columns.

For a given attribute, values may accumulate from Booking and later Delivery documents. Earlier candidate values are not overwritten merely because a later source arrives.

The workbook `Final Source of truth` determines the authoritative value when that source becomes available, subject to an explicitly approved source-label → DI document-type mapping.

Rules:

- supporting sources may supply candidate/provisional values;
- a supporting source never silently becomes the final authority when a configured final source is absent;
- if a final-source document is due but absent, raise/recompute `FINAL_SOURCE_DOCUMENT_MISSING`;
- if the final-source document exists but the attribute is absent, raise/recompute `FINAL_SOURCE_FIELD_MISSING`;
- if an extracted field confidence is <92%, expose `Needs Review` **and display the numeric confidence in the Review/Audit UI**;
- source-of-truth rules are evaluated when the corresponding source is due; Delivery-only sources must not create false Booking-stage missing flags.

The exact workbook source labels are preserved. A source label is mapped to a DI `document_type_key` only when that mapping is explicitly supported by the existing document catalogue/design. Unresolved source aliases remain visible configuration work; they are not guessed.

After Delivery submission the Audit UI renders a live cross-source table:

`Attribute | Resolved Value | actual source document columns... | Match/Mismatch/Single Source/Not Available`

Each source value links to the exact boxed document evidence. This table is a projection over DI facts, not duplicated Audit Core storage.

## 9. Audit recomputation

V2 audit outcomes are derived/recomputable.

Recompute is triggered asynchronously by meaningful evidence changes, including:

- extraction completion;
- replacement/supersession;
- conditional declaration change;
- Booking completion;
- Delivery completion.

At minimum V2 supports/recomputes these finding classes:

- `MANDATORY_DOCUMENT_MISSING`
- `CONDITIONAL_DOCUMENT_NOT_PROVIDED`
- `CLASSIFICATION_UNRESOLVED`
- `FINAL_SOURCE_DOCUMENT_MISSING`
- `FINAL_SOURCE_FIELD_MISSING`
- `FIELD_REVIEW_REQUIRED`
- existing business mismatch rules where their required evidence is available.

## 10. Delivery V2

Delivery uses the same V2 capture engine and phase-lock semantics. Audit Core supplies the Delivery REQUIRED/CONDITIONAL/OPTIONAL document list from its master. Delivery-specific implementation follows Booking V2 after the Booking vertical slice is proven; no second document-capture architecture is introduced.

After Delivery submission, the Audit Review consumes Booking + Delivery DI facts through the same common attribute mapper and displays the cross-source comparison view defined in the 2026-08-30 amendment.

## 11. Rollout

V2 is exposed in parallel with the legacy flow.

- `document_capture_v2_enabled = true|false`
- legacy remains visible during pilot.
- when V2 is proven, legacy can be hidden without changing the V2 contracts or migrating evidence back into legacy capture tables.

## 12. Review amendment reference

The detailed mapping, PC confirmation rules, V1 no-duplication correction, evidence viewer, API contracts and post-Delivery comparison are defined in:

`docs/uc-003-booking-delivery-audit/UC03_ATTRIBUTE_AUDIT_REVIEW_DESIGN_2026-08-30.md`
