# UC03 Simplification Decision — 06-Sep-2026

**Date:** 2026-09-06  
**Status:** APPROVED BUSINESS AUTHORITY  
**Scope:** UC03 Booking / Delivery capture, DI-to-Audit-Core persistence, Journey Detail View, resume/edit behavior, and source precedence.  
**Authority:** Business concurrence received on 2026-09-06 for C-01 through C-07.

---

## 1. Purpose

UC03 has accumulated multiple implementation amendments and parallel flow variants. This document establishes the dated governing decision point so new work does not continue to inherit conflicting assumptions from older Booking / Delivery documents.

The governing rule is:

> Where an older UC03 rule conflicts with a rule explicitly approved in this document, the approved 06-Sep-2026 rule takes precedence. Older documents remain historical evidence and are not deleted.

Only the explicitly approved conflict rows C-01 through C-07 are superseded by this authority. Unrelated earlier UC03 rules remain in force.

---

## 2. Decision status model

Rules in this authority use three states:

- **APPROVED BUSINESS RULE** — explicitly approved on 06-Sep-2026 and governing implementation.
- **APPROVED OVERRULE** — an older approved rule is superseded or narrowed by this document.
- **UNCHANGED** — existing rule remains in force.

No future rule may be treated as an overrule merely because it appears in discussion or implementation. Any new conflict with this authority requires explicit business concurrence before it supersedes these rules.

---

## 3. Approved business rules — target UC03 flow

### 3.1 Booking is reduced to two user screens

Approved flow:

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

**Status:** APPROVED BUSINESS RULE.

### 3.2 No PC-entered customer name is required to create the Journey

When PC clicks `Capture New Booking`:

1. Audit Core creates the Journey immediately.
2. The Journey ID becomes the stable reference used in place of the earlier PC-entered customer-name reference for the existing DI/R2 context.
3. No DI storage hierarchy redesign is introduced.
4. The existing DI/R2 structure, APIs and storage-context model otherwise remain intact.
5. The customer record may remain provisional until documentary identity data arrives.

The Journey ID is a technical/reference value only. It is not the canonical customer identity.

**Status:** APPROVED BUSINESS RULE.

### 3.3 No manual fallback entry screen for old Booking Details fields

Fields that were previously keyed by PC on the removed Additional Information / Booking Details screen must not be requested merely to preserve the old flow.

For each such field:

```text
If a mapped document extract exists -> use the DI/document value.
If no mapped document value exists -> leave the Audit Core field NULL for now.
```

Examples of fields that may remain NULL when no document source is available include Deal Source / Lead Source until a later business decision is made.

No value may be inferred from a loosely related field merely to avoid NULL.

**Status:** APPROVED BUSINESS RULE.

### 3.4 DI data persistence is independent of confidence review

All confirmed DI extracted values must flow into Audit Core according to the agreed field mapping, irrespective of confidence.

Confidence determines review requirement, not whether data is persisted.

```text
DI extraction completes
  -> persist lossless DI lineage in Audit Core
  -> materialize mapped Audit Core business fields
  -> confidence >= 90%: no PC confidence review required
  -> confidence < 90%: PC review required / audit flag raised
```

**Status:** APPROVED BUSINESS RULE.

### 3.5 Extraction completed before Booking submit

If extraction is complete before the PC submits Booking:

- all mapped DI values are already available in Audit Core;
- fields with confidence >= 90% are accepted automatically for confidence purposes;
- only fields with confidence < 90% require PC review;
- required low-confidence review is completed before Booking submission.

**Status:** APPROVED BUSINESS RULE.

### 3.6 Extraction completed after Booking submit

If documents are uploaded but extraction is still running when PC submits Booking:

- Booking submission is not blocked solely by extraction still running;
- DI continues asynchronously;
- when extraction completes, all extracted values are persisted in Audit Core lossless storage and all mapped values are automatically materialized into their canonical Audit Core owners;
- any field with confidence < 90% is flagged for later PC review;
- a field with confidence >= 90% requires no PC confidence-review action;
- Journey Detail View must show the values as soon as Audit Core has them.

If PC later changes a low-confidence DI value after Booking submission:

- the corrected value becomes the effective/canonical value;
- the original DI value and provenance remain preserved;
- an informational alert/finding is raised for TL visibility.

**Status:** APPROVED BUSINESS RULE.

### 3.7 Source precedence for overlapping facts

Customer identity:

- PAN / Aadhaar are the governing documentary sources for customer identity information according to the agreed source-of-truth mapping.
- Booking Form customer values remain evidence/provenance and must not silently override higher-priority KYC sources.

Booking vs Delivery:

