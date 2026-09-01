# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core final-source implementation; Units 1–2 implemented/source-verified**  
Repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-post-delivery-final-source-v1`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Latest application/test SHA before this checkpoint commit: `3c0db7a16f27c47af49eb3163bbd57b8db6d6bf9`  
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
- Typed/source-system report fields stay in typed owners; an earlier draft that duplicated them into the ledger was removed before CI.
- Added focused persistence/migration tests.

### Unit 2 — final-source policy + confirm/read API

**Status: IMPLEMENTED / SOURCE-VERIFIED; NOT YET CI/DB-TESTED**

CHANGED:

- Added `uc03_final_source_policy.py`.
- Executable policy contains only technical document/field pairs already proven by current Audit Core evidence.
- Disputed/unverified mappings are explicit `UNRESOLVED_TECHNICAL_POLICIES`; no fuzzy aliases are executable.
- Added `GET /v2/tenants/{tenant_id}/journeys/{journey_id}/audit/final-source`.
- Added `POST /v2/tenants/{tenant_id}/journeys/{journey_id}/audit/final-source/confirm`.
- Registered the additive final-source router in `main.py`.
- Command uses `_scope`, Delivery If-Match, Journey advisory aggregate lock and existing idempotency infrastructure.
- Command imports/calls no DI client and reads only durable Audit Core reviewed state.
- Candidate query selects latest persisted reviewed fact version per stage/document/field before cross-source comparison.
- Legitimate current sources that disagree fail closed; confidence, stage and recency do not choose a winner.
- Agreeing sources may use deterministic provenance selection because the business value is identical.
- Every configured source is preflighted before the first resolution insert, preventing partial winner sets on disagreement.
- Existing POST_DELIVERY finalization causes conflict rather than a second winner set; idempotent replay remains handled by the existing idempotency record.
- Successful commit creates the POST_DELIVERY stage in `audit_state='IN_PROGRESS'` and appends a safe `FINAL_SOURCE_CONFIRMED` event without raw values.
- GET reports `NOT_READY`, `MAPPING_BLOCKED`, `READY` or `CONFIRMED` and exposes unresolved technical mapping summaries.

CURRENT FAIL-CLOSED STATE:

- `UNRESOLVED_TECHNICAL_POLICIES` is intentionally non-empty because Audit Core cannot prove several DI canonical document/field keys.
- Therefore the POST currently returns mapping-incomplete conflict before final-source mutation. This is intentional, not a stub or guessed mapping.
- Step 2 DI contract validation is required to clear those mappings later.

TESTS ADDED:

- explicit GET/POST route contract;
- Booking + Delivery Review verification helpers;
- source-disagreement fail-closed behavior;
- agreeing-source deterministic provenance behavior;
- unresolved mapping guard occurs before idempotency/final resolution writes;
- all sources are preflighted before first persistence call;
- final-source module contains no DI client dependency;
- policy registry rejects known disputed aliases and keeps known gaps explicit.

NOT YET VERIFIED:

- Ruff;
- pytest;
- fresh PostgreSQL migration through `0052`;
- real DB endpoint/FK behavior.

## Remaining UNKNOWN / fail-closed items

- exact DI canonical technical keys and field keys not already proven in Audit Core;
- exact arithmetic formulas for the two payment/reconciliation report blocks;
- exact selection/concatenation semantics for multiple PC/TL/PMO remarks.

Do not invent these.

## NEXT ACTION

**Implementation Unit 3 — post-Delivery workflow task + report-readiness gate.**

1. inspect only existing workflow instance/task creation helpers and status semantics needed for reuse;
2. on successful final-source commit, create/reuse exactly one idempotent `UC03_POST_DELIVERY_RULE_RUN` workflow task keyed to Journey/finalization version;
3. expose task/audit readiness on the final-source GET;
4. keep report readiness false while the task is READY/CLAIMED/IN_PROGRESS/RETRY_WAIT/FAILED/DEAD_LETTER or POST_DELIVERY audit is not COMPLETE;
5. define the smallest completion hook/helper that a later approved rule worker can call to mark successful post-Delivery audit completion, without implementing rule-engine internals;
6. add focused tests and update checkpoint;
7. then run branch CI/fresh migration/full pytest and stop for merge approval.

Do not enter DI or Web. Do not merge/deploy.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
