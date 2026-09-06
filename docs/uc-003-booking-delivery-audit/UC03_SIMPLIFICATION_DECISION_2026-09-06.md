# UC03 Simplification Decision — 06-Sep-2026

**Date:** 2026-09-06  
**Status:** DRAFT — PENDING BUSINESS CONCURRENCE ON OVERRULES  
**Scope:** UC03 Booking / Delivery capture, DI-to-Audit-Core persistence, Journey Detail View, resume/edit behavior, and source precedence.  
**Important:** This document does **not** supersede any earlier approved UC03 rule until the specific conflict is explicitly approved in the Concurrence Register below.

---

## 1. Purpose

UC03 has accumulated multiple implementation amendments and parallel flow variants. This note establishes one dated decision point so new work does not continue to inherit conflicting assumptions from older Booking / Delivery documents.

The governing principle is:

> Where an older UC03 rule conflicts with a rule explicitly approved in this document, the approved 06-Sep-2026 rule takes precedence. Older documents remain historical evidence and are not deleted.

No rule is treated as overruled merely because it appears in this draft.

---

## 2. Decision status model

Every rule in this document has one of three states:

- **CONFIRMED BUSINESS INTENT** — explicitly stated by the business owner in the 06-Sep-2026 simplification discussion. This records intent, but any identified conflict with an older approved rule is still listed separately for concurrence.
- **PENDING CONCURRENCE** — an older approved rule would need to be superseded or narrowed. No implementation should rely on the supersession until approved.
- **UNCHANGED** — existing rule remains in force.

After concurrence, each approved conflict will be changed to **APPROVED OVERRULE** with the approval date.

---

## 3. Confirmed business intent — target UC03 flow

### 3.1 Booking is reduced to two user screens

Target flow:

```text
Dashboard
  -> Capture New Booking
  -> Journey created immediately
  -> Screen 1: Documents
  -> Screen 2: Review & Submit
```

The separate Customer Name screen is removed.

The separate Booking Additional Information / Booking Details screen is removed from the normal PC capture flow.

The Review screen becomes the final Booking submission screen.

### 3.2 No PC-entered customer name is required to create the Journey

When PC clicks `Capture New Booking`:

1. Audit Core creates the Journey immediately.
2. The Journey ID becomes the stable reference used in place of the earlier PC-entered customer-name reference for the existing DI/R2 context.
3. No DI storage hierarchy redesign is introduced.
4. The existing DI/R2 structure, APIs and storage-context model otherwise remain intact.
5. The customer record may remain provisional until documentary identity data arrives.

The Journey ID is a technical/reference value only. It is not the canonical customer identity.

### 3.3 No manual fallback entry screen for old Booking Details fields

Fields that were previously keyed by PC on the removed Additional Information / Booking Details screen should not be requested merely to keep the old flow alive.

For each such field:

```text
If a mapped document extract exists -> use the DI/document value.
If no mapped document value exists -> leave the Audit Core field NULL for now.
```

Examples of fields that may remain NULL when no document source is available include items such as Deal Source / Lead Source until a later business decision is made.

No value should be inferred from a loosely related field simply to avoid NULL.

### 3.4 DI data persistence is independent of confidence review

All confirmed DI extracted values must flow into Audit Core according to the agreed field mapping, irrespective of confidence.

Confidence determines review requirement, not whether data is persisted.

```text
DI extraction completes
  -> persist lossless DI lineage
  -> materialize mapped Audit Core business fields
  -> confidence >= 90%: no PC confidence review required
  -> confidence < 90%: PC review required / audit flag raised
```

### 3.5 Extraction completed before Booking submit

If extraction is complete before the PC submits Booking:

- all mapped DI values are already available in Audit Core;
- fields with confidence >= 90% are accepted automatically for confidence purposes;
- only fields with confidence < 90% require PC review;
- required low-confidence review is completed before Booking submission.

### 3.6 Extraction completed after Booking submit

If documents are uploaded but extraction is still running when PC submits Booking:

- Booking submission is not blocked solely by extraction still running;
- DI continues asynchronously;
- when extraction completes, all mapped values are automatically persisted/materialized into Audit Core;
- any field with confidence < 90% is flagged for later PC review;
- a field with confidence >= 90% requires no PC confidence-review action;
- Journey Detail View must show the values as soon as Audit Core has them.

If PC later changes a low-confidence DI value after Booking submission:

- the corrected value becomes the effective/canonical value;
- the original DI value and provenance remain preserved;
- an informational alert/finding is raised for TL visibility.

### 3.7 Source precedence for overlapping facts

Customer identity:

- PAN / Aadhaar are the governing documentary sources for customer identity information according to the agreed source-of-truth mapping.
- Booking Form customer values remain evidence/provenance and must not silently override higher-priority KYC sources.

Booking vs Delivery:

- where the same business fact exists in both Booking and Delivery evidence, Delivery-stage information takes precedence after Delivery processing, subject to the explicit field/source mapping.
- earlier Booking values remain available as source history/provenance.

### 3.8 Journey Detail View is the consolidated read model

The Journey Detail View must be able to show all extracted DI information captured so far for the Journey.

The desired trace is:

```text
DI document
  -> extracted field
  -> reviewed/effective value where applicable
  -> Audit Core storage / canonical owner
  -> Journey Detail API
  -> Journey Detail View
```

No extracted DI field should silently disappear because a richer typed owner has not yet been implemented. Lossless Audit Core storage remains mandatory, while typed canonical owners are used wherever defined.

### 3.9 View / Edit behavior

`View Booking` / `View Delivery` always opens the consolidated Journey Detail View, regardless of whether the Journey/stage is complete.

If complete:

- PC: view only + Raise Concern; no Edit action.
- TL/PM: view + controlled Edit.

If incomplete:

- PC: view captured-so-far data + Edit/Continue according to the Journey process.
- TL/PM: view + Edit.

`Edit` / `Continue` for an incomplete Booking or Delivery resumes the correct capture step rather than opening an unrelated page.

---

## 4. Rules explicitly unchanged

The following are not being redesigned by this simplification unless a later approved amendment says otherwise:

1. DI remains the owner of document classification/extraction machinery and evidence provenance.
2. The existing DI/R2 storage architecture remains intact; only the reference value formerly based on PC-entered customer name is proposed to use Journey ID.
3. Document upload/custody/retry behavior remains asynchronous and durable.
4. Existing source-of-truth field mapping remains the basis for deciding which document wins for a field.
5. No processing-order precedence is introduced.
6. Existing evidence history must remain auditable; corrections must not destroy the original DI value.
7. Delivery remains the same UC03 Journey, not a separate unrelated customer/record.

---

## 5. Candidate overruling conflicts — BUSINESS CONCURRENCE REQUIRED

The following are the specific older rules that conflict with the target simplification. They remain **PENDING CONCURRENCE** until explicitly approved.

### C-01 — Three-step Booking flow vs two-screen Booking flow

**Existing approved rule:**  
`UC03_V2_FAST_BOOKING_SEQUENCE_2026-08-30.md` defines `Documents -> Booking Details -> Submit Booking -> Booking Attribute Review`.  
`UC03_DOCUMENT_CAPTURE_V2_FROZEN_DESIGN_2026-08-29.md` defines Screen 1 Documents, Screen 2 Booking Details, Screen 3 Booking Attribute Review.

**Proposed 06-Sep-2026 rule:**  
Normal PC Booking becomes exactly `Documents -> Review & Submit`. The separate Booking Details / Additional Information screen is removed.

**Impact:** Web routing, Audit Core Booking submit contract, state resolver, regression tests.

**Status:** PENDING CONCURRENCE.

---

### C-02 — Booking submitted on Booking Details vs submitted on Review

**Existing approved rule:**  
Booking is submitted on Screen 2 Booking Details, then Review opens afterward.

**Proposed 06-Sep-2026 rule:**  
Review becomes Screen 2 and owns final Booking submission.

**Impact:** Web submit action, Audit Core V2 submit API/command shape, stage transition timing.

**Status:** PENDING CONCURRENCE.

---

### C-03 — Mandatory manually keyed Booking Details vs nullable/document-driven values

**Existing approved rule:**  
Current V2 Booking Details requires operational fields such as Customer Type, Deal Type, Deal Source, Lead Source, Registration State, Territory, District, Registration Type, Registration Category and Outright Purchase, and persists them during Booking submit.

**Proposed 06-Sep-2026 rule:**  
The PC is not required to key those fields in a separate screen. If an approved DI mapping provides the value, use the extracted document value. Otherwise leave the Audit Core field NULL for now. NULL alone does not block Booking submission.

**Impact:** Audit Core validation/persistence, Web field requirements, nullable workflow assumptions, reporting expectations.

**Status:** PENDING CONCURRENCE.

---

### C-04 — Customer name required before capture vs Journey-ID-first creation

**Existing implementation assumption:**  
Booking creation currently expects a PC-entered customer name and the DI storage display context can derive a customer slug/reference from it.

**Proposed 06-Sep-2026 rule:**  
No customer name is requested before document capture. Journey is created first. Journey ID is supplied in the existing reference/display slot used for the R2/DI context. No DI folder/API redesign is introduced.

