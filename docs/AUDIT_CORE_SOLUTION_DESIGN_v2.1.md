# Verigence Audit Core — Consolidated Solution Design

**Document ID:** VAC-SD-003  
**Version:** 2.1  
**Status:** DRAFT FOR REVIEW — candidate to supersede VAC-SD-002 v2.0 after owner approval  
**Date:** 2026-08-15  
**Requirements:** `VAC-REQ-001 v1.0` + `VAC-REQ-ADD-001 v1.1` + `VAC-REQ-ADD-002 v1.2`  
**API contract:** `VAC-API-001 v1.0` / `api/openapi-v1.yaml`  
**Error catalogue:** `VAC-ERR-001 v1.0`

> This revision preserves the supplied process workbooks, the previously approved Project/Dealer/Outlet/Customer/Journey corrections, and the useful parts adopted from the supplied third-party design. It adds the latest owner rules without inventing unresolved business values.

---

## 1. Foundational architecture

Audit Core is the business/audit module for the vehicle-sale journey. It SHALL initially be a **modular monolith** with PostgreSQL, bounded internal domains, durable audit workflow/tasks, REST APIs, versioned masters/rules, transactional outbox/inbox, structured logging, consistent exception/error handling, and adapters to Security, DI, Observability and approved external providers.

The canonical hierarchy is:

```text
PROJECT (= VERIGENCE SECURITY TENANT)
  -> DEALER
      -> DEALER OUTLET / LOCATION
          -> CUSTOMER
              -> CUSTOMER / AUDIT JOURNEY
                  -> Booking
                  -> Commercials / Discounts
                  -> Payments / Finance
                  -> Insurance / VAS
                  -> Trade-In
                  -> Vehicle / Registration
                  -> Delivery
                  -> Evidence / Documents
                  -> Audit Controls / Findings
                  -> Review / CRM / Escalation
                  -> Durable Audit Workflow Tasks
```

One Security Tenant represents one Audit Project. Booking initiates the journey but is not the parent aggregate of all later process areas.

### 1.1 Audit-only operating principle

Audit Core **observes, records, validates, compares, flags and reports**. It SHALL NOT stop, block, cancel, approve, reject or otherwise control the dealership's actual sale/delivery process.

Audit Core may create findings, observations, audit tasks, CRM tasks and escalations for Verigence users. These are **audit actions**, not dealer-business control actions.

The system therefore keeps two independent concepts:

1. **Actual/observed business status** — what happened in the customer's sale/delivery journey according to evidence, source system or legitimate operational input.
2. **Audit state/outcome** — where Verigence is in auditing that journey and what the audit concluded.

No Audit Core public API shall expose a command whose purpose is to prevent the dealer from completing delivery or another business transaction.

---

## 2. Source/process scope

The supplied process workbook contains 104 numbered activities across eight process areas: Booking Capture & Classification; Delivery Readiness & Execution; Payment Verification; Insurance & Accessories Compliance; Daily Audit Operations/EOD; Trade-In Lifecycle; Escalation & CRM Follow-up; System Validation & Analytics.

The supplied process also contains Daily PC/TL Activity Tracker and PC Daily Activity Notepad requirements. The existing-tool workbook contributes fields/checklists for pricing, customer/SC, model/variant/colour, VIN/DMS, registration, Standard-vs-Actual commercials, discounts, delivery documents, payments, DO/finance, observations and review remarks.

The operational audit process starts when the dealership Sales Executive/Sales Consultant hands the booking file to the Process Consultant. Dealer staff remain business participants/reference data in the current scope.

---

## 3. Core design principles

