from pathlib import Path

import yaml

from audit_core.main import app


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "api" / "openapi-uc03-c2.yaml"


FROZEN_TO_RUNTIME = {
    "/tenants/{tenantId}/journeys/{journeyId}/delivery/start": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery/start",
        "post",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/delivery/intimation": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery/intimation",
        "put",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/delivery/vehicle-observation": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery/vehicle-observation",
        "put",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/delivery/complete": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery/complete",
        "post",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/delivery/workspace": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery/workspace",
        "get",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/stages/DELIVERY/documents": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/stages/DELIVERY/documents",
        "get",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/stages/DELIVERY/documents/{requirementKey}": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/stages/DELIVERY/documents/{requirement_key}",
        "put",
    ),
}


def test_c2_checkpoint_openapi_is_valid_and_matches_runtime_surface() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    assert contract["openapi"] == "3.1.0"
    assert contract["info"]["version"] == "1.0.0-c2-frozen"

    frozen_paths = contract["paths"]
    runtime_paths = app.openapi()["paths"]
    assert set(frozen_paths) == set(FROZEN_TO_RUNTIME)

    for frozen_path, (runtime_path, method) in FROZEN_TO_RUNTIME.items():
        assert method in frozen_paths[frozen_path]
        assert runtime_path in runtime_paths
        assert method in runtime_paths[runtime_path]


def test_c2_contract_freezes_non_blocking_delivery_invariant() -> None:
    contract_text = CONTRACT_PATH.read_text()
    assert "does not reject or suppress Delivery Start" in contract_text
    assert "Records physical DELIVERY_COMPLETED" in contract_text
    assert "Audit State may remain IN_PROGRESS" in contract_text
    assert "DI remains internal-only behind Audit Core" in contract_text
