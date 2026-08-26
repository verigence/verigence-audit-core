# UC03 — PC Booking Document Upload — Implementation Handoff

**Document ID:** `VUC03-PC-DOC-HO-001`  
**Date:** 2026-08-26  
**Status:** READY FOR IMPLEMENTATION  
**Design:** `UC03_PC_BOOKING_DOCUMENT_UPLOAD_DESIGN_2026-08-26.md`  
**Plan:** `UC03_PC_BOOKING_DOCUMENT_UPLOAD_IMPLEMENTATION_PLAN_2026-08-26.md`

## 1. Handoff scope

Implement only the Process Consultant Booking document journey:

```text
PC Booking Document Upload
-> asynchronous DI extraction
-> PC Screen-3 extraction review
-> PC approve/correct
-> Audit Core typed value + immutable audit provenance
```

Do not expand scope into TL, PM, Delivery, general DI verification, or broader UC03 cleanup.

## 2. Branches

All implementation starts from current `dev` on:

```text
verigence-di         planning/uc03-pc-document-upload-direct-di
verigence-audit-core planning/uc03-pc-document-upload-direct-di
verigence-web        planning/uc03-pc-document-upload-direct-di
```

The older `planning/uc-003-booking-delivery-audit` branches are not the implementation baseline for this change because they are materially diverged from current DEV.

Security remains on its existing source of truth. No Security branch/change is authorized unless a concrete missing capability is proven.

## 3. Non-negotiable contracts

### Binary path

```text
Web/Android -> DI
```

The file binary must not traverse Audit Core in the new active PC path.

### R2 path

Reuse existing DI Audit Storage Context and existing `build_audit_original_key`. Do not change:

```text
Project / Dealer / Outlet / Customer
```

### Upload/extraction timing

```text
upload = synchronous acceptance
after upload = PC may continue
extraction = asynchronous
```

### Linkage payload

DI -> Audit Core sends only:

```json
{"requirementRef":"<journey_document_requirement_id>","documentId":"<DI document UUID>"}
```

No confidence, processing state, page/box, extracted value, R2 path, or duplicated hierarchy belongs in linkage.

### Screen 3

Screen 3 reads DI directly for:

```text
status
content
machine extraction
source fact ID
page/box
confidence (transported for audit only, not displayed)
```

### Approval/correction

One Audit Core request per document, many fields in the batch.

DI machine extraction is immutable.

Audit Core stores the approved/corrected business value in existing typed domains and writes an immutable audit event.

### Confidence

Confidence is:

```text
DI machine metadata                         YES
transported with PC decision for provenance YES
append-only Audit Core audit event          YES
PC UI display                               NO
Audit Core current/business/linkage tables  NO
TL workflow in this increment               NO
```

## 4. Authentication handoff

Current Security human login token proves global USER identity only; it does not carry Tenant/permission authority.

Therefore new DI PC Booking routes must not use the existing claim-based `require_tenant_permission()` path.

Required pattern:

```text
Browser global USER token
  -> DI validates signature + USER identity
  -> DI uses its ServiceIntegration identity
  -> Security /authorization/check(userId, tenantId, di.permission)
  -> ALLOW permits DI operation
```

Reuse short ALLOW-only caching; never cache DENY/error.

No client secret is exposed to Web/Android.

Expected DI runtime variables:

```text
DI_SECURITY_BASE_URL
DI_SECURITY_CLIENT_ID
DI_SECURITY_CLIENT_SECRET
DI_AUDIT_CORE_BASE_URL
```

Existing DI JWKS configuration continues to verify the human token.

## 5. Files expected to change

### verigence-di

Expected primary files:

```text
backend/alembic/versions/0017_uc03_pc_booking_direct_link.py
backend/src/verigence/di/settings.py
backend/src/verigence/di/auth/verifier.py
backend/src/verigence/di/auth/<live_authorization module>.py
backend/src/verigence/di/api/v1/audit_storage_contexts.py
backend/src/verigence/di/application/intake.py
backend/src/verigence/di/workers/processor.py
backend/src/verigence/di/main.py                 only if router/lifespan wiring is required
backend/tests/<focused direct booking tests>.py
```

Prefer small helpers/repositories over growing `audit_storage_contexts.py` excessively if implementation becomes hard to test.

### verigence-audit-core

Expected primary files:

