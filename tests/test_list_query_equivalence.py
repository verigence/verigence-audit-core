"""The task list and journey counts queries rewritten for the Neon cost review
(13 Nov 2026) answer exactly as the queries they replaced.

- list_tasks now picks its 500 tasks before reading the Journey's customer, outlet,
  vehicle, closure and comment count: the rows must be the same, in the same order.
- journeys:counts with breakdown now reads the scope once (GROUPING SETS) instead of
  COUNTS_SQL plus BREAKDOWN_SQL three times: the counts and every breakdown row must
  be the same.
"""
from __future__ import annotations

import random
from datetime import date
from uuid import uuid4

from sqlalchemy import text
from test_uc03_create_booking import uc03_create_booking_setup  # noqa: F401  (fixture)
from test_uc03_p2_hero_counts import _seed, _task

from audit_core.db import set_tenant_context
from audit_core.task_closure_roles import roles_closable_by
from audit_core.uc03_p2_api import _P2_TASK_LIST_SQL, _P2_TASK_TOTALS_SQL
from audit_core.uc03_p2_journey_views import (
    BREAKDOWN_FIGURES,
    BREAKDOWN_SQL,
    COUNTS_SQL,
    COUNTS_WITH_BREAKDOWN_SQL,
    counts_from_row,
    scope_params,
    split_counts_with_breakdown,
)
from audit_core.uc03_reviewer_closure import closure_columns_sql

# The query list_tasks ran before 13 Nov 2026, word for word.
_OLD_TASK_LIST_SQL = f"""
            SELECT t.task_id, t.journey_id, t.root_task_id, t.parent_task_id,
                   t.round_number, t.task_type, t.category, t.origin_kind,
                   t.source_type, t.source_code, t.title, t.description, t.reference,
                   t.severity, t.priority, t.assigned_role_code, t.assigned_actor_id,
                   t.raised_by_actor_id, t.raised_by_role_code, t.allowed_actions,
                   t.completion_protocol, t.task_status, t.due_at_utc,
                   t.created_at_utc, t.updated_at_utc, t.verified_at_utc,
                   t.completion_result,
                   COALESCE(c.legal_name, c.display_name) AS customer_name,
                   o.outlet_name, jr.journey_reference,
                   {closure_columns_sql("jr")},
                   NULLIF(concat_ws(' · ', NULLIF(jp.model_name_snapshot,''),
                                           NULLIF(jp.variant_name_snapshot,'')), '') AS vehicle,
                   (SELECT COUNT(*) FROM auditcore.p2_task_events ev
                     WHERE ev.tenant_id=t.tenant_id AND ev.task_id=t.task_id
                       AND ev.comment IS NOT NULL) AS comment_count
            FROM auditcore.p2_tasks t
            JOIN auditcore.journeys jr ON jr.tenant_id=t.tenant_id AND jr.journey_id=t.journey_id
            LEFT JOIN auditcore.customers c ON c.tenant_id=jr.tenant_id AND c.customer_id=jr.customer_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id=jr.tenant_id AND o.dealer_id=jr.dealer_id AND o.outlet_id=jr.outlet_id
            LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=t.tenant_id AND jp.journey_id=t.journey_id
            WHERE t.tenant_id=:tenant_id
              AND (CAST(:journey_id AS uuid) IS NULL OR t.journey_id=CAST(:journey_id AS uuid))
              AND (CAST(:role AS varchar) IS NULL OR t.assigned_role_code=ANY(CAST(:roles AS varchar[]))
                   OR (t.origin_kind='HUMAN' AND t.raised_by_actor_id=:actor_id)
                   OR CAST(:view AS varchar)='worked')
              -- The Team Lead's tasks about the Reviewer's own work (the closure to accept, the
              -- Reviewer's corrections) are not on the Reviewer's queue.
              AND NOT (t.task_type IN ('CLOSURE_ACCEPTANCE', 'REVIEWER_CORRECTIONS_INFO')
                       AND upper(COALESCE(CAST(:role AS varchar), ''))='REVIEWER')
              AND (
                -- "Worked on" (product owner 2026-11-04): every task the caller acted on in the
                -- last 30 days, closed or awaiting closure, whatever its role.
                (CAST(:view AS varchar)='worked' AND EXISTS (
                    SELECT 1 FROM auditcore.p2_task_events we
                    WHERE we.tenant_id=t.tenant_id AND we.task_id=t.task_id AND we.actor_id=:actor_id
                      AND we.created_at_utc > now() - interval '30 days'))
                OR (CAST(:status AS varchar) IS NOT NULL AND t.task_status=CAST(:status AS varchar))
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='open'
                    AND t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER'))
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='done'
                    AND t.task_status='VERIFIED_COMPLETE'
                    AND t.verified_at_utc > now() - interval '14 days')
                OR (CAST(:status AS varchar) IS NULL AND CAST(:view AS varchar)='all')
              )
              AND EXISTS (
                SELECT 1
                FROM auditcore.journeys j
                JOIN auditcore.business_assignments ba
                  ON ba.tenant_id=j.tenant_id
                 AND ba.security_actor_id=:actor_id
                 AND ba.assignment_status='ACTIVE'
                 AND ba.effective_from <= now()
                 AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                 AND (
                   ba.dealer_id IS NULL
                   OR (
                     ba.dealer_id=j.dealer_id
                     AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)
                   )
                 )
                WHERE j.tenant_id=t.tenant_id AND j.journey_id=t.journey_id
              )
            ORDER BY t.due_at_utc NULLS LAST, t.created_at_utc
            LIMIT 500
"""

