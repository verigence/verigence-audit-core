# Verigence Audit Core — Requirements Correction Addendum

**Document ID:** VAC-REQ-ADD-002  
**Version:** 1.2  
**Status:** APPROVED BUSINESS CORRECTIONS  
**Date:** 2026-08-15

## Purpose

This addendum records explicit project-owner corrections provided after `VAC-REQ-ADD-001 v1.1`. Where it conflicts with earlier Audit Core requirements or design, this addendum is authoritative.

## VAC-CORR-010 — Audit Core does not control dealership operations

Verigence is performing an audit. Audit Core SHALL NOT stop, block, cancel, approve, reject or otherwise control the dealer's actual booking, payment, delivery or vehicle-sale process.

Audit Core SHALL observe/record business facts, validate evidence, compare facts against approved masters/rules, create findings/observations, route audit work, and report audit outcome.

## VAC-CORR-011 — Actual delivery/business status is separate from audit state

The system SHALL retain the actual/observed delivery status for the customer journey independently of Audit Core audit progress/outcome.

Audit state describes Verigence audit work. Actual delivery/business status describes what happened in the dealer/customer process. Audit exceptions SHALL NOT automatically change actual delivery status or prevent delivery.

The canonical dealer delivery-status vocabulary is not yet approved and SHALL NOT be guessed. The implementation SHALL use an approved/configured status-code set when finalized.

## VAC-CORR-012 — DI is internal-only behind Audit Core

No DI functionality SHALL be exposed directly to Web/Mobile users or other user-facing clients for the Audit Core journey.

All user-facing document/evidence operations SHALL go through Audit Core. Audit Core SHALL authorize the business action, map Journey/business purpose to generic DI operations, call DI internally, and expose Audit Core evidence identifiers/contracts to clients.

DI SHALL remain generic document intelligence. Booking/dealer/customer/delivery/audit business logic SHALL NOT be moved into DI merely to support Audit Core.

## VAC-CORR-013 — Logging, exception handling and stable error catalogue

Audit Core SHALL implement structured logging, typed/domain-aware exceptions, centralized API exception mapping, correlation IDs, and a stable versioned error catalogue.

Public errors SHALL NOT leak raw provider/database exceptions, stack traces, tokens, raw documents or sensitive customer identifiers.

Operational logs SHALL integrate with Verigence Observability while authoritative audit history remains in Audit Core.

## VAC-CORR-014 — Executive tenant-wide super privileges without delete

The Executive role is the senior role for the Project/Tenant and SHALL have tenant-wide super privileges across Audit Core capabilities, subject to Security authorization and audit logging.

For the current baseline, Executive SHALL NOT have delete, purge, hard-delete or equivalent irreversible destructive privileges.

Baseline user-facing Audit Core APIs SHALL therefore not depend on destructive DELETE operations. Corrections/removals SHALL use auditable domain semantics such as retire, inactivate, supersede, void or task cancellation where applicable.

Any future delete/purge capability requires explicit owner approval and a separately designed permission/retention policy.

## VAC-CORR-015 — Explicit API contract required

Audit Core SHALL maintain an explicit, version-controlled API contract defining user-facing resource paths/commands, authentication/tenant rules, DI façade behavior, audit-versus-actual-business-state semantics, idempotency/concurrency rules, error mapping, deletion restrictions, correlation headers and durable task endpoints.

A machine-readable OpenAPI representation SHALL accompany the human-readable API contract.
