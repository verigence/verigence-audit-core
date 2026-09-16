from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_human_principal, get_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal, Principal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    get_security_authorization_client,
)


@dataclass
class AllowedAuthorization:
    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=None,
        )


def _create_sku(connection, *, oem_id, suffix: str, sku_code: str) -> UUID:
    model_id = connection.execute(
        text(
            "INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
            "VALUES (:o, :mc, 'SCORPIO N') RETURNING model_id"
        ),
        {"o": oem_id, "mc": f"M-{suffix}"},
    ).scalar_one()
    variant_id = connection.execute(
        text(
            "INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
            "VALUES (:m, :vc, :vn) RETURNING variant_id"
        ),
        {"m": model_id, "vc": f"V-{suffix}", "vn": f"Variant {suffix}"},
    ).scalar_one()
    return connection.execute(
        text(
            "INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
            "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"
        ),
        {"o": oem_id, "m": model_id, "v": variant_id, "sc": sku_code},
    ).scalar_one()


@pytest.fixture
def correction_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 audit integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-uc03-skucorr-{suffix}"
    actors = {"PC": f"uc03-skucorr-pc-{suffix}", "TL": f"uc03-skucorr-tl-{suffix}"}

    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"SKUCORR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"SKUCORR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'SKU Corr Project', :o, :cat, CURRENT_DATE - 60,
                        'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"SKUCORR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"SKUCORR-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"SKUCORR-O-{suffix}"},
        ).scalar_one()
        for role, actor_id in actors.items():
            c.execute(
                text("""INSERT INTO auditcore.business_assignments
                    (tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id)
                    VALUES (:t, :a, :r, :d, :o)"""),
                {"t": tenant_id, "a": actor_id, "r": role, "d": dealer_id, "o": outlet_id},
            )
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'SKU Corr Customer') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"SKUCORR-J-{suffix}"},
        ).scalar_one()

        wrong_sku = _create_sku(c, oem_id=oem_id, suffix=f"wrong-{suffix}", sku_code=f"SKU-WRONG-{suffix}")
        right_sku = _create_sku(c, oem_id=oem_id, suffix=f"right-{suffix}", sku_code=f"SKU-RIGHT-{suffix}")

        pl_id = c.execute(
            text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
                 "VALUES (:t, :c, 'OEM') RETURNING price_list_id"),
            {"t": tenant_id, "c": f"PL-{suffix}"},
        ).scalar_one()
        plv_id = c.execute(
            text("""INSERT INTO auditcore.price_list_versions
                (tenant_id, price_list_id, version_no, lifecycle_status, effective_from)
                VALUES (:t, :pl, 1, 'DRAFT', CURRENT_DATE - 45) RETURNING price_list_version_id"""),
            {"t": tenant_id, "pl": pl_id},
        ).scalar_one()
        for sku_id in (wrong_sku, right_sku):
            c.execute(
                text("""INSERT INTO auditcore.price_list_items
                    (tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount)
                    VALUES (:t, :plv, :sku, 'EX_SHOWROOM', 1000000)"""),
                {"t": tenant_id, "plv": plv_id, "sku": sku_id},
            )
        c.execute(
            text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
                 "WHERE tenant_id=:t AND price_list_version_id=:plv"),
            {"t": tenant_id, "plv": plv_id},
        )

        # Already CONFIRMED on the wrong SKU -- the exact state this whole
        # flow exists to correct.
        c.execute(
            text("""INSERT INTO auditcore.journey_products
                (tenant_id, journey_id, product_sku_id, model_name_snapshot,
                 selection_source, selection_status, selection_method)
                VALUES (:t, :j, :sku, 'SCORPIO N', 'EVIDENCE', 'CONFIRMED', 'MODEL_RESOLUTION_SYNC_V1')"""),
            {"t": tenant_id, "j": journey_id, "sku": wrong_sku},
        )

    active_actor = {"id": actors["PC"]}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=active_actor["id"])
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=active_actor["id"],
        tenant_id=tenant_id,
        permissions=("audit.work.read", "audit.work.update", "audit.work.manage"),
    )
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowedAuthorization()
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actors": actors,
            "active_actor": active_actor,
            "wrong_sku": wrong_sku,
            "right_sku": right_sku,
        }
    finally:
        app.dependency_overrides.pop(get_human_principal, None)
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(get_security_authorization_client, None)
        engine.dispose()


def _client() -> TestClient:
    return TestClient(app)


def _set_role(setup, role: str) -> None:
    setup["active_actor"]["id"] = setup["actors"][role]


