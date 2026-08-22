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

Only persisted Projects are returned. Ordering is deterministic by Project Name then Project Code.

Selecting one item does not create or copy state. Web sets the returned `tenantId` as its current business context and loads the existing tenant-scoped administration APIs.

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

adds two nullable fields:

```text
errorCode     string | null
errorMessage  string | null
```

Example recovery response:

```json
{
  "operationId": "uuid",
  "tenantId": null,
  "projectName": "MahindraWest",
  "projectStatus": "CONFIGURING",
  "provisioningStatus": "RECOVERY_REQUIRED",
  "currentStep": "SECURITY",
  "errorCode": "SECURITY_ADMIN_FAILED",
  "errorMessage": "Security administrative request failed with HTTP 403"
}
```

The values come from the durable administrative-operation failure record and must be safe for an administrator-facing UI. They must not include credentials, tokens, OTPs, raw uploaded content or other secrets.

For `READY` and `IN_PROGRESS`, both fields are `null` unless a future approved contract says otherwise.

## 4. Web interpretation

Web must interpret the business provisioning status independently of HTTP success:

- `READY` — show successful Project setup and load the Project context;
- `IN_PROGRESS` — show setup in progress;
- `RECOVERY_REQUIRED` — show setup failed/recovery required, render backend `errorCode` and `errorMessage`, and offer the existing retry action.

Web must not display `correlationId` even when the API/problem response contains it.

Normal non-2xx `application/problem+json` behavior remains unchanged; Web displays backend `errorCode` and safe `detail`/`title` through the common Audit Core error formatter.