```text
migrations/versions/0027_uc03_pc_direct_di_documents.py   only if linkage/event support requires schema delta
src/audit_core/security.py / dependencies.py              service callback validator if required
src/audit_core/uc03_booking_evidence.py or focused new module
src/audit_core/uc03_booking_capture.py
src/audit_core/main.py                                    router registration if focused new module
api/openapi-v1.yaml                                       when contract is stable
tests/<focused callback/context/batch tests>.py
```

Prefer reusing `auditcore.evidence`, `journey_document_assessments`, typed capture helpers and `journey_workflow_events` rather than creating parallel tables.

### verigence-web

Expected primary files:

```text
src/services/di/bookingDocuments.ts
src/services/audit-core/uc03Booking.ts
src/pages/BookingWorkspacePage.tsx
src/features/uc03/BookingDocumentDetails.tsx
src/features/uc03/BookingReviewDocumentPanel.tsx
src/features/uc03/DocumentFieldReview.tsx
focused tests
```

Only touch the components actually used by the current PC Booking path after grounding imports/call sites.

## 6. Compatibility rules

Do not remove the legacy Audit Core evidence upload/refresh/proposal endpoints in the first implementation pass. The new Web path simply stops using them.

Reason: removing old endpoints is unrelated cleanup and unnecessarily increases regression risk.

Do not drop `journey_capture_proposals` in this increment. The new path simply does not create proposal-cache rows.

## 7. Data and audit rules

### Active document linkage

Use the existing Audit Core evidence model if it can represent:

```text
requirementRef -> current DI document
```

without forcing copied DI processing data.

A replacement upload switches the requirement/assessment to the new evidence link; prior evidence remains historical.

### Extraction decisions

For each approved/corrected field, the immutable event must identify:

```text
requirementRef
documentId
fieldKey
sourceFactRef
sourceFactVersion
sourceConfidence
decision
actor + role
decision time
owning business-domain reference
```

The approved/corrected business value is persisted in the typed domain. Do not duplicate sensitive values into generic `safe_payload` merely to make logging convenient.

### DI source fact version

For current immutable DI facts:

```text
sourceFactVersion = 1
```

A re-extraction creates a new source fact UUID. Do not add a DI DB column solely to maintain an artificial counter.

## 8. Performance/transaction constraints

- one context preparation per Booking workspace, not per file;
- one file upload request per actual file;
- zero Audit Core binary relay requests;
- one DI context status call per refresh cycle, not N per document;
- heavy extraction/content loaded only when needed;
- one Audit Core DB transaction per reviewed document batch;
- no repeated DI -> Audit Core status callbacks;
- DI -> Audit Core only the one logical link event per accepted document, with retry of the same event when required.

## 9. Failure behavior

### Upload failure

Show upload failure. Do not create an active Audit Core link for a rejected/failed DI upload.

### Extraction still processing

PC can continue to other Booking work. Screen 3 shows the document as processing and refreshes DI directly.

### DI read/refresh failure

Do not trap the PC indefinitely. Existing non-blocking Booking behavior remains.

### DI -> Audit Core linkage failure

DI keeps the link PENDING and retries. Upload itself remains accepted. Audit Core callback is idempotent.

### Batch decision failure

Rollback the full document batch. Do not leave typed business values committed without their audit events.

## 10. Definition of done

Engineering is not `DONE` because builds are green.

It is `DONE` only after automated tests plus a real functional Booking prove:

```text
direct DI binary upload
correct R2 hierarchy
documentId returned
async extraction
async requirementRef/documentId linkage
Screen 3 direct DI status/extraction/content/page/box
PC approve + correct
one Audit Core batch submission
approved/corrected typed values persisted
confidence captured in append-only audit events only
DI machine facts unchanged
PC can continue while extraction/link refresh is not instantaneous
```

Do not record human UAT as passed unless a human actually performs and confirms it.

## 11. Explicit stop conditions

Stop and report rather than silently redesign if implementation proves any of these:

1. Security cannot authorize the PC's existing `di.*` permissions through current `/authorization/check`.
2. DI cannot obtain a Security ServiceIntegration token with the already-used service credentials pattern.
3. Audit Core cannot authenticate an `aud=audit` ServiceIntegration callback without a Security source change.
4. Existing Audit Storage Context cannot reproduce the current R2 hierarchy on the direct route.
5. Existing typed-domain capture does not have an approved owner mapping for a field the UI attempts to submit.

A stop condition is evidence of a missing capability; it is not permission to broaden scope automatically.