**Impact:** Web Create Booking contract, Audit Core create transaction, DI display/reference input only.

**Status:** PENDING CONCURRENCE.

---

### C-05 — 92% confidence review threshold vs 90%

**Existing approved rule:**  
`UC03_DOCUMENT_CAPTURE_V2_FROZEN_DESIGN_2026-08-29.md` states `<92%` = Needs Review and `>=92%` does not create confidence-only review work.

**Proposed 06-Sep-2026 rule:**  
`<90%` requires PC review. `>=90%` is automatically accepted for confidence-review purposes.

**Impact:** Audit Core review policy, Web Review presentation, tests, historical design text.

**Status:** PENDING CONCURRENCE.

---

### C-06 — Audit Core does not duplicate raw DI values vs all DI values must be retained in Audit Core

**Existing approved rule:**  
The 29/30-Aug V2 documents state that Audit Core should not duplicate DI raw extracted values and that confirmed references/typed projections are sufficient.

**Proposed 06-Sep-2026 rule:**  
Every DI extracted field must be durably represented in Audit Core lossless lineage/storage so it can be audited and shown in Journey Detail. Mapped fields additionally materialize into their canonical business owners. This is not intended to replace DI as extraction owner.

**Impact:** Audit Core persistence model and older no-duplication wording.

**Status:** PENDING CONCURRENCE.

---

### C-07 — PC confirmation blocked while extraction pending vs Booking may submit while extraction continues

**Existing approved rule:**  
The frozen 29-Aug design says PC confirmation is blocked while extraction is pending or a document has failed processing.

**Proposed 06-Sep-2026 rule:**  
If required document-capture/classification conditions are satisfied, Booking can be submitted while extraction is still processing. Late extraction automatically fills Audit Core and creates PC review work only for <90% fields.

**Impact:** Booking closure gate, late-DI callback/materialization, PC work-item generation.

**Status:** PENDING CONCURRENCE.

---

## 6. Implementation guardrails after concurrence

After a conflict is approved, implementation must follow these guardrails:

- Do not redesign DI storage layout or existing DI APIs merely to implement Journey-ID reference usage.
- Do not create replacement manual-entry fields to mimic the removed Booking Details screen.
- Do not infer values when the source mapping does not provide one; retain NULL.
- A stale/null UI payload must never overwrite a newer DI-populated canonical value.
- PC correction explicitly made during required review may update the effective canonical value while preserving DI provenance.
- Data persistence and Journey Detail availability must not depend on the PC opening Review again after asynchronous DI completion.
- Every approved rule must have an end-to-end regression test covering the actual DEV flow.

---

## 7. Concurrence Register

No candidate override below is approved by the existence of this document.

| Conflict | Decision | Approval date | Notes |
|---|---|---|---|
| C-01 Two-screen Booking | PENDING | — | — |
| C-02 Submit on Review | PENDING | — | — |
| C-03 Document-driven / NULL old manual fields | PENDING | — | — |
| C-04 Journey-ID-first / no customer-name screen | PENDING | — | — |
| C-05 90% confidence threshold | PENDING | — | — |
| C-06 Lossless DI facts in Audit Core | PENDING | — | — |
| C-07 Submit allowed while extraction continues | PENDING | — | — |

Once business concurrence is given, update only the approved rows to `APPROVED OVERRULE`, record the approval date, and then update the affected implementation/design references. Do not broadly mark unrelated historical UC03 documents obsolete.

---

## 8. Acceptance baseline after approved simplification

The minimum Booking acceptance journey will be:

```text
Login
 -> Dashboard
 -> Capture New Booking
 -> Journey created without customer-name entry
 -> Documents
 -> Review & Submit
 -> Dashboard / Journey Detail
```

Verification must prove:

- Journey ID is created and used as the temporary/reference value without DI storage redesign;
- document upload/classification still works;
- DI extraction values populate Audit Core whether extraction completes before or after Booking submit;
- >=90% values require no confidence-only PC action;
- <90% values generate PC review work;
- approved DI-mapped former manual fields are populated from documents;
- unmapped former manual fields remain NULL without blocking Booking;
- Review/Submit is the second and final normal PC Booking screen;
- Journey Detail shows all extracted information available so far;
- later Delivery values take precedence where the approved source mapping says so;
- PC/TL/PM permissions follow the consolidated View/Edit rule.

---

## 9. Authority statement

This document becomes the dated UC03 simplification authority **only for the individual conflict rows explicitly marked `APPROVED OVERRULE`**. Until then, it is a controlled draft capturing the proposed simplified target and the precise older rules that require business concurrence.
