# Verigence Audit Core — Design Reconciliation Addendum

**Document ID:** VAC-DR-003  
**Version:** 2.1  
**Status:** DRAFT FOR REVIEW  
**Date:** 2026-08-15  
**Companion design:** `AUDIT_CORE_SOLUTION_DESIGN_v2.1.md`

## Purpose

This document records the changes introduced after the v2.0 consolidated design. It supplements `AUDIT_CORE_DESIGN_RECONCILIATION_v2.0.md`; the earlier record of items adopted/rejected from the supplied third-party design remains valid except where this addendum explicitly changes v2.0.

## Owner inputs incorporated in v2.1

| Owner input | v2.1 treatment |
|---|---|
| Verigence is auditing; it cannot stop/take action on dealer business process | Audit Core is explicitly observational/auditing. Business-control commands such as block/approve/stop delivery are excluded. Findings/tasks/escalations are audit actions only. |
| There must be actual delivery status for the customer | Actual/observed delivery status is stored separately from audit state/outcome, with source/provenance. Delivery status code set remains configurable until business vocabulary is approved. |
| No DI functionality exposed directly to users | Removed the v2.0 Client -> DI pattern. Web/Mobile calls Audit Core only; Audit Core calls DI internally and exposes Audit Core evidence IDs/contracts. |
| Avoid business logic in DI | Dealer/customer/journey/document-requirement/audit-control/reconciliation logic remains in Audit Core. DI remains generic document intelligence. |
| Add logging, exceptions, error handling and error catalogue | Added structured logging requirements, typed exception layers, centralized mapper, Problem Details response format, stable `VAC-*` error catalogue and Observability integration. |
| Executive has super privileges within tenant but no delete | Executive is tenant-wide super privileged for Audit Core except delete/purge/destructive actions. Baseline public API contains no DELETE operations. |
| API contract must be explicit | Added `VAC-API-001 v1.0` plus `api/openapi-v1.yaml`. |

## v2.0 elements explicitly superseded

1. **Direct client-to-DI upload flow** is superseded. Audit Core is the sole user-facing DI façade.
2. **Journey lifecycle language** that could be read as dealer-process control is superseded by an audit-only observational model.
3. Delivery `CANCELLED`/similar business lifecycle concepts must not be interpreted as Audit Core commands. Dealer status is observed/recorded; Audit Core does not command it.
4. Public user-facing API is explicitly **no DELETE** for the baseline.
5. Executive's earlier read/oversight-only interpretation is superseded by tenant-wide super privilege except destructive deletion.

## Items retained from the supplied third-party design and v2.0

- Project = Security Tenant;
- modular monolith startup architecture;
- ports/adapters;
- versioned masters and Standard-vs-Actual pattern;
- transactional outbox/inbox;
- concrete validation-rule and analytics catalogues as design inputs;
- NotificationPort and InsuranceCalculatorPort;
- mobile/offline idempotency;
- Dealer staff as reference participants;
- DI as document-intelligence authority and Audit Core as business-audit authority;
- no cross-module private DB reads/foreign keys;
- durable internal audit workflow with task history/retry/recovery.

## Open items still not guessed

- canonical actual dealer delivery-status codes;
- exact TL versus PM verification gates;
- Satellite threshold/policy;
- PM versus PMO terminology;
- repeat-customer reuse policy;
- Dealer Outlet <-> Security Location mapping;
- unresolved commercial/trade-in/payment formulas already listed in earlier requirements/design.
