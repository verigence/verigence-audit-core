# UC03 — PC Booking Document Upload — Implementation Plan

**Document ID:** `VUC03-PC-DOC-PLAN-001`  
**Date:** 2026-08-26  
**Status:** APPROVED IMPLEMENTATION SEQUENCE  
**Design:** `UC03_PC_BOOKING_DOCUMENT_UPLOAD_DESIGN_2026-08-26.md`  
**Scope:** PC Booking document upload + extraction review/approval/correction only  
**Branch in touched repos:** `planning/uc03-pc-document-upload-direct-di`

## 1. Objective

Replace the current PC Booking path in which Audit Core transports file bytes and refreshes/copies DI extraction with the focused design:

```text
prepare context:        Web -> Audit Core -> DI (once/context)
upload binary:          Web -> DI
linkage:                DI -> Audit Core (async, requirementRef + documentId only)
extraction review:      Web -> DI
approved/corrected data Web -> Audit Core (one batch/document)
```

No TL, Delivery, confidence-driven workflow, Security redesign, or R2 hierarchy change is part of this plan.

## 2. Grounded constraints from current code

### 2.1 Security token shape

The current browser login token is a global USER identity token only. It intentionally contains no Tenant, role or permission claims. Therefore the existing DI `require_tenant_permission()` dependency cannot authorize the new direct browser path.

Implementation rule:

- DI verifies the global USER identity token cryptographically;
- DI calls Security's existing live `/security/v1/authorization/check` using DI's own ServiceIntegration identity for the selected Tenant and required `di.*` permission;
- only a successful ALLOW is cached briefly, matching the existing Audit Core pattern;
- no Security repository source change is made.

Required DI runtime settings:

```text
DI_SECURITY_BASE_URL
DI_SECURITY_CLIENT_ID
DI_SECURITY_CLIENT_SECRET
DI_AUDIT_CORE_BASE_URL
```

Existing `DI_SECURITY_JWKS_URL` remains the human-token signature source.

### 2.2 Existing DI storage path is reused

`audit_storage_contexts` and `intake_document(..., audit_storage_context=...)` already freeze the Project/Dealer/Outlet/Customer slugs and build the correct R2 key. No alternative upload/storage implementation is permitted.

### 2.3 Existing DI `fields` endpoint is insufficient

The existing context `/fields` endpoint exposes accepted/CONFIRMED `document_field_values`. PC Screen 3 needs immutable pre-confirmation `extracted_facts`. A separate review endpoint is required.

### 2.4 Existing Audit Core proposal table is not the new machine-data cache

`journey_capture_proposals` remains for backward compatibility/history, but the new direct path must not populate it from DI simply to render Screen 3.

### 2.5 Source fact version

`docintel.extracted_facts` is immutable and has a unique `extracted_fact_id`, but no separate row-version column. For this increment:

```text
sourceFactRef     = extracted_fact_id
sourceFactVersion = 1
```

A re-extraction creates a new immutable `sourceFactRef`; it does not update the existing fact. No DI schema column is added merely to manufacture a second version identity.

## 3. DI implementation

### D1 — schema: lightweight document linkage delivery state

Add migration `0017_uc03_pc_booking_direct_link.py` (after `0016_uc03_business_document_types`). Extend `docintel.documents` only with the minimum reliable callback state:

```text
audit_requirement_ref               varchar(160) nullable
audit_link_status                   varchar(20) NOT NULL DEFAULT 'NOT_REQUIRED'
                                    NOT_REQUIRED | PENDING | ACKNOWLEDGED
audit_link_attempt_count            integer NOT NULL DEFAULT 0
audit_link_last_attempt_at_utc      timestamptz nullable
audit_link_acknowledged_at_utc      timestamptz nullable
audit_link_last_error               text nullable
```

Index only pending links:

```text
(tenant_id, audit_link_status, registered_at_utc)
WHERE audit_link_status='PENDING'
```

No Project/Dealer/Outlet/Customer or confidence duplication is added.

### D2 — human identity + live DI permission authorization

Add DI components modeled on the already-proven Audit Core authorization pattern:

- verify global Security human token (`actor_type=USER`, no authority claims trusted);
- obtain/reuse Security ServiceIntegration token with `audience=security`;
- POST `/security/v1/authorization/check` for `userId + tenantId + permissionKey`;
- cache ALLOW briefly (60s maximum); never cache DENY/error.

New direct PC endpoints use this dependency. Existing service-only Audit Storage Context endpoints remain unchanged.

### D3 — direct PC upload endpoint

Add a human-authorized endpoint under the existing Audit Storage Context namespace:

```http
POST /v1/tenants/{tenantId}/audit-storage-contexts/{externalContextRef}/pc-booking-documents
```

Multipart:

```text
file
documentTypeKey
requirementRef
```

Permission: `di.document.upload`.

Flow:

