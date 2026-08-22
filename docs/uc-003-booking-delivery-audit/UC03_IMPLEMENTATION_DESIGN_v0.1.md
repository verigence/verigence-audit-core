# Verigence UC03 — Booking & Delivery Audit — Implementation Design

**Document ID:** `VUC03-ID-001`  
**Version:** `0.1`  
**Status:** DRAFT FOR IMPLEMENTATION REVIEW / NO CODE AUTHORIZATION  
**Date:** 2026-08-22  
**Canonical business design:** `VUC03-SD-002 / UC03_SOLUTION_DESIGN_v1.1.md`  
**Workflow contract:** `VUC03-WF-002 / UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`  
**Rules:** `VUC03-RF-001 / UC03_RULE_FLAG_CATALOG_v1.0.md`  
**Field/document scope:** `VUC03-FM-001 / UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`  
**Reconciliation:** `VUC03-DR-001 / UC03_RECONCILIATION_DECISIONS_v1.0.md`  
**UX amendment:** `verigence-web / UC03_UX_REVIEW_NOTES_v0.2.md`

---

## 0. Purpose

This document translates the approved UC03 planning direction into a concrete cross-module implementation blueprint for:

- Audit Core;
- DI;
- Web / Android Capacitor;
- Security only if a concrete missing authorization/context capability is demonstrated during implementation.

It does **not** authorize production implementation yet. Its purpose is to establish the target persistence model, Project-context flow, API/read models, Workflow Manager services, rule/flag integration, DI contracts, Web/Android component boundaries, migration order and test strategy before implementation branches are opened.

The non-negotiable business invariant remains:

> **Audit Core records the dealer's real Booking/Delivery progression. Audit incompleteness or non-compliance may raise/escalate flags and keep audit work In Progress, but may not reject or roll back a real Delivery Start or Delivery Completed event solely because the dealer process is non-compliant.**

---

## 1. Frozen source baselines

| Module | Repository | Planning baseline | Planning branch |
|---|---|---|---|
| Audit Core | `verigence-audit-core` | `dev@082cc2ada5cd934bf0707ccae945667feb3f6e37` | `planning/uc-003-booking-delivery-audit` |
| DI | `verigence-di` | `dev@c97b3f3e5f8577160c88af1080496808189206fb` | `planning/uc-003-booking-delivery-audit` |
| Web/Android | `verigence-web` | `dev@2c98f753ed1428c0d5f7a0b7144169d528a5bb78` | `planning/uc-003-booking-delivery-audit` |
| Security | `verigence-security` | current `dev` Security v2 source of truth | no UC03 branch unless a verified gap requires one |

The existing Audit Core physical model already establishes two critical invariants used here:

```text
1 Security Tenant = 1 Audit Project
Project -> Dealer -> Outlet -> Customer -> Journey
```

The existing schema also contains `business_assignments`, keyed by `tenant_id`, `security_actor_id`, `business_role_code`, Dealer/Outlet scope and effective dates. UC03 will reuse that as the operational Project/routing projection rather than creating another Project-membership table.

---

## 2. Cross-module runtime boundary

```text
Android / Web
      |
      | Security human access token
      v
AUDIT CORE  <---------------------- SECURITY
      |                              live human authorization
      |
      +---- Workflow Manager
      +---- Project/context read model
      +---- Booking / Delivery domains
      +---- Document requirements
      +---- Rule Engine
      +---- Audit Flags
      +---- UC03 aggregate read models
      |
      | internal ServiceIntegration boundary
      v
DI
      |
      +---- evidence processing
      +---- classification
      +---- extraction
      +---- confidence/provenance
```

Web/Android never calls DI directly and never becomes authoritative for workflow state or rule outcomes.

---

# PART A — OPERATIONAL PROJECT CONTEXT

## 3. Project selection requirement

PC, TL and PM may be assigned to more than one Project and may hold a different operating role per Project.

Because Project is currently represented by `tenant_id`, Project context is selected **after global login** and before the operational landing page.

The Security human login remains global. UC03 shall not add `tenantId` to login.

### 3.1 Existing endpoint that must NOT be reused

Current:

```text
GET /v1/projects
```

