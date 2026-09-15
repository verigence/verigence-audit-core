# Unified Work Items — Design v1.0

## Problem

Two things a role acts on today are stored, routed, and displayed by two
independently-invented mechanisms, inside audit-core alone:

| Mechanism | Table | Status vocabulary | Scope |
|---|---|---|---|
| Audit finding ("flag") | `audit_findings` | `OPEN → ACKNOWLEDGED → RESOLVED / VOIDED` | `journey_id` or `daily_ops_run_id` (migration 0080's `subject_kind`) |
| Workflow task ("My Work") | `workflow_tasks` | `PENDING → READY → CLAIMED → IN_PROGRESS → COMPLETED / FAILED / CANCELLED / DEAD_LETTER` | `journey_id` (`NOT NULL` FK — hard-coupled to Audit's own journeys) |

Two status machines were independently built inside one service before any
other domain entered the picture. Left alone, every new domain (Daily Ops
proper, Feedback follow-ups, price/SKU master approvals, Attendance if it
ever plugs in here, whatever comes next) invents a third, fourth, fifth.
That is the literal failure mode this design closes off.

**Attendance is explicitly out of scope for this design.** It lives in a
separate deployable (`verigence-security`) with its own database — folding
it in means a real cross-service decision (does it call an API into this
spine, or keep its own store forever) that's deliberately deferred, not
solved here. Everything below is scoped to audit-core's own two
mechanisms. `subject_kind` is still built as an open enum so a
cross-service answer can slot in later without a schema change — it just
isn't designed for that yet.

Review Queue (`uc03_review_queue.py`) already gestures at the fix without
completing it: its own docstring says a finding's `subject_kind` lets
"the tenant-wide Review Queue... list both kinds side by side," loaded via
two separate queries merged in Python rather than one union. That instinct
generalizes directly to the design below — it just needs a real spine
table under it instead of stopping at two hand-merged queries.

## Target shape

One spine table owns everything shared across every kind of item: status,
ownership, due date, priority, which queue it shows under. Each kind's own
specific fields live in a thin 1:1 detail table keyed by the same id —
class-table inheritance, the same pattern ServiceNow's `task` base table
and Jira's `issue` table use under Incident/Problem/Change and Bug/Story/
Epic respectively.

```
auditcore.work_items                          (the spine — one row per item)
├── auditcore.work_item_finding_detail         (1:1, item_kind='FINDING')
└── auditcore.work_item_task_detail            (1:1, item_kind='EXECUTION_TASK')
```

### `work_items` (the spine)

```sql
CREATE TABLE auditcore.work_items (
  tenant_id           varchar(128) NOT NULL,
  work_item_id        uuid NOT NULL DEFAULT gen_random_uuid(),
  item_kind           varchar(20)  NOT NULL,   -- FINDING | EXECUTION_TASK
                                                -- (the ONLY thing that decides which
                                                --  detail table to join -- nothing else)
  origin_kind         varchar(20)  NOT NULL,   -- MACHINE | SYSTEM | HUMAN
                                                -- who/what raised it -- orthogonal to
                                                -- item_kind (a MACHINE or SYSTEM origin
                                                -- can both be item_kind='FINDING')
  subject_kind        varchar(20)  NOT NULL,   -- JOURNEY | DAILY_OPS | GENERAL
  subject_ref         uuid,                    -- journey_id / daily_ops_run_id / null for GENERAL
  classification      varchar(40),             -- DATA_GAP | DOCUMENT_GAP | VIOLATION | EXECUTION
  owner_role_code     varchar(40),
  assigned_actor_id   varchar(160),
  -- priority and due_at_utc are set BY THE PRODUCER at write time, using
  -- whatever policy that producer's own domain applies -- see "Who decides
  -- SLA and priority" below. The spine stores them; it never computes them.
  priority            integer NOT NULL DEFAULT 50,
  due_at_utc          timestamptz,
  status              varchar(20)  NOT NULL DEFAULT 'OPEN'
                       CHECK (status IN ('OPEN','IN_PROGRESS','RESOLVED','CANCELLED')),
  title               varchar(500) NOT NULL,
  summary             text,
  created_by_actor_id varchar(160),
  created_at_utc      timestamptz NOT NULL DEFAULT now(),
  updated_at_utc      timestamptz NOT NULL DEFAULT now(),
  version_no          bigint NOT NULL DEFAULT 1,
  correlation_id      varchar(128),
  PRIMARY KEY (tenant_id, work_item_id)
);

CREATE INDEX ix_work_items_queue
  ON auditcore.work_items(tenant_id, subject_kind, status, priority DESC, due_at_utc);
CREATE INDEX ix_work_items_owner
  ON auditcore.work_items(tenant_id, assigned_actor_id, status);
```

### Vocabulary: `OPEN → IN_PROGRESS → RESOLVED → CANCELLED`

Picked over the existing `audit_findings` wording (`ACKNOWLEDGED`/`VOIDED`)
because it's the vocabulary that actually travels: GitHub Issues, Jira,
Azure Boards, and ITIL's own incident/request lifecycle all converge on
this exact four-word shape for "something to act on, regardless of
whether it's a decision or a unit of work." `ACKNOWLEDGED` and `VOIDED`
are real words in the alerting world (PagerDuty/Opsgenie) but read oddly
for a re-upload task; `IN_PROGRESS`/`CANCELLED` read naturally for both a
finding and a task, which is the entire point of a shared vocabulary.
`REOPEN` is a transition back to `OPEN`, not a fifth status — matches
GitHub/Jira and matches what `uc03_audit_flags.py`'s own `REOPEN` action
already does today.

The detail table still records the specific flavor
(`disposition = FIXED | CONFIRMED_BREACH | FALSE_POSITIVE` on a finding,
whatever a task's own terminal reason was) — the spine only needs to know
the item is open, being worked, or closed. Presentation layers are free to
render `RESOLVED` as "Completed" for a task and "Resolved" for a finding;
that's a label choice per `item_kind`, not a second status column.

### Who decides SLA and priority: the producer, not the spine

`due_at_utc` and `priority` are plain stored columns, written by whichever
system creates or updates the row — **using that system's own policy**,
not a shared formula. Concretely: `uc03_finding_routing.py`'s
`sla_due_at()`/severity-weighting logic doesn't move into the spine or
become a generic service both Audit and future domains call into — it
stays exactly where it is, as the code Audit runs *before* writing to
`work_items`, and it writes the number it computed straight into
`due_at_utc`. A future domain with a completely different notion of
urgency (a payroll-cycle deadline has nothing to do with severity) writes
its own number the same way, with no dependency on Audit's policy at all.

The spine's only job on these two columns is to store what it's told and
expose trivial, domain-blind derived facts on read — `overdue = now() >
due_at_utc`, and an escalation bucket purely as a function of *how*
overdue, the way `escalation_level()` already works today. It never
decides *what* due_at_utc or priority should be. That responsibility
moving with the producer is also what makes phase 5 (a new domain joining)
free — a new producer needs no permission from, or change to, Audit's own
SLA code to set a due date that makes sense for it.

### Detail tables (kind-specific, nothing lost)

```sql
CREATE TABLE auditcore.work_item_finding_detail (
  tenant_id            varchar(128) NOT NULL,
  work_item_id         uuid NOT NULL,
  rule_key             varchar(120),
  rule_version_id      uuid,
  severity             varchar(20) NOT NULL,
  expected_summary     text,
  observed_summary     text,
  resolution_reason    text,
  disposition          varchar(30),   -- FIXED | CONFIRMED_BREACH | FALSE_POSITIVE
  blocking_completion  boolean NOT NULL DEFAULT false,
  stage_code           varchar(20),   -- BOOKING | DELIVERY, JOURNEY subject only
  PRIMARY KEY (tenant_id, work_item_id),
  FOREIGN KEY (tenant_id, work_item_id) REFERENCES auditcore.work_items(tenant_id, work_item_id)
);

CREATE TABLE auditcore.work_item_task_detail (
  tenant_id              varchar(128) NOT NULL,
  work_item_id           uuid NOT NULL,
  task_type              varchar(120) NOT NULL,
  effect_key             varchar(240),
  attempt_count          integer NOT NULL DEFAULT 0,
  max_attempts           integer NOT NULL DEFAULT 5,
  next_attempt_at_utc    timestamptz,
  lease_owner            varchar(200),
  lease_acquired_at_utc  timestamptz,
  lease_expires_at_utc   timestamptz,
  task_payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_error_code        varchar(80),
  last_error_summary     text,
  PRIMARY KEY (tenant_id, work_item_id),
  FOREIGN KEY (tenant_id, work_item_id) REFERENCES auditcore.work_items(tenant_id, work_item_id)
);
```

`finding_evidence` and `audit_finding_events` keep working unchanged —
just re-point their FK from `audit_finding_id` to `work_item_id` (same
UUID, new name). `workflow_task_events`/`workflow_task_attempts` re-point
the same way to `work_item_id`.

### Why `item_kind` and `origin_kind` are separate columns

A rule finding, a manual-verification flag, and a human-raised flag are
three different **origins** sharing one **nature** — something is wrong,
a decision is owed. An execution task is a genuinely different nature —
work is owed, not a verdict. Collapsing origin and nature into one column
loses exactly the distinction that came up mid-conversation: Manual
Verification is `origin_kind='SYSTEM'` (a DI confidence threshold decided
this, not a rule, not a person) with the same DECISION nature as a rule
finding — it needs `item_kind='FINDING'`-shaped handling (goes in
`work_item_finding_detail`, not `task_detail`), but isn't literally "a
rule fired," so `rule_key` stays null on its detail row. `origin_kind`
answers "who/what raised this" (for filtering, reporting, permissions);
`item_kind` only ever answers "which detail table" — that's why it stays
a two-value enum instead of growing a new value for every new origin.

### The queues fall out for free

`subject_kind` on the spine is the tab, not a separate table:

- **Journeys** — `WHERE subject_kind = 'JOURNEY'`
- **Daily Tasks** — `WHERE subject_kind = 'DAILY_OPS'`
- **Additional** — `WHERE subject_kind = 'GENERAL'` (nothing lives here yet;
  it's the slot a future non-journey, non-daily-ops domain lands in
  without a schema change)

One query (`SELECT ... FROM work_items WHERE tenant_id=:t AND
subject_kind=ANY(:kinds) AND status IN ('OPEN','IN_PROGRESS') ...`), one
page, tabs are a client-side or `WHERE` filter — not three systems to
maintain.

## What does NOT change

- **Rule Engine** (bespoke Python checks + the external declarative
  service) keeps deciding *whether* something is wrong. It changes what
  it writes to (`work_items` + `work_item_finding_detail` instead of
  `audit_findings` directly) — not what it decides.
- **Routing/SLA logic** (`uc03_finding_routing.py`,
  `uc03_finding_classification.py`) is unchanged in substance — it still
  decides `classification`/`owner_role_code`/`due_at_utc` exactly as it
  does today; it just writes the result into `work_items` instead of
  `audit_findings`, and reads it back from there too (see "Who decides
  SLA and priority" above — this logic doesn't move or generalize).
- **Evidence linking, the Compliance Report, the Audit Timeline** — all
  keep working, re-pointed at `work_item_id`.
- Confirm-Breach / Mark-False-Positive / evidence attachment / blocking-
  completion gating stay exactly where they are conceptually: real,
  audit-specific behavior that lives in `work_item_finding_detail` and the
  finding-specific action handlers, not something the generic spine needs
  an opinion about.

## Migration path (phased, each phase independently shippable)

1. **Add the spine, dual-write.** Create `work_items` +
   `work_item_finding_detail` + `work_item_task_detail`. Every place that
   currently `INSERT`s into `audit_findings` or `workflow_tasks` also
   inserts the matching spine + detail row in the same transaction.
   `audit_findings`/`workflow_tasks` remain the read path — zero behavior
   change, pure additive instrumentation. Backfill existing rows once via
   migration.
2. **Cut reads over.** Review Queue, the `/flags` API, the `/tasks` API,
   and the Compliance Report each move their `SELECT`s to
   `work_items` (+ join the relevant detail table). One endpoint at a
   time, each independently verifiable against the dual-written data
   before the next moves.
3. **Cut writes over, retire the dual-write.** Once every reader is off
   the old tables, mutation endpoints (`create_flag`, `act_on_flag`,
   `claim_workflow_task`, etc.) write `work_items`/detail tables only.
4. **Drop `audit_findings`/`workflow_tasks`** once nothing references them
   (a straight rename to the detail-table shape is likely cheaper than a
   drop-and-recreate — evaluate at the time).
5. **New in-service domains plug in at the spine directly** — a future
   audit-core-native source of work gets its own detail table (or none, if
   there's nothing beyond title/summary) and never touches a bespoke
   status machine again. A cross-service domain (Attendance, or anything
   else living outside audit-core) is a separate, later decision — see
   "Deferred" below.

Phases 1-3 are the only ones that touch live behavior; each is a normal
PR-sized change against the existing verification discipline (full suite
+ `ruff` + real-DB migration check via CI, SHA-verified deploy). Phase 4
is cleanup with no functional risk once phase 3 is confirmed live. Phase 5
is "the payoff" — no audit-core change required at all for a new domain to
get a working queue, claim/assign lifecycle, and SLA for free.

## Decided

- **Status vocabulary**: `OPEN → IN_PROGRESS → RESOLVED → CANCELLED`,
  fixed at the spine, with kind-specific disposition detail (`FIXED`,
  `CONFIRMED_BREACH`, `FALSE_POSITIVE`, a task's own terminal reason)
  living in the detail table, not a second status column. See above.
- **SLA and priority**: written by the producer at create/update time,
  using that producer's own domain policy (Audit keeps
  `uc03_finding_routing.py` exactly as-is; it just writes the result to
  `work_items` instead of `audit_findings`). The spine never computes
  either — it stores what it's told and derives only domain-blind facts
  (`overdue`, an escalation bucket) on read. See above.
- **Attendance / any cross-service domain**: explicitly deferred. Nothing
  in this design blocks deciding it later — `subject_kind='GENERAL'` and
  an open `origin_kind` enum are exactly the seams a later answer (an API
  push from another service, most likely) would use — but no cross-service
  contract is being designed now.

## Still open, deliberately, until phase 1 is underway

- **Owning service for `work_items`**: audit-core, by default (it already
  owns `business_assignments`, the table Review Queue joins against for
  "MINE" scoping, and both mechanisms being unified already live there).
  Only worth revisiting if a cross-service domain decision later changes
  the calculus — not before.