1. **Project = Tenant.** Security `tenant_id` is the Audit Project boundary.
2. **Journey-centric domain.** Dealer Outlet -> Customer -> Journey is the core hierarchy.
3. **Audit, do not control.** Audit Core never blocks the dealer's business process.
4. **Observed business status != audit state.** Delivery/business status and audit progress/outcome are orthogonal.
5. **Evidence first.** Audit users should not re-key facts already available from documents/screenshots/upstream evidence.
6. **PC capture != formal verification.** PC captures/records/submits; TL/PM verify/validate according to approved policy.
7. **DI is internal-only.** No Web/Mobile/user-facing client calls DI directly. All DI functionality is mediated by Audit Core.
8. **DI remains generic.** Audit business rules, requirement logic and reconciliation remain in Audit Core.
9. **Versioned reproducibility.** Published decision-relevant masters are immutable; journeys/evaluations retain exact versions/snapshots.
10. **Durable audit work.** Once committed, audit tasks cannot silently disappear through crash/restart/deployment/replay.
11. **Security is authorization authority.** Audit Core consumes effective Security permissions and never stores credentials.
12. **Executive super privilege, no delete.** Executive has tenant-wide Audit Core capability except destructive delete/purge operations.
13. **Loose coupling.** No reads of Security/DI private databases and no cross-module database foreign keys.
14. **Consistent errors and telemetry.** Failures follow a stable error catalogue and correlation model without leaking sensitive data.
15. **Transactional events.** Audit state + directly resulting task + outbox event commit atomically where one command causes them.

---

## 4. Platform and interaction boundary

```text
                         Web / Mobile
                              |
                       Audit Core APIs only
                              |
                              v
                   +---------------------+
                   |     AUDIT CORE      |
                   | business hierarchy  |
                   | journey/audit logic |
                   | durable audit work  |
                   | DI orchestration    |
                   | API/error boundary  |
                   +----+-----------+----+
                        |           |
              internal |           | telemetry
                        v           v
                  +-----------+  +----------------+
                  |    DI     |  | OBSERVABILITY  |
                  | generic   |  | logs/metrics/  |
                  | document  |  | traces/analytics|
                  | intelligence| +----------------+
                  +-----------+

Security -> JWT/JWKS/permissions -> Audit Core
```

### 4.1 User-facing boundary

Web/Mobile SHALL invoke Audit Core only for journeys, evidence upload/view, extracted facts, audit tasks, findings, reviews and analytics.

No DI route or DI permission is part of the user-facing application contract. Audit Core may retain DI identifiers internally but public APIs expose Audit Core `evidenceId` values.

### 4.2 Authority matrix

| Concern | Authority |
|---|---|
| Identity/authentication/session/device/access | Security |
| Effective permissions | Security |
| Project boundary | Security Tenant + Audit Core Project projection |
| Dealer/Outlet/Customer/Journey | Audit Core |
| Actual observed delivery/business status | Audit Core record, sourced from evidence/operational facts |
| Audit state/tasks/findings/review outcome | Audit Core |
| Versioned project masters | Audit Core |
| Raw document/image content | DI |
| Generic document processing/extraction/quality | DI |
| Business evidence requirements/journey association | Audit Core |
| Business reconciliation/audit conclusion | Audit Core |
| Operational telemetry | Observability |

---

## 5. Domain hierarchy

### 5.1 Project

One Audit Core Project is a 1:1 business projection of one Security Tenant. `tenant_id` is the authorization boundary and Project identity anchor. Project configures OEM/product context, operating dates/timezone, Dealer participation, Satellite policy, applicable master versions and project-team coverage.

### 5.2 Dealer and Outlet

A Project contains one or more Dealers; each Dealer contains one or more Outlets. Outlet classification supports `ONSITE` and `SATELLITE`; threshold and approval mechanics remain configurable/open until approved.

Outlet may carry an optional Security Location reference for geo/schedule enforcement, but Outlet and Security Location are not assumed identical.

### 5.3 Customer

Customer is a business record in Dealer Outlet context and anchors one or more audited journeys according to the final repeat-customer policy. Duplicate/match detection must operate project-wide across Dealers/Outlets using protected normalized match keys and evidence-derived facts.