is a SuperAdmin Project Administration list/resume endpoint. It shall remain an admin/control-plane endpoint.

UC03 must not broaden it to PC/TL/PM.

### 3.2 New operational endpoint

Proposed:

```http
GET /v1/me/projects
```

Purpose:

> Return active Audit Projects in which the authenticated human has a current operational business assignment, with the role and business-scope summary required to select context.

Initial response:

```json
{
  "projects": [
    {
      "tenantId": "...",
      "projectCode": "HYD-PB",
      "projectName": "Hyundai Punjab Audit",
      "projectStatus": "ACTIVE",
      "timezoneName": "Asia/Kolkata",
      "operatingRole": "PC",
      "scope": {
        "allDealers": false,
        "dealerCount": 2,
        "outletCount": 5
      }
    }
  ]
}
```

Only user-safe Project data is returned. Internal assignment IDs are unnecessary.

### 3.3 Source for `/me/projects`

Preferred Phase-1 source:

```text
Security human actor identity
    +
Audit Core active business_assignments
    +
Audit Core projects
```

Eligibility conceptually requires:

```text
business_assignments.security_actor_id = authenticated actor
assignment_status = ACTIVE
current time within effective range
project_status = ACTIVE
```

`business_assignments` is an operational routing projection, **not** a replacement for Security authorization. Every later tenant-scoped operation still performs the normal synchronous Security permission check.

### 3.4 Security gap rule

Do **not** modify Security merely to make Project selection convenient.

During implementation, verify that UC02 Project role mapping keeps `business_assignments` synchronized with the authoritative Security Tenant operating-role assignment.

If that invariant is already reliable, no Security change is required.

Only if implementation proves that Audit Core cannot reliably derive current Project membership/role shall UC03 raise a concrete Security gap for a narrow current-user Tenant-assignment read capability. No Clerk/authentication redesign is permitted.

### 3.5 Client Project-selection behavior

```text
0 available Projects
  -> user-safe no-assignment screen

1 available Project
  -> auto-select
  -> go directly to Project landing

>1 available Projects
  -> Choose Project screen
  -> user selects
  -> Project landing
```

For multi-Project users, Web/Android exposes `Switch Project` later without sign-out.

### 3.6 Web session/context redesign

Current Web state has one global `role` plus `tenantId/dealerId/outletId`. That is insufficient when a human is PC in one Project and TL/PM in another.

Target separation:

```text
AuthenticatedIdentityContext
  accessToken
  userId / displayName
  global classification where needed

SelectedProjectContext
  tenantId
  projectCode
  projectName
  timezoneName
  operatingRole
  dealerIds / outletIds or scope summary
```

Changing Project shall clear Dealer/Outlet local selections and invalidate all tenant-scoped React Query caches.

The selected operating role drives operational navigation/presentation, but server authorization remains authoritative.

---

# PART B — PROJECT LANDING / WORK LIST

## 4. UC03 Project landing

The existing generic Dashboard/Recent Journeys model is not the target PC/TL/PM UC03 landing.

The UC03 landing must use business terminology and selected Project context.

Primary metrics direction:

```text
Bookings In Progress
Delivery In Progress
Needs Attention
Audit Flags
```

There is no `Delivery Today` metric.

### 4.1 Work-list endpoint

Proposed:

```http
GET /v1/tenants/{tenantId}/uc03/work-items
```

Query parameters:

```text
workType     ALL | BOOKING | DELIVERY          default ALL
fromDate     YYYY-MM-DD                        optional
toDate       YYYY-MM-DD                        optional
limit        fixed/default 10, maximum 10
cursor       opaque                            optional
```

The server resolves Project timezone before evaluating business dates.

### 4.2 Default query

With no date filters:

```text
latest 10 authorized cases
ORDER BY latest_activity_at_utc DESC, journey_id DESC
```

The list is scope-filtered for the current user according to the selected Project role/business assignment plus Security authorization.

### 4.3 Date semantics

Working Phase-1 contract:

- `BOOKING`: range matches the configured Booking business date/event date;
- `DELIVERY`: range matches the relevant Delivery business date/event date;
- `ALL`: include a case once if relevant Booking or Delivery business activity falls in range.