- where the same business fact exists in both Booking and Delivery evidence, Delivery-stage information takes precedence after Delivery processing, subject to the explicit field/source mapping;
- earlier Booking values remain available as source history/provenance.

**Status:** APPROVED BUSINESS RULE.

### 3.8 Journey Detail View is the consolidated read model

The Journey Detail View must be able to show all extracted DI information captured so far for the Journey.

The required trace is:

```text
DI document
  -> extracted field
  -> reviewed/effective value where applicable
  -> Audit Core lossless storage
  -> canonical Audit Core owner where mapped
  -> Journey Detail API
  -> Journey Detail View
```

No extracted DI field may silently disappear because a richer typed owner has not yet been implemented. Lossless Audit Core storage is mandatory, while typed canonical owners are used wherever defined.

**Status:** APPROVED BUSINESS RULE.

### 3.9 View / Edit behavior

`View Booking` / `View Delivery` always opens the consolidated Journey Detail View, regardless of whether the Journey/stage is complete.

If complete:

- PC: view only + Raise Concern; no Edit action.
- TL/PM: view + controlled Edit.

If incomplete:

- PC: view captured-so-far data + Edit/Continue according to the Journey process.
- TL/PM: view + Edit.

`Edit` / `Continue` for an incomplete Booking or Delivery resumes the correct capture step rather than opening an unrelated page.

**Status:** APPROVED BUSINESS RULE.

---

## 4. Rules explicitly unchanged

The following are not redesigned by this simplification unless a later approved amendment explicitly says otherwise:

1. DI remains the owner of document classification/extraction machinery and evidence provenance.
2. The existing DI/R2 storage architecture remains intact; only the reference value formerly based on PC-entered customer name changes to Journey ID.
3. Document upload/custody/retry behavior remains asynchronous and durable.
4. Existing source-of-truth field mapping remains the basis for deciding which document wins for a field.
5. No processing-order precedence is introduced.
6. Existing evidence history remains auditable; corrections must not destroy the original DI value.
7. Delivery remains part of the same UC03 Journey, not a separate unrelated customer/record.

**Status:** UNCHANGED.

---

## 5. Approved overruling conflicts

The following older rules are explicitly superseded by business concurrence dated 06-Sep-2026.

### C-01 — Three-step Booking flow vs two-screen Booking flow

**Earlier rule:**  
`UC03_V2_FAST_BOOKING_SEQUENCE_2026-08-30.md` defines `Documents -> Booking Details -> Submit Booking -> Booking Attribute Review`.  
`UC03_DOCUMENT_CAPTURE_V2_FROZEN_DESIGN_2026-08-29.md` defines Screen 1 Documents, Screen 2 Booking Details, Screen 3 Booking Attribute Review.

**Governing 06-Sep-2026 rule:**  
Normal PC Booking is exactly `Documents -> Review & Submit`. The separate Booking Details / Additional Information screen is removed.

**Impact:** Web routing, Audit Core Booking submit contract, state resolver, regression tests.

**Status:** APPROVED OVERRULE — 2026-09-06.

---

### C-02 — Booking submitted on Booking Details vs submitted on Review

**Earlier rule:**  
Booking is submitted on Screen 2 Booking Details, then Review opens afterward.

**Governing 06-Sep-2026 rule:**  
Review is Screen 2 and owns final Booking submission.

**Impact:** Web submit action, Audit Core V2 submit API/command shape, stage transition timing.

**Status:** APPROVED OVERRULE — 2026-09-06.

---

### C-03 — Mandatory manually keyed Booking Details vs nullable/document-driven values

**Earlier rule:**  
Current V2 Booking Details requires operational fields such as Customer Type, Deal Type, Deal Source, Lead Source, Registration State, Territory, District, Registration Type, Registration Category and Outright Purchase, and persists them during Booking submit.

**Governing 06-Sep-2026 rule:**  
The PC is not required to key those fields in a separate screen. If an approved DI mapping provides the value, use the extracted document value. Otherwise leave the Audit Core field NULL for now. NULL alone does not block Booking submission.

**Impact:** Audit Core validation/persistence, Web field requirements, nullable workflow assumptions, reporting expectations.

**Status:** APPROVED OVERRULE — 2026-09-06.

---

### C-04 — Customer name required before capture vs Journey-ID-first creation

**Earlier implementation assumption:**  
Booking creation expects a PC-entered customer name and the DI storage display context can derive a customer slug/reference from it.

**Governing 06-Sep-2026 rule:**  
No customer name is requested before document capture. Journey is created first. Journey ID is supplied in the existing reference/display slot used for the R2/DI context. No DI folder/API redesign is introduced.