### 5.4 Customer/Audit Journey

Journey correlates the end-to-end audit of one customer vehicle-sale journey. Booking, payments, insurance, trade-in, vehicle/registration and delivery are peer process entities linked to the same Journey.

Audit Core SHALL NOT treat Journey as a dealership workflow engine. Actual business fields are observed facts. Audit work is tracked separately.

---

## 6. State model — actual business status versus audit state

### 6.1 Rule

Audit Core shall never conflate dealership/customer business status with Verigence audit progress.

### 6.2 Actual/observed delivery status

The system records what actually occurred for the customer journey from operational input and/or evidence. It SHALL retain at minimum:

- planned delivery date/time where known;
- delivery intimation date/time where known;
- **actual delivery status code/value**;
- actual delivery date/time where known;
- VIN/chassis and relevant invoice/gate-pass/registration references where available;
- source/provenance of material delivery facts.

Because the canonical dealer delivery-status vocabulary has not yet been approved, this design does **not invent fixed delivery enum values**. `actual_delivery_status_code` references an approved/configured code set.

Audit Core may record that delivery occurred despite an audit exception. It may not change or block that delivery.

### 6.3 Audit state

Audit state describes Verigence work only. Based on the supplied PC/TL process, baseline semantics are:

```text
NOT_STARTED
  -> IN_PROGRESS
  -> PC_SUBMITTED
  -> TL_REVIEW
      -> SENT_BACK -> IN_PROGRESS
      -> REVIEW_COMPLETE
```

Where approved policy requires PM review, `PM_REVIEW` may be used after TL review or for exceptions. Exact PM gate remains open.

### 6.4 Audit outcome

Audit outcome is separate:

- `PENDING`
- `NO_BREACH`
- `BREACH`

`SENT_BACK` is never an audit outcome.

Findings/observations are audit records. Their disposition never implies Audit Core changed the dealer transaction.

---

## 7. Master-data architecture and versioning

Material decision configuration uses immutable published versions:

```text
DRAFT -> PUBLISHED -> RETIRED
```

A published version is never edited in place; corrections create a new version.

Versioned domains include at minimum price lists/component values, discount schemes/eligibility/benefits, document requirement profiles, audit control/rule versions, decision-relevant thresholds, and registration/insurance/VAS configuration where used in audit decisions.

Journeys/evaluations retain exact version references or snapshots used to reproduce an audit conclusion.

`Standard` values come from effective published configuration. `Actual` values come from evidence/source/legitimate operational facts with provenance.

---

## 8. DI integration — Audit Core is the sole user-facing façade

### 8.1 Boundary rule

No DI capability is exposed directly to a user or user-facing client. Web/Mobile invokes Audit Core; Audit Core invokes DI internally.

### 8.2 Business logic stays in Audit Core

Audit Core owns:

- which evidence is required;
- business purpose/stage of evidence;
- who may upload/view/use evidence;
- mapping extracted facts to business facts;
- price/discount/payment/delivery reconciliation;
- findings/breach outcome;
- CRM/escalation triggers;
- durable audit work routing.

DI remains reusable generic document intelligence and SHALL NOT own dealership/customer/journey business logic.

### 8.3 Upload flow

```text
1. Client -> Audit Core: upload evidence for Journey/requirement/purpose
2. Audit Core: authorize Tenant + business scope + action
3. Audit Core -> DI: ensure/resolve internal Subject and stream/forward document
4. DI -> Audit Core: document ID + generic processing status
5. Audit Core: persist normalized evidence link using Audit Core evidence ID
6. Audit Core -> Client: Audit Core evidence response only
7. Audit Core -> DI: refresh processing/extracted facts internally as required
8. Audit Core: project relevant facts with provenance and execute business audit controls
```

Audit Core SHOULD stream/forward file content to DI without persisting raw binaries in Audit Core storage.

### 8.4 Internal identifiers