The API never returns duplicate rows for the same `journey_id` merely because both stages match.

Exact source-date precedence (source Booking date vs accepted event date, etc.) must be frozen in API contract testing before implementation completion.

### 4.4 Pagination

Use cursor/keyset paging to avoid duplicate/skipped rows while activity continues.

Cursor encodes at minimum:

```text
latestActivityAtUtc
journeyId
filter fingerprint/version
```

Response:

```json
{
  "items": [],
  "pageSize": 10,
  "nextCursor": "...",
  "previousCursor": null,
  "filters": {
    "workType": "ALL",
    "fromDate": null,
    "toDate": null,
    "timezoneName": "Asia/Kolkata"
  }
}
```

UI presents simple Previous/Next controls.

### 4.5 Work-item projection

Each item should contain enough data to render phone/tablet/Web without N+1 calls:

```text
journeyId                     internal navigation key
bookingReference
customerDisplayName
customerMobileLast4 optional
productLabel
projectName optional
 dealerId / outletId internal
 dealerName / outletName user-facing

booking.businessStatus
booking.auditState
booking.auditStatus
booking.businessDate

delivery.businessStatus nullable
delivery.auditState
delivery.auditStatus
delivery.businessDate nullable

openFlagCount
totalFlagCount
highestOpenSeverity nullable
processingDocumentCount
proposalReadyCount
latestActivityAtUtc
nextActionCode
```

User-facing UI does not display `journeyId`.

### 4.6 `latestActivityAtUtc`

This is a backend projection, not browser guesswork.

It advances for material UC03 activity such as:

```text
workflow event
material field capture
required document upload/assessment
extraction proposal accepted/corrected
flag raised/reviewed/resolved
Delivery progression
```

Pure background polling/read access does not change it.

---

# PART C — WORKFLOW MANAGER PERSISTENCE

## 5. Reuse before new schema

The existing Audit Core already has:

- `projects`;
- `business_assignments`;
- `customers` / `journeys`;
- typed Booking/Commercial/Payment/Finance/Insurance/Trade-In/Vehicle/Registration/Delivery domains;
- evidence + DI mapping/facts;
- versioned document requirement profiles;
- versioned audit controls/evaluations;
- `audit_findings`;
- durable task/history/audit/outbox infrastructure.

UC03 must extend these rather than create parallel authoritative subsystems.

## 6. Proposed schema delta

Exact DDL names may change during migration review, but the semantic model is frozen.

### 6.1 `journey_stage_states`

One row per Journey/stage projection.

```text
tenant_id
journey_id
stage_code                   BOOKING | DELIVERY | POST_DELIVERY
business_status              nullable for reserved/no-stage status
closure_disposition          Booking only; nullable otherwise
audit_state                  NOT_STARTED | IN_PROGRESS | COMPLETE
audit_status                 NOT_EVALUATED | NO_FLAGS | FLAGS_RAISED
first_started_at_utc
business_completed_at_utc    nullable
capture_completed_at_utc     nullable
latest_activity_at_utc
version_no
created_at_utc
updated_at_utc
```

Unique:

```text
(tenant_id, journey_id, stage_code)
```

Post Delivery row may be absent until future activation; the enum/resolver can reserve it.

### 6.2 `journey_workflow_events`

Append-only immutable event stream:

```text
tenant_id
event_id
journey_id
stage_code
event_type
source_kind                  HUMAN | MACHINE | SOURCE_SYSTEM
actor_id nullable
actor_role_snapshot nullable
idempotency_key nullable
correlation_id nullable
safe_payload jsonb
occurred_at_utc
recorded_at_utc
aggregate_version
```

Append-only mutation protection is required.

Do not store bearer tokens, unmasked Aadhaar, secrets or arbitrary request bodies in `safe_payload`.

### 6.3 Booking close record

Prefer stage/event payload plus normalized closure columns rather than a second Booking-closure subsystem.

Required durable facts:

```text
close_reason_code
closure_disposition          PROCEED_TO_DELIVERY | NO_DELIVERY
remarks
closed_by_actor_id
closed_at_utc
```

Reason catalogue is configuration-driven.

### 6.4 `journey_document_assessments`