1. validate global human identity;
2. live authorize `di.document.upload` for route Tenant;
3. resolve existing Audit Storage Context;
4. provision the human actor in DI if required by current FK model;
5. call existing `intake_document` with the same Audit Storage Context;
6. on accepted/FIT upload, persist opaque `audit_requirement_ref` and mark link `PENDING` in the same intake transaction;
7. wake the worker;
8. return current DI upload response with `documentId`.

No Audit Core call blocks the upload response.

### D4 — durable async linkage delivery

Extend the existing DI worker loop to process at most one pending Audit linkage per loop iteration in addition to extraction work.

Selection:

```text
upload_status='FIT'
audit_link_status='PENDING'
FOR UPDATE SKIP LOCKED
```

Callback:

```http
POST {DI_AUDIT_CORE_BASE_URL}/v1/internal/di/booking-document-links
Authorization: Bearer <Security ServiceIntegration token, aud=audit>

{
  "requirementRef": "...",
  "documentId": "..."
}
```

On 2xx: mark `ACKNOWLEDGED` once.

On failure: increment attempt count, retain `PENDING`, store a bounded safe error summary. Subsequent worker polls retry the same logical payload. Do not couple callback retry to extraction completion.

### D5 — direct context status endpoint

Add:

```http
GET /v1/tenants/{tenantId}/audit-storage-contexts/{externalContextRef}/pc-booking-documents
```

Permission: `di.document.read`.

Return one lightweight list for documents in that context:

```text
documentId
auditRequirementRef
documentTypeKey
uploadStatus
processingStatus
registeredAtUtc
```

No extracted values and no confidence in this status response.

### D6 — extraction review endpoint

Add:

```http
GET /v1/tenants/{tenantId}/audit-storage-contexts/{externalContextRef}/pc-booking-documents/{documentId}/extraction-review
```

Permission: `di.document.fields.read`.

Read immutable `extracted_facts` for the latest/current completed processing run and return:

```text
sourceFactRef
sourceFactVersion = 1
fieldKey
rawValue / normalizedValue
confidenceScore
pageNo
evidenceRegion
foundStatus
```

The endpoint is available before DI human verification/confirmation. It does not mutate `document_field_values`.

### D7 — direct original content endpoint

Add a human-authorized PC Booking content route reusing `_document_content_response` and existing context ownership validation.

Permission: `di.document.content.read`.

Do not duplicate storage reads or buffer the whole object in memory.

## 4. Audit Core implementation

### A1 — prepare context endpoint

Add a PC Booking preparation endpoint:

```http
POST /v1/tenants/{tenantId}/journeys/{journeyId}/booking/document-upload-context
```

Use global human identity + existing live Security authorization/business scope.

One call:

1. require active Booking;
2. load Journey/customer/dealer/outlet/project context;
3. resolve/create the existing DI subject mapping if absent;
4. ensure the DI Audit Storage Context once through the existing service-only DI API;
5. return `externalContextRef` and applicable Booking requirement slots:

```text
requirementRef = journey_document_requirement_id
requirementKey
documentTypeKey
required/applicability metadata already needed by the UI
currentDocumentId if already linked
```

No binary crosses this endpoint.

### A2 — internal DI linkage callback

Add service-authenticated endpoint:

```http
POST /v1/internal/di/booking-document-links
```

Payload exactly:

```json
{"requirementRef":"...","documentId":"..."}
```

Audit Core validates a Security ServiceIntegration token with audience `audit`.

Database behavior in one transaction:

1. resolve requirement row by `journey_document_requirement_id`;
2. require Booking process area/applicable Journey;
3. derive tenant/journey/customer/document type from Audit Core data;
4. idempotently create/reuse an `auditcore.evidence` linkage for the DI document;
5. make it the current `journey_document_assessments.evidence_id` for that requirement;
6. preserve prior evidence rows as history; do not delete the older DI document;
7. do not copy DI processing/confidence/extraction values into current-state caches for the new path.

Duplicate delivery of the same pair returns success without another business event.

### A3 — batch PC extraction decision endpoint

Add:

```http
POST /v1/tenants/{tenantId}/journeys/{journeyId}/booking/document-extraction-decisions
```

One document per request; many fields per request.

Validate:

- active Booking and PC scope;
- `requirementRef` belongs to this Journey;
- `documentId` is the currently linked DI document for that requirement;
- supported DI field mapping;
- decision is `APPROVED` or `CORRECTED`;
- confidence 0..100 when supplied;
- unique field/source fact entries in the batch.

Reuse existing typed-domain `_write_typed_capture` logic. Do **not** call DI from Audit Core and do **not** create DI proposal-cache rows.

For every field append one immutable `journey_workflow_events` record with event type:

```text
BOOKING_EXTRACTION_APPROVED
BOOKING_EXTRACTION_CORRECTED
```

Audit-event safe payload includes:

```text
requirementRef
documentId
fieldKey
sourceFactRef
sourceFactVersion
sourceConfidence
decision
owningDomainKey
owningRecordReference
```

