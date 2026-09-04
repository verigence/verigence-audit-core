# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core final-source implementation; Units 1–3 CI/DB verified; STOPPED at merge approval gate**  
Repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-post-delivery-final-source-v1`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Latest application/test SHA before this checkpoint commit: `6617e86c9d2ef191f8e5919c4675c5597e66931e`  
Draft verification PR: **#136 — `UC03: post-delivery final source implementation`**  
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

**Status: IMPLEMENTED / CI + FRESH-DB VERIFIED**

- Added additive migration file `0052_uc03_final_source_resolution.py` with final Alembic revision ID `0052_uc03_final_source`.
- Reused `journey_attribute_resolutions`; no new table.
- Added `resolved_value_snapshot jsonb` and nullable `source_reviewed_field_id`.
- Added tenant+Journey-safe FK to `journey_document_extracted_fields`.
- Kept existing DI source NOT NULL requirements unchanged.
- Added `uc03_final_source_persistence.py` for document-derived POST_DELIVERY winner persistence.
- Final snapshot is loaded from durable reviewed `effective_value`, never live DI.
- Typed/source-system report fields stay in typed owners; the earlier draft that duplicated them into the ledger was removed before CI.
- Focused persistence/migration tests passed in full CI.

### Unit 2 — final-source policy + confirm/read API

**Status: IMPLEMENTED / CI VERIFIED**

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

**Status: IMPLEMENTED / CI + DB-LIFECYCLE VERIFIED**

- Added `uc03_post_delivery_rule_gate.py`.
- Reuses existing `create_workflow_task_once()` and workflow task lifecycle; no new rule-run table or evaluator implementation.
- Final-source commit creates/reuses exactly one `UC03_POST_DELIVERY_RULE_RUN` task using effect key `Journey + finalization version` after the POST_DELIVERY stage is created and before the final-source workflow event is appended.
- Task uses workflow type `UC03_POST_DELIVERY_AUDIT`, process area `POST_DELIVERY`, and preserves Journey dealer/outlet scope.
- Final-source GET exposes POST_DELIVERY audit state/status, rule task id/status/effect key and `reportReady`.
- `reportReady` is true only when the rule task is `COMPLETED` and POST_DELIVERY `audit_state='COMPLETE'`; pending/retry/failure/dead-letter states remain false.
- Added a narrow future-worker completion boundary that validates Journey/process/task identity and active lease, records attempt `SUCCEEDED`, clears lease/retry/error state, appends `WORKER_COMPLETED`, then marks POST_DELIVERY audit COMPLETE.
- `NO_FLAGS` vs `FLAGS_RAISED` is derived from persisted findings joined through `audit_evaluations.process_area='POST_DELIVERY'`.
- No rule evaluation, DI access or Web/Security change was added.
- DB-backed lifecycle test passed in full CI: task create/reuse, READY not-ready gate, worker claim/start/complete, POST_DELIVERY completion, `NO_FLAGS`, `reportReady=true`, attempt `SUCCEEDED`, and single task/workflow instance.

## Verification evidence

Draft PR #136 was opened **only to trigger CI**; it remains draft and unmerged.

### CI attempt 1 — run `33536352239`

- Build: PASS.
- Ruff: PASS.
- Fresh PostgreSQL migration: FAIL at Alembic version bookkeeping after applying `0052` DDL.
- Root cause: revision ID `0052_uc03_final_source_resolution` exceeded existing `alembic_version.version_num varchar(32)`.
- Pytest: skipped because migration failed.
- Deployment: skipped.

Correction:

- Shortened only the Alembic revision identifier to `0052_uc03_final_source`; migration behavior/schema remained unchanged.

### CI attempt 2 — run `33536633900`, application/test SHA `6617e86c9d2ef191f8e5919c4675c5597e66931e`

- Build package: **PASS**.
- Ruff `ruff check src tests migrations`: **PASS — All checks passed**.
- Fresh PostgreSQL `alembic upgrade head`: **PASS**, single head `0052_uc03_final_source`.
- Full pytest with `DATABASE_URL`: **PASS — 383 passed, 1 warning in 22.66s**.
- The warning is the existing Starlette/httpx TestClient deprecation warning; no test failure.
- Railway DEV deployment job: **SKIPPED**.
- Security diagnosis job: **SKIPPED**.
- No merge or deployment was performed.

## Remaining UNKNOWN / fail-closed items

These are not implementation failures and were deliberately not guessed:

- exact DI canonical technical keys and field keys not already proven in Audit Core;
- exact arithmetic formulas for the two payment/reconciliation report blocks;
- exact selection/concatenation semantics for multiple PC/TL/PMO remarks.

## NEXT ACTION

**STOP — explicit merge approval required.**

Current implementation branch and draft PR are CI-green for the approved Audit Core final-source scope. Do not merge PR #136 and do not deploy until separately approved.

If merge is approved, merge PR #136 into `dev` only. Deployment still requires separate explicit approval unless the user explicitly grants both actions.

Do not enter DI or Web as part of this branch.

## Anti-stuck rule

If future work resumes and a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