Audit Core may store `di_subject_id` and `di_document_id` internally. Public APIs expose `evidenceId` and business metadata rather than requiring clients to understand DI identities.

If policy requires generic DI document verification, Audit Core invokes it internally after Audit Core authorization. PC does not automatically receive verification authority.

---

## 9. Durable audit workflow

Audit workflow coordinates Verigence audit work only; it does not orchestrate dealer business activity.

Persistent components include `workflow_instance`, `workflow_task`, append-only `workflow_task_event`, `workflow_task_attempt`, idempotency/effect-key store, transactional outbox, and inbox/deduplication for future inbound events.

When a command directly causes a task:

```text
BEGIN
  update audit state / append review decision
  create/transition durable task
  append task event
  append authoritative audit event
  insert outbox event
COMMIT
```

All or none are persisted.

The design shall handle service restart/deployment, worker crash, duplicate mobile/API retry, duplicate event delivery, temporary dependency failure, scheduler restart and stale in-progress worker tasks. Worker tasks use persisted lease/heartbeat and stale-lease recovery. Retry state is persisted; exhausted work moves to a visible dead-letter/failed state and is never silently deleted.

Human tasks use optimistic concurrency/version checks. Completion/cancellation requires actor/time and, when cancelled, a reason. Task history is immutable.

---

## 10. Security and role model

Audit Core verifies Security-issued JWTs via Security JWKS and authorizes using effective `permissions[]`. Token Tenant must match the Audit Project/Tenant context.

| Role | Baseline intent |
|---|---|
| PC | receive handoff, capture/upload evidence through Audit Core, record permitted operational/audit facts, field checks, remarks, daily operations, submit audit work |
| TL | review/verify configured PC work, Breach/No Breach/Send Back, team activity review |
| PM/PMO | project-level oversight, configured exception verification/validation, finding/escalation management, management review |
| CRM | execute durable CRM tasks and record outcomes |
| Executive | **tenant-wide super privileges across Audit Core**, including read/oversight/admin/operational actions, **except delete/purge/destructive-delete privileges** |

### 10.1 Executive no-delete rule

For the current baseline:

- Executive receives no `delete`, `purge`, hard-delete or irreversible destructive permission;
- baseline user-facing API contains no HTTP DELETE operations;
- corrections use auditable `retire`, `inactivate`, `supersede`, `void` or task `cancel` semantics where appropriate;
- future destructive-delete capability requires explicit owner approval and separate permission/retention design.

### 10.2 PC verification rule

PC baseline permissions SHALL NOT include final `audit.*.verify` or DI verification-write capabilities merely because PC captured evidence. Formal validation/verification belongs to TL/PM according to approved process policy.

---

## 11. Explicit API contract

The user-facing contract is defined in:

- `docs/AUDIT_CORE_API_CONTRACT_v1.0.md`
- `api/openapi-v1.yaml`

Contract invariants:

- base path `/v1/tenants/{tenantId}/...`;
- clients call Audit Core only, never DI;
- no business `block/approve/stop/cancel delivery` commands;
- actual delivery status is recorded as an observed fact;
- audit-state commands affect audit work only;
- no baseline public DELETE operations;
- retryable create/upload/command endpoints support or require `Idempotency-Key`;
- material mutable resources use optimistic concurrency;
- `X-Correlation-ID` is accepted/generated and returned;
- errors use `application/problem+json` with stable Audit Core `errorCode`.

---

## 12. Exception and error-handling architecture

Domain/application code raises typed Audit Core exceptions rather than returning raw transport/database/provider errors.

Categories include authentication/authorization, business-scope denial, validation, not-found, concurrency/state conflict, master/configuration conflict, workflow/task failure, DI/dependency failure, idempotency conflict and unexpected/system failure.

A centralized API exception mapper translates typed exceptions to HTTP status + stable error code + safe title/detail + correlation ID + optional field/retry metadata.

