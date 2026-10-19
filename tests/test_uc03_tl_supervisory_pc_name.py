from __future__ import annotations

from sqlalchemy import text

from audit_core import uc03_tl_supervisory as tl
from audit_core.db import set_tenant_context
from audit_core.security import HumanPrincipal
from tests.test_uc03_backfill_document_sync_producers import (
    seeded_tenant,  # noqa: F401  (fixture)
)


def test_new_style_booking_shows_its_pc_on_the_tl_list(seeded_tenant, monkeypatch) -> None:  # noqa: F811
    """A booking started in the new flow has no PC_BOOKING_CAPTURE_SUBMITTED workflow event; the TL
    list must still show who the PC is (the Journey's own PC and name), not 'PC not recorded'."""
    engine, tenant_id, journey_id = (
        seeded_tenant["engine"], seeded_tenant["tenant_id"], seeded_tenant["journey_id"],
    )
    with engine.begin() as c:
        set_tenant_context(c, tenant_id)
        c.execute(
            text("UPDATE auditcore.journeys SET created_by_actor_id='pc-actor-1', created_by_display_name='Priya Sharma' "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, capture_completed_at_utc) "
                 "VALUES (:t, :j, 'BOOKING', now())"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                 "VALUES (:t, 'tl-actor-1', 'TL')"),
            {"t": tenant_id},
        )
    monkeypatch.setattr(tl, "_authorize_workspace", lambda *a, **k: None)

    with engine.begin() as c:
        page = tl.list_tl_supervisory_cases(
            tenant_id, HumanPrincipal(subject="tl-actor-1"), object(), c, limit=10, offset=0,
        )

    assert page.totalCount == 1
    assert page.items[0].responsiblePcName == "Priya Sharma"
    assert page.items[0].responsiblePcActorId == "pc-actor-1"
