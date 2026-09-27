# UC03 Phase 2 — Design Closure Matrix

Source of truth: **Verigence Phase 2 Design Blueprint v2.2 (27 Sep 2026)**.
The v2.1 mockups remain useful for information/data intent, but v2.2 UI guardrails supersede their literal layout.

Status legend: **PASS**, **FIXED-IN-BRANCH**, **PARTIAL**, **OPEN**, **BLOCKED**.

| Design area | Status | Evidence / required closure |
|---|---|---|
| P2 isolation (p2_* / /p2/v1 / /p2/) | PASS | Runtime schema/routes are isolated; legacy remains available. |
| Security is sole P2 authorization authority | FIXED-IN-BRANCH | uc03_p2_access.py no longer treats local business assignments as a second allow/deny authority. |
| Durable upload init → direct object PUT → finalize | PASS-CODE | P2 init/finalize + storage adapter exist. DEV end-to-end evidence still required. |
| Multi-page PDF split / per-page durable queue | PASS-CODE | uc03_p2_worker.py contains split/ingest/reconcile paths. 20-page/no-loss restart evidence still required. |
| P2 worker separate runtime | BLOCKED | railway.p2-worker.toml exists, but no DEV worker deployment workflow/evidence currently exists. |
| P2 Documents unified list | PASS-CODE | P2 document list returns batch/page state. |
| P2 Document Review/Edit routes | FIXED-IN-BRANCH | Router existed but was not mounted in main.py; branch mounts uc03_p2_documents_router. |
| Source preview + extracted/effective values | PASS-CODE | uc03_p2_documents.py + P2 review UI. DEV smoke still required. |
| Field correction + approval path | PASS-CODE | Low-confidence direct correction and high-confidence TL review task exist. DEV round-trip evidence required. |
| Replace / re-upload document contract | OPEN | v2.2 requires logical replacement lineage; no P2 replace endpoint is currently exposed. |
| Logical void/supersede contract | OPEN | v2.2 requires non-destructive logical void/supersede; no P2 delete/void endpoint currently exposed. |
| Six-stage Journey engine | PASS-CODE | Booking stage evaluator exists; Delivery business gates intentionally remain placeholder per design. |
| Booking completion gates | PASS-CODE | Booking Form + PAN + Aadhaar + minimum payment + no manual verification encoded. Integration evidence required. |
| One P2 control ledger | PARTIAL | p2_control_state + rule-execution mirror exist. Full executable control registry/dependency validation remains to be proven. |
| Machine task dedupe + async verification | PASS-CODE | p2_tasks, TASK_VERIFY, exact-origin control rerun path exist. Retry/restart evidence required. |
| Human requester Accept/Reject round trip | PASS-CODE | Requester-confirmation task lifecycle exists. UI/e2e evidence required. |
| Task Queue Journey/Customer context | FIXED-IN-BRANCH | API branch returns customer/dealer/outlet/vehicle context; Web draft already renders it. |
| Task Queue professional action-first UX | PARTIAL | Web draft removes raw JSON and surfaces reason/reference/action. Needs final responsive/UAT review. |
| Journey 360 P2 local overview | PARTIAL | P2 overview exists but current backend contract lacks the full v2.2 statistics block expected by Web draft. |
| Preserve legacy Journey 360 unchanged | OPEN-WEB-FIX | Current Web draft modifies legacy Journey360Page; must be reverted and P2 enhancements kept in P2 page. |
| Booking / Delivery / Journey statistics | OPEN | Web contract exists; backend data contract still needs implementation/validation. |
| Source-aware Deal & Commercial view | PARTIAL | Canonical/source tables and legacy Journey 360 exist. P2 lazy business projection parity is not yet proven. |
| Vehicle / Finance / Insurance / Registration P2 business tabs | PARTIAL | Canonical data exists; isolated P2 lazy projections are not yet complete/proven. |
| Documents / Tasks / Findings / Activity lazy detail | PARTIAL | Documents/tasks endpoints exist; full P2 findings/activity presentation/parity not complete. |
| P2 events incremental refresh | PASS-CODE | P2 activity/events endpoint exists. DEV behavior still needs smoke evidence. |
| P2.0 executable registry/catalog validation | OPEN | Need automated validation of document registry, DI schema refs, control catalog and task catalog against live baseline. |
| P2.1 20-page/no-page-loss test | OPEN | Required rollout evidence not recorded. |
| P2.2 desktop/mobile UAT + correction round trip | OPEN | Required rollout evidence not recorded. |
| P2.3 stage/payment integration tests | OPEN | Required rollout evidence not recorded. |
| P2.4 task dedup/retry/restart tests | OPEN | Required rollout evidence not recorded. |
| P2.5 Journey 360 p95/parity reconciliation | OPEN | Required rollout evidence not recorded. |
| P2.6 canary | OPEN | Not started/proven. |
| P2.7 existing Journey bootstrap | OPEN/OPTIONAL | No explicit bootstrap/reconciliation evidence. |
| P2.8 cutover | NOT READY | Must not become default until prior gates are closed. |

## Merge rule

Phase 2 is **not complete** because CI is green. A merge-ready declaration requires:
1. every design-critical row above to be PASS or explicitly accepted as a designed placeholder;
2. current branch synchronized with dev;
3. CI + Android validation green;
4. Audit Core API + P2 worker + Web deployed from the tested SHAs;
5. DEV smoke of Documents upload/review, Task Queue, Journey 360 and task action round trips;
6. no legacy UC03 route/page behavior modified during P2 stabilization.