Raw stack traces, DB errors, tokens, raw documents, PAN/Aadhaar values and provider payloads SHALL NOT be returned to clients.

DI/Security/provider failures are wrapped by adapters and translated into Audit Core errors. Retryable dependency failures are retried only where idempotency and policy permit; durable background retries persist state.

The canonical catalogue is `VAC-ERR-001 v1.0`.

---

## 13. Logging and Observability

Audit Core emits structured logs, metrics and traces compatible with the `verigence-observability` baseline.

Required safe context where applicable:

- UTC timestamp, level, service/module, environment;
- correlation ID, request/trace/span IDs;
- tenant/project, Dealer/Outlet, Customer/Journey IDs;
- workflow/task ID;
- authenticated actor ID/type;
- operation/action/result;
- stable error code;
- latency and dependency name for external calls.

Never log access/refresh tokens, OTP/password/secrets, raw PAN/Aadhaar/full sensitive identifiers, raw document/image content, extracted sensitive payload dumps or unredacted provider responses containing customer data.

Authoritative audit history is stored in Audit Core. Operational logs are not the audit record.

---

## 14. Error catalogue

`docs/AUDIT_CORE_ERROR_CATALOG_v1.0.md` defines the stable public families:

- `VAC-AUTH-*` — authentication/authorization/tenant/business scope;
- `VAC-VAL-*` — request/business validation;
- `VAC-NF-*` — not found;
- `VAC-CONFLICT-*` — state/version/idempotency conflict;
- `VAC-WF-*` — workflow/task;
- `VAC-DI-*` — internal DI integration;
- `VAC-MASTER-*` — master/version/configuration;
- `VAC-SYS-*` — system/dependency/rate protection.

Every public failure maps to one catalogue entry and carries `correlationId`.

---

## 15. Data-model impact

The next physical schema (`VAC-DB-002`) must reflect:

- Project keyed 1:1 by Tenant;
- Dealer -> Outlet -> Customer -> Journey hierarchy;
- separate actual delivery/business status from audit state/outcome;
- no business-control state implying Audit Core can stop/cancel delivery;
- normalized evidence links with internal DI IDs hidden from public contract;
- durable workflow/retry/lease/history structures;
- structured authoritative audit history;
- idempotency/outbox/inbox;
- immutable published master versions;
- no dependency on destructive user-facing delete lifecycle.

The previous `VAC-DB-001 v1.0` remains on implementation hold and SHALL NOT be deployed unchanged.

---

## 16. Open decisions not guessed

1. Satellite monthly-volume threshold and auto/manual classification policy.
2. PM versus PMO terminology/role distinction.
3. Exact normal-path versus exception-path TL/PM verification gates.
4. Per-car Total Discount / Above Scheme formula.
5. PO/DO/Refund realised-payment logic.
6. Insurance Calculator integration method/OEM-specific rules.
7. Trade-in ageing/resale threshold (60 vs 90 days in source material).
8. Dedicated trade-in Sales field/business meaning where source material is ambiguous.
9. Deal-level Short/Excess formula/label.
10. Notification provider/channel.
11. Repeat-customer reuse/link policy.
12. Dealer Outlet <-> Security Location cardinality.
13. Canonical actual dealership delivery-status vocabulary/code set.

---

## 17. Changes from v2.0

1. Audit Core is explicitly **audit-only/observational** and cannot control dealer operations.
2. Actual delivery/business status is separated from audit state/outcome.
3. Direct Client -> DI interaction is removed; Audit Core is the sole user-facing DI façade.
4. DI remains generic; Audit business logic stays in Audit Core.
5. Structured logging, typed exceptions, centralized error handling and a formal error catalogue are introduced.
6. Executive becomes tenant-wide super privileged except delete/purge/destructive operations.
7. DELETE operations are excluded from the baseline public API.
8. A human-readable API contract and machine-readable OpenAPI contract are introduced.
