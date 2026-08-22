# Verigence Audit Core — API Contract UC02 Project Resume and Provisioning Error Addendum

**Document ID:** VAC-API-004  
**Version:** 1.3  
**Status:** APPROVED FOR UC02 DEV IMPLEMENTATION  
**Date:** 2026-08-22  
**Base contract:** `VAC-API-003 v1.2` / `docs/AUDIT_CORE_API_CONTRACT_v1.2.md`  

This addendum changes only UC02 Project selection/resume and Project provisioning outcome visibility. All other VAC-API-003 v1.2 semantics remain unchanged.

## 1. List existing Projects for administration

```text
GET /v1/projects
```

Authorization:

```text
Authorization: Bearer <Security-issued human SuperAdmin JWT>
```

Audit Core remains the browser backend boundary. Audit Core uses the same human Bearer token to obtain the Security Tenant directory and resolves each persisted Audit Core Project under its Tenant context. Web does not call Security directly.

Response:

```json
[
  {
    "tenantId": "tenant-id",
    "projectCode": "tenant-owned-project-code",
    "projectName": "MahindraWest",
    "projectStatus": "CONFIGURING",
    "securityTenantStatus": "PROVISIONING"
  }
]
```

Only persisted Projects whose tracked initial `PROJECT_PROVISION` operation is completed are selectable. A legacy/pre-existing Project with no tracked initial provisioning operation may also be returned. A Project whose initial provisioning is `IN_PROGRESS` or `RECOVERY_REQUIRED` must not become selectable as a way to bypass its provisioning/retry path. Ordering is deterministic by Project Name then Project Code.

Selecting one item does not create or copy state. Web sets the returned `tenantId` as its current business context and loads the existing tenant-scoped administration APIs.

If the Security administrative control plane required to resolve the Project directory is unavailable, Audit Core returns a normal `application/problem+json` response with public code `VAC-SYS-002` and a business-safe message. Raw endpoint names, HTTP/network causes and downstream exception text are not returned to Web.

## 2. Resume child administration state

After Project selection, Web must use the existing authoritative APIs for persisted UC02 state:

```text
GET /v1/tenants/{tenantId}/project
GET /v1/tenants/{tenantId}/dealers
GET /v1/tenants/{tenantId}/dealers/{dealerId}/outlets
GET /v1/tenants/{tenantId}/role-mapping-candidates
GET /v1/tenants/{tenantId}/role-mappings
GET /v1/tenants/{tenantId}/project-masters
GET /v1/tenants/{tenantId}/project/readiness
```

Therefore an existing Dealer or Dealer Outlet already mapped to the selected Project must be returned by those APIs and displayed when its step is opened. The same rule applies to role mappings, Project Masters and readiness state.

## 3. Project provisioning response error fields

The response schema used by:

```text
POST /v1/projects
GET /v1/project-provisioning-operations/{operationId}
POST /v1/project-provisioning-operations/{operationId}/retry
```

contains two nullable **public** fields:

```text
errorCode     string | null
errorMessage  string | null
```

These fields are a business-safe translation of the durable technical failure state. Audit Core keeps the internal technical code, downstream HTTP/network cause and diagnostic summary in protected operation state/logs for support, but must not return those technical details to Web.

Public mapping for UC02 Project provisioning is:

| Internal failure category | Public errorCode | Public errorMessage |
|---|---|---|
| Security administrative/provisioning failure | `VAC-SYS-002` | `Project security setup could not be completed. Please try again.` |
| Audit Core Project projection/persistence failure | `VAC-SYS-001` | `Project setup could not be completed. Please try again.` |
| DI unavailable/transient failure | `VAC-DI-001` | `Project processing service is temporarily unavailable. Please try again.` |
| Other DI integration failure | `VAC-DI-004` | `Project processing setup could not be completed. Please try again.` |

Example recovery response:

```json
{
  "operationId": "uuid",
  "tenantId": null,
  "projectName": "MahindraWest",
  "projectStatus": "CONFIGURING",
  "provisioningStatus": "RECOVERY_REQUIRED",
  "currentStep": "SECURITY",
  "errorCode": "VAC-SYS-002",
  "errorMessage": "Project security setup could not be completed. Please try again."
}
```

The API must not expose downstream endpoint URLs, downstream HTTP status text, network exception text, database exception text, stack traces, credentials, tokens, OTPs, raw uploaded content or other sensitive/technical details in these fields.

For `READY` and `IN_PROGRESS`, both fields are `null` unless a future approved contract says otherwise.

## 4. Web interpretation

Web must interpret the business provisioning status independently of HTTP success:

- `READY` — show successful Project setup and load the Project context;
- `IN_PROGRESS` — show setup in progress;
- `RECOVERY_REQUIRED` — show setup failed/recovery required, render the public `errorCode` and business-safe `errorMessage`, and offer the existing retry action.

Web must not display `correlationId`, operation internals, downstream endpoint information or raw technical error text.

Normal non-2xx `application/problem+json` behavior remains unchanged; Web displays only the backend public `errorCode` and safe `detail`/`title` through the common Audit Core error formatter.
