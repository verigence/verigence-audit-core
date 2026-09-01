# UC03 Post-Delivery Final Source of Truth — Investigation / Design

Date: 2026-09-01  
Repository: `verigence-audit-core`  
Branch: `investigation/uc03-post-delivery-final-source`  
Baseline: current deployed `dev` after PR #135  
Mode: **INVESTIGATION / DESIGN ONLY — NO SCHEMA OR APPLICATION IMPLEMENTATION IN THIS UNIT**

## 1. Purpose

Define the smallest durable post-Delivery resolution contract that:

1. consumes already-reviewed Booking and Delivery facts from Audit Core;
2. does not use a fresh/live DI read as final business state;
3. records deterministic final-source selection with provenance;
4. provides a reliable gate for the post-Delivery rule run;
5. allows the final report to project from durable Journey state;
6. avoids a speculative generic 152-field business table or speculative repeated Invoice entity.

This document records verified current behavior, proposed design, and remaining blockers. It is not implementation approval.

## 2. Governance boundary

Current stabilization governance requires the sequence:

`Delivery Capture -> Normalize Document Identity -> Persist Delivery Facts -> Compare Legitimate Sources -> Resolve/Persist Final Source of Truth -> Post-Delivery Rule Run -> Final Report`.

The final report is a projection/snapshot of resolved Journey business state. The supplied final-report workbook is the output contract and must be mapped exactly before report implementation. The stabilization plan requires this investigation/design step to identify reuse, required extensions, genuinely new structures (if any), durable final-source persistence, and report consumption before code changes.

## 3. Verified current state

### 3.1 Reviewed Booking and Delivery facts are already durable

Migration `0051_uc03_lossless_review_fields` extends the existing `auditcore.journey_document_extracted_fields` table as the durable reviewed-field store for both `BOOKING` and `DELIVERY`.

It already preserves:

- `extracted_field_id` as a stable row identity;
- stage (`BOOKING` / `DELIVERY`);
- DI document identity;
- canonical-field identity and source fact version;
- source document type and field key;
- original extracted value;
- reviewed `effective_value`;
- confidence with explicit scale;
- modification flag;
- reviewer and review time.

Therefore final resolution does **not** need to re-read DI to obtain the reviewed value.

### 3.2 Delivery Review now commits before `VERIFIED`

`POST /delivery/review/confirm` persists Delivery reviewed fields first and only then advances Delivery `pc_verification_status` to `VERIFIED`.

This establishes a usable boundary for post-Delivery finalization: final-source resolution should consume durable reviewed rows only after Review verification.

### 3.3 Existing cross-source Audit GET is not a durable finalization path

`GET /audit/source-comparison` currently:

- becomes available after Delivery submission, not after Delivery Review verification;
- obtains Booking + Delivery documents/facts through live DI-backed review functions;
- groups facts through `ATTRIBUTE_SPECS`;
- calculates comparison state and a deterministic `resolvedValue` in memory;
- persists no post-Delivery resolution.

It is therefore an evidence/read projection, not final business state.

### 3.4 Current resolver does not encode source stage

`AttributeCandidate` carries document type, document id, field key, confidence and DI lineage, but not `BOOKING` versus `DELIVERY` stage.

`resolve_candidate(...)` selects by:

1. document-type source priority;
2. confidence within equal priority;
3. deterministic document/field tie break.

This is insufficient as the final post-Delivery rule whenever the same legitimate document/source family can occur in both stages. Final rules must not silently imply `Delivery wins`, `latest wins`, or `highest confidence wins` across stages unless that precedence is explicitly approved per attribute.

### 3.5 `journey_attribute_resolutions` is a strong reuse candidate, but currently reference-only

Migration `0037_uc03_attribute_resolution_refs` already created `auditcore.journey_attribute_resolutions` with:

- `stage_code` including reserved `POST_DELIVERY`;
- one resolution per Journey + stage + attribute;
- selected DI document/canonical-field/fact-version provenance;
- resolution rule + mapping version;
- optional typed owning-domain/reference;
- resolved actor/time;
- append-only runtime semantics (`SELECT, INSERT`, no runtime update/delete).

However it stores no final value snapshot and has no direct foreign key to the durable reviewed-field row introduced/expanded by `0051`.

### 3.6 Existing workflow infrastructure is sufficient for the rule-run status anchor

`workflow_tasks` already supports:

