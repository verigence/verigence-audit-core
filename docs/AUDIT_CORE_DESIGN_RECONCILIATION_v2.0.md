# Verigence Audit Core — Design Reconciliation / Source Adoption Register

**Document ID:** VAC-DR-002  
**Version:** 2.0  
**Status:** DRAFT FOR REVIEW  
**Date:** 2026-08-15  
**Companion:** `AUDIT_CORE_SOLUTION_DESIGN_v2.0.md`

## 1. Source precedence used

The consolidated v2.0 design uses the following authority order:

1. Explicit project-owner corrections/instructions given during the Audit Core design discussion, with the latest correction taking precedence over earlier statements.
2. Supplied SPR process workbook (`SPR_Tool_Process_SubProcess_Activity_Details...xlsx`) and the current-tool workbook (`SPR Details - Copy...xlsx`).
3. `VAC-REQ-001 v1.0` plus `VAC-REQ-ADD-001 v1.1` correction addendum.
4. The other architect's draft `REQ_Project-Onboarding_and_Audit-Core_v0.1.md` and `SDD_Project-Onboarding_and_Audit-Core_v0.1.md` supplied in the ZIP.
5. Verified current Verigence Security and DI runtime contracts, for integration details only.
6. Previous Audit Core v1.0 solution/schema as historical input, not as authority where it conflicts with items 1–5.

No unresolved source item is silently converted into a business rule.

## 2. Explicit owner corrections incorporated

| Correction | Incorporated in v2.0 |
|---|---|
| One Security Tenant = one Audit Project | Project is a 1:1 Audit projection keyed by `tenant_id`; no Tenant→many-Projects hierarchy. |
| Project → Dealer → Dealer Outlet → Customer → Journey | This is the canonical business hierarchy and logical model. |
| Booking, Delivery, Insurance, Trade-In, Payments etc. are all parts of the Journey | `Journey` is the lifecycle coordinator; Booking is the kick-off process, not the parent of every later domain entity. |
| Keep master-data versioning | Published price/discount/document/control versions are immutable and effective-dated. |
| Workflow must be durable; no lost tasks | v2.0 contains a PostgreSQL-backed durable workflow subsystem with transactional task creation, retries, leases/recovery, dead-letter state, task history, idempotency and outbox/inbox. |
| PC captures/uploads; TL/PM validate/verify | Permission and workflow responsibility model explicitly separates capture from formal verification. |
| Audit team should not re-key evidence facts unnecessarily | DI-derived evidence facts are projected with provenance; UI/API should reuse them rather than force duplicate manual entry. |
| Sales Executive starts the process by handing booking file to PC | Dealership staff remains reference data; booking initiation/handoff is represented without requiring dealer login. |

## 3. Items adopted from the other architect's design

The v2.0 design deliberately adopts or strengthens these ideas from the supplied v0.1 draft:

1. **Project = Security Tenant.** Adopted as a foundational rule.
2. **Audit Core as a modular monolith.** Adopted for startup cost/operational simplicity while preserving bounded-context seams.
3. **Ports-and-adapters / anti-corruption interfaces.** Adopted for Security, DI, Notifications, Insurance Calculator and future integrations.
4. **Configuration as data.** Retained, but strengthened with mandatory immutable versioning for decision-relevant masters.
5. **Transactional outbox.** Adopted and expanded with inbox/deduplication.
6. **Dealer staff as reference/master data, not application users in current scope.** Adopted.
7. **Standard-versus-Actual modelling.** Adopted as a first-class audit pattern across price, discounts, insurance, accessories and similar components.
8. **16 initial validation-rule catalogue (VR-01…VR-16) and ON_SAVE vs NIGHTLY execution classification.** Adopted as the initial control catalogue; final business formulas remain versioned/configurable.
9. **AN-01…AN-13 / discount analytics catalogue.** Adopted as the initial business analytics catalogue, with unresolved formulas left open.
10. **NotificationPort.** Adopted; provider/channel remains open.
11. **InsuranceCalculatorPort.** Adopted; OEM-specific integration remains open.
12. **Mobile/offline idempotency requirement.** Adopted and connected to durable command/task idempotency.
13. **DI document ownership vs Audit business reconciliation boundary.** Adopted conceptually: DI owns document/evidence processing; Audit Core owns journey/business compliance and reconciliation.
14. **No cross-module database foreign keys/shared private tables.** Adopted.
15. **High-volume/nightly analytics separated from interactive transaction path.** Adopted via query/read models and scheduled durable work.

