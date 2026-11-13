"""The dashboard of each role (product owner, 13 Nov 2026: "bring me the dashboards with control
to superadmin what can be shown and what can't be for PC/Reviewer/TL/PM/Exec").

One screen per role, thought from the seat of the person reading it:

A PC          what I did (bookings and deliveries uploaded, delivery files closed, tasks closed),
              what is waiting for me and for how long, my daily and weekly duties day by day, how I
              stand against my peers (not named), how clean my uploads are and how I upload
A Reviewer    what I reviewed and closed, what waits for review at my outlets and for how long,
              my closures waiting for the TL / PM, against my peers
A TL, PM or   the team: what was done, what is pending and how old, what needs attention in words,
Executive     bookings and deliveries against the gate register, tasks raised and closed, and a
              scorecard of each PC, Reviewer (for a PM the Team Leads, for an Executive the PMs),
              outlet and dealership; duties, quality and how files come in

Each block is a widget a SuperAdmin shows or hides per role (WIDGETS); a measure switched off for
the role leaves its tile and its scorecard column out. Nothing here reads the database: it shapes
the parts already read and cached for the Performance tabs, so the dashboard costs what one tab
of each kind costs, once every two minutes per Project and period.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from audit_core.uc03_p2_team_insights import BUCKETS, PENDING_KINDS

WIDGETS: tuple[dict[str, str], ...] = (
    {"key": "w.kpis", "label": "Dashboard: work done (headline figures)"},
    {"key": "w.pending", "label": "Dashboard: pending now, by age"},
    {"key": "w.attention", "label": "Dashboard: needs attention"},
    {"key": "w.trend", "label": "Dashboard: day by day / week by week chart"},
    {"key": "w.tasks", "label": "Dashboard: tasks new, closed and open"},
    {"key": "w.team", "label": "Dashboard: scorecards (people, outlets, dealerships)"},
    {"key": "w.duties", "label": "Dashboard: daily and weekly duties"},
    {"key": "w.quality", "label": "Dashboard: quality and duplicates"},
    {"key": "w.channels", "label": "Dashboard: how files are uploaded"},
)
_KIND_LABEL = {k["key"]: k["label"] for k in PENDING_KINDS}
_PC_KINDS = ("deliveriesNotUploaded", "deliveryFilesOpen", "missingDocuments", "tasksOpen", "tasksOverdue",
             "pagesToReupload", "paymentsNotReconciled")
_TEAM_KINDS = (*_PC_KINDS, "awaitingReview", "awaitingDecision")
# A kind counted inside another (an overdue task is an open task): shown under it, not added to it.
_WITHIN = {"missingDocuments": "deliveryFilesOpen", "tasksOverdue": "tasksOpen"}
_ZERO = [0, 0, 0, 0]
_QUALITY_REASONS = (("blurred", "Blurred"), ("faded", "Faded"), ("tooSmall", "Too small"), ("otherQuality", "Other"))


def _short(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def _dates(days: list[str], limit: int = 4) -> str:
    shown = ", ".join(_short(d) for d in days[-limit:])
    return f"{shown} and {len(days) - limit} earlier" if len(days) > limit else shown


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _add_counts(all_counts: list[dict[str, list[int]]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for counts in all_counts:
        for kind, buckets in counts.items():
            into = out.setdefault(kind, [0, 0, 0, 0])
            for i, n in enumerate(buckets):
                into[i] += n
    return out


def _pct(part: float, whole: float) -> int | None:
    return round(100 * part / whole) if whole else None


def _n(counts: dict[str, list[int]], kind: str) -> int:
    return sum(counts.get(kind, _ZERO))


def _old(counts: dict[str, list[int]], kind: str) -> int:
    b = counts.get(kind, _ZERO)
    return b[2] + b[3]


def build(part: Any, data: dict[str, Any], *, view: str, actor_id: str, visible: list[str], names: dict[str, Any],
          people: dict[str, Any], outlets: list[dict[str, Any]], iso: list[str], usual_days: list[str],
          hidden: set[str], rank: int, peers: dict[str, Any], planned: list[dict[str, Any]]) -> dict[str, Any]:
    """``view``: PC, REVIEWER or TEAM. ``outlets``: the outlets the reader covers. ``visible``: the
    people the reader may read by name (a PC or Reviewer: themselves). ``rank``: the reader's role
    rank (a PM and above also reads the Team Leads, an Executive the PMs)."""
    values, duties, by_outlet = data["values"], data["duties"], data["by_outlet"]
    last = iso[-1]
    period = _short(iso[0]) if len(iso) == 1 else f"{_short(iso[0])} to {_short(last)}"
    backlog = part("backlog")
    outlet_ids = [str(o["outlet_id"]) for o in outlets]
    outlet_name = {str(o["outlet_id"]): o["outlet_name"] for o in outlets}

    def shown(widget: str) -> bool:
        return f"w.{widget}" not in hidden

    def measure(key: str) -> bool:
        return key not in hidden

    def total(source: dict[str, Any], key: str, days: list[str] | None = None) -> float:
        cell = source.get(key, {})
        return sum(cell.get(d, 0) for d in (days if days is not None else iso))

    def series(source: dict[str, Any], key: str) -> list[float]:
        cell = source.get(key, {})
        return [cell.get(d, 0) for d in iso]

    def name_of(actor: str) -> str:
        return names.get(actor) or "Name not recorded"

    def kpi(key: str, label: str, value: Any, hint: str = "", tone: str | None = None) -> dict[str, Any]:
        return {"key": key, "label": label, "value": value, "hint": hint, "tone": tone}

    def pending_block(title: str, counts: dict[str, list[int]], kinds: tuple[str, ...],
                      labels: dict[str, str] | None = None) -> dict[str, Any]:
        return {"title": title, "buckets": list(BUCKETS), "kinds": [
            {"key": k, "label": (labels or {}).get(k, _KIND_LABEL[k]), "total": _n(counts, k),
             "buckets": list(counts.get(k, _ZERO)), "within": _WITHIN.get(k)} for k in kinds]}

    def duty_block(actors: list[str], *, strip_names: bool) -> dict[str, Any]:
        """Each duty of the Task Manager: done on time, late, missed; and every day of the period."""
        rows = []
        for schedule in data["schedules"]:
            sid = schedule["scheduleId"]
            tot = {"due": 0, "onTime": 0, "late": 0, "notDone": 0, "pending": 0}
            days: dict[str, dict[str, int]] = {}
            missed_by: dict[str, list[str]] = {}
            for a in actors:
                for d, cell in duties.get(a, {}).get(sid, {}).items():
                    into = days.setdefault(d, {"due": 0, "onTime": 0, "late": 0, "notDone": 0, "pending": 0})
                    for k in tot:
                        tot[k] += cell[k]
                        into[k] += cell[k]
                    if cell["notDone"]:
                        missed_by.setdefault(a, []).append(d)
            if not tot["due"]:
                continue
            rows.append({
                "title": schedule["title"], "frequency": schedule["frequency"], **tot,
                "days": [{"date": d, **days[d]} if d in days else {"date": d, "due": 0} for d in iso],
                "missedBy": [] if strip_names else sorted(
                    ({"name": name_of(a), "dates": sorted(ds)} for a, ds in missed_by.items()),
                    key=lambda r: -len(r["dates"]))[:8],
            })
        return {"rows": rows, "planned": [{"title": p["title"]} for p in planned]}

    def missed_dates(actor: str) -> list[str]:
        return sorted({d for by_day in duties.get(actor, {}).values() for d, cell in by_day.items() if cell["notDone"]})

    def quality_block(q: dict[str, int], pages: float, to_reupload: int = 0) -> dict[str, Any]:
        """Failures and duplicates of the uploads of the period; ``to_reupload``: pages that failed
        for good and still wait to be uploaded again (now, whatever the period)."""
        rejected = sum(q.get(k, 0) for k, _ in _QUALITY_REASONS)
        return {"pagesUploaded": int(pages), "rejected": rejected, "rightFirstTime": _pct(pages - rejected, pages),
                "toReupload": to_reupload,
                "reasons": [{"key": k, "label": label, "value": q.get(k, 0)} for k, label in _QUALITY_REASONS],
                "duplicatePages": q.get("duplicatePages", 0), "duplicateFiles": q.get("duplicateFiles", 0)}

    def sum_outlets(source: dict[str, Any]) -> dict[str, int]:
        q: dict[str, int] = {}
        for o in outlet_ids:
            for k, v in source.get(o, {}).items():
                q[k] = q.get(k, 0) + v
        return q

    def channel_block(source: dict[str, Any]) -> dict[str, int]:
        return {c: int(sum(source.get(c, {}).get(d, 0) for d in iso)) for c in ("direct", "bulk", "drive")}

    def usual(actor: str, key: str) -> float | None:
        if not usual_days:
            return None
        return round(total(values.get(actor, {}), key, usual_days) / len(usual_days), 1)

    def peer_block(role: str, lines: tuple[tuple[str, str], ...]) -> dict[str, Any] | None:
        """Me against the middle of my peers, a working day; nobody is named."""
        group = peers.get(role) or {}
        if "peerComparison" in hidden or not usual_days or (group.get("people") or 0) < 2:
            return None
        rows = [{"key": k, "label": label, "me": usual(actor_id, k), "peers": (group.get("usualMedian") or {}).get(k)}
                for k, label in lines if measure(k)]
        rows = [r for r in rows if r["peers"] is not None]
        return {"title": "Me and my peers, a working day", "people": group["people"], "rows": rows} if rows else None

    out: dict[str, Any] = {"view": view, "period": period, "days": iso,
                           "widgets": [w["key"][2:] for w in WIDGETS if w["key"] not in hidden]}

    # ------------------------------------------------------------------ a PC: my own work
    if view == "PC":
        mine = values.get(actor_id, {})
        flow = part("flow")["byPerson"].get(actor_id, {})
        counts = backlog["byPerson"].get(actor_id, {})
        bookings = [flow.get(d, {}).get("bookings", 0) for d in iso]
        deliveries = [flow.get(d, {}).get("deliveries", 0) for d in iso]
        q = part("quality")["byPerson"].get(actor_id, {})
        pages = total(mine, "pagesUploaded")
        out["title"] = "My dashboard"
        if shown("kpis"):
            quality = quality_block(q, pages, _n(counts, "pagesToReupload"))
            tiles = []
            if measure("journeysUploaded"):
                tiles += [kpi("bookings", "Bookings uploaded", sum(bookings), f"{bookings[-1]} on {_short(last)}"),
                          kpi("deliveries", "Deliveries uploaded", sum(deliveries), f"{deliveries[-1]} on {_short(last)}")]
            if measure("journeysClosed"):
                tiles.append(kpi("filesClosed", "Delivery files closed", int(total(mine, "journeysClosed")),
                                 f"{int(total(mine, 'journeysClosed', [last]))} on {_short(last)}"))
            if measure("tasksClosed"):
                tiles.append(kpi("tasksClosed", "Tasks closed", int(total(mine, "tasksClosed")),
                                 f"{int(total(mine, 'tasksClosed', [last]))} on {_short(last)}"))
            if measure("pagesRejected") and quality["rightFirstTime"] is not None:
                tiles.append(kpi("firstTime", "Pages right first time", f"{quality['rightFirstTime']}%",
                                 f"{quality['rejected']} of {int(pages)} pages rejected",
                                 "warn" if quality["rightFirstTime"] < 90 else "good"))
            out["kpis"] = {"title": f"What I did, {period}", "tiles": tiles}
        if shown("pending"):
            out["pending"] = pending_block("Waiting for me now", counts, _PC_KINDS, {"tasksOpen": "Open tasks"})
        if shown("trend"):
            out["trend"] = {"title": "My work", "series": [
                {"name": "Bookings uploaded", "data": bookings}, {"name": "Deliveries uploaded", "data": deliveries},
                {"name": "Delivery files closed", "data": series(mine, "journeysClosed")}]}
        if shown("tasks"):
            raised = part("tasks")["raisedByPerson"].get(actor_id, {})
            out["tasks"] = {"raised": [raised.get(d, 0) for d in iso], "closed": series(mine, "tasksClosed"),
                            "open": _n(counts, "tasksOpen"), "overdue": _n(counts, "tasksOverdue")}
        if shown("duties"):
            out["duties"] = duty_block([actor_id], strip_names=True)
        if shown("quality"):
            out["quality"] = quality_block(q, pages, _n(counts, "pagesToReupload"))
        if shown("channels"):
            out["channels"] = channel_block(part("channels")["byPerson"].get(actor_id, {}))
        if shown("team"):
            block = peer_block("PC", (("journeysUploaded", "Journeys uploaded"), ("journeysClosed", "Delivery files closed"),
                                      ("tasksClosed", "Tasks closed"), ("pagesRejected", "Pages rejected")))
            if block:
                out["peers"] = block
        return out

    # ------------------------------------------------------------------ a Reviewer: my reviews
    if view == "REVIEWER":
        mine = values.get(actor_id, {})
        own = backlog["byPerson"].get(actor_id, {})
        at_outlets = _add_counts([backlog["byOutlet"].get(o, {}) for o in outlet_ids])
        counts = {"awaitingReview": at_outlets.get("awaitingReview", list(_ZERO)),
                  "awaitingDecision": own.get("awaitingDecision", list(_ZERO))}
        out["title"] = "My dashboard"
        if shown("kpis"):
            tiles = []
            if measure("reviews"):
                tiles.append(kpi("reviews", "Files reviewed", int(total(mine, "reviews")),
                                 f"{int(total(mine, 'reviews', [last]))} on {_short(last)}"))
            if measure("journeysClosed"):
                tiles.append(kpi("closed", "Journeys closed", int(total(mine, "journeysClosed")),
                                 f"{int(total(mine, 'journeysClosed', [last]))} on {_short(last)}"))
            if measure("tasksClosed"):
                tiles.append(kpi("tasksClosed", "Tasks closed", int(total(mine, "tasksClosed")),
                                 f"{int(total(mine, 'tasksClosed', [last]))} on {_short(last)}"))
            out["kpis"] = {"title": f"What I did, {period}", "tiles": tiles}
        if shown("pending"):
            out["pending"] = pending_block("Waiting now", counts, ("awaitingReview", "awaitingDecision"), {
                "awaitingReview": "Waiting for review at my outlets", "awaitingDecision": "My closures with the TL / PM"})
        if shown("trend"):
            out["trend"] = {"title": "My reviews", "series": [
                {"name": "Files reviewed", "data": series(mine, "reviews")},
                {"name": "Journeys closed", "data": series(mine, "journeysClosed")}]}
        if shown("team"):
            rows = [{"id": o, "name": outlet_name[o], "sub": "",
                     "values": {"waiting": _n(backlog["byOutlet"].get(o, {}), "awaitingReview"),
                                "old": _old(backlog["byOutlet"].get(o, {}), "awaitingReview"),
                                "reviewed": int(total(by_outlet.get(o, {}), "reviews"))}} for o in outlet_ids]
            rows = [r for r in rows if r["values"]["waiting"] or r["values"]["reviewed"]]
            if rows:
                out["scorecards"] = [{"key": "outlets", "title": "My outlets", "sort": "waiting", "columns": [
                    {"key": "waiting", "label": "Waiting for review", "kind": "bar"},
                    {"key": "old", "label": "Older than 3 days", "kind": "alert"},
                    {"key": "reviewed", "label": "Reviewed in the period", "kind": "num"}], "rows": rows}]
            block = peer_block("REVIEWER", (("reviews", "Files reviewed"), ("journeysClosed", "Journeys closed"),
                                            ("tasksClosed", "Tasks closed")))
            if block:
                out["peers"] = block
        if shown("duties"):
            block = duty_block([actor_id], strip_names=True)
            if block["rows"]:  # the duties planned and not set up yet are a PC's
                out["duties"] = {**block, "planned": []}
        # What comes in at the outlets I review: how it is uploaded, failures and duplicates.
        if shown("quality"):
            out["quality"] = quality_block(sum_outlets(part("quality")["byOutlet"]),
                                           sum(total(by_outlet.get(o, {}), "pagesUploaded") for o in outlet_ids),
                                           _n(at_outlets, "pagesToReupload"))
        if shown("channels"):
            ch = part("channels")["byOutlet"]
            out["channels"] = {c: int(sum(ch.get(o, {}).get(c, {}).get(d, 0) for o in outlet_ids for d in iso))
                               for c in ("direct", "bulk", "drive")}
        return out

    # ------------------------------------------------------------------ a TL, PM or Executive: the team
    flow = part("flow")["byOutlet"]
    person_flow = part("flow")["byPerson"]

    def flow_total(key: str, days: list[str] | None = None, only: list[str] | None = None) -> int:
        return int(sum(flow.get(o, {}).get(d, {}).get(key, 0)
                       for o in (only if only is not None else outlet_ids) for d in (days or iso)))

    def unit_total(key: str, only: list[str] | None = None, days: list[str] | None = None) -> int:
        return int(sum(total(by_outlet.get(o, {}), key, days) for o in (only if only is not None else outlet_ids)))

    counts = _add_counts([backlog["byOutlet"].get(o, {}) for o in outlet_ids])
    by_role: dict[str, list[str]] = {}
    for a in visible:
        if a != actor_id:
            by_role.setdefault(people[a]["role"], []).append(a)
    pcs = by_role.get("PC", [])
    out["title"] = "Team dashboard"

    if shown("kpis"):
        tiles = []
        if measure("journeysUploaded"):
            tiles += [
                kpi("bookings", "Bookings uploaded", flow_total("bookings"), f"{flow_total('bookings', [last])} on {_short(last)}"),
                kpi("deliveries", "Deliveries uploaded", flow_total("deliveries"),
                    f"{flow_total('onRegister')} on the gate registers"),
            ]
        if measure("journeysClosed"):
            tiles.append(kpi("filesClosed", "Delivery files closed", unit_total("deliveryFilesClosed"),
                             f"{unit_total('deliveryFilesClosed', days=[last])} on {_short(last)}"))
        if measure("reviews"):
            tiles.append(kpi("reviews", "Files reviewed", unit_total("reviews"), f"{unit_total('reviews', days=[last])} on {_short(last)}"))
        if measure("journeysClosed"):
            tiles.append(kpi("journeysClosed", "Journeys closed", unit_total("journeysClosed"), "by Reviewer, TL or PM"))
        if measure("tasksClosed"):
            raised = part("tasks")["raisedByOutlet"]
            new = int(sum(raised.get(o, {}).get(d, 0) for o in outlet_ids for d in iso))
            tiles.append(kpi("tasksClosed", "Tasks closed", unit_total("tasksClosed"), f"{new} new in the period"))
        missed_registers = flow_total("registerMissing")
        tiles.append(kpi("registers", "Gate registers missed", missed_registers, "outlet days with no register",
                         "bad" if missed_registers else "good"))
        out["kpis"] = {"title": f"Done, {period}", "tiles": tiles}

    if shown("pending"):
        out["pending"] = pending_block("Pending now", counts, _TEAM_KINDS)

    if shown("attention"):
        lines = []
        for o in outlets:
            oid = str(o["outlet_id"])
            missed = flow_total("registerMissing", only=[oid])
            if missed:
                lines.append({"tone": "bad", "rank": 0, "n": missed, "who": o["outlet_name"],
                              "text": f"Gate register not uploaded on {_plural(missed, 'day')}"})
            stale = _old(backlog["byOutlet"].get(oid, {}), "deliveriesNotUploaded")
            if stale:
                lines.append({"tone": "bad", "rank": 1, "n": stale, "who": o["outlet_name"],
                              "text": f"{_plural(stale, 'delivery', 'deliveries')} on the register not uploaded for over 3 days"})
            waiting = _old(backlog["byOutlet"].get(oid, {}), "awaitingReview")
            if waiting >= 5:
                lines.append({"tone": "warn", "rank": 5, "n": waiting, "who": o["outlet_name"],
                              "text": f"{waiting} bookings / deliveries waiting over 3 days for review"})
        for a in pcs:
            missed_on = missed_dates(a)
            if missed_on:
                lines.append({"tone": "bad", "rank": 2, "n": len(missed_on), "who": name_of(a),
                              "text": f"Duty missed on {_dates(missed_on)}"})
            mine_counts = backlog["byPerson"].get(a, {})
            if _old(mine_counts, "tasksOverdue") >= 3:
                lines.append({"tone": "warn", "rank": 3, "n": _old(mine_counts, "tasksOverdue"), "who": name_of(a),
                              "text": f"{_old(mine_counts, 'tasksOverdue')} tasks overdue for over 3 days"})
            if _old(mine_counts, "deliveryFilesOpen") >= 5:
                lines.append({"tone": "warn", "rank": 4, "n": _old(mine_counts, "deliveryFilesOpen"), "who": name_of(a),
                              "text": f"{_old(mine_counts, 'deliveryFilesOpen')} delivery files not closed for over 3 days"})
            if date.fromisoformat(last).weekday() != 6 and len(iso) > 1 \
                    and total(values.get(a, {}), "filesUploaded", [last]) == 0 and total(values.get(a, {}), "filesUploaded") > 0:
                lines.append({"tone": "warn", "rank": 6, "n": 0, "who": name_of(a), "text": f"Nothing uploaded on {_short(last)}"})
        lines.sort(key=lambda x: (x["rank"], -x["n"]))
        out["attention"] = {"total": len(lines), "lines": [{"tone": x["tone"], "who": x["who"], "text": x["text"]}
                                                           for x in lines[:12]]}

    if shown("trend"):
        out["trend"] = {"title": "Bookings and deliveries", "series": [
            {"name": "Bookings uploaded", "data": [flow_total("bookings", [d]) for d in iso]},
            {"name": "Deliveries uploaded", "data": [flow_total("deliveries", [d]) for d in iso]},
            {"name": "Deliveries on gate register", "kind": "line", "data": [flow_total("onRegister", [d]) for d in iso]}]}

    if shown("tasks"):
        raised = part("tasks")["raisedByOutlet"]
        out["tasks"] = {"raised": [int(sum(raised.get(o, {}).get(d, 0) for o in outlet_ids)) for d in iso],
                        "closed": [unit_total("tasksClosed", days=[d]) for d in iso],
                        "open": _n(counts, "tasksOpen"), "overdue": _n(counts, "tasksOverdue")}

    if shown("team"):
        cards: list[dict[str, Any]] = []
        eff = part("efficiency")
        quality_by = part("quality")["byPerson"]
        dealer_outlets: dict[str, list[str]] = {}
        dealer_name: dict[str, str] = {}
        for o in outlets:
            dealer_outlets.setdefault(str(o["dealer_id"]), []).append(str(o["outlet_id"]))
            dealer_name[str(o["dealer_id"])] = o["dealer_name"]

        def where(a: str) -> list[str]:
            p = people[a]
            if p["wide"]:
                return outlet_ids
            ids = set(p["outlets"]) | {o for d in p["dealers"] for o in dealer_outlets.get(d, [])}
            return [o for o in outlet_ids if o in ids]

        def where_text(a: str) -> str:
            ids = where(a)
            if people[a]["wide"] or len(ids) == len(outlet_ids) > 3:
                return "All outlets"
            return ", ".join(outlet_name[o] for o in ids[:2]) + (f" +{len(ids) - 2}" if len(ids) > 2 else "")

        def columns(*cols: tuple[str, str, str, str | None]) -> list[dict[str, str]]:
            return [{"key": k, "label": label, "kind": kind} for k, label, kind, needs in cols if needs is None or measure(needs)]

        def active_hours(a: str) -> float:
            return round(total(values.get(a, {}), "activeMinutes") / 60, 1)

        if pcs:
            rows = []
            for a in pcs:
                v, c, e, q = values.get(a, {}), backlog["byPerson"].get(a, {}), eff.get(a, {}), quality_by.get(a, {})
                pages = total(v, "pagesUploaded")
                rejected = sum(q.get(k, 0) for k, _ in _QUALITY_REASONS)
                due = int(e.get("tasksWithDue", 0))
                rows.append({"id": a, "name": name_of(a), "sub": where_text(a), "spark": series(v, "journeysUploaded"), "values": {
                    "bookings": sum(person_flow.get(a, {}).get(d, {}).get("bookings", 0) for d in iso),
                    "deliveries": sum(person_flow.get(a, {}).get(d, {}).get("deliveries", 0) for d in iso),
                    "filesClosed": int(total(v, "journeysClosed")), "tasksClosed": int(total(v, "tasksClosed")),
                    "notUploaded": _n(c, "deliveriesNotUploaded"), "filesOpen": _n(c, "deliveryFilesOpen"),
                    "overdue": _n(c, "tasksOverdue"), "dutiesMissed": len(missed_dates(a)),
                    "firstTime": _pct(pages - rejected, pages),
                    "inTime": _pct(int(e.get("tasksInTime", 0)), due), "hours": active_hours(a)}})
            cards.append({"key": "pcs", "title": "PCs", "sort": "deliveries", "spark": "Journeys uploaded, day by day",
                          "columns": columns(
                              ("bookings", "Bookings uploaded", "bar", "journeysUploaded"),
                              ("deliveries", "Deliveries uploaded", "bar", "journeysUploaded"),
                              ("filesClosed", "Delivery files closed", "num", "journeysClosed"),
                              ("tasksClosed", "Tasks closed", "num", "tasksClosed"),
                              ("notUploaded", "Deliveries not uploaded", "alert", None),
                              ("filesOpen", "Files not closed", "warn", None),
                              ("overdue", "Overdue tasks", "alert", None),
                              ("dutiesMissed", "Duty days missed", "alert", "duties"),
                              ("firstTime", "Pages right first time", "pct", "pagesRejected"),
                              ("inTime", "Tasks closed in time", "pct", "tasksClosed"),
                              ("hours", "Active hours", "num", "activeMinutes")), "rows": rows})

        def lead_card(key: str, title: str, actors: list[str], spark_label: str) -> None:
            rows = []
            for a in actors:
                v = values.get(a, {})
                theirs = _add_counts([backlog["byOutlet"].get(o, {}) for o in where(a)])
                rows.append({"id": a, "name": name_of(a), "sub": where_text(a), "spark": series(v, "reviews"), "values": {
                    "reviews": int(total(v, "reviews")), "closed": int(total(v, "journeysClosed")),
                    "tasksClosed": int(total(v, "tasksClosed")),
                    "awaitingReview": _n(theirs, "awaitingReview"), "awaitingReviewOld": _old(theirs, "awaitingReview"),
                    "awaitingDecision": _n(theirs, "awaitingDecision"),
                    "withLead": _n(backlog["byPerson"].get(a, {}), "awaitingDecision"), "hours": active_hours(a)}})
            if key == "reviewers":
                cols = columns(("reviews", "Files reviewed", "bar", "reviews"), ("closed", "Journeys closed", "bar", "journeysClosed"),
                               ("tasksClosed", "Tasks closed", "num", "tasksClosed"),
                               ("awaitingReview", "Waiting for review at their outlets", "warn", None),
                               ("awaitingReviewOld", "Older than 3 days", "alert", None),
                               ("withLead", "Their closures with TL / PM", "num", None),
                               ("hours", "Active hours", "num", "activeMinutes"))
            else:
                cols = columns(("reviews", "Reviews and decisions", "bar", "reviews"),
                               ("closed", "Journeys closed", "num", "journeysClosed"),
                               ("tasksClosed", "Tasks closed", "num", "tasksClosed"),
                               ("awaitingDecision", "Closures waiting for a decision", "alert", None),
                               ("awaitingReview", "Waiting for review at their outlets", "warn", None),
                               ("hours", "Active hours", "num", "activeMinutes"))
            cards.append({"key": key, "title": title, "sort": "reviews", "spark": spark_label, "columns": cols, "rows": rows})

        if by_role.get("REVIEWER"):
            lead_card("reviewers", "Reviewers", by_role["REVIEWER"], "Files reviewed, day by day")
        if rank > 2 and by_role.get("TL"):
            lead_card("leads", "Team Leads", by_role["TL"], "Reviews and decisions, day by day")
        if rank > 3 and by_role.get("PM"):
            lead_card("managers", "Project Managers", by_role["PM"], "Reviews and decisions, day by day")

        def unit_row(unit_id: str, name: str, sub: str, ids: list[str]) -> dict[str, Any]:
            c = _add_counts([backlog["byOutlet"].get(o, {}) for o in ids])
            return {"id": unit_id, "name": name, "sub": sub, "spark": [flow_total("deliveries", [d], ids) for d in iso], "values": {
                "bookings": flow_total("bookings", only=ids), "deliveries": flow_total("deliveries", only=ids),
                "onRegister": flow_total("onRegister", only=ids), "notNotified": flow_total("notNotified", only=ids),
                "notUploaded": _n(c, "deliveriesNotUploaded"), "filesOpen": _n(c, "deliveryFilesOpen"),
                "overdue": _n(c, "tasksOverdue"), "payments": _n(c, "paymentsNotReconciled"),
                "awaitingReview": _n(c, "awaitingReview"), "registersMissed": flow_total("registerMissing", only=ids)}}

        unit_columns = columns(
            ("bookings", "Bookings uploaded", "bar", "journeysUploaded"), ("deliveries", "Deliveries uploaded", "bar", "journeysUploaded"),
            ("onRegister", "On gate register", "num", None), ("notNotified", "Not intimated to PC", "warn", None),
            ("notUploaded", "Deliveries not uploaded", "alert", None), ("filesOpen", "Files not closed", "warn", None),
            ("overdue", "Overdue tasks", "alert", None), ("payments", "Payments not reconciled", "warn", None),
            ("awaitingReview", "Awaiting Reviewer", "num", None), ("registersMissed", "Registers missed", "alert", None))
        if outlets:
            cards.append({"key": "outlets", "title": "Outlets", "sort": "deliveries", "spark": "Deliveries uploaded, day by day",
                          "columns": unit_columns, "rows": [
                              unit_row(str(o["outlet_id"]), o["outlet_name"], o["dealer_name"], [str(o["outlet_id"])])
                              for o in outlets]})
        if len(dealer_outlets) > 1:
            cards.append({"key": "dealers", "title": "Dealerships", "sort": "deliveries", "spark": "Deliveries uploaded, day by day",
                          "columns": unit_columns, "rows": [
                              unit_row(d, dealer_name[d], _plural(len(ids), "outlet"), ids) for d, ids in dealer_outlets.items()]})
        out["scorecards"] = cards

    if shown("duties"):
        out["duties"] = duty_block([a for a in visible if a != actor_id], strip_names=False)
    if shown("quality"):
        out["quality"] = quality_block(sum_outlets(part("quality")["byOutlet"]), unit_total("pagesUploaded"),
                                       _n(counts, "pagesToReupload"))
    if shown("channels"):
        ch = part("channels")["byOutlet"]
        out["channels"] = {c: int(sum(ch.get(o, {}).get(c, {}).get(d, 0) for o in outlet_ids for d in iso))
                           for c in ("direct", "bulk", "drive")}
    return out
