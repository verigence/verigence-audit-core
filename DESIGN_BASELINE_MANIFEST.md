# Verigence Audit Core — Design Baseline Manifest

## Current status

**Historical baseline:** Audit Core Solution Design v1.0  
**Historical status:** BASELINED on 2026-08-15, but **IMPLEMENTATION HOLD** after explicit project-owner corrections  
**Current design candidate:** `VAC-SD-002` / Audit Core Solution Design v2.0 — **DRAFT FOR REVIEW**  
**Requirements:** `VAC-REQ-001 v1.0` plus authoritative correction addendum `VAC-REQ-ADD-001 v1.1`

> v1.0 remains preserved as design history. It SHALL NOT be implemented unchanged because its Tenant→Project, Customer/Audit Case and Booking-root assumptions were corrected by the project owner. The existing `VAC-DB-001` physical schema and v1.0 Security catalogue are also on implementation/registration hold until replaced/aligned to the approved v2.0 design.

## v2.0 review package

| Artifact | Document ID | Status | Git blob SHA | Commit |
|---|---|---|---|---|
| `docs/AUDIT_CORE_REQUIREMENTS_CORRECTION_ADDENDUM_v1.1.md` | `VAC-REQ-ADD-001` | APPROVED BUSINESS CORRECTIONS | `0dd38f27edbaef0a24df9bd9f127ce15f49b56f6` | `f75579a07d2ab32e63bc7d6b5283858cf020752c` |
| `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.0.md` | `VAC-SD-002` | DRAFT FOR REVIEW | `5c2033c42b31c5c9132dbc7cb534e225471ba543` | `f75579a07d2ab32e63bc7d6b5283858cf020752c` |
| `docs/AUDIT_CORE_DESIGN_RECONCILIATION_v2.0.md` | `VAC-DR-002` | DRAFT FOR REVIEW | `bc4eb4ca863a876ced605b4c8d6627ec7e0beebd` | `f75579a07d2ab32e63bc7d6b5283858cf020752c` |

The v2.0 candidate freezes nothing beyond the explicit business corrections until project-owner review/approval. After approval, a new v2.0 baseline manifest revision and replacement physical DDL (`VAC-DB-002`) shall be produced.

## Foundational corrections already authoritative

1. **One Security Tenant = one Audit Project.**
2. Canonical business hierarchy: **Project → Dealer → Dealer Outlet → Customer → Customer/Audit Journey**.
3. Booking starts the Journey; Booking, Delivery, Payments, Finance, Insurance/VAS, Trade-In, Vehicle/Registration and related processes are peer parts of the Journey rather than children of one Booking aggregate.
4. Decision-relevant master data remains versioned/effective-dated; published versions are immutable.
5. Workflow/tasks must be durable and recoverable; committed tasks cannot be lost on restart/crash/deploy/retry.
6. PC capture/upload responsibilities are separated from formal TL/PM verification/validation responsibilities.
7. Dealer staff remain business reference participants in the current scope.
8. Dealer Outlet is an Audit Core business entity; any Security Location mapping is explicit rather than assumed.

## Historical v1.0 package — retained for traceability only

| Artifact | Document ID | Historical status | Git blob SHA | Original commit |
|---|---|---|---|---|
| `docs/AUDIT_CORE_SOLUTION_DESIGN_v1.0.md` | `VAC-SD-001` | BASELINED / IMPLEMENTATION HOLD | `35cefb4429066bf6fac6a3801a7a5c7395a3de4b` | `fcf78704c5c467faae7b8237b336e5181d21444d` |
| `database/AUDIT_CORE_POSTGRESQL_SCHEMA_v1.0.sql` | `VAC-DB-001` | BASELINED PHYSICAL DESIGN / IMPLEMENTATION HOLD | `ae9f27a8f37be8672cbe77ad15ef03bb028038b5` | `88269e25913ce1e18959448a9c068d8c337c595f` |
| `design/AUDIT_CORE_SECURITY_CATALOG_v1.0.json` | design companion | PROPOSED / REGISTRATION HOLD | `f5bde0bc839dbc6c11607755227896da7b8be505` | `e73c505bb3c0be4a9ad18eddd533bc3e160f38f6` |

Requirements baseline history remains:

- `docs/AUDIT_CORE_REQUIREMENTS_BASELINE_v1.0.md`
- Document ID `VAC-REQ-001`
- requirements blob SHA `c59721009214681eed793bf21b427ebd0253d462`
- corrected by `VAC-REQ-ADD-001 v1.1` where conflicts exist.

## v1.0 decisions retained in v2.0 unless changed explicitly

The following v1.0 architecture principles remain useful and are retained/strengthened in the v2.0 candidate:

- Audit Core as a modular monolith initially;
- Security as identity/effective-permission authority;
- DI as document/evidence-content and document-intelligence authority;
- no Security/DI private database reads or cross-module database foreign keys;
- immutable/versioned published master configuration;
- PostgreSQL tenant isolation with composite tenant keys, `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`, using a non-owner runtime role without `BYPASSRLS`;
- transactional outbox and inbox/idempotency patterns;
- authoritative Audit Core business audit trail distinct from operational Observability logs;
- unresolved business formulas/thresholds remain open/configurable rather than guessed.

## External contract review points

The current candidate design is insulated through adapters and was checked against the then-current development contracts of:

- `verigence-security` — Security-issued access token/JWKS model with Tenant and effective `permissions[]` claims;
- `verigence-di` — Tenant-scoped Subjects (`PERSON`, `ORGANIZATION`, `OTHER`), document operations and generic document external-entity links.

These external repositories are not modified by this Audit Core design work. Their future changes must be absorbed through Audit Core integration adapters and separately approved integration changes.

## Governance

Material changes to Project/Tenant identity, Dealer/Outlet/Customer/Journey hierarchy, workflow durability, Security/DI ownership, master versioning, event contracts or physical data ownership require explicit design change control.

No implementation shall use `VAC-SD-002` as a baselined contract until the project owner approves the v2.0 design candidate. No change to Security or DI is implied by merely documenting proposed Audit Core integration/permission requirements.
