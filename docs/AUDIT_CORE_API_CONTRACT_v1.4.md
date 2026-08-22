# Verigence Audit Core — API Contract UC02 Parallel Provisioning Addendum

**Document ID:** VAC-API-005  
**Version:** 1.4  
**Status:** APPROVED FOR UC02 DEV IMPLEMENTATION  
**Date:** 2026-08-22  
**Base contract:** `VAC-API-004 v1.3` / `docs/AUDIT_CORE_API_CONTRACT_v1.3.md`

This addendum changes only the internal orchestration/recovery semantics of UC02 Project provisioning. The public endpoint paths and `ProjectProvisioningResponse` schema remain unchanged.

## 1. Project provisioning orchestration

For:

```text
POST /v1/projects
POST /v1/project-provisioning-operations/{operationId}/retry
```

Audit Core must execute the distributed Project provisioning operation as follows:

1. create/reuse the Security Tenant using the authenticated human SuperAdmin identity and Project idempotency key;
2. persist the Security receipt including the Security-owned `tenantId`;
3. once the Security receipt exists, execute these independent branches concurrently when both are incomplete:
   - Audit Core Project projection;
   - DI Tenant provisioning via `PUT /v1/tenants/{tenantId}/admin/provisioning`;
4. persist each successful branch receipt independently;
5. report `READY` only after both the Audit Core receipt and a DI `READY` receipt exist.

Security cannot be parallelized with the other branches because its `tenantId` is an input to both.

## 2. Retry semantics

Retry is receipt-driven and idempotent:

- an existing `security_receipt` prevents another Security Tenant create;
- an existing `audit_core_receipt` prevents another Project projection;
- an existing `di_receipt` whose `provisioningStatus` is `READY` prevents another DI provisioning mutation;
- only the missing or incomplete branch is rerun.

If one post-Security branch succeeds while the other fails, the successful receipt is retained. `RECOVERY_REQUIRED` identifies the branch that still requires work. A retry must not discard or repeat the already-successful branch.

## 3. Response semantics

The v1.3 response remains:

```text
operationId
 tenantId
 projectName
 projectStatus
 provisioningStatus = READY | IN_PROGRESS | RECOVERY_REQUIRED
 currentStep = SECURITY | AUDIT_CORE | DI | COMPLETE
 errorCode
 errorMessage
```

`currentStep` is the current/recovery branch indicator. Under parallel post-Security execution it is not a claim that Audit Core and DI always executed in chronological sequence.

Public business-error mapping and Web display rules from v1.3 remain unchanged.

## 4. Completion invariant

A Project provisioning operation may be marked `COMPLETED` only when all of the following are true:

```text
security_receipt exists
AND audit_core_receipt exists
AND di_receipt.provisioningStatus == READY
```

No partial success may be reported as `READY`.
