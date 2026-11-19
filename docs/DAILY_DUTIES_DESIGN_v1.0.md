# Daily Duties — Design v1.0

Product owner decisions of 2026-11-18 (evening, IST). Built the same night as
version 1: `daily_duties.py`, `daily_duty_checkers.py`, `daily_duties_api.py`,
migration `0242_daily_duties`, `tests/test_daily_duties.py`; in the web, the
Attendance screen (after check-in, at check-out), the Project Tracker and the duty
list for Executives / SuperAdmin.

Read with `TASK_MANAGER_DESIGN_v1.0.md` (Task Manager, Daily Gate Pass) and the gate
register rules in `uc03_p2_gate_pass.py`.

## 1. What it is for

Each PC has 5–8 duties a day; the number may change. The PC must:

- see which duties are pending, and do them in any order;
- by day end, have every mandatory duty done **or give a reason** for each one
  that is not;
- find anything not done on the next day's list.

The TL sees the progress of every PC. The list of duties may grow or shrink,
and a duty may become mandatory or optional.

## 2. The rules the design serves

**Simple.** "A simple checkbox type ... to start off with": a duty is a line with
a tick. Some duties ask for numbers with the tick.

**Asked or read.** "PC needs to be asked first if the task is completed like cash
register and all, for others like physical deliveries it can be read directly from
the system." A duty is either ticked by the PC (`TICK`) or by the system (`AUTO`,
through a checker, section 4).

**Loosely coupled.** Daily Duties is its own module with its own tables
(`p2_duty_*`). It only *reads* the existing flows (gate register, uploads, vehicle
photos, bank statements). Those modules are not changed and do not know duties
exist. Duty rows are **not** stored in `p2_tasks`: that would inflate "tasks
closed" in My Tracker, the Project Tracker and Performance, and would repeat the
pattern where gate register logic sits inside `task_manager.py`.

**Switchable at any time.** One switch per Project (off by default), plus active /
inactive per duty.

| Switch | Effect |
|---|---|
| Off | Every read answers `enabled: false`; nothing is created; no list, no day close, no team view. Every other part of Verigence works exactly as before. History is kept. |
| On again | Starts from that day. Days while it was off are not created and not counted. Items open at switch-off were closed as "switched off". |