_STATUSES = ("READY", "READY", "IN_PROGRESS", "ACTION_COMPLETED", "VERIFIED_COMPLETE",
             "CANCELLED", "FAILED")
_ROLES = ("PC", "TL", "PM", "REVIEWER", "EXECUTIVE")


def _more_tasks(connection, setup, journeys, rng):
    """Tasks in every status and role, some raised by hand by the caller, some with
    comments, some verified recently or long ago, some acted on by the caller."""
    tenant = setup["tenant_id"]
    for journey_id in journeys:
        for _ in range(rng.randint(3, 8)):
            _task(connection, tenant, journey_id, rng.choice(_ROLES), status=rng.choice(_STATUSES))
    connection.execute(
        text("""
            UPDATE auditcore.p2_tasks
               SET due_at_utc = CASE WHEN random() < 0.2 THEN NULL
                                     ELSE now() + (random() * 10 - 5) * interval '1 day' END,
                   verified_at_utc = CASE WHEN task_status='VERIFIED_COMPLETE'
                                          THEN now() - (random() * 30) * interval '1 day' END,
                   origin_kind = CASE WHEN random() < 0.25 THEN 'HUMAN' ELSE origin_kind END,
                   raised_by_actor_id = CASE WHEN random() < 0.25 THEN :actor ELSE raised_by_actor_id END,
                   task_type = CASE WHEN random() < 0.1 THEN 'CLOSURE_ACCEPTANCE' ELSE task_type END
             WHERE tenant_id = :t
        """),
        {"t": tenant, "actor": setup["actor_id"]},
    )
    task_ids = connection.execute(
        text("SELECT task_id, journey_id FROM auditcore.p2_tasks WHERE tenant_id=:t"), {"t": tenant}
    ).all()
    for task_id, journey_id in task_ids:
        for _ in range(rng.randint(0, 3)):
            connection.execute(
                text("""
                    INSERT INTO auditcore.p2_task_events
                      (tenant_id, task_id, journey_id, event_type, actor_id, comment, created_at_utc)
                    VALUES (:t, :task, :j, 'COMMENTED', :actor, :comment,
                            now() - (:days) * interval '1 day')
                """),
                {"t": tenant, "task": task_id, "j": journey_id,
                 "actor": rng.choice([setup["actor_id"], "someone-else"]),
                 "comment": rng.choice([None, "Looked at it"]), "days": rng.randint(0, 45)},
            )


def _rows(connection, sql, params):
    return [dict(row) for row in connection.execute(text(sql), params).mappings().all()]


