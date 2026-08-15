# Verigence Audit Core — Design Baseline Manifest

**Design baseline:** Audit Core Solution Design v1.0  
**Status:** BASELINED  
**Baseline date:** 2026-08-15  
**Requirements baseline:** VAC-REQ-001 v1.0

## Canonical design package

| Artifact | Document ID | Version / status | Git blob SHA | Original commit |
|---|---|---|---|---|
| `docs/AUDIT_CORE_SOLUTION_DESIGN_v1.0.md` | `VAC-SD-001` | v1.0 / BASELINED | `35cefb4429066bf6fac6a3801a7a5c7395a3de4b` | `fcf78704c5c467faae7b8237b336e5181d21444d` |
| `database/AUDIT_CORE_POSTGRESQL_SCHEMA_v1.0.sql` | `VAC-DB-001` | v1.0 / BASELINED physical design | `ae9f27a8f37be8672cbe77ad15ef03bb028038b5` | `88269e25913ce1e18959448a9c068d8c337c595f` |
| `design/AUDIT_CORE_SECURITY_CATALOG_v1.0.json` | design companion | v1.0 / PROPOSED FOR SECURITY REGISTRATION | `f5bde0bc839dbc6c11607755227896da7b8be505` | `e73c505bb3c0be4a9ad18eddd533bc3e160f38f6` |

Requirements remain independently frozen by `BASELINE_MANIFEST.md`:

- `docs/AUDIT_CORE_REQUIREMENTS_BASELINE_v1.0.md`
- Document ID `VAC-REQ-001`
- requirements blob SHA `c59721009214681eed793bf21b427ebd0253d462`

## Baseline architecture decisions

The v1.0 design freezes these architecture decisions unless superseded through explicit design change control:

1. Audit Core is initially a **modular monolith**, not a collection of microservices.
2. Audit Core does **not** require a separate BPM/workflow engine in v1; explicit state machines + durable Work Items + scheduler + transactional outbox are used.
3. Booking/Audit Case is the primary transaction aggregate.
4. Customer is a separate Tenant-scoped aggregate and may own multiple Audit Cases.
5. A Verigence DI Subject maps to the Audit Core Customer; individual DI Documents are associated to Audit Cases through Audit Core Evidence Links and, where available, DI external entity links.
6. Audit Core never reads Security or DI private databases.
7. Security is authoritative for identity and effective platform permissions; Audit Core adds Project/Dealer/Outlet business-scope enforcement.
8. Security device/location claims are not reused as Dealer Outlet business scope.
9. DI is authoritative for document content, document processing/extraction and document verification; Audit Core is authoritative for business evidence requirement, business controls/findings and audit outcome.
10. Published master/configuration versions are immutable by design and historical cases preserve version/snapshot context.
11. Business lifecycle state, review/work state and audit outcome are separate dimensions.
12. PostgreSQL Tenant isolation uses composite Tenant keys plus `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`; the runtime role must be non-owner and without `BYPASSRLS`.
13. Audit Core uses a transactional outbox; a message broker is not required at launch.
14. Audit Core keeps its own authoritative tamper-evident business audit trail, distinct from operational Observability logs and Security/DI audit trails.
15. Open business formulas/thresholds from VAC-REQ-001 remain unresolved/configurable and are not silently invented in the physical schema.

## External contract review points used for v1.0 design

The solution design was checked against the then-current development contracts of:

- `verigence-security` — Verigence-owned RS256 access token/JWKS model with Tenant and effective `permissions[]` claims; Security remains the authentication/authorization authority.
- `verigence-di` — Tenant-scoped Subjects (`PERSON`, `ORGANIZATION`, `OTHER`), document permissions and generic document external-entity links.

These external repositories are **not** part of this Audit Core baseline and were not modified. Their future implementation changes must be absorbed through Audit Core integration adapters rather than silently changing Audit Core domain ownership.

## Governance

Material changes to domain boundaries, state semantics, Security/DI ownership, master versioning, event contracts or physical data ownership require an explicit design revision (v1.1+ or v2.0 depending on compatibility impact).

The proposed Audit Core Security catalog is not considered installed or active in Security merely because it exists in this repository. Registration in `verigence-security` is a separate future integration action requiring deliberate review/change in that repository.
