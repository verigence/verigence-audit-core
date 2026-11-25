"""Names from Security replace ids in the Task Manager (2026-10-27). Real Postgres."""
from __future__ import annotations

import httpx
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.people_names import fetch_names, store_names
from audit_core.task_manager import known_names


def test_names_are_fetched_in_batches_and_preferred():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        ids = request.url.params["ids"].split(",")
        seen.append(len(ids))
        return httpx.Response(200, json=[{"userId": i, "displayName": f"Name {i}"} for i in ids if i != "x2"])

    names = fetch_names([f"x{n}" for n in range(160)], base_url="https://security.test", token="tok",
                        transport=httpx.MockTransport(handler))
    assert seen == [150, 10] and len(names) == 159 and "x2" not in names

    engine = database_engine()
    journey = create_p2_journey(engine, prefix="names")
    try:
        store_names(engine, {journey.actor_id: "Asha Mohanty"})
        with engine.begin() as c:
            set_tenant_context(c, journey.tenant_id)
            assert known_names(c, tenant_id=journey.tenant_id)[journey.actor_id] == "Asha Mohanty"
    finally:
        with engine.begin() as c:
            set_platform_super_admin_context(c)
            c.execute(text("DELETE FROM auditcore.people_names WHERE actor_id = :a"), {"a": journey.actor_id})
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def test_assigned_people_are_found_in_every_project():
    """Assignments are tenant-isolated: the platform context alone sees none (2026-10-27)."""
    from audit_core.people_names import _assigned_actor_ids

    engine = database_engine()
    journey = create_p2_journey(engine, prefix="names2")
    try:
        assert journey.actor_id in _assigned_actor_ids(engine)
    finally:
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def test_same_name_word_for_word() -> None:
    """DMS booking matching on a name with no number (2026-11-24): every word agrees, spelling
    aside; no word missing, no first letter differing."""
    from audit_core.uc03_p2_names import same_name_word_for_word as same

    assert not same("Chitta Ranjan Patra", "RANJANA PATRA")
    assert not same("PRAKASH DAS", "PRAKASH KUMAR DASH")
    assert not same("SITENDRA NAYAK", "JITENDRA NAYAK")
    assert same("RASMITA MALLIK", "RASMITA MALLICK")
    assert same("BISWANATH SAHOO", "BISWANATH SAHU")
    assert same("RASMIRANJAN DAS", "RASMI RANJAN DAS")
    assert same("JENA SWADHIN", "Mr. Swadhin Jena")
    assert same("DINESHCHANDRA R AGRAWAL INFRACON PVT LTD", "DINESHCHANDRA R AGRAWAL INFRACON PRIVATE LIMITED")
    assert not same("ABC MOTORS PVT LTD", "XYZ MOTORS PVT LTD")
