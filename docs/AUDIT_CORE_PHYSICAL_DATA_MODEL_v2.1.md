# Verigence Audit Core — Physical Data Model v2.1

**Document ID:** VAC-DM-002  
**Version:** 1.0  
**Status:** DRAFT FOR REVIEW  
**Date:** 2026-08-15  
**Solution Design:** VAC-SD-003 v2.1  
**Physical DDL:** `database/AUDIT_CORE_POSTGRESQL_SCHEMA_v2.1.sql` / VAC-DB-002

## 1. Purpose

This document explains the physical PostgreSQL model generated from the corrected Audit Core v2.1 design. It replaces the v1 physical model as the implementation candidate; it does not modify or migrate the historical v1 schema automatically.

The model is intentionally based on these approved architecture rules:

1. one Security Tenant = one Audit Project;
2. Project -> Dealer -> Dealer Outlet -> Customer -> Journey;
3. Booking, Payment, Finance, Insurance/VAS, Trade-In, Vehicle/Registration and Delivery are peer process areas of a Journey;
4. Audit Core observes dealer/customer business facts and does not control dealer operations;
5. actual delivery/business status is separate from Verigence audit state/outcome;
6. all user-facing document intelligence is exposed through Audit Core; DI identifiers remain internal;
7. workflow/tasks are durable and recoverable;
8. published decision-relevant master versions are immutable;
9. no baseline public hard-delete lifecycle exists;
10. operational logs go to Observability; Audit Core stores its authoritative business audit trail separately.

## 2. Core hierarchy

```text
projects (PK = tenant_id)
  -> dealers
      -> dealer_outlets
          -> customers
              -> journeys
                  -> bookings
                  -> journey_products
                  -> commercial_lines
                  -> discount_applications
                  -> payments / payment_verification_events
                  -> finance_records
                  -> insurance_records / journey_addons
                  -> trade_in_cases
                  -> vehicle_records / registration_records
                  -> deliveries / delivery_status_history
                  -> evidence / evidence_facts
                  -> audit_evaluations / audit_findings / review_decisions
                  -> workflow_instances / workflow_tasks
```

`tenant_id` is carried on every tenant-owned table and is part of the relevant primary/foreign keys so cross-tenant relationships cannot be formed accidentally.

## 3. Project = Tenant

`projects.tenant_id` is the Project key. The schema does not create a separate Project UUID authorization hierarchy below Security Tenant.

Dealer, Outlet, Customer and Journey records all carry the same `tenant_id`. PostgreSQL RLS is enabled and forced on every table containing `tenant_id`.

The runtime database role must be different from the schema owner, must not have `BYPASSRLS`, and should not receive `DELETE` on Audit Core business tables.

## 4. Actual business status vs audit state

### 4.1 Configured actual business statuses

`business_status_codes` is a tenant-owned configurable code catalogue. It deliberately contains no seeded dealer delivery-status values because the canonical dealer status vocabulary is still an open business decision.

Process records may point to status domains such as `BOOKING`, `PAYMENT`, `TRADE_IN`, `DELIVERY` and `JOURNEY` without Audit Core interpreting those values as permission to control the dealership process.

### 4.2 Delivery

`deliveries` stores the current observed delivery facts:

- planned delivery time;
- delivery intimation time;
- configured `actual_delivery_status_code`;
- actual delivered time;
- provenance/source;
- supporting evidence reference.

`delivery_status_history` is append-only and preserves every observed status transition with source/provenance. It has no audit-state field.

### 4.3 Audit state

`journeys.audit_state` and `journeys.audit_outcome` are Verigence concepts only.

Audit state currently supports the v2.1 review flow:

`NOT_STARTED -> IN_PROGRESS -> PC_SUBMITTED -> TL_REVIEW -> SENT_BACK/PM_REVIEW/REVIEW_COMPLETE`

Audit outcome is separate: `PENDING | NO_BREACH | BREACH`.

`audit_state_events` is append-only. `SEND_BACK` is audit work state and never changes actual delivery status.

## 5. DI façade model

The user-facing key is `evidence.evidence_id`. The following are internal-only integration fields:

- `di_subject_mappings.di_subject_id`;
- `evidence.di_subject_id`;
- `evidence.di_document_id`.

No raw document bytes are stored in Audit Core.

### 5.1 Evidence ingestion reliability

`evidence_ingestion_operations` persists the Audit Core -> DI orchestration state for an idempotent upload request. This addresses the partial-failure case where DI may accept a document but the outer user request fails before Audit Core completes its evidence link.

Its states are technical orchestration states only:

`RECEIVED | DI_SUBMITTING | DI_ACCEPTED | LINKED | RETRY_WAIT | FAILED | DEAD_LETTER`.

This table is not exposed as a business workflow to users.

### 5.2 Evidence facts

`evidence_facts` stores a provenance/read projection of DI-extracted facts required by Audit Core rules. It does not replace DI as document/extraction authority.

`finding_evidence` connects Audit findings to Audit Core evidence and, optionally, a specific projected evidence fact.

## 6. Master/version model

Stable master identity and decision versions are separate.

```text
price_lists -> price_list_versions -> price_list_items
discount_schemes -> discount_scheme_versions -> eligibility / benefits
document_requirement_profiles -> profile_versions -> requirement_items
audit_controls -> audit_control_versions
project -> project_policy_versions
```