One current versioned assessment per applicable requirement/stage plus history/versioning direction:

```text
tenant_id
journey_id
stage_code
requirement_key
profile_version_id
applicability_state
applicability_reason
answer                       YES | NO | NA | UNANSWERED
evidence_id nullable
remarks nullable
answered_by_actor_id nullable
answered_by_role nullable
answered_at_utc nullable
version_no
created_at_utc
updated_at_utc
```

Requirement applicability is recalculated when driving facts change. An applicability-change workflow/audit event records why.

### 6.5 Extraction proposal acceptance

Do **not** create a generic authoritative 123-field table.

DI facts remain provenance-bearing machine facts. Audit Core adds a proposal/acceptance layer only where needed to distinguish:

```text
machine proposal
accepted value
human correction
owning business-domain persistence
```

Working table/service direction:

```text
capture_proposal_id
journey_id
stage_code
field_key
source_evidence_id
source_evidence_fact_id
proposed_value / normalized value
confidence
proposal_status              PENDING | ACCEPTED | CORRECTED | REJECTED | SUPERSEDED
accepted_value nullable
accepted_by_actor_id nullable
accepted_at_utc nullable
owning_domain_key
owning_record_reference nullable
version_no
```

On accept/correct, the owning typed domain is updated transactionally or through a controlled application service. The proposal row is provenance, not the business source of truth.

### 6.6 Extend `audit_findings`

Reuse existing Journey-scoped finding table.

Add/ensure fields required by UC03:

```text
stage_code
origin_kind                  MACHINE | HUMAN
origin_actor_id nullable
origin_role_snapshot nullable
rule_key nullable
rule_version_id nullable
blocking_completion boolean/default false or equivalent effect-policy reference
correlation_id
```

If existing `audit_evaluation_id` / control version linkage already provides rule version, avoid redundant storage unless human/manual origin requires it.

### 6.7 `audit_finding_events`

Append-only flag history:

```text
RAISED
REMARK_ADDED
EVIDENCE_ADDED
ACKNOWLEDGED
RESOLVED
REOPENED
VOIDED
RECLASSIFIED
```

Persist actor/role/time/reason and safe payload.

### 6.8 Rule versions

Extend current `audit_controls` / `audit_control_versions` rather than create a second UC03 rules database.

Required evaluator/effect configuration includes:

```text
stage_code
trigger_family
applicability expression/config
input fact/document keys
evaluator_key
rule parameters
severity default
flag type/message template
effect policy
review policy
completion policy influence
```

Allowed effect policy explicitly excludes business-event blocking:

```text
VALIDATION_ONLY
FLAG_ONLY
BOOKING_COMPLETION_GUARD
AUDIT_COMPLETION_GUARD
ESCALATE_FLAG
```

There is no `BLOCK_DELIVERY` effect.

---

# PART D — WORKFLOW MANAGER SERVICES / COMMAND API

## 7. Command service invariant

All business progression commands execute conceptually as:

```text
authenticate human
authorize requested tenant/action with Security
validate current aggregate version / sequence
validate semantic idempotency
persist real event
update stage projection
run progression/checkpoint rules
raise/update flags
append audit/outbox records
commit atomically
return refreshed aggregate snapshot
```

For real Delivery progression, audit-rule failure occurs after/alongside durable event acceptance and cannot roll it back.

## 8. Proposed UC03 command endpoints

Exact paths will be frozen in OpenAPI before coding.

### Booking

```http
POST /v1/tenants/{tenantId}/journeys/{journeyId}/booking/start
POST /v1/tenants/{tenantId}/journeys/{journeyId}/booking/close-ready
POST /v1/tenants/{tenantId}/journeys/{journeyId}/booking/close-no-delivery
POST /v1/tenants/{tenantId}/journeys/{journeyId}/booking/cancel
POST /v1/tenants/{tenantId}/journeys/{journeyId}/booking/mark-duplicate
```

All state-changing commands require `Idempotency-Key`; optimistic concurrency uses `If-Match` / expected aggregate version.

### Delivery

```http
POST /v1/tenants/{tenantId}/journeys/{journeyId}/delivery/start
POST /v1/tenants/{tenantId}/journeys/{journeyId}/delivery/complete
```