**Impact:** Web Create Booking contract, Audit Core create transaction, DI display/reference input only.

**Status:** APPROVED OVERRULE — 2026-09-06.

---

### C-05 — 92% confidence review threshold vs 90%

**Earlier rule:**  
`UC03_DOCUMENT_CAPTURE_V2_FROZEN_DESIGN_2026-08-29.md` states `<92%` = Needs Review and `>=92%` does not create confidence-only review work.

**Governing 06-Sep-2026 rule:**  
`<90%` requires PC review. `>=90%` is automatically accepted for confidence-review purposes.

**Impact:** Audit Core review policy, Web Review presentation, tests, historical design interpretation.

**Status:** APPROVED OVERRULE — 2026-09-06.

---

### C-06 — Audit Core does not duplicate raw DI values vs all DI values retained in Audit Core

**Earlier rule:**  
The 29/30-Aug V2 documents state that Audit Core should not duplicate DI raw extracted values and that confirmed references/typed projections are sufficient.

**Governing 06-Sep-2026 rule:**  
Every DI extracted field must be durably represented in Audit Core lossless lineage/storage so it can be audited and shown in Journey Detail. Mapped fields additionally materialize into their canonical business owners. DI remains the extraction owner and evidence source.

**Impact:** Audit Core persistence model and older no-duplication wording.

**Status:** APPROVED OVERRULE — 2026-09-06.

---

### C-07 — PC confirmation blocked while extraction pending vs Booking may submit while extraction continues

**Earlier rule:**  
The frozen 29-Aug design says PC confirmation is blocked while extraction is pending or a document has failed processing.

**Governing 06-Sep-2026 rule:**  
If required document-capture/classification conditions are satisfied, Booking can be submitted while extraction is still processing. Late extraction automatically fills Audit Core and creates PC review work only for <90% fields.

**Impact:** Booking closure gate, late-DI callback/materialization, PC work-item generation.

**Status:** APPROVED OVERRULE — 2026-09-06.

---

## 6. Implementation guardrails

Implementation must follow these guardrails:

- Do not redesign DI storage layout or existing DI APIs merely to implement Journey-ID reference usage.
- Do not create replacement manual-entry fields to mimic the removed Booking Details screen.
- Do not infer values when the source mapping does not provide one; retain NULL.
- A stale/null UI payload must never overwrite a newer DI-populated canonical value.
- PC correction explicitly made during required review may update the effective canonical value while preserving DI provenance.
- Data persistence and Journey Detail availability must not depend on the PC opening Review again after asynchronous DI completion.
- Confidence controls review only; it never controls whether DI data is persisted.
- Every extracted DI field is preserved in Audit Core lossless storage.
- Every mapped DI field is additionally written to its approved canonical Audit Core owner.
- Every approved rule must have end-to-end regression coverage against the actual DEV flow.

---

## 7. Concurrence Register

Business concurrence for C-01 through C-07 was explicitly given on 06-Sep-2026.

| Conflict | Decision | Approval date | Notes |
|---|---|---|---|
| C-01 Two-screen Booking | APPROVED OVERRULE | 2026-09-06 | Documents -> Review & Submit |
| C-02 Submit on Review | APPROVED OVERRULE | 2026-09-06 | Review owns final Booking submit |
| C-03 Document-driven / NULL old manual fields | APPROVED OVERRULE | 2026-09-06 | Use mapped DI value; otherwise NULL and non-blocking |
| C-04 Journey-ID-first / no customer-name screen | APPROVED OVERRULE | 2026-09-06 | Journey ID replaces old customer-name reference only; no DI redesign |
| C-05 90% confidence threshold | APPROVED OVERRULE | 2026-09-06 | <90% review; >=90% automatic for confidence purposes |
| C-06 Lossless DI facts in Audit Core | APPROVED OVERRULE | 2026-09-06 | All extracted facts retained; mapped facts also canonicalized |
| C-07 Submit allowed while extraction continues | APPROVED OVERRULE | 2026-09-06 | Late extraction auto-persists/materializes and flags <90% |

No unrelated historical UC03 rule is made obsolete by this register.

---

## 8. Acceptance baseline

The minimum Booking acceptance journey is:

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
- all extracted DI fields are retained losslessly in Audit Core;
- mapped DI fields populate their canonical Audit Core owners;
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

Effective 06-Sep-2026, this document is the governing UC03 simplification authority for C-01 through C-07.

Where the explicitly identified older rules conflict with C-01 through C-07, this document takes precedence. Older documents remain historical records and must be interpreted subject to this authority.

Any future proposal that conflicts with this authority must be identified explicitly and must receive business concurrence before it can overrule or narrow these approved rules.
