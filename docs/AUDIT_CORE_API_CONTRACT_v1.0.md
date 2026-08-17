# Verigence Audit Core — API Contract

**Document ID:** VAC-API-001  
**Version:** 1.0  
**Status:** DRAFT FOR REVIEW  
**Date:** 2026-08-15  
**Solution design:** VAC-SD-003 v2.1  
**Machine-readable contract:** `api/openapi-v1.yaml`

## 1. Contract principles

1. User-facing clients call **Audit Core only**. DI is internal and never directly exposed.
2. Audit Core records actual dealership/customer journey facts but does not stop/block dealer operations.
3. Audit state/outcome is separate from actual delivery/business status.
4. JWT Tenant must match `{tenantId}`.
5. Every material action enforces Security permission plus Audit Core business scope.
6. No baseline public DELETE endpoints.
7. `Idempotency-Key` is required for retryable creation/upload/command operations identified below.
8. `X-Correlation-ID` is accepted; if absent Audit Core generates one. It is returned on every response.
9. Material mutable resources use `versionNo`/ETag optimistic concurrency where applicable.
10. Errors use `application/problem+json` with stable Audit Core `errorCode`.

## 2. Common headers

### Request

- `Authorization: Bearer <Security JWT>`
- `X-Correlation-ID: <opaque string>` — optional but recommended
- `Idempotency-Key: <opaque unique key>` — required where stated
- `If-Match: "<versionNo>"` — for concurrency-sensitive updates where stated

### Response

- `X-Correlation-ID`
- `ETag` on resources supporting optimistic concurrency

## 3. Error response

Content-Type: `application/problem+json`

```json
{
  "type": "urn:verigence:audit-core:error:VAC-VAL-001",
  "title": "Validation failed",
  "status": 400,
  "detail": "One or more request fields are invalid.",
  "errorCode": "VAC-VAL-001",
  "correlationId": "c-123",
  "fieldErrors": [
    {"field": "bookingDate", "message": "must be present"}
  ]
}
```

Client responses never expose raw DI/provider/database exceptions.

## 4. Project / Dealer / Outlet

```text
GET    /v1/tenants/{tenantId}/project
PATCH  /v1/tenants/{tenantId}/project
POST   /v1/tenants/{tenantId}/dealers
GET    /v1/tenants/{tenantId}/dealers
GET    /v1/tenants/{tenantId}/dealers/{dealerId}
PATCH  /v1/tenants/{tenantId}/dealers/{dealerId}
POST   /v1/tenants/{tenantId}/dealers/{dealerId}/outlets
GET    /v1/tenants/{tenantId}/dealers/{dealerId}/outlets
GET    /v1/tenants/{tenantId}/dealers/{dealerId}/outlets/{outletId}
PATCH  /v1/tenants/{tenantId}/dealers/{dealerId}/outlets/{outletId}
```

Records may be inactivated/retired through PATCH where applicable. No DELETE endpoint is part of baseline.

### 4.1 Product SKU reference

```text
GET /v1/tenants/{tenantId}/reference/product-skus
```

The Product SKU reference lookup requires `audit.master.read` and returns active SKU references only for the OEM configured on the requested Tenant/Project. The existing query contract supports optional `q` search and `limit` from 1 to 100, default 50.

## 5. Customer / Journey

```text
POST   /v1/tenants/{tenantId}/outlets/{outletId}/customers
GET    /v1/tenants/{tenantId}/outlets/{outletId}/customers
GET    /v1/tenants/{tenantId}/customers/{customerId}
PATCH  /v1/tenants/{tenantId}/customers/{customerId}
GET    /v1/tenants/{tenantId}/customers/matches
POST   /v1/tenants/{tenantId}/customers/{customerId}/journeys
GET    /v1/tenants/{tenantId}/customers/{customerId}/journeys
GET    /v1/tenants/{tenantId}/journeys/{journeyId}
PATCH  /v1/tenants/{tenantId}/journeys/{journeyId}
```

Journey PATCH updates permitted observational/operational metadata; it is not a dealer-process control endpoint.

## 6. Booking and process data

```text
PUT    /v1/tenants/{tenantId}/journeys/{journeyId}/booking
GET    /v1/tenants/{tenantId}/journeys/{journeyId}/booking
GET/PUT       /v1/tenants/{tenantId}/journeys/{journeyId}/commercials
GET/POST/PATCH /v1/tenants/{tenantId}/journeys/{journeyId}/payments
GET/PUT       /v1/tenants/{tenantId}/journeys/{journeyId}/finance
GET/PUT       /v1/tenants/{tenantId}/journeys/{journeyId}/insurance
GET/PUT       /v1/tenants/{tenantId}/journeys/{journeyId}/trade-in
GET/PUT       /v1/tenants/{tenantId}/journeys/{journeyId}/vehicle
GET/PUT       /v1/tenants/{tenantId}/journeys/{journeyId}/registration
GET/PUT       /v1/tenants/{tenantId}/journeys/{journeyId}/delivery
```

### Delivery representation

Delivery records observed business status separately from audit state:

```json
{
  "journeyId": "uuid",
  "plannedDeliveryAt": "2026-08-20T10:30:00+05:30",
  "deliveryIntimatedAt": "2026-08-20T09:55:00+05:30",
  "actualDeliveryStatusCode": "<configured-code>",
  "actualDeliveredAt": "2026-08-20T11:05:00+05:30",
  "statusSource": "EVIDENCE|OPERATIONAL_INPUT|SOURCE_SYSTEM",
  "auditState": "IN_PROGRESS",
  "auditOutcome": "PENDING",
  "versionNo": 7
}
```