The confidence score exists only in this append-only event. Do not persist confidence in Booking/customer/evidence/assessment/current-state tables.

To avoid duplicating sensitive business values in generic audit JSON, the authoritative approved/corrected value remains in the typed domain. The audit event identifies exactly which field changed and its source fact/document; the original machine value remains in DI.

All typed writes + all audit events + aggregate-version update commit atomically in one DB transaction for the document.

### A4 — old refresh/proxy path

Do not delete old endpoints in the first compatibility commit. Stop the PC Booking Web flow from calling:

```text
/booking/extraction/refresh
Audit Core evidence binary upload proxy
Audit Core Screen-3 evidence fact/content proxy
per-proposal accept/correct endpoints
```

They can be deprecated after functional proof; removing them in the same change would enlarge regression scope unnecessarily.

## 5. Web/Android implementation

### W1 — DI Booking service

Add `src/services/di/bookingDocuments.ts` using the same fetch/error/correlation style as DI Test Console but **not** its mock-token behavior.

Functions:

```text
uploadPcBookingDocument(...)
listPcBookingDocuments(...)
getPcBookingExtractionReview(...)
getPcBookingDocumentContent(...)
```

All calls use the normal logged-in global Security access token and selected Tenant route.

### W2 — Audit Core service changes

Add:

```text
prepareBookingDocumentUploadContext(...)
submitBookingDocumentExtractionDecisions(...)
```

Keep old service functions temporarily for compatibility but remove them from the active PC path.

### W3 — Step 1 document upload

On entering/initializing the Booking document step:

- prepare context once and cache it for the Booking workspace lifetime;
- upload each selected file directly to DI;
- mark UI `Uploaded` from DI's synchronous response;
- do not wait for extraction;
- do not call Audit Core upload afterward;
- do not show confidence.

Missing document rules/Continue Anyway behavior already implemented remain unchanged.

### W4 — Screen 3 extraction review

When Screen 3 opens:

- one DI context list call;
- refresh only while at least one relevant document is pending and the screen remains mounted;
- use bounded backoff (3s, 5s, 8s, 10s cap);
- stop polling when all documents are terminal or the component unmounts;
- lazy-load extraction/content for a document when its review card/panel is opened;
- use DI page/box data in the existing review visualization;
- never render confidence.

DI read failure is non-blocking for Booking progression, consistent with the current product decision.

### W5 — document approval/correction

Maintain field edits locally while reviewing one document. On document approval/continue action, submit one batch to Audit Core. Include DI `sourceConfidence` even though it is not displayed so Audit Core can write it to the audit event.

## 6. Test sequence

### DI

- migration upgrade/downgrade shape;
- global human token accepted only as identity; forbidden authority claims rejected;
- Security ALLOW/DENY/error tests;
- direct upload uses existing Audit Storage Context and identical R2 hierarchy builder;
- accepted upload returns `documentId` without waiting for extraction;
- `requirementRef` is opaque and stored once;
- pending link callback success/retry/acknowledgement;
- status endpoint returns lightweight state only;
- extraction review reads `extracted_facts` before confirmation and returns source fact/page/box/confidence;
- content route streams existing original.

### Audit Core

- prepare context reuses existing subject/context and returns requirement IDs;
- service callback authentication/audience;
- callback idempotency;
- replacement document makes assessment point to new evidence while retaining old evidence history;
- batch approved decision writes typed data + event;
- batch corrected decision writes corrected typed data + event;
- confidence appears in workflow-event audit payload only;
- no `journey_capture_proposals` insert for direct path;
- transaction rollback proves typed value cannot commit without audit event;
- wrong requirement/document/Journey rejected.

### Web

- TypeScript build and lint;
- upload service sends binary to DI URL, not Audit Core;
- Step 1 does not wait for extraction;
- Screen 3 batch status polling stops correctly;
- direct extraction review drives page/box UI;
- confidence is retained in request model but not rendered;
- one Audit Core decision call per reviewed document, not one per field;
- existing Continue Anyway behavior remains non-blocking.

### Functional acceptance

Use a real Booking flow, not login-only smoke:

```text
Booking documents visible
-> upload at least one processing document
-> prove R2 path hierarchy
-> prove DI documentId response
-> prove Audit Core async linkage
-> move to Details while extraction continues
-> enter Screen 3
-> see extraction arrive through DI refresh
-> approve one field and correct one field
-> submit document once
-> verify typed Audit Core values
-> verify immutable audit events contain confidence
-> verify DI extracted facts remain unchanged
```

No `DONE` status until this functional path passes.

## 7. Implementation order / rollback boundary

Execute in this exact order:

```text
1 DI auth + schema + direct APIs + linkage worker
2 Audit Core prepare + callback + batch decision
3 contract/unit tests for DI and Audit Core
4 Web service rewiring
5 Web build/tests
6 full PC Booking functional test
```

Each repository remains independently revertible on the feature branch. No deployment or `dev` merge is part of this plan unless separately authorized.
