# Audit Core UC02 — Parallel Project Provisioning Amendment

**Status:** APPROVED FOR UC02 DEV IMPLEMENTATION  
**Date:** 2026-08-22  
**Repository:** `verigence/verigence-audit-core`  
**Applies to:** `PROJECT_PROVISION` orchestration

This amendment refines the UC02 Project provisioning sequence after the DEV timeout defect proved that a long-running downstream administrative mutation can commit successfully while the browser-facing orchestrator times out.

## 1. Required orchestration sequence

Security remains the first and serial step because Security owns Tenant creation and therefore owns the `tenantId` required by all Project-scoped modules.

After a durable Security Tenant receipt exists, Audit Core Project projection and DI Tenant provisioning are independent Phase-1 branches and must be started concurrently:

```text
Security Tenant create
        |
        v
 durable tenantId / Security receipt
        |
        +---------------------------+
        |                           |
        v                           v
Audit Core Project projection    DI Tenant provisioning
        |                           |
        +-------------+-------------+
                      |
                      v
          complete only when both succeed
```

DI's UC02 provisioning API requires the Security-owned `tenantId`, authenticated human SuperAdmin context and idempotency key; it does not require an Audit Core Project row. Therefore no business dependency requires Audit Core projection to finish before DI provisioning starts.

## 2. Durable branch receipts and retry

The existing `auditcore.administrative_operations` receipts remain authoritative:

- `security_receipt` — Security Tenant creation completed;
- `audit_core_receipt` — Audit Core Project projection completed;
- `di_receipt` — DI Tenant provisioning completed.

Each successful branch receipt must be persisted independently. A successful branch must not be rerun solely because the other parallel branch failed.

On retry:

- if `security_receipt` exists, Tenant creation is not repeated;
- if `audit_core_receipt` exists, Audit Core projection is not repeated;
- if `di_receipt` exists and its provisioning status is `READY`, DI provisioning is not repeated;
- only missing or incomplete branches execute.

The operation reaches `COMPLETED` / `COMPLETE` only when the Security receipt exists, the Audit Core receipt exists, and DI reports `READY`.

## 3. Recovery semantics

If one parallel branch succeeds and the other fails, the successful receipt is retained and the operation becomes `RECOVERY_REQUIRED` for the failed/incomplete branch.

The existing public response shape is unchanged. `currentStep` identifies the branch requiring recovery rather than implying that all Project provisioning work is strictly sequential.

Public errors continue to use business-safe UC02 error codes/messages. Technical exception details remain internal.

## 4. Concurrency and correctness constraints

- Audit Core and DI work execute in separate call/transaction contexts; a SQLAlchemy `Engine` may provide the independent connection used by the Audit Core branch.
- The same authenticated human identity and the same Project provisioning idempotency key are preserved in downstream calls.
- No shared mutable database session may be used concurrently across branches.
- Parallel execution must not weaken Tenant isolation, authorization, idempotency, correlation, auditability or recovery behavior.
- Web must continue to block a fresh Project create while a provisioning operation is unresolved and must use the retry operation for recovery.

## 5. Performance intent

The expected critical path becomes approximately:

```text
Security latency + max(Audit Core projection latency, DI provisioning latency)
```

rather than:

```text
Security latency + Audit Core projection latency + DI provisioning latency
```

This is a latency optimization only; completion remains fail-closed until all required module state is durable and ready.