Published/retired version rows are protected by database triggers. Child rows of a version may be inserted/updated/deleted only while the parent version is `DRAFT`.

The database therefore prevents changing a published price list or discount eligibility after an Audit Journey was evaluated against it.

## 7. Durable audit workflow

The durable workflow model is deliberately separate from dealer business state.

### 7.1 `workflow_instances`

Correlates a durable Audit workflow to a Journey.

### 7.2 `workflow_tasks`

Stores durable human/system work including:

- process area and task type;
- assignment role/actor and Dealer/Outlet business scope;
- durable state;
- priority/availability/due time;
- optimistic `version_no`;
- retry counters and next-attempt time;
- lease owner/heartbeat/expiry for worker tasks;
- idempotent `effect_key`;
- stable last error code/summary.

Ready/retry queue and stale-lease indexes are included for `FOR UPDATE SKIP LOCKED` worker patterns.

### 7.3 Immutable history

`workflow_task_events` is append-only. Task cancellation is a state transition with actor/reason, not deletion.

`workflow_task_attempts` records individual system execution attempts. Exhausted work is represented visibly by `workflow_dead_letters`; only one unresolved dead-letter entry per task is allowed.

### 7.4 Atomicity

Application transactions can atomically persist:

- audit-state/review change;
- resulting workflow task;
- workflow event;
- authoritative Audit event;
- outbox event.

This is the primary mechanism preventing lost audit tasks.

## 8. Reliability and integration records

`idempotency_records` supports API/mobile replay.

`outbox_events` provides reliable post-commit integration publication without a broker dependency at launch.

`inbox_events` supports future deduplicated inbound asynchronous events.

Retryable records carry stable Audit Core error codes rather than raw provider/database exceptions.

## 9. Audit trail vs logging

`audit_chain_heads` + `audit_events` form the authoritative entity-scoped tamper-evident Audit Core business history. `audit_events` is append-only.

Operational application logs, metrics and traces are intentionally not duplicated into business tables; they are emitted to `verigence-observability` according to the Observability baseline and the logging rules in VAC-SD-003.

## 10. No-delete model

The physical model supports correction through business semantics such as:

- `ACTIVE/INACTIVE` for master/reference records;
- `VOIDED/SUPERSEDED/UNLINKED` for evidence associations;
- `RETIRED` for published master versions;
- `VOIDED` for findings;
- `CANCELLED` for Audit workflow tasks;
- immutable history records for review/audit/task/status transitions.

The DDL does not assume a runtime database role name, so `DELETE` privilege revocation is a deployment responsibility. The runtime role contract in VAC-DB-002 explicitly requires no DELETE access to Audit Core business/audit/master/workflow tables in the current baseline.

## 11. Table groups

| Group | Principal tables |
|---|---|
| Platform references | `product_categories`, `oems`, `product_models`, `product_variants`, `colours`, `product_skus` |
| Project landscape | `projects`, `project_policy_versions`, `dealers`, `dealer_outlets`, `dealership_staff`, `business_assignments`, `business_status_codes` |
| Versioned masters | `price_*`, `discount_*`, `document_requirement_*`, `audit_control*` |
| Customer/Journey | `customers`, `customer_identity_index`, `di_subject_mappings`, `journeys`, `audit_state_events`, `bookings`, `journey_products` |
| DI façade | `journey_document_requirements`, `evidence_ingestion_operations`, `evidence`, `evidence_facts` |
| Sale journey | `commercial_lines`, `discount_applications`, `payments`, `payment_verification_events`, `finance_records`, `insurance_records`, `journey_addons`, `trade_in_cases`, `vehicle_records`, `registration_records`, `deliveries`, `delivery_status_history` |
| Audit | `audit_evaluations`, `audit_findings`, `finding_evidence`, `finding_remarks`, `review_decisions` |
| Durable workflow | `workflow_instances`, `workflow_tasks`, `workflow_task_events`, `workflow_task_attempts`, `workflow_dead_letters` |
| Daily/CRM/Escalation | `daily_ops_runs`, `daily_ops_items`, `activity_records`, `pc_daily_notes`, `crm_interactions`, `escalations` |
| Reliability | `idempotency_records`, `outbox_events`, `inbox_events` |
| Authoritative history | `audit_chain_heads`, `audit_events` |

## 12. Open business decisions intentionally not encoded

VAC-DB-002 does not invent:

- Satellite vehicle-volume threshold value;
- dealer actual delivery status codes;
- PM versus PMO final terminology;
- exact TL/PM verification routing gates;
- Total Discount / Above Scheme formula;
- PO/DO/Refund realised-payment formula;
- trade-in 60 vs 90-day ageing threshold;
- Short/Excess formula;
- Insurance Calculator provider/rule details;
- Dealer Outlet <-> Security Location cardinality;
- repeat-customer reuse policy.

These remain configuration or future approved design decisions rather than hard-coded database rules.

## 13. Review status

`VAC-DB-002` is a **DRAFT FOR REVIEW physical-schema candidate** aligned to `VAC-SD-003 v2.1`. It must not be applied to an environment as an automatic replacement for `VAC-DB-001`; implementation/migration sequencing should be designed only after this model is approved.
