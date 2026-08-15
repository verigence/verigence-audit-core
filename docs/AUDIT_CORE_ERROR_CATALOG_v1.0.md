# Verigence Audit Core — Error Catalogue

**Document ID:** VAC-ERR-001  
**Version:** 1.0  
**Status:** DRAFT FOR REVIEW  
**Date:** 2026-08-15

All public API errors use these stable codes. Internal DI/Security/database/provider errors are translated to this catalogue before returning to clients.

| Error code | HTTP | Title | Meaning / handling |
|---|---:|---|---|
| VAC-AUTH-001 | 401 | Authentication required | Missing/invalid/expired Security access token. |
| VAC-AUTH-002 | 403 | Permission denied | Actor lacks required effective permission. |
| VAC-AUTH-003 | 403 | Tenant mismatch | JWT tenant does not match requested tenant. |
| VAC-AUTH-004 | 403 | Business scope denied | Actor not allowed for requested Dealer/Outlet/Journey/task scope. |
| VAC-VAL-001 | 400 | Validation failed | Request schema/field validation failed. |
| VAC-VAL-002 | 422 | Business validation failed | Syntactically valid request violates an approved Audit Core invariant. |
| VAC-VAL-003 | 400 | Unsupported evidence | Evidence cannot be accepted under current requirement/configuration. |
| VAC-NF-001 | 404 | Project not found | Tenant Project projection not found/inactive for requested operation. |
| VAC-NF-002 | 404 | Dealer not found | Dealer not found in tenant. |
| VAC-NF-003 | 404 | Outlet not found | Outlet not found in tenant/dealer. |
| VAC-NF-004 | 404 | Customer not found | Customer not found in tenant. |
| VAC-NF-005 | 404 | Journey not found | Journey not found in tenant. |
| VAC-NF-006 | 404 | Evidence not found | Audit Core evidence association not found. |
| VAC-NF-007 | 404 | Task not found | Audit workflow task not found. |
| VAC-CONFLICT-001 | 409 | Version conflict | Optimistic concurrency/version check failed. |
| VAC-CONFLICT-002 | 409 | Invalid audit state | Requested audit action is not valid from current audit state. |
| VAC-CONFLICT-003 | 409 | Idempotency conflict | Same Idempotency-Key reused with different semantic payload. |
| VAC-CONFLICT-004 | 409 | Duplicate active task | Effect/idempotency rule prevents duplicate durable task. |
| VAC-MASTER-001 | 409 | Master version immutable | Attempt to modify a published immutable version. |
| VAC-MASTER-002 | 422 | No effective master version | Required effective price/discount/profile/control version cannot be resolved. |
| VAC-MASTER-003 | 409 | Master publish conflict | Version cannot be published due to overlapping/conflicting configuration. |
| VAC-WF-001 | 409 | Task already claimed | Task is already claimed/in progress by another actor/worker. |
| VAC-WF-002 | 409 | Task already completed | Attempt to act on a completed terminal task. |
| VAC-WF-003 | 422 | Task completion invalid | Required audit inputs/remarks are missing for completion. |
| VAC-WF-004 | 503 | Workflow temporarily unavailable | Durable workflow persistence/worker capability unavailable. |
| VAC-DI-001 | 503 | Document intelligence unavailable | Audit Core cannot reach DI or DI returns transient failure. |
| VAC-DI-002 | 422 | Document rejected | DI rejects document due to generic quality/type/content issue. |
| VAC-DI-003 | 409 | Document not ready | Requested evidence facts/verification are not ready yet. |
| VAC-DI-004 | 502 | Document intelligence error | Translated DI integration failure requiring support review. |
| VAC-SYS-001 | 500 | Internal error | Unexpected Audit Core failure; correlationId required for support. |
| VAC-SYS-002 | 503 | Dependency unavailable | Non-DI downstream dependency temporarily unavailable. |
| VAC-SYS-003 | 429 | Too many requests | Rate/concurrency protection triggered. |

## Error logging rules

Every mapped error log includes `errorCode`, `correlation_id`, tenant/project and safe resource identifiers where applicable. Sensitive payloads, access tokens, raw documents and raw personal identifiers are excluded.

Expected client/domain errors (4xx) are not logged with full stack traces by default. Unexpected 5xx errors include internal exception class/stack trace in protected server logs only and are correlated by `correlation_id`.
