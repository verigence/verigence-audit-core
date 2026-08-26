# UC03 — PC Booking Document Upload — Direct DI Design

**Document ID:** `VUC03-PC-DOC-001`  
**Date:** 2026-08-26  
**Status:** FROZEN FOR IMPLEMENTATION  
**Scope:** **Process Consultant (PC) — Booking Document Upload and PC extraction approval/correction only**  
**Implementation branch:** `planning/uc03-pc-document-upload-direct-di`

## 1. Scope and precedence

This document changes only the UC03 PC Booking document path. It does **not** redesign UC03 Booking generally, Delivery, TL/PM review, Audit flag review, DI verification, Project onboarding, or Security.

For this narrow slice, this document supersedes the following earlier statements wherever they conflict:

- `UC03_IMPLEMENTATION_DESIGN_v0.1.md` statement that Web/Android never calls DI directly;
- the Audit-Core-mediated binary upload / extraction-refresh design for PC Booking documents;
- `UC03_EXTRACTION_CORRECTION_POLICY_2026-08-25.md` confidence-driven UI/business-state/TL behavior for a PC extraction correction.

The earlier policy's requirement to retain the confidence observed at the PC decision point remains valid **only as append-only audit provenance**. Confidence is not retained in current Booking/document/business state and does not drive TL behavior in this increment.

All other UC03 design remains unchanged.

## 2. Frozen principles

1. **Document binary travels once:** Web/Android -> DI directly.
2. **R2 hierarchy is unchanged:** Project -> Dealer -> Outlet -> Customer -> Documents.
3. **Audit Storage Context remains the DI storage boundary.** It is established once for the Booking Journey/customer context and reused by all PC document uploads.
4. **Upload acceptance is synchronous; extraction is asynchronous.** A successful upload response means the document was accepted/registered and stored. It does not mean extraction is complete.
5. **DI owns immutable machine evidence:** original document, extraction, source facts, fact versions, page/box localization, processing state, and confidence.
6. **Audit Core does not persist confidence in operational/current-state tables.** The confidence attached to a reviewed DI source fact is copied only into the append-only Audit extraction decision event at PC approval/correction time.
7. **Cross-system upload linkage is minimal:** `journey_document_requirement_id` (called `requirementRef` at the boundary) <-> `documentId`.
8. **DI treats `requirementRef` as opaque.** DI does not own or derive Audit Core `requirementKey` semantics.
9. **DI emits one logical asynchronous linkage notification after successful document registration.** The same notification may be retried until acknowledged; retries do not represent additional business events.
10. **Audit Core linkage consumption is idempotent.** Re-delivery of the same `requirementRef + documentId` produces one active linkage.
11. **PC Screen 3 reads extraction directly from DI.** Audit Core is not used as an extraction/status/content proxy.
12. **PC-approved/corrected values go to Audit Core.** Audit Core stores the approved business value and provenance/audit history; DI's machine extraction remains unchanged.
13. **Approval/correction is batched per document** to minimize API calls and DB transactions.
14. **No TL functionality is introduced by this increment.**
15. **No Security source change is required unless runtime validation proves a gap.** The approved PC bundle already includes the required DI document permissions.

## 3. Ownership boundary

### DI owns

- `documentId`;
- original binary and R2 object;
- Audit Storage Context association;
- document type processing;
- asynchronous extraction;
- immutable extracted source facts and fact versions;
- page number and evidence/bounding-box localization;
- processing state;
- confidence associated with machine-extracted source facts.

### Audit Core owns

- Journey and Booking stage;
- `journey_document_requirement_id` / `requirementKey` / applicability;
- the active linkage from the Journey requirement to the DI `documentId`;
- PC-approved or PC-corrected business values;
- typed-domain persistence of those approved values;
- provenance from an approved value to DI `documentId + sourceFactRef + sourceFactVersion`;
- append-only audit/workflow record that the PC approved or corrected the extraction;
- the **confidence snapshot seen for that source fact at the decision time inside that append-only audit record only**.

### Web/Android owns no authoritative data