- idempotent creation through an `effect_key`;
- `READY`, `CLAIMED`, `IN_PROGRESS`, `RETRY_WAIT`, `COMPLETED`, `CANCELLED` and `DEAD_LETTER` lifecycle behavior;
- worker leases/recovery;
- retries and dead-letter handling;
- append-only task events.

`audit_evaluations` already stores individual control evaluation results.

A new generic rule-run status table is therefore not justified by the current evidence.

### 3.7 `POST_DELIVERY` stage state already exists structurally

`journey_stage_states.stage_code` already accepts `POST_DELIVERY`, with `audit_state`, `audit_status`, timestamps and versioning. This can act as the Journey-level post-Delivery aggregate/status boundary instead of introducing another generic status table.

## 4. Proposed design

Status of the decisions in this section: **PROPOSED — ready for business/implementation approval after remaining blockers are closed.**

### D1. Final resolution input is Audit Core reviewed fields, never live DI

Final-source resolution reads `auditcore.journey_document_extracted_fields` for the Journey and stages `BOOKING` + `DELIVERY`.

Candidate value is the reviewed `effective_value`.

- accepted unchanged field: effective value equals extracted value;
- corrected field: effective value is the reviewed correction;
- rejected/no-accepted-effective-value field: not eligible as a final value candidate, while original evidence remains durable.

DI may still be called later to display document content/page/box evidence, but not to determine final business value.

### D2. Finalization gate requires both reviewed stages to be stable

Post-Delivery finalization should require:

- Booking capture submitted and Booking `pc_verification_status = 'VERIFIED'`;
- Delivery capture submitted and Delivery `pc_verification_status = 'VERIFIED'`;
- no second finalization already committed for the same Journey;
- Journey authorization + aggregate locking + idempotency consistent with existing UC03 commands.

Reason: a final source cannot truthfully be frozen while either reviewed input set is still mutable/pending.

### D3. Final source map must be stage-aware and explicit

The current `ATTRIBUTE_SPECS` registry remains the explicit mapping foundation, but the post-Delivery resolver requires an explicit stage-aware source selector.

Conceptually an ordered allowed source should be addressable as:

`(source_stage_code, document_type_key, field_key/canonical mapping)`

not only `document_type_key`.

No fuzzy label matching and no blanket `Delivery wins` rule.

The mapping/version recorded on the final resolution must identify the exact final-source policy used.

### D4. Reuse and minimally extend `journey_attribute_resolutions`; do not create a parallel final-field table

Smallest recommended persistence change for a later implementation unit:

- reuse `journey_attribute_resolutions` with `stage_code='POST_DELIVERY'`;
- add a direct reference to the selected durable reviewed field (`source_reviewed_field_id` -> `journey_document_extracted_fields.extracted_field_id` within tenant);
- persist a `resolved_value_snapshot` for the post-Delivery row.

Why both reference and snapshot are recommended:

- the reviewed-field reference gives exact source identity and provenance;
- the final value snapshot makes the final resolution stable/reproducible even though the generic reviewed-field table is technically update-capable;
- this is a sparse **resolution result**, not a second generic copy of all reviewed fields.

Existing DI source-reference columns, resolution rule, mapping version, actor/time and owning-domain/reference remain useful and should not be replaced.

**No genuinely new final-source table is currently justified.**

### D5. Final resolution remains sparse; typed domains remain authoritative operational state

Do not create a 152-column or 152-row authoritative business-state table.

Final report consumption should combine:

1. existing typed Journey domains for approved operational/PC-entered business state;
2. `POST_DELIVERY` final resolution snapshots for explicitly mapped document-derived attributes where a final source must be frozen;
3. existing repeated domain entities (especially Payments) for genuinely repeated business data;
4. audit evaluations/flags/observations for system-computed audit outputs;
5. final-report generation snapshot/output once that contract is mapped.

Final-source selection must not silently overwrite already-approved typed operational values merely because a later document differs. A typed-domain mutation requires an explicit business rule/approved owner behavior.

### D6. Cross-source comparison and finalization must use the same durable candidate set after Review

Recommended later behavior:

- pre-verification comparison may remain a live evidence preview if UX still needs it;
- once Booking and Delivery Review are `VERIFIED`, the post-Delivery comparison used for finalization should be built from durable reviewed fields, not fresh DI values;
- evidence content/page/region may be hydrated on demand from DI using stored source identity.

This prevents the UI comparison from showing one source set while finalization commits another.

### D7. Post-Delivery stage state + workflow task provide the rule-run boundary

Recommended aggregate lifecycle:

1. final-source confirm commits `POST_DELIVERY` resolution rows;
2. `POST_DELIVERY` stage enters/retains `audit_state='IN_PROGRESS'`;
3. in the same transaction, create one idempotent workflow task, for example task type `UC03_POST_DELIVERY_RULE_RUN`, keyed to the Journey/finalization version;
4. worker executes the approved post-Delivery controls and writes individual `audit_evaluations` / flags;
5. successful task completion advances `POST_DELIVERY.audit_state='COMPLETE'` and the applicable `audit_status`;
6. final report is unavailable until the task is `COMPLETED` and post-Delivery audit state is complete;
7. `RETRY_WAIT` / `DEAD_LETTER` means report generation remains blocked with an explicit status/error.

The required **synchronous boundary** is therefore a hard report gate on successful rule completion; it does not require every rule to execute inside the HTTP transaction.

### D8. Proposed API boundary (contract sketch only)

A smallest explicit command could be:

`POST /v2/tenants/{tenant_id}/journeys/{journey_id}/audit/final-source/confirm`

Responsibilities:

- authorize Journey scope;
- lock Journey aggregate;
- verify Booking + Delivery Review are both `VERIFIED`;
- load durable reviewed fields;
- apply only explicit stage-aware final-source rules;
- persist `POST_DELIVERY` resolution rows;
- create/reuse the post-Delivery rule-run workflow task;
- return finalization count/version + rule task id/status.

A read endpoint may expose the persisted final-source set/status without re-resolving DI.

The existing `GET /audit/source-comparison` must not be mutated into a write endpoint.

## 5. Repeated entities / Invoice decision

### Verified

- Payments/receipts already have genuine repeated Journey-domain representation where required.
- Current scalar attribute resolver chooses one candidate per attribute and therefore cannot itself represent an arbitrary repeated Invoice collection.
- Multiple invoice document types already appear as legitimate evidence sources for scalar attributes.

### Decision

**Do not create a typed repeated Invoice entity yet.**

It becomes justified only if the final-report contract or an approved business rule needs invoice-level repeated business rows/identities beyond document provenance and scalar final-source selection.

Until the exact final-report workbook is mapped, this remains `UNKNOWN / BLOCKED`, not a schema defect.

## 6. Final-report artifact investigation

Files currently available in the project were inspected:

- `SPR Details - Copy (2).xlsx` — Booking/process UI/form flow, not the final 152-field report contract;
- `SPR_Tool_Process_SubProcess_Activity_Details (1).xlsx` — process/rule/activity catalogue and analytics expectations, not the final 152-field output workbook;
- `Dealership_Audit_Scope_of_Work (1).pptx` — business audit scope, reporting cadence and deliverables, not an exact final-report field contract;
- supplied Booking/Delivery PDFs/images — source-document examples, not the report workbook.

No currently supplied artifact provides the exact 152-field final-report column/field mapping described by the stabilization charter.

Therefore the following cannot yet be truthfully frozen:

- exact 152 output fields;
- each output field's typed/resolved/computed owner;
- exact output ordering/formatting;
- whether any output needs a repeated typed Invoice entity.

**Blocker:** obtain/identify the actual final-report workbook before report-mapping implementation or any Invoice schema decision.

## 7. Remaining design work before implementation approval

1. Obtain/identify the actual final-report workbook and build the exact output-field -> Audit Core source/owner matrix.
2. Confirm canonical document aliases/families from authoritative catalogue/contracts; do not invent aliases.
3. For every post-Delivery resolvable attribute, freeze the ordered stage-aware legitimate-source rule and disagreement behavior.
4. Validate the proposed `POST_DELIVERY` resolution extension against final-report mapping, especially repeated invoice/report rows.
5. Define focused API/error/idempotency/version tests for the final-source confirm + rule-run gate.
6. Then stop and request implementation approval before any migration/application write.

## 8. Current conclusion

The investigation does **not** support a new generic final-state table or a new generic rule-run table.

The smallest evidence-based direction is:

- reuse durable reviewed fields from `journey_document_extracted_fields`;
- reuse and minimally extend `journey_attribute_resolutions` for immutable `POST_DELIVERY` winner + value snapshot;
- reuse `journey_stage_states.POST_DELIVERY` for aggregate state;
- reuse `workflow_tasks` for the rule-run status/retry boundary;
- reuse `audit_evaluations`/flags for individual rule results;
- project the final report from typed domains + persisted final resolutions + repeated domains + computed audit outputs.

Implementation remains intentionally blocked until the final-report workbook and exact stage-aware source mapping are closed.