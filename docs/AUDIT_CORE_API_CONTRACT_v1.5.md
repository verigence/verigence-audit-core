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

Success response is `201`:

```json
{
  "tenantId": "tenant-id",
  "projectCode": "tenant-owned-project-code",
  "projectName": "MahindraWest",
  "projectStatus": "CONFIGURING"
}
```

There is no normal `202`, `IN_PROGRESS`, `RECOVERY_REQUIRED`, `operationId`, `currentStep`, provisioning operation GET, or provisioning retry endpoint for synchronous Project Create.

Failure is returned as the existing business-safe `application/problem+json` contract. Before returning failure, Audit Core must synchronously rollback/compensate any Project-create business state already written in Audit Core, DI and Security as defined by `AUDIT_CORE_UC02_ADMIN_TRANSACTION_ALIGNMENT.md`.

## 2. Project provisioning operation endpoints removed from the UC02 Web contract

The following endpoints are no longer part of the Phase-1 Web contract:

```text
GET  /v1/project-provisioning-operations/{operationId}
POST /v1/project-provisioning-operations/{operationId}/retry
```

Existing historical rows in `administrative_operations` are audit/diagnostic history only and are not required to drive new Project Create requests.

## 3. Role Mapping mutation response

For:

```text
PUT    /v1/tenants/{tenantId}/role-mappings/{userId}
DELETE /v1/tenants/{tenantId}/role-mappings/{userId}
```

successful PUT returns the resulting mapping directly:

```json
{
  "userId": "user-id",
  "operatingRole": "PC",
  "dealerIds": [],
  "outletIds": ["outlet-id"]
}
```

successful DELETE returns `204 No Content`.

`RECOVERY_REQUIRED`, `operationId` and `operationStatus` are removed from the normal role-mapping contract. If a later local write fails after Security has changed, Audit Core restores the prior Security operating role before returning failure.

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