There is no Delivery Close endpoint.

### Document assessment

```http
PUT /v1/tenants/{tenantId}/journeys/{journeyId}/stages/{stage}/documents/{requirementKey}
```

Payload supports allowed `YES/NO/NA`, evidence reference and remarks.

### Capture values / extraction proposals

```http
PUT  /v1/tenants/{tenantId}/journeys/{journeyId}/capture/{fieldKey}
POST /v1/tenants/{tenantId}/journeys/{journeyId}/extraction-proposals/{proposalId}/accept
POST /v1/tenants/{tenantId}/journeys/{journeyId}/extraction-proposals/{proposalId}/correct
```

Where typed-domain-specific endpoints already exist and are clearer, UC03 service can route to them instead of exposing one universal generic write API. The browser must not become the orchestration authority.

### Flags

```http
GET  /v1/tenants/{tenantId}/journeys/{journeyId}/flags
POST /v1/tenants/{tenantId}/journeys/{journeyId}/flags
POST /v1/tenants/{tenantId}/journeys/{journeyId}/flags/{flagId}/remarks
POST /v1/tenants/{tenantId}/journeys/{journeyId}/flags/{flagId}/acknowledge
POST /v1/tenants/{tenantId}/journeys/{journeyId}/flags/{flagId}/resolve
POST /v1/tenants/{tenantId}/journeys/{journeyId}/flags/{flagId}/reopen
POST /v1/tenants/{tenantId}/journeys/{journeyId}/flags/{flagId}/void
```

The server authorizes actions from configurable permission policy. React does not hard-code TL/PM/Executive authority as security enforcement.

---

# PART E — AGGREGATE UC03 READ MODEL

## 9. Case workspace endpoint

Proposed:

```http
GET /v1/tenants/{tenantId}/journeys/{journeyId}/uc03-workspace
```

The client should not assemble the operational screen from 15 independent calls.

Response groups:

```text
caseHeader
projectContext
bookingStage
  business status
  Audit State / Audit Status
  fields/read model
  applicable document requirements
  extraction summary/proposals
  flags summary
  completion/checkpoint summary

deliveryStage
  same concepts + photos/payments/intimation

processingSummary
flagSummary
permittedActions
historySummary
aggregateVersion
```

`permittedActions` may improve UX but never substitutes for server enforcement.

### 9.1 Extraction processing endpoint

Option A: include processing snapshot in workspace and poll workspace with ETag.

Preferred initial optimization:

```http
GET /v1/tenants/{tenantId}/journeys/{journeyId}/processing-status
```

Cheap response:

```text
version
pendingCount
readyProposalCount
failedCount
documents[]
  requirementKey
  processingStatus
  elapsed/updated timestamp
  proposalCount
```

Use `ETag` / `If-None-Match` if practical.

Web/Android polling rules:

- poll only while `pendingCount > 0`;
- stop when zero;
- pause when app/page backgrounded;
- immediate refresh on resume/focus/reconnect;
- extraction failures are document-local;
- never expose DI/backend names in UI.

---

# PART F — DI IMPLEMENTATION DELTA

## 10. DI responsibilities

DI changes remain controlled and configuration/extraction focused.

Required UC03 work:

1. reconcile provisional document requirements with DI document types/profiles;
2. configure supported extraction profiles for the 57 extracted source fields;
3. preserve source-document-specific facts when multiple documents can produce the same field;
4. expose processing state/confidence/provenance through existing Audit Core integration;
5. support explicit retry/new-evidence processing without overwriting prior machine facts;
6. preserve original machine value after human correction;
7. provide vehicle identifier facts where available, but never decide VIN business match;
8. do not add Aadhaar extraction/raw-retention by assumption.

`UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md` classifies mappings as `SUPPORTED`, `PROVISIONAL`, or `TBD`. Only reconciled mappings may become published production profiles.

### 10.1 Requirement/evidence linkage

Audit Core evidence association must retain:

```text
journey_id
stage_code
requirement_key
evidence_id
```

DI may receive document type/profile context internally, but UI uses business requirement names.

---

# PART G — WEB / ANDROID IMPLEMENTATION DELTA

## 11. Brand asset

