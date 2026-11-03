"""SKU_SET: the SKU the product owner decides for a booking (2026-11-03)."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine

from audit_core.uc03_sku_set import run_sku_set


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="skuset")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def test_each_item_is_reported_and_nothing_is_set_without_commit(journey) -> None:
    result = run_sku_set(journey.engine, f"{journey.tenant_id}:po-1103",
                         ["NO-SUCH-REF=XUV3XO::AX5 PM MT", "missing-separator"])
    assert result["commit"] is False
    assert [i.get("error") for i in result["items"]] == [
        "No booking with this reference.", "Expected '<journey reference>=<sku code>'."]


def test_the_tag_names_the_project() -> None:
    with pytest.raises(ValueError):
        run_sku_set(None, "no-tenant", [])  # type: ignore[arg-type]
