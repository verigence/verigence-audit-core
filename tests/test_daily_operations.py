from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.daily_operations import (
    add_daily_ops_item,
    add_pc_daily_note,
    complete_daily_ops_run,
    create_daily_ops_run,
    get_daily_ops_run,
    list_pc_daily_notes,
    record_activity,
    set_daily_ops_item_status,
)


def test_daily_ops_activity_and_notepad_persist_without_hidden_state() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for daily operations test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-daily-{suffix}"
    pc_actor_id = f"pc-{suffix}"
    tl_actor_id = f"tl-{suffix}"
    business_date = date(2026, 8, 15)

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"DCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Daily OEM') RETURNING oem_id"
            ),
            {"code": f"DOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Daily Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"DP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Daily Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"DD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Daily Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"DO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Daily Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'DAILY-JOURNEY'
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
            },
        ).scalar_one()

        run_id = create_daily_ops_run(
            connection,
            tenant_id=tenant_id,
            outlet_id=outlet_id,
            business_date=business_date,
            pc_actor_id=pc_actor_id,
            correlation_id=f"daily-{suffix}",
        )
        gate_item_id = add_daily_ops_item(
            connection,
            tenant_id=tenant_id,
            daily_ops_run_id=run_id,
            item_type="GATE_OUT_REGISTER",
            details={"source": "gate register photograph"},
        )
        reconciliation_item_id = add_daily_ops_item(
            connection,
            tenant_id=tenant_id,
            daily_ops_run_id=run_id,
            item_type="DELIVERY_RECONCILIATION",
            journey_id=journey_id,
            details={"gateCount": 2, "caseCount": 1},
        )
        set_daily_ops_item_status(
            connection,
            tenant_id=tenant_id,
            daily_ops_item_id=gate_item_id,
            item_status="COMPLETED",
        )
        set_daily_ops_item_status(
            connection,
            tenant_id=tenant_id,
            daily_ops_item_id=reconciliation_item_id,
            item_status="EXCEPTION",
            details={"gateCount": 2, "caseCount": 1, "gap": 1},
        )
        record_activity(
            connection,
            tenant_id=tenant_id,
            actor_id=pc_actor_id,
            actor_role_code="PC",
            outlet_id=outlet_id,
            journey_id=journey_id,
            activity_type="DELIVERY_RECONCILIATION",
            details={"result": "EXCEPTION"},
        )
        record_activity(
            connection,
            tenant_id=tenant_id,
            actor_id=tl_actor_id,
            actor_role_code="TL",
            outlet_id=outlet_id,
            activity_type="DAILY_ACTIVITY_REVIEW",
            details={"pcActorId": pc_actor_id},
        )
        note_id = add_pc_daily_note(
            connection,
            tenant_id=tenant_id,
            pc_actor_id=pc_actor_id,
            outlet_id=outlet_id,
            note_date=business_date,
            note_text="Follow up the one-case delivery reconciliation gap tomorrow.",
        )
        complete_daily_ops_run(
            connection,
            tenant_id=tenant_id,
            daily_ops_run_id=run_id,
            run_status="COMPLETED",
        )

    engine.dispose()

    restarted_engine = create_engine(database_url)
    try:
        with restarted_engine.begin() as connection:
            run = get_daily_ops_run(
                connection,
                tenant_id=tenant_id,
                daily_ops_run_id=run_id,
            )
            assert run["run_status"] == "COMPLETED"
            assert run["completed_at_utc"] is not None
            assert run["business_date"] == business_date

            items = connection.execute(
                text(
                    """
                    SELECT item_type, item_status, details
                    FROM auditcore.daily_ops_items
                    WHERE tenant_id = :tenant_id AND daily_ops_run_id = :run_id
                    ORDER BY item_type
                    """
                ),
                {"tenant_id": tenant_id, "run_id": run_id},
            ).mappings().all()
            assert [(row["item_type"], row["item_status"]) for row in items] == [
                ("DELIVERY_RECONCILIATION", "EXCEPTION"),
                ("GATE_OUT_REGISTER", "COMPLETED"),
            ]
            assert items[0]["details"]["gap"] == 1

            activity_rows = connection.execute(
                text(
                    """
                    SELECT actor_role_code, activity_type
                    FROM auditcore.activity_records
                    WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
                    ORDER BY actor_role_code
                    """
                ),
                {"tenant_id": tenant_id, "outlet_id": outlet_id},
            ).all()
            assert activity_rows == [
                ("PC", "DELIVERY_RECONCILIATION"),
                ("TL", "DAILY_ACTIVITY_REVIEW"),
            ]

            notes = list_pc_daily_notes(
                connection,
                tenant_id=tenant_id,
                pc_actor_id=pc_actor_id,
                note_date=business_date,
            )
            assert len(notes) == 1
            assert notes[0]["pc_daily_note_id"] == note_id
            assert "Follow up" in notes[0]["note_text"]
    finally:
        restarted_engine.dispose()
