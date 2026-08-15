# Verigence Audit Core — Baseline Manifest

**Current baseline:** Audit Core Requirements v1.0  
**Status:** BASELINED — Initial Process Baseline  
**Baseline date:** 2026-08-15  

## Canonical requirements document

| Item | Value |
|---|---|
| Document ID | `VAC-REQ-001` |
| Canonical file | `docs/AUDIT_CORE_REQUIREMENTS_BASELINE_v1.0.md` |
| Version | `1.0` |
| Status | `BASELINED — Initial Process Baseline` |
| Git blob SHA | `c59721009214681eed793bf21b427ebd0253d462` |
| Original baseline commit | `7220d952c3c47e37deaf6990496f8f2d234c9068` |

## Source set represented by v1.0

The baseline records requirements derived from:

1. `SPR_Tool_Process_SubProcess_Activity_Details (2)(1).xlsx`
   - 104 numbered activities across Booking, Delivery, Payment, Insurance/Accessories, Daily Audit, Trade-In, Escalation/CRM, and System Validation/Analytics.
   - Daily PC/TL Activity Tracker requirement.
   - PC Daily Activity Notepad requirement.
   - Supporting RSA, Registration, EW, Service Package, Corporate Discount and Trade-In reference images.
2. `SPR Details - Copy (3)(1).xlsx`
   - existing-tool journey/screens covering price list, booking/customer/SC, deal/product/VIN/DMS, registration, commercial/discount/trade-in, delivery documents, receipts/payment verification, DO verification and observations.
3. Project-owner requirements supplied on 2026-08-15 for:
   - Project onboarding;
   - one OEM/product context per initial Project;
   - multiple Dealers and Dealer Locations/Outlets;
   - Onsite/Satellite outlet classification;
   - PC, TL, PM, CRM and Executive Project roles;
   - dealership participant landscape;
   - OEM Model/Variant/Colour hierarchy;
   - price masters and time-bound discounts;
   - Security and DI integration requirements.

## Governance

The canonical file above is the source of truth for Audit Core requirements v1.0.

Additional business processes are expected. They must be incorporated through a new versioned baseline rather than silently overwriting the meaning of this baseline.

The following rules apply:

- unresolved items remain `OPEN` until explicitly confirmed;
- solution design and database schema must trace back to the current approved requirements baseline;
- solution design must not invent unresolved calculation logic, thresholds or role rights;
- existing-tool screenshots are source requirements/reference only and do not freeze the future UI design;
- Security and DI remain separate modules; this baseline does not authorise changes in those repositories.

## Next planned artifacts

After business review of v1.0:

1. Audit Core Solution Design
2. Logical Data Model
3. Physical Database Schema
4. Role/Permission Matrix
5. Document Requirement Matrix
6. Business Rule Catalogue