The implementation SHALL use the existing approved bundled asset:

```text
src/assets/verigenceLockup.ts
```

The static mockup's textual placeholder is never copied into runtime UI.

## 12. Route/context gate

Proposed operational flow:

```text
/login
   |
   v
/project-context        conditional gate
   |
   +-- 0 -> no Project assignment state
   +-- 1 -> auto-select and redirect
   +-- >1 -> Choose Project
   v
/work or /dashboard     selected Project landing
```

A route guard prevents tenant-scoped operational pages from loading without a selected authorized Project context.

### 12.1 Project-switch behavior

On switch:

```text
set selected Project + operatingRole
clear dealerId/outletId local context
clear current case state
invalidate tenant-scoped query cache
navigate to Project landing
```

## 13. Web domain types

Replace/extend current global-role-oriented models with conceptually:

```text
AvailableProject
SelectedProjectContext
Uc03WorkItem
Uc03WorkPage
BookingStageSummary
DeliveryStageSummary
AuditStageSummary
DocumentRequirementView
ExtractionProposalView
AuditFlagView
Uc03Workspace
```

Keep internal `journeyId` in models/routes but user labels are Booking/Delivery references.

## 14. Landing screen

Replace operational PC/TL/PM `Recent Journeys` direction with:

```text
Latest Bookings & Deliveries
```

Top controls:

```text
All | Bookings | Deliveries
Date / date range
```

Maximum 10 rows/cards per page.

Phone uses cards; tablet/Desktop may use denser responsive rows but must show the same information.

## 15. UC03 components

Reusable component direction:

```text
ProjectContextGate
ProjectChooser
ProjectSwitcher
Uc03WorkFilters
Uc03WorkItemCard
StageStatusSummary
DocumentRequirementCard
DocumentProcessingState
ExtractionProposalCard
ExtractionProposalGroup
BookingConclusionSheet
DeliveryProgressionNotice
VehiclePhotoCaptureCard
PaymentReceiptCard
AuditFlagCard
AuditFlagComposer
AuditFlagReviewPanel
Uc03HistoryTimeline
Uc03StickyActionBar
```

Desktop and Android use the same business components/layout primitives rather than separate apps.

## 16. Android-specific behavior

Existing Capacitor/native foundation is reused.

UC03 Android requirements:

- Camera integration for vehicle/photo evidence;
- native back behavior must close sheets/drawers before navigating away;
- keyboard resize for long forms/remarks;
- safe-area/status-bar behavior retained;
- 44–48 px minimum touch targets;
- background/resume restarts processing refresh safely;
- transient network loss never fabricates completion;
- no silent upload duplication on retry; use idempotency/client upload keys.

No iOS implementation is in UC03 scope.

---

# PART H — SECURITY / PERMISSIONS

## 17. Security impact

Expected Security impact is minimal.

Security remains responsible for:

- global human authentication;
- global USER lifecycle;
- Tenant operating role/permission authority;
- live tenant-scoped authorization checks;
- machine ServiceIntegration authentication.

UC03 shall not change Clerk integration, token identity model or login semantics.

### 17.1 New/confirmed permission keys

Implementation review must map UC03 actions to existing permission catalogue first. Only missing functional permissions are added through the established Security module-permission registration path.

Required capability families conceptually include:

```text
uc03.project-context.read
uc03.booking.read / write / conclude
uc03.delivery.read / write / complete
uc03.document.read / assess / upload
uc03.flag.read / raise / remark / review / resolve
uc03.workspace.read
```

Names above are design placeholders until reconciled with current Audit Core permission naming conventions. Do not create duplicate permissions when existing `audit.*` keys already express the capability.

### 17.2 Role policy

Phase-1 business default:

```text
Raise flag: PC / TL / PM / Executive
Review: TL / PM / Executive
Resolve: TL / PM / Executive
```

But enforcement must be policy/permission driven, not `if role ===` in Web.

---

# PART I — CONSISTENCY, IDEMPOTENCY AND PRIVACY

## 18. Optimistic concurrency

All state-changing UC03 commands use aggregate/version checks.

If PC and TL operate on stale versions, server returns a safe conflict response; client refreshes and asks the user to reapply only when necessary.