def test_task_list_query_answers_as_before(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    _client, _base, journeys = _seed(setup)
    rng = random.Random(20261113)
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _more_tasks(connection, setup, journeys, rng)
        compared = 0
        for role in (None, "PC", "TL", "REVIEWER", "PM"):
            for view in ("open", "done", "all", "worked"):
                for status in (None, "READY", "VERIFIED_COMPLETE"):
                    for journey_id in (None, journeys[2]):
                        params = {"tenant_id": setup["tenant_id"], "journey_id": journey_id,
                                  "status": status, "actor_id": setup["actor_id"], "view": view,
                                  "role": role, "roles": roles_closable_by(role)}
                        old = _rows(connection, _OLD_TASK_LIST_SQL, params)
                        new = _rows(connection, _P2_TASK_LIST_SQL, params)
                        # Same rows; the old order had no last tie-break, so ties are compared
                        # as sets and the new order must still be the old sort key.
                        assert sorted(map(repr, new)) == sorted(map(repr, old)), (role, view, status)
                        keys = [((r["due_at_utc"] is None), r["due_at_utc"] or 0, r["created_at_utc"])
                                for r in new]
                        assert [k[:1] for k in keys] == sorted(k[:1] for k in keys)
                        compared += len(new)
        assert compared > 200  # the cases above did return tasks


def _counts_the_old_way(connection, params):
    result = {"counts": counts_from_row(connection.execute(text(COUNTS_SQL), params).mappings().one())}

    def rows(key_columns, group_by):
        out = []
        for row in connection.execute(text(BREAKDOWN_SQL.format(key_columns=key_columns, group_by=group_by)),
                                      params).mappings().all():
            item = dict(row)
            for key in BREAKDOWN_FIGURES:
                item[key] = int(item[key] or 0)
            out.append(item)
        return out

    result["byDealer"] = [{**r, "dealer_id": str(r["dealer_id"])}
                          for r in rows("s.dealer_id, MAX(s.dealer_name) AS dealer_name", "s.dealer_id")]
    result["byOutlet"] = [{**r, "outlet_id": str(r["outlet_id"])}
                          for r in rows("s.outlet_id, MAX(s.outlet_name) AS outlet_name, "
                                        "MAX(s.outlet_code) AS outlet_code, "
                                        "MAX(CAST(s.dealer_id AS text)) AS dealer_id", "s.outlet_id")]
    result["byPc"] = rows("s.created_by_actor_id AS pc_actor_id, MAX(s.pc_name) AS pc_name",
                          "s.created_by_actor_id")
    return result


def test_counts_with_breakdown_answer_as_before(uc03_create_booking_setup):  # noqa: F811
    setup = uc03_create_booking_setup
    client, base, journeys = _seed(setup)
    tenant = setup["tenant_id"]
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, tenant)
        # A second Dealership and Outlet the caller sees, and journeys started by two PCs.
        dealer = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'Second Dealer') RETURNING dealer_id"),
            {"t": tenant, "c": f"EQ-{uuid4().hex[:6]}"}).scalar_one()
        outlet = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'Second Outlet') RETURNING outlet_id"),
            {"t": tenant, "d": dealer, "c": f"EQO-{uuid4().hex[:6]}"}).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, "
                 "business_role_code, dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": tenant, "a": setup["actor_id"], "d": dealer, "o": outlet})
        connection.execute(
            text("UPDATE auditcore.journeys SET dealer_id=:d, outlet_id=:o, created_by_actor_id='pc-two', "
                 "created_by_display_name='PC Two' WHERE tenant_id=:t AND journey_id IN (:a, :b)"),
            {"t": tenant, "d": dealer, "o": outlet, "a": journeys[0], "b": journeys[3]})
        _more_tasks(connection, setup, journeys, random.Random(7))
        # A stuck file on an open journey and on the closed one.
        for journey_id in (journeys[1], journeys[3]):
            connection.execute(
                text("INSERT INTO auditcore.p2_upload_batches (tenant_id, journey_id, original_filename, "
                     "size_bytes, original_object_key, uploaded_by_actor_id, batch_status) "
                     "VALUES (:t, :j, 'f.pdf', 1, :k, :a, 'FAILED')"),
                {"t": tenant, "j": journey_id, "k": f"k/{uuid4()}", "a": setup["actor_id"]})

        for month, basis in ((None, None), ("2026-11", None), ("2026-11", "business")):
            params = {"tenant_id": tenant, "actor_id": setup["actor_id"], **scope_params(search=None),
                      "month": month, "month_basis": basis, "date_floor": date(1900, 1, 1),
                      "timezone": "Asia/Kolkata"}
            old = _counts_the_old_way(connection, params)
            new = split_counts_with_breakdown(
                connection.execute(text(COUNTS_WITH_BREAKDOWN_SQL), params).mappings().all())
            assert new["counts"] == old["counts"]
            for part in ("byDealer", "byOutlet", "byPc"):
                assert sorted(map(repr, new[part])) == sorted(map(repr, old[part])), part
            assert len(new["byDealer"]) == 2 and len(new["byPc"]) == 2

    # And through the endpoint.
    body = client.get(f"{base}/journeys:counts", params={"breakdown": "true"}).json()
    assert body["counts"]["all"] == 5 and len(body["byOutlet"]) == 2


