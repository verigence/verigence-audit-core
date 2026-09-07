# UC03 Engineering Handoff — Current Defect Register & Remediation Guide

**Document ID:** `VUC03-EH-001`
**Version:** `1.0`
**Status:** ACTIVE ENGINEERING AUTHORITY
**Date:** 2026-09-07
**Branch:** `planning/uc-003-booking-delivery-audit`
**Governing authority:** `UC03_SIMPLIFICATION_DECISION_2026-09-06.md` (C-01 through C-07)
**Preceding checkpoint record:** `status/UC03_PHASE1_PRODUCT_BASELINE.md`

---

## 0. Purpose and scope

This document supersedes all ChatGPT-generated UC03 analysis. It is grounded in a direct
code inspection of the current planning branch across Audit Core, Web, and the shared
design canon. Every defect listed below was confirmed against actual file content.

**The only source of truth for UC03 business rules is:**

1. `UC03_SOLUTION_DESIGN_v1.1.md`
2. `UC03_IMPLEMENTATION_HANDOFF_v1.1.md`
3. `UC03_SIMPLIFICATION_DECISION_2026-09-06.md` (governs C-01 through C-07)
4. `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`
5. The actual code on branch `planning/uc-003-booking-delivery-audit`

**The acceptance test is the real end-to-end flow. CI green is not acceptance.**

---

## 1. Confirmed defects — graded by impact

### DEF-01 — CRITICAL: Booking navigation skips Review

FIXED in commit 2ad2efe (verigence-web dev).

Both `navigate()` calls in `BookingCaptureV2WorkspacePage.tsx` now go to
`/v2/bookings/:journeyId/review` instead of `/details`.
Approved flow: Documents → Review & Submit → Booking Details (C-01, C-02).

---

### DEF-02 — CRITICAL: Confidence threshold wrong in Review pages (92 → 90)

FIXED in commit 2ad2efe (verigence-web dev).

`DeliveryReviewV2Page.tsx` REVIEW_THRESHOLD corrected from 92 to 90.
`BookingReviewV2Page.tsx` was already 90 on remote dev — no change needed.
Audit Core `REVIEW_THRESHOLD_PERCENT = 90.0` unchanged. (C-05)

---

### DEF-04 — HIGH: `temp-uc03-runtime-diagnostic.yml` left in Audit Core

FIXED: deleted from `verigence-audit-core dev` in this commit.

---

### DEF-03 — HIGH: Journey UUID leaks into customer display_name

**Status: OPEN — requires verification on DEV.**

`uc03_simplified_create_atomic.py` line 67 writes `journey_id::text` to
`customers.display_name` as a temporary placeholder (correct per C-04).

Required: confirm that `uc03_post_extraction_materialization.py` replaces
the UUID with the PAN/Aadhaar legal name after extraction, and that
`uc03_journey_overview_projection.py` lines 417-424 read the resolved
documentary name rather than the raw UUID when available.

Do not fix by adding a new module. Fix in `uc03_post_extraction_materialization.py`
if the replacement path is missing.

---

### DEF-05 — MEDIUM: `BookingDetailsV2Page` requires mandatory-all-fields gate (violates C-03)

**Status: OPEN.**

`BookingDetailsV2Page.tsx` lines 142-159 — `missingFields` blocks submission
when any legacy manual field is empty. Per C-03 these fields are nullable.
BookingDetails is the post-submission read/edit view; it must not re-impose
the old removed manual-details validation on PC.

---

### DEF-06 — MEDIUM: Fragmented DI → Core write path

**Status: OPEN — requires end-to-end verification.**

`uc03_confidence_review_policy.py` IS wired (via `install_uc03_v2_capture_business_rules`).
ChatGPT was incorrect that it was dead. However the write path across five modules
must be traced and verified end-to-end on real DEV data before any further patching.

Path to verify:
```
DI fact → journey_document_extracted_fields → booking_form_review_values
  → canonical table → /booking/details API response
```

---

### DEF-07 — MEDIUM: `booking_docket` materialization parity unproven

**Status: OPEN — requires DEV test.**

Run a Docket-only Booking, complete Review, confirm `booking_form_review_values`
and canonical tables are populated identically to a `booking_form` Booking.
Fix in `uc03_strict_review_core_ownership.py` if a gap is found.

