# Audit Core UC02 — Synchronous Administrative Transaction Alignment

**Status:** APPROVED UC02 ALIGNMENT  
**Date:** 2026-08-23  
**Repository:** `verigence/verigence-audit-core`  
**Supersedes for synchronous admin mutations:** recovery/retry semantics in `AUDIT_CORE_UC02_ADMIN_ALIGNMENT.md` section 10 and `AUDIT_CORE_API_CONTRACT_v1.4.md`

## 1. Platform administrative invariant

For Phase-1 synchronous administrative mutations across Security, Audit Core and DI:

```text
validate first
-> execute synchronously
-> commit only when the complete business operation succeeds
-> if a downstream side effect has already committed and a later step fails, compensate it immediately in reverse order
-> return one success or one business-safe error
```

The following are not part of the normal Phase-1 synchronous admin contract:

- automatic retry;
- `RECOVERY_REQUIRED` as a persisted business-operation outcome;
- user-driven `/retry` workflow;
- leaving half-created Projects, Tenants, role mappings or DI provisioning state for later completion.

Protected audit/error history may remain. Incomplete business data must not remain merely to support recovery.

## 2. Local versus cross-module rollback

A mutation wholly owned by one database uses the owning module's local transaction and rolls back on exception.

A cross-module mutation cannot use one physical SQL transaction across services. Audit Core therefore uses synchronous compensation for already-committed downstream side effects. Compensation is part of the same request path and is not a retry workflow.

If compensation itself cannot be completed because the owning service is unavailable, the request fails with a critical operational error and protected logs/audit evidence identify the unresolved compensation. The API must not report success or hide the inconsistency.

## 3. Project Create

`POST /v1/projects` is synchronous.

Required sequence:

1. validate Project request, references, dates and SuperAdmin authorization before creating business state;
2. create the Security Tenant using the same human SuperAdmin identity;
3. start the Audit Core Project transaction and insert the Project projection without committing;
4. synchronously provision DI for the Security-owned `tenantId`;
5. if DI succeeds, commit Audit Core and return `201`;
6. on any failure after Security Tenant creation:
   - rollback the Audit Core transaction;
   - if DI provisioning committed, remove only the DI provisioning state created for the new Tenant;
   - hard-delete the Security Tenant last;
   - return one business-safe error.

No Project provisioning operation resource or retry endpoint is required for this synchronous Phase-1 create path.

## 4. Role Mapping

Role Mapping is one logical administrative mutation spanning Security operating role and Audit Core Dealer/Outlet business scope.

Before mutation Audit Core resolves and retains the current business mapping. It then:

1. validates the requested role/scope;
2. changes the Security operating role;
3. writes the Audit Core business scope in one local transaction;
4. if the Audit Core write fails, restore the prior Security operating role (or remove the role when there was no prior role) before returning failure.

Removal follows the same rule: remove Security role first, then remove Audit Core scope; restore the prior Security role if the Audit Core write fails.

The API returns a normal completed mapping result or an error. It does not return `RECOVERY_REQUIRED`.

## 5. Project Activation

Readiness is fully evaluated before either module is changed.

Audit Core updates its Project status within a local transaction and invokes Security activation before committing the Audit Core transaction. If Security activation fails, Audit Core rolls back its transaction and remains `CONFIGURING`.

Activation returns success only when both Security and Audit Core are `ACTIVE`.

## 6. DI provisioning compensation boundary

DI remains authoritative for its own Tenant provisioning state. DI must expose a SuperAdmin-only synchronous compensation endpoint for a newly provisioned Tenant. The endpoint:

- is allowed only when no DI documents/operational work exist for the Tenant;
- deletes only the provisioning rows created by the UC02 Tenant provisioning operation;
- executes in one DI database transaction;
- returns success only after zero-state verification.

This is a compensation API, not a background purge/retry lifecycle.

## 7. Other administrative tasks

The same invariant applies to Dealer, Dealer Outlet, Project Master, configuration and other administrative mutations:

- single-module mutations remain ordinary local transactions;
- cross-module mutations must validate before writes and compensate already-committed downstream effects synchronously on failure;
- do not add durable retry/recovery state merely because more than one module participates.

Long-running operational processing and explicit whole-Project hard-delete/purge are separate concerns and are not converted by this alignment into synchronous single-transaction work.
