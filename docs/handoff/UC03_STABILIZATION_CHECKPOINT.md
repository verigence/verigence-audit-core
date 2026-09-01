# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core final-source implementation; Units 1–3 implemented/source-verified**  
Repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-post-delivery-final-source-v1`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Latest application/test SHA before this checkpoint commit: `0f0076660680e7a6ead9dee49d1984453d497dfc`  
Mode: **WRITE APPROVED for final-source scope; merge/deploy NOT approved**

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → `UC03_IMPLEMENTATION_CONTEXT.md` → continue from `NEXT ACTION`.

Do not reconstruct completed work from chat history or broadly rescan completed repositories.

## Locked business decisions

- One Journey; Booking and Delivery are stages of that Journey.
- Booking facts persist at Booking Review; Delivery facts persist at Delivery Review.
- Every populated reviewed DI field remains durable in Audit Core.
- Repeated Payments/Receipts and repeated/multiple Invoice documents remain distinct.
- Final report uses the user-supplied business Final Source of Truth list; `NA` means non-document-derived.
- Exact technical DI aliases/keys must be authoritative; never invent them.
- Final report is blocked until post-Delivery final-source resolution + successful post-Delivery rule run.
- Current authoritative report contract supersedes the earlier 152-field assumption: 122 physical rows, 113 labelled outputs excluding two `-` separators, 81 unique non-separator labels.
- Current report does not require a typed repeated Invoice table.
- Typed/source-system report fields remain existing typed owners and are not duplicated into the final-resolution ledger.

## Completed baseline on `dev`

PR #135 is merged/deployed and verified:

- merge `ab0cf4c6a3e97cf70e482d0afdb6ae4c0ada6dd1`;
- live baseline `10701bf9968968d0efe4920b9230c2ed2664bd5f`;
- migration through `0051`: PASS;
- Ruff: PASS;
- pytest: 366 passed;
- Railway DEV deployment/fresh verification/smoke: PASS.

Do not reopen the completed lossless Booking/Delivery reviewed-field persistence baseline without contradictory evidence.

## Approved final-source implementation scope

User approved **`Approved: Audit Core final-source implementation`** on 2026-09-01.

Allowed:

1. additive `journey_attribute_resolutions` extension;
2. persisted final-source resolver using authoritative business-source policy where technical mappings are proven;
3. additive final-source confirm/read API with authorization, If-Match, idempotency and aggregate locking;
4. reuse existing typed business/commercial structures;
5. post-Delivery workflow task/readiness gate;
6. focused migration/API/resolution/repeated-payment/report-contract tests;
7. checkpoint/context updates.

Not allowed:

- DI/Web/Security changes;
- invented aliases/field mappings;
- rule-engine internals;
- new generic final/report table;
- Invoice table;
- Payment redesign;
- merge/deploy without separate approval.

## Completed implementation units

### Unit 1 — final-resolution ledger foundation

**Status: IMPLEMENTED / SOURCE-VERIFIED; NOT YET CI/DB-TESTED**

- Added additive migration `0052_uc03_final_source_resolution.py`.
- Reused `journey_attribute_resolutions`; no new table.
- Added `resolved_value_snapshot jsonb` and nullable `source_reviewed_field_id`.
- Added tenant+Journey-safe FK to `journey_document_extracted_fields`.
- Kept existing DI source NOT NULL requirements unchanged.
- Added `uc03_final_source_persistence.py` for document-derived POST_DELIVERY winner persistence.
- Final snapshot is loaded from durable reviewed `effective_value`, never live DI.
- Typed/source-system report fields stay in typed owners; the earlier draft that duplicated them into the ledger was removed before CI.
- Added focused persistence/migration tests.

### Unit 2 — final-source policy + confirm/read API

**Status: IMPLEMENTED / SOURCE-VERIFIED; NOT YET CI/DB-TESTED**

- Added `uc03_final_source_policy.py`.
- Executable policy contains only technical document/field pairs already proven by current Audit Core evidence.
- Disputed/unverified mappings are explicit `UNRESOLVED_TECHNICAL_POLICIES`; no fuzzy aliases are executable.
- Added additive final-source GET + confirm POST and registered the router in `main.py`.
- Command uses `_scope`, Delivery If-Match, Journey advisory aggregate lock and existing idempotency infrastructure.
- Command imports/calls no DI client and reads only durable Audit Core reviewed state.
- Candidate query selects the latest persisted reviewed fact version per stage/document/field before cross-source comparison.
- Legitimate current sources that disagree fail closed; confidence, stage and recency do not choose a winner.
- Agreeing sources may use deterministic provenance selection because the business value is identical.
- Every configured source is preflighted before the first resolution insert, preventing partial winner sets on disagreement.
- Existing POST_DELIVERY finalization causes conflict rather than a second winner set; idempotent replay remains handled by the existing idempotency record.
- GET reports `NOT_READY`, `MAPPING_BLOCKED`, `READY` or `CONFIRMED` and exposes unresolved technical mapping summaries.

CURRENT FAIL-CLOSED STATE:

- `UNRESOLVED_TECHNICAL_POLICIES` is intentionally non-empty because Audit Core cannot prove several DI canonical document/field keys.
- Therefore the POST currently returns mapping-incomplete conflict before final-source mutation. This is intentional, not a stub or guessed mapping.
- Step 2 DI contract validation is required to clear those mappings later.

### Unit 3 — post-Delivery rule task + report-readiness gate

**Status: IMPLEMENTED / SOURCE-VERIFIED; NOT YET CI/DB-TESTED**

CHANGED:

- Added `uc03_post_delivery_rule_gate.py`.
- Reuses existing `create_workflow_task_once()` and workflow task lifecycle; no new rule-run table or evaluator implementation.
- Final-source commit now creates/reuses exactly one `UC03_POST_DELIVERY_RULE_RUN` task using effect key `Journey + finalization version` after the POST_DELIVERY stage is created and before the final-source workflow event is appended.
- The task uses workflow type `UC03_POST_DELIVERY_AUDIT`, process area `POST_DELIVERY`, and preserves Journey dealer/outlet scope.
- Final-source GET now exposes:
  - POST_DELIVERY audit state/status;
  - rule task id/status/effect key;
  - `reportReady`.
- `reportReady` is true only when the rule task is `COMPLETED` **and** POST_DELIVERY `audit_state='COMPLETE'`; all pending/retry/failure/dead-letter states remain false automatically.
- Added a narrow future-worker completion boundary that:
  - requires the correct Journey/process/task identity;
  - requires an active worker lease while task is IN_PROGRESS;
  - records the workflow attempt as `SUCCEEDED`;
  - clears lease/retry/error state;
  - appends `WORKER_COMPLETED`;
  - marks POST_DELIVERY audit COMPLETE only after the task succeeds;
  - derives `NO_FLAGS` vs `FLAGS_RAISED` from persisted findings joined through `audit_evaluations.process_area='POST_DELIVERY'`.
- No rule evaluation, DI access or Web/Security change was added.

SOURCE CORRECTIONS DURING UNIT 3:

- Initial draft manually queried/created the task; replaced with the repository's existing `create_workflow_task_once()` idempotent/effect-key boundary.
- Initial draft assumed `audit_findings.stage_code`; current schema does not provide that column. Finding scope is now derived through the persisted Audit Evaluation `process_area`.
- Existing schema permits successful attempt result `SUCCEEDED`; Unit 3 uses that schema value rather than copying an unrelated Booking helper's `SUCCESS` literal. Booking rule code was not changed in this scope.

TESTS ADDED/UPDATED:

- final-source confirm source-order test proves preflight → resolution writes → POST_DELIVERY stage → rule-task creation → workflow event;
- final-source GET contract test proves rule/audit/report-readiness projection;
- rule effect key is finalization-version scoped;
- source test proves use of existing workflow reliability helper and no evaluator/DI dependency;
- DB-backed lifecycle test creates/reuses one task, verifies READY is not report-ready, claims/starts/completes through the worker boundary, verifies POST_DELIVERY `NO_FLAGS`, verifies `reportReady=true`, verifies attempt `SUCCEEDED`, and verifies one task + one workflow instance.

NOT YET VERIFIED:

- Ruff;
- full pytest;
- fresh PostgreSQL migration through `0052`;
- actual DB execution of the new focused lifecycle/FK tests;
- CI.

## Remaining UNKNOWN / fail-closed items

- exact DI canonical technical keys and field keys not already proven in Audit Core;
- exact arithmetic formulas for the two payment/reconciliation report blocks;
- exact selection/concatenation semantics for multiple PC/TL/PMO remarks.

Do not invent these.

## NEXT ACTION

**Verification only — do not broaden implementation.**

1. trigger the repository CI against `fix/uc03-post-delivery-final-source-v1` without merge/deploy;
2. require CI to execute Ruff, fresh PostgreSQL `alembic upgrade head`, and full pytest with `DATABASE_URL`;
3. inspect and fix only failures attributable to the approved final-source changes;
4. when green, update this checkpoint with exact migration/Ruff/pytest/CI results and latest SHA;
5. stop for explicit merge approval.

Do not enter DI or Web. Do not merge/deploy.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