def test_breakdown_rows_keep_their_order():
    """Most needing attention first, then most open, as BREAKDOWN_SQL ordered them."""
    def row(part, key, attention, open_):
        base = {k: 0 for k in BREAKDOWN_FIGURES}
        return {"part": part, "dealer_id": key, "dealer_name": key, "outlet_id": None, "outlet_name": None,
                "outlet_code": None, "outlet_dealer_id": None, "pc_actor_id": None, "pc_name": None,
                **base, "open": open_, "attention": attention}
    rows = [row("dealer", "a", 0, 9), row("dealer", "b", 2, 1), row("dealer", "c", 2, 5),
            row("dealer", "d", 0, 0)]
    assert [r["dealer_id"] for r in split_counts_with_breakdown(rows)["byDealer"]] == ["c", "b", "a", "d"]


def test_p2_requirement_rows_are_one_per_document_type():
    """_ensure_p2_requirement_rows adds them all in one statement, which sees none of its own
    rows: that answers as the old row-by-row loop only while no two share a document type."""
    from audit_core.uc03_p2_worker import _P2_ONLY_REQUIREMENTS

    types = [document_type for _, document_type in _P2_ONLY_REQUIREMENTS]
    assert len(types) == len(set(types))


def test_p2_requirement_rows_added_once(uc03_create_booking_setup):  # noqa: F811
    from audit_core.uc03_p2_worker import (
        _P2_ONLY_REQUIREMENTS,
        _ensure_p2_requirement_rows,
    )

    setup = uc03_create_booking_setup
    _client, _base, journeys = _seed(setup)
    tenant = setup["tenant_id"]
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, tenant)
        # One of the document types already has a row of another key: no second row for it.
        connection.execute(
            text("DELETE FROM auditcore.journey_document_requirements WHERE tenant_id=:t AND journey_id=:j "
                 "AND requirement_key LIKE 'p2\\_%'"), {"t": tenant, "j": journeys[0]})
        connection.execute(
            text("INSERT INTO auditcore.journey_document_requirements (tenant_id, journey_id, requirement_key, "
                 "document_type_key, process_area, requirement_level, requirement_status, condition_snapshot) "
                 "VALUES (:t, :j, 'catalogue_form_21', 'form_21', 'DELIVERY', 'OPTIONAL', 'PENDING', '{}')"),
            {"t": tenant, "j": journeys[0]})
        for _ in range(2):
            _ensure_p2_requirement_rows(connection, tenant_id=tenant, journey_id=journeys[0])
        rows = connection.execute(
            text("SELECT requirement_key, document_type_key FROM auditcore.journey_document_requirements "
                 "WHERE tenant_id=:t AND journey_id=:j AND document_type_key = ANY(:types)"),
            {"t": tenant, "j": journeys[0], "types": [d for _, d in _P2_ONLY_REQUIREMENTS]}).all()
    by_type = {}
    for key, document_type in rows:
        by_type.setdefault(document_type, []).append(key)
    assert by_type["form_21"] == ["catalogue_form_21"]
    assert all(len(keys) == 1 for keys in by_type.values())
    assert set(by_type) == {d for _, d in _P2_ONLY_REQUIREMENTS}


def test_the_queue_totals_count_the_tasks_the_list_lists(uc03_create_booking_setup):  # noqa: F811
    """The Task Queue's totals are the queue's, not the length of the capped list (product owner
    2026-11-14: "500 / 500 overdue" with 3,675 open): same filter, no cap."""
    setup = uc03_create_booking_setup
    _client, _base, journeys = _seed(setup)
    rng = random.Random(20261114)
    with setup["engine"].begin() as connection:
        set_tenant_context(connection, setup["tenant_id"])
        _more_tasks(connection, setup, journeys, rng)
        for role in (None, "PC", "TL", "REVIEWER"):
            params = {"tenant_id": setup["tenant_id"], "journey_id": None, "status": None,
                      "actor_id": setup["actor_id"], "view": "open", "role": role, "roles": roles_closable_by(role)}
            listed = _rows(connection, _P2_TASK_LIST_SQL, params)
            totals = connection.execute(text(_P2_TASK_TOTALS_SQL), params).mappings().one()
            assert totals["open_total"] == len(listed), role
            overdue = sum(1 for r in listed if r["due_at_utc"] is not None
                          and r["due_at_utc"] < connection.execute(text("SELECT now()")).scalar_one())
            assert totals["overdue_total"] == overdue, role