def _propose(setup, *, key: str, product_sku_id, reason: str = "Document reader misread the variant."):
    return _client().post(
        f"/v2/tenants/{setup['tenant_id']}/journeys/{setup['journey_id']}"
        f"/booking/model-resolution/propose-correction",
        headers={"Idempotency-Key": key},
        json={"productSkuId": str(product_sku_id), "reason": reason},
    )


def test_propose_correction_creates_a_tl_task(correction_setup):
    response = _propose(correction_setup, key="propose-0001", product_sku_id=correction_setup["right_sku"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposedProductSkuId"] == str(correction_setup["right_sku"])
    assert body["previousProductSkuId"] == str(correction_setup["wrong_sku"])

    with correction_setup["engine"].connect() as c:
        task = c.execute(
            text("SELECT task_type, assigned_role_code, task_status FROM auditcore.workflow_tasks "
                 "WHERE tenant_id=:t AND workflow_task_id=:tid"),
            {"t": correction_setup["tenant_id"], "tid": UUID(body["taskId"])},
        ).mappings().one()
    assert task["task_type"] == "MODEL_SELECTION_CORRECTION_REVIEW"
    assert task["assigned_role_code"] == "TL"
    assert task["task_status"] in ("PENDING", "READY")


def test_propose_correction_fails_when_nothing_is_confirmed_yet(correction_setup):
    with correction_setup["engine"].begin() as c:
        c.execute(
            text("UPDATE auditcore.journey_products SET selection_status='TENTATIVE' "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        )
    response = _propose(correction_setup, key="propose-0002", product_sku_id=correction_setup["right_sku"])
    assert response.status_code == 422, response.text


def test_propose_correction_fails_for_the_same_sku_already_confirmed(correction_setup):
    response = _propose(correction_setup, key="propose-0003", product_sku_id=correction_setup["wrong_sku"])
    assert response.status_code == 422, response.text


def test_propose_correction_fails_for_a_sku_outside_the_effective_price_list(correction_setup):
    response = _propose(correction_setup, key="propose-0004", product_sku_id=uuid4())
    assert response.status_code == 422, response.text


def test_completing_the_task_reassigns_the_sku_and_recomputes_the_deal(correction_setup):
    task_id = _propose(
        correction_setup, key="propose-0005", product_sku_id=correction_setup["right_sku"]
    ).json()["taskId"]
    _set_role(correction_setup, "TL")
    action = _client().post(
        f"/v1/tenants/{correction_setup['tenant_id']}/tasks/{task_id}/complete",
        headers={"Idempotency-Key": "complete-0005"},
    )
    assert action.status_code == 200, action.text
    assert action.json()["status"] == "COMPLETED"

    with correction_setup["engine"].connect() as c:
        row = c.execute(
            text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        ).mappings().one()
    assert row["product_sku_id"] == correction_setup["right_sku"]
    assert row["selection_status"] == "CONFIRMED"

    with correction_setup["engine"].connect() as c:
        applied_at = c.execute(
            text("SELECT applied_at_utc FROM auditcore.model_selection_correction_proposals "
                 "WHERE tenant_id=:t AND workflow_task_id=:tid"),
            {"t": correction_setup["tenant_id"], "tid": UUID(task_id)},
        ).scalar_one()
    assert applied_at is not None

    # Deal reconciliation re-ran against the corrected SKU -- a commercial
    # line now exists for it (never left showing the wrong SKU's price).
    with correction_setup["engine"].connect() as c:
        commercial_count = c.execute(
            text("SELECT count(*) FROM auditcore.commercial_lines "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        ).scalar_one()
    assert commercial_count > 0


def test_cancelling_the_task_leaves_the_original_sku_untouched(correction_setup):
    task_id = _propose(
        correction_setup, key="propose-0006", product_sku_id=correction_setup["right_sku"]
    ).json()["taskId"]
    _set_role(correction_setup, "TL")
    action = _client().post(
        f"/v1/tenants/{correction_setup['tenant_id']}/tasks/{task_id}/cancel",
        headers={"Idempotency-Key": "cancel-0006"},
        json={"reason": "The original SKU was actually correct."},
    )
    assert action.status_code == 200, action.text
    assert action.json()["status"] == "CANCELLED"

    with correction_setup["engine"].connect() as c:
        row = c.execute(
            text("SELECT product_sku_id FROM auditcore.journey_products "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": correction_setup["tenant_id"], "j": correction_setup["journey_id"]},
        ).mappings().one()
    assert row["product_sku_id"] == correction_setup["wrong_sku"]

    with correction_setup["engine"].connect() as c:
        applied_at = c.execute(
            text("SELECT applied_at_utc FROM auditcore.model_selection_correction_proposals "
                 "WHERE tenant_id=:t AND workflow_task_id=:tid"),
            {"t": correction_setup["tenant_id"], "tid": UUID(task_id)},
        ).scalar_one()
    assert applied_at is None
