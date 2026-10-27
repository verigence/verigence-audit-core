# Task Manager, Daily Status and Employee Utilisation — Design v1.0

Product owner decisions of 2026-10-26 (conversation of that evening, IST).
Builds on the Phase 2 task queue (`p2_tasks`, `p2_task_events`), the durable
once-only run claim (`p2_scheduled_runs`), the Daily Gate Pass (0163) and the
Daily Processing report. Nothing here changes how Journey tasks are raised,
worked or verified.

## 1. Scope

Everything is **per Project** (tenant). No list, task, schedule, status or
dashboard mixes Projects. Superadmin works one selected Project at a time from
its own admin view.

Three kinds of task scope live in `p2_tasks.scope_kind`:

| scope_kind | journey_id | outlet_id | Raised by |
|---|---|---|---|
| `JOURNEY` (all existing rows) | required | — | existing producers, TL/PM raise on a Journey |
| `OUTLET` | null | required | gate pass task, gate-pass mismatch task, outlet-limited Task Manager task |
| `PROJECT` | null | null | Task Manager |

"Journey Tasks" = `JOURNEY`. "Task Manager tasks" = `OUTLET` + `PROJECT`.

## 2. Who can raise a Task Manager task, and to whom (server-enforced)

| Raiser (operating role in the Project) | Can assign to |
|---|---|
| Superadmin | anyone in the Project (PC, TL, PM, CRM, Reviewer, Executive) |
| Executive | anyone in the Project |
| PM | TL, PC, CRM |
| TL | PC, CRM, Reviewer (Reviewer added 2026-10-27) |
| CRM | TL, PM |
| Reviewer | PC |
| PC | no one |

The assignee must hold an active business assignment in that role in the
Project. A task can go to several people at once; each gets their own task.

**Reassign**: the raiser, a PM, an Executive or Superadmin. The new assignee
must be someone the reassigning person may assign to. Recorded as a
`REASSIGNED` event.

## 3. One-off tasks

- The opener **must** set the expected time to close (2h, 4h, 1 day, 2 days,
  3 days or custom minutes). `due_at_utc = created + expected_close_minutes`.
- Assignee completes with a comment. The task then goes to the reviewer.
- Reviewer = the opener. Review SLA = 2 days from completion
  (`review_due_at_utc`). Accept closes; Reject needs a comment and returns
  the task to the assignee (original due time stands, send-back counted in
  `round_number`).
- **Tasks assigned to a PM have no reviewer**: the PM's completion closes it.

## 4. Recurring tasks (schedules, `p2_task_schedules`)

