# Verigence Audit Core — Design Baseline Manifest

## Current status

**Historical baseline:** Audit Core Solution Design v1.0 — BASELINED historically, **IMPLEMENTATION HOLD**  
**Superseded review candidate:** `VAC-SD-002` / v2.0 — retained for traceability  
**Current design candidate:** `VAC-SD-003` / Audit Core Solution Design v2.1 — **DRAFT FOR REVIEW**  
**Requirements:** `VAC-REQ-001 v1.0` + authoritative corrections `VAC-REQ-ADD-001 v1.1` and `VAC-REQ-ADD-002 v1.2`

> No historical v1.0/v2.0 artifact shall be implemented where it conflicts with the approved correction addenda or v2.1 review candidate. `VAC-DB-001` and the v1.0 Security catalogue remain on implementation/registration hold until replacement/alignment to an approved v2.x design.

## Current v2.1 review package

| Artifact | Document ID | Status |
|---|---|---|
| `docs/AUDIT_CORE_REQUIREMENTS_CORRECTION_ADDENDUM_v1.2.md` | `VAC-REQ-ADD-002` | APPROVED BUSINESS CORRECTIONS |
| `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.1.md` | `VAC-SD-003` | DRAFT FOR REVIEW |
| `docs/AUDIT_CORE_DESIGN_RECONCILIATION_v2.1.md` | `VAC-DR-003` | DRAFT FOR REVIEW |
| `docs/AUDIT_CORE_API_CONTRACT_v1.0.md` | `VAC-API-001` | DRAFT FOR REVIEW |
| `api/openapi-v1.yaml` | machine-readable API companion | DRAFT FOR REVIEW |
| `docs/AUDIT_CORE_ERROR_CATALOG_v1.0.md` | `VAC-ERR-001` | DRAFT FOR REVIEW |

## Authoritative foundational corrections

1. **One Security Tenant = one Audit Project.**
2. Business hierarchy: **Project -> Dealer -> Dealer Outlet -> Customer -> Customer/Audit Journey**.
3. Booking starts the Journey; Booking, Delivery, Payments, Finance, Insurance/VAS, Trade-In, Vehicle/Registration and related processes are peer parts of the Journey.
4. Decision-relevant master data is versioned/effective-dated; published versions are immutable.
5. Audit workflow/tasks are durable/recoverable; committed tasks cannot silently disappear.
6. PC capture/upload responsibilities are separated from formal TL/PM verification/validation.
7. Dealer staff are business reference participants in the current scope.
8. Dealer Outlet is an Audit Core business entity; Security Location mapping is explicit rather than assumed.
9. **Audit Core audits/observes; it does not stop, block, approve, reject or control dealer business operations.**
10. **Actual delivery/business status is separate from Audit Core audit state/outcome.**
11. **DI is internal-only behind Audit Core for user-facing journeys; Web/Mobile never calls DI directly.**
12. Audit business logic remains in Audit Core; DI remains generic document intelligence.
13. Audit Core uses structured logging, typed exceptions, centralized error mapping and a stable error catalogue.
14. Executive has tenant-wide Audit Core super privileges **except delete/purge/destructive removal**.
15. Baseline public Audit Core API contains no HTTP DELETE operations.
16. Audit Core maintains an explicit human-readable API contract plus OpenAPI representation.

## v2.0 package — historical review traceability

- `docs/AUDIT_CORE_REQUIREMENTS_CORRECTION_ADDENDUM_v1.1.md` — `VAC-REQ-ADD-001`
- `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.0.md` — `VAC-SD-002`
- `docs/AUDIT_CORE_DESIGN_RECONCILIATION_v2.0.md` — `VAC-DR-002`

v2.0 is superseded as the current review candidate by v2.1, particularly for the direct Client->DI flow and any language that could imply Audit Core controls dealer delivery/business lifecycle.

## Historical v1.0 package — traceability only

- `docs/AUDIT_CORE_SOLUTION_DESIGN_v1.0.md` — `VAC-SD-001` — IMPLEMENTATION HOLD
- `database/AUDIT_CORE_POSTGRESQL_SCHEMA_v1.0.sql` — `VAC-DB-001` — IMPLEMENTATION HOLD
- `design/AUDIT_CORE_SECURITY_CATALOG_v1.0.json` — REGISTRATION HOLD
- `docs/AUDIT_CORE_REQUIREMENTS_BASELINE_v1.0.md` — `VAC-REQ-001`, corrected by v1.1/v1.2 addenda where conflicts exist

## Retained architecture principles

- modular monolith initially;
- Security as identity/effective-permission authority;
- DI as generic document/evidence-content and document-intelligence authority;
- no Security/DI private database reads or cross-module DB foreign keys;
- immutable/versioned published master configuration;
- PostgreSQL Tenant isolation with RLS/forced RLS and a non-owner runtime role without `BYPASSRLS`;
- transactional outbox/inbox/idempotency;
- authoritative Audit Core audit history distinct from operational Observability logs;
- unresolved business formulas/thresholds remain open/configurable rather than guessed.

## Governance

No implementation shall treat `VAC-SD-003`, `VAC-API-001`, `VAC-ERR-001` or `api/openapi-v1.yaml` as a formally approved baseline until project-owner review/approval.

After approval, a replacement physical DDL (`VAC-DB-002`) and aligned Audit Core Security catalogue shall be produced. No change to Security or DI repositories is implied by this design documentation.