**Fails safe.** A checker that fails leaves its items as they are ("could not
check") and never fails the request. The web never holds a check-in or check-out
up for a duties call that fails.

**Nothing in the worker.** Items are created when someone opens a list (catching
up any days nobody opened), and "incomplete" and "not closed" are read from the
dates. There is no job to schedule and nothing that runs at night.

## 3. Duty list (per Project)

Kept only by an **Executive or SuperAdmin**. Team Leads and PMs can read it.

| Setting | Meaning |
|---|---|
| Name, description | What the PC sees |
| Mandatory / optional | A reason is asked only for mandatory duties |
| Days | The days of the week the duty is for |
| Outlets | All, onsite only, satellite only, or chosen outlets |
| Day it is for | Same day (N) or previous day (N-1) |
| How it closes | The PC ticks it, or the system does (which checker) |
| Values | Up to 6 values the tick asks for: whole number, amount or text; required or not |
| "Not received today" | For duties that depend on the dealership |
| Rolling | One open item at a time (bank statement); the next starts the day after it closes |
| Carry days | Project default (5), or the duty's own, 0–30 |
| Order, active / inactive | |

"Add the standard duties" adds the six named on 18 Nov 2026, those not there yet;
each can then be changed like any other.

A change shows on the days not yet started: an item already on someone's list is a
copy of the duty as it was that day. Making a duty inactive takes its open items
off every list.

Project settings: the switch, the cut-off time (21:00), the default carry days
(5), the PC's working days (Monday to Saturday).

## 4. Checkers (`daily_duty_checkers.py`)

A checker answers one question for a batch of items, each an (outlet, day): is the
work done? It only reads, and reads once for the whole batch.

| Answer | Meaning |
|---|---|
| `DONE` | The work is there (when, by whom) |
| `PENDING` | Not yet; a note says what is missing ("Photos on 3 of 5 deliveries") |
| `NOTHING` | The system has nothing to judge by for that day; the PC confirms with a tick |

| Checker | Reads | Final |
|---|---|---|
| `GATE_REGISTER` | The outlet's ACTIVE gate out register of the day with its counts entered | yes |
| `BANK_STATEMENT` | A statement of the outlet or of its dealership uploaded on or after the item's day | yes |
| `VEHICLE_PHOTOS` | Every delivery of the day has a vehicle photo. Deliveries of a day: Journeys whose vehicle gate pass is dated that day (the gate register's rule) and Journeys photographed that day | no |
| `UPLOADS` | Journeys uploaded to at the outlet that day (My Tracker's rule; direct, bulk and Drive) against the counts of the "files received" duty of the same outlet and day | no |
| `OPEN_TASKS` | The PC tasks on the outlet's Journeys (the Tasks screen): a PC acted on at least one that day. Actions and closings are the Project Tracker's own lists. The note carries the numbers ("Worked on 14 tasks, 9 closed, 230 still open") | no |

**Final or not.** An uploaded register stays uploaded, so the system ticks at
once. "Every delivery of today has photos" is only true *so far* while the day
runs: the item shows **On track**, needs no reason at day close, and is ticked for
good on the first read after its day is over, dated by the last photo or upload.
So a duty the system ticked never comes back.

## 5. The duties named so far

| Duty | For day | Closes by | If not done |
|---|---|---|---|
| Gate out register | N-1, every day | System (`GATE_REGISTER`) | Carried |
| Booking and delivery files received | N | PC: two numbers, 0 allowed | Carried |
| Upload bookings and deliveries | N | System (`UPLOADS`) | Carried |
| Physical delivery verification | N | System (`VEHICLE_PHOTOS`); PC confirms a day with no deliveries | Carried, "Photos on 3 of 5 deliveries" |
| Bank statement | when given | System (`BANK_STATEMENT`), or "Not received today" | One open item rolls forward |
| Cash ledger | N-1 | PC: amount and number of transactions, or "Not received today" | That day's ledger is carried and asked again |
| Work on open tasks | N | System (`OPEN_TASKS`); PC confirms an outlet with no open task | Not carried: a reason at day close, the day is Incomplete |

- **Gate register**: captured once, for N-1. Its flow, screens and upload windows
  are unchanged. The Task Manager's Daily Gate Pass task is **retired** (product
  owner, 2026-11-19: "i dont want to show gatepass twice... we will use now the
  daily tracker with this new tasks only"): it is no longer raised, its system
  schedule is archived and the open tasks were withdrawn (CANCELLED, outcome
  RETIRED). Finished and incomplete ones stay as history. The gate register figures
  of the daily report, My Tracker, Project Tracker and Performance read the
  register itself (`p2_gate_passes`), not the task, and are unchanged.
  `AUDIT_CORE_GATE_PASS_DAILY_TASK=on` raises the task again.
- **Work on open tasks** (product owner, 2026-11-19: "one duty of working on the
  Tasks which has been accumulated or raised as we upload the files"): read from
  the tasks. A day counts as worked when a PC of the outlet acted on at least one
  PC task of the outlet's Journeys; there is no daily target number yet, the
  numbers are shown instead. Not counted: what the system closes by itself (a
  missing document task that closes when the document is uploaded from the booking
  screen) and what a Team Lead or Reviewer does. Not carried forward (carry days
  0): a day's work on the tasks cannot be made up the next day.
- **Physical delivery verification**: for the deliveries happening on that day.
  Photos are the booking's existing Vehicle photos; a typed VIN does not count.
- **Bank statement**: timing depends on the dealership. One PC picks it up: the
  first upload closes it for every PC of the outlet.
- **Cash ledger** ("till N-1"): the checklist of day N asks for the ledger of N-1.
- **Not received today** (cash ledger, bank statement): the PC is asked **whether
  they have informed the relevant WhatsApp group** and must answer yes or no. The
  item stays open and is asked again the next day.

## 6. The PC's day

Clubbed with attendance (product owner: "club this with the check in and check
out", "they should be able to see the list project wise", "post check in").

- **After check-in** the Attendance screen shows today's list, Project by
  Project and outlet by outlet: today's duties, what is carried from earlier days
  (with its age and days left), and what was finished today.
- The PC ticks through the day, in any order. A tick can be taken back by the
  person who made it, on the same day.
- **At check-out** every mandatory duty not done needs a reason; that is the day
  close. A duty marked "not received today", or on track, needs none.

| Reason | Side |
|---|---|
| Dealership did not give it | Dealership |
| Booking is not in Verigence | Dealership |
| Another PC is doing it (a PC of the same outlet is chosen) | PC |
| System issue | System |
| On leave or holiday | — |
| Other (a few words needed) | PC |

- A PC who finishes something later the same day can still tick it.
- **Cut-off** (21:00 IST, configurable): from then on a PC who has not closed
  shows as "not closed" for the day in the team's view. A later close is still
  taken.
- **Outlet with several PCs**: items are per outlet; any PC of the outlet can
  complete one. Day close is per PC.

## 7. Carry-forward

- Every item stays tied to the day it is for. The register of the 17th is a
  different register from the 18th, so items are never merged.
- An item shows on the PC's next working day when the day it would show is not a
  working day (Saturday's and Sunday's registers both come on Monday).
- An item not done stays on the list for its carry days, then leaves the PC's
  list and is **Incomplete**.
- A system-checked item whose work arrives after that (a register uploaded on day
  6) is still ticked by the system, as done late.

## 8. The team's view

Team Lead (the outlets under them), PM, Executive, SuperAdmin; up to 31 days.

- Per day and outlet: every item on the list that day with its state — done (who,
  when, the values), done late, on track, pending, not received (with the WhatsApp
  group answer), incomplete — and the reasons given that day.
- Per PC: day closed (when), not closed, still open, or not a working day.
- Totals, per duty, and per PC (days closed / not closed).
- **Reasons are see-only** in this version: no accept / reject.

## 9. Data (migration 0242)

All per Project, RLS by tenant; the runtime role cannot delete.

| Table | Holds |
|---|---|
| `p2_duty_settings` | The switch, cut-off, carry days, working days, the last day made |
| `p2_duties` | The duty list |
| `p2_duty_items` | One row per duty, outlet and day it is for: a copy of the duty, the tick, the values. Unique on (duty, outlet, for date) |
| `p2_duty_reasons` | Why an item was not done on a day, per person; the WhatsApp group answer |
| `p2_duty_day_closes` | A PC's day close |
| `p2_duty_events` | Who did what, when |

## 10. API

Project users, `/p2/v1/tenants/{tenant_id}/duties`:

| Method | Path | Who |
|---|---|---|
| GET | `/my-day` | anyone (a list only for a PC) |
| POST | `/items/{id}/tick`, `/items/{id}/undo`, `/items/{id}/not-received` | PC of the outlet |
| POST | `/my-day/close` | PC |
| GET | `/team?from=&to=` | TL, PM, Executive |
| GET | `/definitions` | TL, PM, Executive |
| POST / PATCH | `/definitions`, `/definitions/{id}`, `/definitions/standard` | Executive |
| GET / PUT | `/settings` | anyone / Executive |

SuperAdmin: the same team view, definitions and settings under
`/v1/admin/tenants/{tenant_id}/duties/...` for the selected Project.

## 11. Not in version 1

- No daily target for "Work on open tasks" (a number of tasks, or nothing older
  than N days): one task worked on counts the day.
- Reasons are not accepted or rejected by the TL.
- No WhatsApp reminders; no leave feed from myHR (a PC on leave is a reason).
- No comparison of the cash ledger with cash receipts.
- Performance view: the "Cash register" placeholder is not yet fed from duties.

## 12. Decisions on record (product owner, 2026-11-18)

- Integrate with existing flows, not tightly coupled; switchable on/off at any time.
- Fully flexible and easy to use; a simple checkbox type to start with.
- The PC is asked for duties like the cash register; others, like physical
  deliveries, are read directly from the system.
- Not received (cash register, bank statement): ask the PC whether they intimated
  the relevant WhatsApp group.
- Clubbed with check-in and check-out; the list Project wise, after check-in.
- Physical delivery verification = photographs of the deliveries, for the
  deliveries happening on that day.
- Cash ledger (not customer ledger): amount and number of transactions; till N-1.
- Gate register is captured once, for N-1.
- Bank statement: timing depends on the dealership; picked by one PC.
- Reasons: see-only for the TL to start with.
- Carry-forward: configurable, starting at 5 days.
- Duty list: changed only by Executive and SuperAdmin.

Product owner, 2026-11-19:

- The gate pass is not shown twice: the Daily Gate Pass task and its system
  schedule are retired; the daily duties carry the gate register.
- One more duty: working on the tasks accumulated or raised as files are uploaded.