The client orchestrates the user experience only. It does not become a source of truth for document processing, linkage, approved business values, or confidence.

## 4. Target runtime sequence

### 4.1 Prepare upload context — once per Booking Journey

```text
Web/Android -> Audit Core: prepare Booking document upload context
Audit Core:
  authorize PC / active Booking
  resolve or create existing DI Subject mapping
  ensure DI Audit Storage Context once
  return contextRef + applicable document requirements
```

The response exposes, for each applicable Booking document slot:

```text
requirementRef   = journey_document_requirement_id UUID
requirementKey   = Audit Core business key for UI mapping
documentTypeKey  = expected DI document type
```

The client does not send Project/Dealer/Outlet/Customer names on each upload. DI obtains the existing frozen hierarchy from the Audit Storage Context.

### 4.2 Upload — synchronous acceptance

```text
Web/Android -> DI
  contextRef
  requirementRef
  documentTypeKey
  file

DI:
  authorize human token: di.document.upload
  resolve existing Audit Storage Context
  create Document
  write original to existing Project/Dealer/Outlet/Customer R2 hierarchy
  perform intake validation
  create async extraction job when required
  persist opaque requirementRef with the Document
  return documentId + uploadStatus
```

PC UI displays only an upload result such as `Uploaded` or `Upload failed`. It does not display confidence.

### 4.3 DI -> Audit Core linkage — one logical async notification

After successful registration/acceptance:

```json
{
  "requirementRef": "<journey_document_requirement_id>",
  "documentId": "<di_document_id>"
}
```

No other values are sent. Specifically the payload excludes:

- confidence;
- processing/extraction status;
- page/box;
- R2 path;
- Project/Dealer/Outlet/Customer data;
- extracted values;
- requirementKey/documentTypeKey.

Audit Core validates that `requirementRef` is an applicable Booking requirement and upserts the active `documentId` linkage idempotently.

A replacement upload for the same requirement makes the newly acknowledged document the current active document while historical evidence remains traceable; destructive deletion is not introduced.

### 4.4 Extraction — asynchronous and DI-local

DI extraction continues independently after upload. DI does not stream intermediate state into Audit Core.

Audit Core does not poll DI merely to cache processing status.

### 4.5 Screen 3 — direct DI read

When the PC reaches extraction review:

1. Web/Android calls one DI context-level lightweight document status/list endpoint.
2. If any relevant document is still processing, the client performs bounded lightweight refresh only while Screen 3 is open and pending documents remain.
3. For a ready document, Web/Android calls DI directly for:
   - original content;
   - machine extraction review data;
   - `sourceFactRef` / `sourceFactVersion`;
   - `pageNo`;
   - `evidenceRegion` / bounding box;
   - confidence for audit-provenance capture at the later PC decision.
4. Confidence is **not displayed** in the PC UI for this increment.

The existing DI endpoint that exposes only CONFIRMED fields is not sufficient for this flow. A dedicated pre-confirmation machine-extraction review read contract is required.

### 4.6 PC approval/correction — one Audit Core transaction per document

The PC reviews the fields locally and submits one batch for the document:

```json
{
  "requirementRef": "<requirement UUID>",
  "documentId": "<DI document UUID>",
  "fields": [
    {
      "fieldKey": "customer_name",
      "sourceFactRef": "<DI fact UUID>",
      "sourceFactVersion": 1,
      "sourceConfidence": 93.4,
      "decision": "APPROVED",
      "approvedValue": "Rahul Sharma"
    },
    {
      "fieldKey": "booking_reference_number",
      "sourceFactRef": "<DI fact UUID>",
      "sourceFactVersion": 1,
      "sourceConfidence": 78.6,
      "decision": "CORRECTED",
      "approvedValue": "BK-12345"
    }
  ]
}
```

`sourceConfidence` is audit provenance for the referenced immutable DI fact. It is not a business field.

Audit Core performs one transaction:

- validate PC scope and active Booking;
- validate `requirementRef -> documentId` linkage;
- validate supported field mapping;
- persist approved/corrected values into existing typed business domains;
- persist current decision provenance excluding confidence from operational/current-state tables (`documentId`, `sourceFactRef`, `sourceFactVersion`, decision, actor, timestamp);
- append the appropriate extraction-approved/extraction-corrected audit/workflow record, including the source confidence snapshot;
- commit atomically.

Audit Core operational/current-state tables do **not** persist:

```text
DI confidence
pageNo
bounding box/evidenceRegion
machine extraction processing status
R2 location
a second raw copy of the DI machine extraction
```

The append-only extraction decision audit record **does retain the source confidence snapshot** together with the field/source fact/document/actor/time and approved/corrected value required by the audit policy.

For a correction, the original machine value remains retrievable from immutable DI source fact provenance. Audit Core stores the corrected/approved business value because that is the value subsequently used by Audit business rules.

## 5. Minimal persistent linkage

### DI document-side linkage

A DI Document uploaded through the Audit Storage Context carries an optional opaque Audit requirement reference and linkage-delivery acknowledgement metadata sufficient for reliable retry.

Conceptually:

```text
external_requirement_ref
external_link_acknowledged_at_utc nullable
```

No Journey business data is duplicated in the DI Document.

### Audit Core requirement-side linkage

The active Booking requirement linkage is conceptually:

```text
journey_document_requirement_id
current_di_document_id
linked_at_utc
```

Existing evidence tables may be reused where they preserve this meaning without retaining DI processing/confidence caches. The implementation must not create a second extraction-data store.

## 6. Reliability

### Upload

Upload success is returned only after DI has completed intake acceptance/storage work required by the existing DI contract.

### Linkage

The DI -> Audit Core callback is asynchronous and durable:

- one logical notification per accepted uploaded Document;
- same payload retried if delivery fails;
- Audit Core callback is service-authenticated;
- Audit Core operation is idempotent;
- DI records acknowledgement without generating processing-state chatter.

No Kafka/RabbitMQ/new platform broker is introduced for this increment.

## 7. Security

The PC human Security token remains the caller identity for direct Web/Android -> DI operations.

The approved PC bundle already contains:

```text
di.subject.create
di.subject.read
di.document.upload
di.document.read
di.document.content.read
di.document.fields.read
```

Therefore no Security repository change is authorized by this design.

DI -> Audit Core asynchronous linkage uses ServiceIntegration authentication; no service secret is exposed to Web/Android.

## 8. Explicit non-goals

This increment does not implement or redesign:

- TL document list/review;
- PM review;
- Delivery document upload;
- DI human-verification workflow;
- confidence-based PC UI or confidence-driven TL workflow;
- confidence persistence in Booking/document/current-state business tables;
- R2 hierarchy changes;
- generic DI storage redesign;
- Project/Dealer/Outlet/Customer duplication;
- broader UC03 rule/flag lifecycle;
- CI/CD architecture.

## 9. Acceptance criteria

The increment is complete only when the following are proven on the implementation branch:

1. PC Booking screen obtains one reusable DI Audit Storage Context for the Journey.
2. Booking document binary goes Web/Android -> DI without traversing Audit Core.
3. Accepted file is present in the existing Project/Dealer/Outlet/Customer R2 hierarchy.
4. DI returns a `documentId` synchronously after accepted intake.
5. Extraction runs asynchronously and does not block PC progression from the upload screen.
6. DI emits one logical `requirementRef + documentId` linkage and retries safely until Audit Core acknowledges it.
7. Audit Core operational/current-state tables store no DI confidence and no copied machine extraction merely for display.
8. Screen 3 obtains document status/content/extraction/page/box/confidence directly from DI; confidence is not shown to the PC.
9. PC can approve/correct supported extracted fields.
10. Audit Core commits the approved/corrected business values plus DI provenance and append-only audit history atomically in one batch transaction per reviewed document.
11. The append-only extraction decision audit record captures the source confidence snapshot.
12. Existing R2 hierarchy is unchanged.
13. No TL or Delivery behavior is introduced.