`actualDeliveryStatusCode` uses an approved configured code set. The contract deliberately does not invent code values before business approval.

There is no Audit Core API such as `approve-delivery`, `block-delivery`, `stop-delivery` or `cancel-delivery`.

## 7. Evidence / DI façade

### 7.1 Upload evidence

```text
POST /v1/tenants/{tenantId}/journeys/{journeyId}/evidence
Content-Type: multipart/form-data
Idempotency-Key: required
```

Form fields:

- `file` — required
- `evidencePurpose` — required
- `requirementKey` — optional/conditional
- `documentTypeKey` — Audit Core business/document key where known

Audit Core authorizes the action, resolves the internal DI Subject, calls DI internally, persists an Audit Core evidence link, and returns an Audit Core `evidenceId`.

Example response:

```json
{
  "evidenceId": "uuid",
  "journeyId": "uuid",
  "documentTypeKey": "BOOKING_DOCKET",
  "processingStatus": "PENDING",
  "verificationStatus": "NOT_VERIFIED",
  "createdAtUtc": "2026-08-15T08:45:00Z"
}
```

DI document IDs are internal implementation details.

### 7.2 Evidence queries/actions

```text
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/evidence
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/evidence/{evidenceId}
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/evidence/{evidenceId}/facts
POST /v1/tenants/{tenantId}/journeys/{journeyId}/evidence/{evidenceId}/refresh
POST /v1/tenants/{tenantId}/journeys/{journeyId}/evidence/{evidenceId}/verify
```

`verify` is permission-gated for formal verifier roles. PC baseline does not receive that permission.

Evidence is not hard-deleted through baseline public APIs. Incorrect associations use auditable void/supersede/unlink semantics; DI content deletion is not exposed through this contract.

## 8. Audit state / findings / review

```text
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/audit
POST /v1/tenants/{tenantId}/journeys/{journeyId}/audit/start
POST /v1/tenants/{tenantId}/journeys/{journeyId}/audit/submit
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/findings
POST /v1/tenants/{tenantId}/journeys/{journeyId}/findings
PATCH /v1/tenants/{tenantId}/journeys/{journeyId}/findings/{findingId}
POST /v1/tenants/{tenantId}/journeys/{journeyId}/review-decisions
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/review-decisions
```

Review decision values from the supplied process are:

- `BREACH`
- `NO_BREACH`
- `SEND_BACK`

`SEND_BACK` changes audit work state and creates/reopens durable PC work; it does not alter the customer's actual delivery/business status.

## 9. Durable audit tasks

```text
GET  /v1/tenants/{tenantId}/tasks
GET  /v1/tenants/{tenantId}/tasks/{taskId}
POST /v1/tenants/{tenantId}/tasks/{taskId}/claim
POST /v1/tenants/{tenantId}/tasks/{taskId}/start
POST /v1/tenants/{tenantId}/tasks/{taskId}/complete
POST /v1/tenants/{tenantId}/tasks/{taskId}/cancel
GET  /v1/tenants/{tenantId}/tasks/{taskId}/history
```

Task cancellation is an audit-work action, not deletion. It requires actor, reason and history. Tasks are never hard-deleted through public APIs.

## 10. Daily/EOD, CRM and escalation

```text
POST /v1/tenants/{tenantId}/outlets/{outletId}/daily-ops
GET  /v1/tenants/{tenantId}/outlets/{outletId}/daily-ops
GET  /v1/tenants/{tenantId}/daily-ops/{runId}
POST /v1/tenants/{tenantId}/daily-ops/{runId}/complete
GET/POST       /v1/tenants/{tenantId}/journeys/{journeyId}/crm-interactions
GET/POST/PATCH /v1/tenants/{tenantId}/journeys/{journeyId}/escalations
```

## 11. Versioned master APIs

```text
/v1/tenants/{tenantId}/masters/price-lists
/v1/tenants/{tenantId}/masters/discount-schemes
/v1/tenants/{tenantId}/masters/document-requirement-profiles
/v1/tenants/{tenantId}/masters/audit-controls
```

Version operations:

```text
POST .../{masterId}/versions
GET  .../{masterId}/versions
POST .../{masterId}/versions/{versionId}/publish
POST .../{masterId}/versions/{versionId}/retire
```

Published versions are immutable.

## 12. Analytics/read APIs

```text
GET /v1/tenants/{tenantId}/dashboard
GET /v1/tenants/{tenantId}/analytics/findings
GET /v1/tenants/{tenantId}/analytics/payments
GET /v1/tenants/{tenantId}/analytics/delivery
GET /v1/tenants/{tenantId}/analytics/daily-ops
```

Exact analytics formulas remain governed by the approved analytics catalogue/open business decisions.

## 13. Executive authorization rule

Executive is tenant-wide super privileged for Audit Core except delete/purge/destructive operations. Every Executive action remains tenant-bound, permission-checked and audit-logged.

## 14. Idempotency

Required at minimum for Journey creation, evidence upload, audit submit, review decision, task completion/cancellation, Daily/EOD completion, and other commands designated retryable in OpenAPI.

Same key + same semantic request returns the original logical result. Same key + conflicting payload returns `VAC-CONFLICT-003`.

## 15. Pagination/filtering

Growing lists use cursor pagination with `limit` and `cursor`. Domain filters may include dealer, outlet, auditState, auditOutcome, `actualDeliveryStatusCode`, assignedActorId and approved date ranges.

## 16. Deletion policy

Baseline public API provides **no HTTP DELETE endpoints**. Corrections use auditable `retire`, `inactivate`, `void`, `supersede` or task `cancel` semantics according to domain meaning. Future deletion/purge requires explicit owner approval and separate permission/retention design.