## 4. Items from the other design not adopted as written

| Other design item | v2.0 decision | Reason |
|---|---|---|
| Dealer Outlet = Security Tenant Location | Not hard-coded. Outlet is Audit business entity with optional Security Location reference. | Business outlet and access-control location are distinct concerns; exact mapping remains open. |
| Booking is the central aggregate containing customer + all later processes | Replaced by Customer/Audit Journey coordinator. | Owner clarified hierarchy and that booking/delivery/payments/insurance/trade-in are all parts of the journey. |
| Customer embedded only in Booking | Replaced by Customer entity under Dealer Outlet and Journey reference. | Owner clarified Customer precedes/owns the journey context. |
| Booking status only BOOKING/DELIVERED/CANCELLED | Replaced by Journey lifecycle + per-process state + review/work state. | Real process has parallel/long-running payment, insurance, trade-in, delivery and review work. |
| SEND_BACK stored as breach status | Separated into review/workflow state. | SEND_BACK is not a compliance outcome. |
| PC receives `*.verify` and DI verification authority | Removed from PC baseline; formal verification belongs to TL/PM as configured. | Explicit owner correction. |
| DI upload `schema_key`, `/di/v1/...`, 202 EXTRACTING response | Not used. | Does not match the verified current DI API. |
| Assumed DI `document.extracted` / `document.reconciled` callback contract | Not required in v2.0. | Current DI stable event contract is not assumed. Adapter supports polling/refresh now and events later. |
| JWT `authorization_version` used as a current claim | Not used as required contract. | Not part of the verified current Security access-token contract used for Audit design. |
| Evidence/photo/document ID arrays embedded as JSONB | Replaced by normalized evidence-link relationships. | Better provenance, queryability, audit history and individual document lifecycle. |
| Mutable/effective-dated price/discount rows without strict published-version immutability | Strengthened to identity + immutable version model. | Audit decisions must remain reproducible after master changes. |
| Single `file_review` record | Replaced by immutable review-decision history and durable review tasks. | Send-back/resubmission may create multiple review cycles. |
| Repository filtering alone for tenant isolation | v2.0 requires tenant keys + PostgreSQL RLS/forced RLS for tenant tables. | Defense in depth for an audit platform. |

## 5. Previous Audit Core v1.0 design changes

The previous v1.0 design remains historical but SHALL NOT be implemented unchanged. v2.0 corrects these areas:

- removes Tenant→many Projects;
- changes Project to a 1:1 Tenant projection;
- changes `Customer → AuditCase/Booking` dominance into `Outlet → Customer → Journey`;
- removes Booking as the universal aggregate root;
- introduces Journey as lifecycle/business-correlation root;
- preserves process-specific entities as peer parts of the Journey;
- strengthens PC/TL/PM separation;
- replaces generic lightweight `work_items` with a fully durable workflow/task subsystem;
- keeps master versioning from v1.0;
- retains strong tenant isolation, outbox/inbox and tamper-evident audit concepts;
- retains DI/Security anti-corruption adapters while correcting the business hierarchy.

## 6. Open items intentionally not resolved

The consolidated design leaves the business uncertainties listed in `VAC-REQ-ADD-001 v1.1` open. In addition, the following integration/detail decisions remain review points rather than assumptions:

- whether every Dealer Outlet maps 1:1 to a Security Location, many-to-one, or only where geo/schedule enforcement is required;
- repeat-customer reuse/link policy across journeys;
- exact PM versus TL verification gates for normal versus exception journeys;
- notification provider/channel;
- whether a dedicated external workflow service is ever needed after v1 (the v2.0 design does not require it).