---

### DEF-08 — MEDIUM: DI → Audit Core V2 linkage chain unproven

**Status: OPEN — requires DEV verification.**

Verify one real upload chain: upload → classification → extraction →
`journey_document_extracted_fields` → canonical owner → Details API response.
If `requirementRef` is missing from DI upload metadata, fix in
`uc03_pc_booking_documents.py` or `uc03_document_capture_v2.py`.

---

## 2. What is correctly implemented (do not regress)

| Area | Status |
|---|---|
| `uc03_confidence_review_policy.py` IS wired | Correct |
| Audit Core 90% threshold | Correct |
| `booking_docket` attribute specs | Correct |
| `booking_docket` in work-item enrichment | Correct |
| Route structure (Documents / Review / Details) | Correct |
| Machine findings never block real Delivery | Correct |
| `DELIVERY_COMPLETED` + Audit State IN_PROGRESS | Correct |
| Append-only finding events + FLAGS_RAISED sticky | Correct |

---

## 3. Correct Booking write path

```
Capture New Booking
  → POST /uc03/bookings
  → customers(display_name=journey_uuid), journeys, journey_stage_states
  → navigate /v2/bookings/:journeyId  [Documents]

Upload document
  → POST /booking/v2/documents
  → DI ensure_audit_storage_context + create_document_upload_session
  → evidence → journey_document_requirements (via requirementRef)

DI async: classify + extract
  → POST /di/v2/link-callback
  → journey_document_extracted_fields (lossless)

Click Continue → navigate /v2/bookings/:journeyId/review  [Review]
  → PC reviews <90% fields; accepts or corrects
  → POST /booking/v2/review/confirm
  → persist_reviewed_di_fields + materialize_reviewed_di_business_values
  → booking_form_review_values, bookings, registrations, customers
  → business_status = BOOKING_CLOSED
  → navigate /v2/bookings/:journeyId/details  [Booking Details]

BookingDetails reads Audit Core (not DI directly)
  → GET /booking/details → uc03_booking_details.py

Late DI: uc03_post_extraction_materialization.py
  → fills Audit Core after submit
  → <90% fields → PC review flag
  → Journey Detail updated automatically
```

---

## 4. Remaining actions before human UAT

| Priority | Action | Defect |
|---|---|---|
| 1 | ~~Fix Documents→Review navigation~~ | DEF-01 DONE |
| 2 | ~~Fix REVIEW_THRESHOLD 92→90~~ | DEF-02 DONE |
| 3 | ~~Delete temp diagnostic workflow~~ | DEF-04 DONE |
| 4 | End-to-end DI chain verification on live DEV | DEF-08 |
| 5 | booking_docket-only Booking test | DEF-07 |
| 6 | Confirm UUID replaced by PAN/Aadhaar name | DEF-03 |
| 7 | Remove mandatory-field gate from BookingDetailsV2Page | DEF-05 |
| 8 | Draw + verify full write path before touching any code | DEF-06 |

---

## 5. Rules for remediation work

1. Draw the write path first. Do not add a new module until the path is on paper.
2. Fix in the owning module. Never create `uc03_*_fix.py`.
3. Confidence controls review only — never controls persistence.
4. PAN/Aadhaar is the authoritative customer identity. UUID is a temporary slot.
5. Do not redesign DI storage or DI APIs.
6. Audit never blocks real dealer progression (INV-02).
7. CI green is not acceptance. The only test is the real end-to-end flow.

---

## 6. Acceptance baseline

```
1. Login as PC with active Project
2. Capture New Booking → Journey created (no customer name screen)
3. Documents screen → upload Booking Form + PAN
4. Continue → navigates to /review (not /details)
5. Review shows extracted fields; <90% flagged
6. PC resolves flagged fields → Submit
7. Status = BOOKING_CLOSED
8. /details shows consolidated Audit Core data
9. Customer name = PAN/Aadhaar legal name (not UUID)
10. Docket-only test → identical field population
11. Late-DI test → fields appear automatically; <90% create review flags
12. Delivery starts even with incomplete Booking (flag raised, not rejected)
```

Steps 4, 5, 7, 9, 10, 11 are Phase-1 promotion blockers.

---

*Produced from direct code inspection 2026-09-07. Supersedes all ChatGPT UC03 analysis.*