Never silently overwrite newer stage/flag state.

## 19. Idempotency

At minimum:

```text
START_BOOKING
CLOSE_BOOKING_READY_FOR_DELIVERY
CLOSE_BOOKING_NO_DELIVERY
CANCEL_BOOKING
MARK_DUPLICATE_BOOKING
START_DELIVERY
COMPLETE_DELIVERY
DOCUMENT_UPLOAD
RAISE_FLAG
ACKNOWLEDGE_FLAG
RESOLVE_FLAG
```

Same key + same semantic request returns existing result.

Same key + different request returns conflict.

## 20. Provenance

Every accepted/corrected extracted value must retain:

```text
source evidence
machine fact/value
confidence
accepted/corrected value
actor
role snapshot
timestamp
```

Late evidence after Delivery Completed keeps its true timestamp.

## 21. Sensitive data

- Aadhaar is masked in ordinary UC03 UX;
- no unmasked Aadhaar in generic workflow event payloads/logs;
- no credentials/tokens in audit events;
- UI does not show internal tenant/journey/DI identifiers;
- raw technical/provider errors are mapped to user-safe copy;
- access to identity/evidence remains scope-authorized.

---

# PART J — MIGRATION / BACKFILL DIRECTION

## 22. Migration sequence

No destructive replacement of existing business history.

Proposed rollout:

1. add UC03 stage/workflow/flag-event/document-assessment structures;
2. add required indexes/RLS/triggers;
3. add rule-version/evaluator metadata extensions;
4. add application services and dual-read compatibility where necessary;
5. backfill current Journeys conservatively;
6. switch UC03 read model to new projections;
7. switch Web operational flows;
8. retire legacy UI-only status assumptions only after successful reconciliation.

### 22.1 Existing Journey backfill

For pre-UC03 Journeys:

- derive stage rows only from durable existing facts/status history;
- do not manufacture exact timestamps that are unknown;
- mark migration provenance;
- preserve existing `auditState/auditOutcome` history rather than pretending it was UC03 Audit State/Status;
- unresolved cases can enter `IN_PROGRESS` when source facts support it;
- historical Delivered cases may map to Delivery Completed only where actual delivery evidence/status is reliable.

A migration report must count unmapped/ambiguous records.

---

# PART K — IMPLEMENTATION INCREMENTS

## 23. Proposed implementation branches after approval

Do not create these until this Implementation Design is approved.

```text
verigence-audit-core
work/uc-003-booking-delivery-audit

verigence-di
work/uc-003-booking-delivery-audit

verigence-web
work/uc-003-booking-delivery-audit
```

Security branch only if a verified missing capability exists:

```text
verigence-security
work/uc-003-project-context-authz   [only if required]
```

## 24. Increment I1 — Project context + read models

Audit Core:

- `/me/projects`;
- current-user business assignment projection query;
- UC03 work-list endpoint with latest 10 + filters/cursor;
- tests for role/project isolation.

Web:

- selected Project context store;
- conditional Project chooser;
- Project switcher;
- updated operational landing/read model;
- approved logo asset guaranteed.

No workflow mutations yet.

## 25. Increment I2 — Workflow Manager foundation

Audit Core:

- stage state projection;
- workflow events;
- idempotent command framework;
- optimistic concurrency;
- Booking Start/In Progress/Close/Cancel/Duplicate;
- Delivery Start/In Progress/Completed;
- automatic incomplete-Booking-at-Delivery flag.

Tests include the non-blocking progression invariant.

## 26. Increment I3 — Booking vertical slice

- dynamic Booking document requirements;
- Booking capture ownership;
- DI upload/extraction proposals;
- accept/correct flow;
- Booking rules/flags;
- Booking conclusion UX;
- Android phone/tablet + desktop Booking screens.

## 27. Increment I4 — Delivery vertical slice

- Delivery intimation;
- applicable Delivery documents;
- Android camera/photo evidence;
- payments;
- VIN Rule Engine integration;
- Delivery rules/flags;
- Delivery Completed with audit continuing when necessary;
- Delivery screens across devices.

## 28. Increment I5 — Review / hardening

