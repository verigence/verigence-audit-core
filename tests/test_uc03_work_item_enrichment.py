from types import SimpleNamespace

from audit_core.main import app
from audit_core.uc03_work_item_enrichment import _product_label, _product_values


def test_work_item_enrichment_route_is_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/tenants/{tenant_id}/uc03/work-items/enrich" in paths
    assert "post" in paths["/v1/tenants/{tenant_id}/uc03/work-items/enrich"]


def test_product_values_use_only_ready_vehicle_attributes() -> None:
    attributes = [
        SimpleNamespace(attributeKey="model", resolvedValue="  XUV700  ", reviewState="READY"),
        SimpleNamespace(attributeKey="variant", resolvedValue="AX7L", reviewState="NEEDS_REVIEW"),
        SimpleNamespace(attributeKey="color", resolvedValue="Midnight Black", reviewState="READY"),
        SimpleNamespace(attributeKey="customer_name", resolvedValue="Ignored", reviewState="READY"),
    ]

    assert _product_values(attributes) == {
        "model": "XUV700",
        "variant": None,
        "color": "Midnight Black",
    }


def test_product_label_uses_only_available_vehicle_parts() -> None:
    assert _product_label(model="XUV700", variant=None, colour="Midnight Black") == (
        "XUV700 · Midnight Black"
    )
    assert _product_label(model=None, variant=None, colour=None) is None