- Created only by an **Executive** or **Superadmin**.
- Frequency `DAILY` (weekdays chosen by the owner, default Mon–Sat),
  `WEEKLY` (weekday(s)) or `MONTHLY` (day of month; 29–31 fall back to the
  month's last day).
- **Sunday**: a daily task is never generated on a Sunday. A weekly or
  monthly occurrence that falls on a Sunday moves to the Monday.
- Generation time `run_time_ist` (default 09:00 IST); due =
  occurrence time + `expected_close_minutes`.
- Assignee rule: a named person (`ACTOR`), every active person of a role
  (`ROLE_EACH`), or one task per outlet for that outlet's PCs (`OUTLET_PCS`).
- Review: the **PM** reviews every occurrence by default; only a PM's
  acceptance closes it; review SLA 2 days. If the assignee is a PM there is no
  reviewer. If the Project has no PM, the Executive reviews.
- PM rejects: back to the assignee with a mandatory comment, original due time
  stands, send-back counted.
- An occurrence still open when the next occurrence is generated stays open
  and is marked **Missed** (`missed_at_utc`). Nothing auto-closes.
- The owner (or Executive / Superadmin) can skip one date (holiday) with a
  reason: an existing occurrence is cancelled, a future date is added to
  `skip_dates`.
- Generation is claimed once per occurrence with
  `claim_scheduled_run(run_name='TM_OCCURRENCE:<schedule_id>', run_key=<date>)`
  and each task carries `dedupe_key = sched:<schedule_id>:<date>:<assignee>`,
  so restarts and two workers never duplicate.

## 5. Daily Gate Pass task (system-reviewed)

- One system schedule per Project (`system_kind='GATE_PASS'`, `OUTLET_PCS`,
  daily, Mon–Sat), seeded by the worker; Executive / Superadmin can pause it.
- One `OUTLET` task per outlet with an active PC, per working day N, assigned
  to that outlet's PCs together (role `PC`, no named actor); any of them
  satisfies it.
- No human reviewer (`completion_protocol='SYSTEM_VERIFIED'`). The system
  closes it as soon as the ACTIVE gate pass of that outlet and day has its
  photo and is finished (`completed_at_utc`).
- Due 23:59:59 IST on N+1. Not finished by then → status `FAILED`,
  outcome `INCOMPLETE`, flagged.

The gate-pass mismatch task ("Delivery not notified to the PC", raised to the
TL) moves from `p2_daily_tasks` into `p2_tasks` as an `OUTLET` task with no
reviewer; existing rows are copied by the migration. The `/daily-tasks`
endpoints keep their response shape and read/write `p2_tasks`.

## 6. Lifecycle (Task Manager tasks)

Statuses reuse `p2_tasks.task_status`:

```
READY ──START──▶ IN_PROGRESS ──COMPLETE (comment)──▶ AWAITING_REQUESTER_REVIEW ──ACCEPT──▶ VERIFIED_COMPLETE
  │                                    │                         └──REJECT (comment)──▶ RETURNED ──COMPLETE──▶ …
  │                                    └──(no reviewer)──────────────────────────────▶ VERIFIED_COMPLETE
  └──CANCEL (raiser / Executive / Superadmin, reason)──▶ CANCELLED
SYSTEM_VERIFIED (gate pass): READY ──system──▶ VERIFIED_COMPLETE | FAILED (INCOMPLETE)
```

`completion_protocol`: `REQUESTER_CONFIRMED` (has a reviewer),
`SELF_CLOSING` (no reviewer), `SYSTEM_VERIFIED` (gate pass).
Every action writes a `p2_task_events` row (journey_id null for these tasks).

## 7. Flags (daily status)

| Flag | Rule |
|---|---|
| Overdue | open for the assignee (`READY`, `IN_PROGRESS`, `RETURNED`) and `due_at_utc` passed |
| Review overdue | `AWAITING_REQUESTER_REVIEW` and `review_due_at_utc` passed (flagged against the reviewer) |
| Missed | recurring occurrence still open when the next one was generated |
| Incomplete | gate pass task not finished by 23:59 IST on N+1 |
| Closed late | completed after `due_at_utc` |

Daily status is two new sections of the Daily Processing report (TL, PM,
Executive; Superadmin from its admin view): **Journey Tasks** and
**Task Manager tasks**, each with per-person counts (open, overdue, completed
on the day, completed late, review overdue, missed) and the flagged items.

## 8. Employee utilisation dashboard

Visible to **Executive and Superadmin only**. Per person active in the
Project, for one day or a week (with per-day breakdown):

- Output: journeys started, files and pages uploaded, failed pages.
- Tasks: assigned, completed, completed on time, overdue now, average time to
  complete, send-backs received; tasks worked (actions on Journey tasks).
- Reviews: Task Manager reviews done, review-overdue now.
- Time on Verigence: active, open-but-idle, background minutes; first and last
  activity; hour-by-hour active minutes.

Computed on read from indexed range queries; no batch job.

## 9. Time on Verigence (client-side)

- The browser counts the day in 1-minute buckets: **active** = a click, tap,
  key or scroll in that minute with the app visible; **idle** = app visible,
  no interaction for 4 minutes or more; **background** = tab hidden or app
  minimised. Between the last interaction and the 4-minute mark the minute
  still counts as active (reading).
- Several tabs share one tally through `localStorage`, so minutes are never
  double-counted.
- About every 15 minutes, and when the page is hidden or closed, the browser
  sends one small upsert (`POST /p2/v1/tenants/{t}/activity`, `fetch` with
  `keepalive`) with the day's cumulative totals for the selected Project.
- The server keeps one row per person, Project, IST day and client kind
  (`p2_user_activity_daily`) and only ever raises counters (`GREATEST`),
  so a repeated or late send cannot inflate anything.
- Nothing personal is captured: no keystrokes, page text or screenshots —
  counts, first/last activity, the hour strip and the display name.
- Limits stated on the dashboard: it measures interaction, not work; minutes
  since the last send are lost if the browser is killed (undercount, never
  overcount); a person using two browsers the same day shows the larger one.
- No notice is shown at sign-in (product owner, 2026-10-26).

## 10. Schema (migration 0164_task_manager)

`p2_tasks` (additive):
`scope_kind` (default `JOURNEY`), `dealer_id`, `outlet_id`, `journey_id` nullable
with a scope check, `expected_close_minutes`, `reviewer_role_code`,
`reviewer_actor_id`, `review_due_at_utc`, `action_completed_at_utc`,
`completed_by_actor_id`, `schedule_id`, `occurrence_date`, `missed_at_utc`,
`raised_by_display_name`, `assigned_display_name`;
`completion_protocol` also allows `SELF_CLOSING` and `SYSTEM_VERIFIED`.
`p2_task_events.journey_id` nullable. The `trg_p2_tasks_notify` function skips
rows without a Journey.

New: `p2_task_schedules`, `p2_user_activity_daily` (both RLS by tenant).

## 11. API

Project users (`/p2/v1/tenants/{tenant_id}`), Security permission
`audit.journey.read` + the role matrix above from business assignments:

| Method | Path | |
|---|---|---|
| GET | `/task-manager/context` | caller role, who they may assign to, people |
| GET | `/task-manager/tasks` | `view=mine|raised|review|all`, `status=open|done|all` |
| POST | `/task-manager/tasks` | raise (one task per assignee) |
| GET | `/task-manager/tasks/{id}` | task + events |
| POST | `/task-manager/tasks/{id}/actions` | START, COMPLETE, ACCEPT, REJECT, ADD_COMMENT, REASSIGN, CANCEL |
| GET/POST | `/task-manager/schedules` | Executive only |
| PATCH | `/task-manager/schedules/{id}` | Executive only |
| POST | `/task-manager/schedules/{id}/skip` | owner / Executive |
| GET | `/reports/task-status?day=` | TL, PM, Executive (also inside `/reports/daily-processing`) |
| GET | `/reports/employee-utilisation?from=&to=` | Executive |
| POST | `/activity` | any role, own row only |

Superadmin (`/v1/admin/tenants/{tenant_id}/task-manager/...`, Security
attests super admin on every request): the same task, schedule, task-status
and employee-utilisation operations for the selected Project.