- TL/PM/Executive flag review/resolution;
- complete event history/timeline;
- migration/backfill verification;
- permission matrix tests;
- load/pagination tests;
- real-device Android UAT;
- desktop/tablet regression;
- business scenario pack.

Post-Delivery reconciliation remains out of scope.

---

# PART L — TEST MATRIX

## 29. Mandatory end-to-end scenarios

At minimum:

```text
01 one Project -> auto-enter landing
02 multiple Projects -> choose Project
03 same user PC in Project A and TL/PM in Project B
04 Project switch clears stale tenant/dealer/outlet data
05 no Project assignment -> safe empty state
06 latest list returns max 10 and stable next page
07 date + Booking filter
08 date + Delivery filter
09 Project timezone date-boundary test
10 create/start Booking
11 extraction still processing while PC enters fields
12 clean extraction bulk accept
13 low confidence proposal corrected
14 unreadable document retry/escalation
15 dynamic corporate/exchange/finance document applicability
16 duplicate Booking -> status + flag
17 normal Booking close ready for Delivery
18 Booking close with no Delivery reason + remarks
19 Delivery starts while Booking remains In Progress -> event accepted + automatic flag
20 missing Delivery document answered No -> flag, no app failure
21 non-intimated Delivery -> reason + flag, continue
22 VIN rule mismatch -> critical flag, real Delivery still recordable
23 payment mismatch/unverified -> flag
24 human PC flag
25 TL review/resolve
26 PM review/resolve
27 Executive full Phase-1 flag action set
28 physical Delivery Completed while Delivery Audit remains In Progress
29 late evidence after Delivery Completed preserves timestamp
30 Audit State COMPLETE + FLAGS_RAISED valid
31 flags resolved but stage Audit Status remains FLAGS_RAISED
32 stale version conflict does not overwrite
33 duplicate idempotency key replay does not duplicate event/flag
34 Android camera evidence joins correct requirement
35 app background/resume during DI processing
36 raw backend error never displayed to user
37 approved Verigence logo renders phone/tablet/Web
```

---

# PART M — PERFORMANCE / OPERABILITY

## 30. Landing/read performance

The default landing is intentionally bounded to 10 cases.

Required indexes should support tenant/business-scope filtering and ordering by `latest_activity_at_utc` without scanning full Journey history.

Target direction:

```text
(tenant_id, latest_activity_at_utc DESC, journey_id DESC)
```

Additional role/dealer/outlet indexes follow actual query plan.

## 31. Processing polling

One case-level processing request per active case; never N polls for N documents.

Backoff/visibility behavior belongs to client hook. Server response should be cache/ETag friendly.

## 32. Observability

Technical telemetry may include:

- command/event type;
- tenant/project identifier in protected logs;
- aggregate version;
- rule key/version;
- DI processing correlation;
- API latency/error class;
- idempotency replay/conflict counts.

User UI must not expose these technical details.

---

# PART N — OPEN ITEMS BEFORE CODE FREEZE

## 33. Must reconcile during implementation review/UAT

1. final business reconciliation of 26-vs-29 document requirements;
2. `TBD` / provisional DI source mappings before production profile publication;
3. exact VIN/chassis normalization/match rule;
4. exact published high/critical flag review requirements for Audit State completion;
5. final date-source precedence for Booking/Delivery filter semantics;
6. final existing-permission vs new-permission mapping;
7. confirm UC02 role-mapping synchronization makes `business_assignments` a reliable operational Project-context projection;
8. exact migration/backfill scope for existing Journeys.

These items must not be silently invented in React or DI configuration.

---

## 34. Definition of implementation-design approval

This document is ready for implementation handoff when reviewers agree that:

- Project selection and multi-Project role context are correct;
- landing/read model satisfies latest-10 + date-filter behavior;
- schema delta reuses existing Audit Core domains rather than duplicating them;
- business progression cannot be blocked by audit non-compliance;
- Rule Engine / Flag model is configurable;
- DI remains document-intelligence only;
- Android-first/Web component boundaries are acceptable;
- Security impact is limited and explicit;
- migration/test order is safe.

After approval, create the final **UC03 Implementation Handoff** with exact implementation branch heads, migration filenames, OpenAPI changes, service/component file plan, execution sequence and Definition of Done.
