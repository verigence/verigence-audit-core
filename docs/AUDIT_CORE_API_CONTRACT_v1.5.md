# Verigence Audit Core — API Contract UC02 Synchronous Administration Addendum

**Document ID:** VAC-API-006  
**Version:** 1.5  
**Status:** APPROVED FOR UC02 DEV IMPLEMENTATION  
**Date:** 2026-08-23  
**Base contract:** `VAC-API-005 v1.4` / `docs/AUDIT_CORE_API_CONTRACT_v1.4.md`

This addendum supersedes the v1.3/v1.4 Project provisioning recovery/retry semantics and applies the approved synchronous administrative transaction invariant to UC02 cross-module mutations.

## 1. Project Create

```text
POST /v1/projects
```

Request and authorization remain unchanged, including the required `Idempotency-Key` header.

For Web compatibility the existing response shape is retained, but successful synchronous create has only one business outcome:

```json
{
  "operationId": "request-receipt-uuid",
  "tenantId": "tenant-id",
  "projectName": "MahindraWest",
  "projectStatus": "CONFIGURING",
  "provisioningStatus": "READY",
  "currentStep": "COMPLETE",
  "errorCode": null,
  "errorMessage": null
}
```

There is no normal `202`, `IN_PROGRESS` or `RECOVERY_REQUIRED` outcome for a new synchronous Project Create. The `operationId` is a completed request/audit receipt only; it is not a recoverable workflow handle.

Failure is returned as the existing business-safe `application/problem+json` contract. Before returning failure, Audit Core must synchronously rollback/compensate any Project-create business state already written in Audit Core, DI and Security as defined by `AUDIT_CORE_UC02_ADMIN_TRANSACTION_ALIGNMENT.md`.

## 2. Project provisioning retry removed from the UC02 Web contract

The following retry endpoint is no longer part of the Phase-1 Web contract:

```text
POST /v1/project-provisioning-operations/{operationId}/retry
```

A GET for an existing historical provisioning receipt may remain diagnostic/read-only for compatibility, but new create failures are not persisted as `RECOVERY_REQUIRED` business operations and are not resumed.

## 3. Role Mapping mutation response

For Web compatibility, the existing mutation response shape is retained, but successful synchronous role-mapping mutations return only:

```text
operationStatus = COMPLETED
```

`RECOVERY_REQUIRED` is not a valid new mutation outcome. A downstream failure returns a normal non-2xx business-safe error.

Existing `Idempotency-Key` rules remain in force for Role Mapping `PUT`/`DELETE`. Idempotency is request replay/conflict protection only; it does not create or resume a recovery workflow.

If a later Audit Core local write fails after Security has changed, Audit Core restores the prior Security operating role before returning failure. Removal follows the same rule.

## 4. Project Activation

```text
POST /v1/tenants/{tenantId}/project/activate
```

The existing success schema remains unchanged.

Readiness must be fully validated first. Audit Core Project status is changed inside an uncommitted local transaction; Security activation is then invoked. Security failure rolls back the Audit Core transaction. Success is returned only after Security reports `ACTIVE` and the Audit Core transaction commits.

## 5. DI Tenant provisioning compensation

Audit Core may invoke the DI human-admin compensation endpoint only while compensating a failed new-Project create:

```text
DELETE /v1/tenants/{tenantId}/admin/provisioning
```

DI must reject the compensation when operational/document data exists. On success, DI removes the Tenant provisioning rows transactionally and verifies zero state.

This endpoint is not exposed by Web.

## 6. Error handling

All failures use the existing public business-error catalogue. Customer-facing responses must not expose internal module names, downstream URLs, raw database/network exceptions, credentials, tokens, correlation IDs or stack details.

Technical failure and compensation evidence remains in protected service logs/audit records.
