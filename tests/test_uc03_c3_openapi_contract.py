from pathlib import Path

import yaml

from audit_core.main import app

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "api" / "openapi-uc03-c3.yaml"

FROZEN_TO_RUNTIME = {
    "/tenants/{tenantId}/journeys/{journeyId}/uc03/audit-summary": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/audit-summary",
        "get",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/uc03/flags": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/flags",
        {"get", "post"},
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/uc03/flags/{flagId}/actions": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/flags/{flag_id}/actions",
        "post",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/uc03/flags/{flagId}/remarks": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/flags/{flag_id}/remarks",
        "post",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/uc03/stages/{stageCode}/audit/complete": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/stages/{stage_code}/audit/complete",
        "post",
    ),
    "/tenants/{tenantId}/journeys/{journeyId}/uc03/timeline": (
        "/v1/tenants/{tenant_id}/journeys/{journey_id}/uc03/timeline",
        "get",
    ),
}


def _methods(value: str | set[str]) -> set[str]:
    return {value} if isinstance(value, str) else value


def test_c3_checkpoint_openapi_is_valid_and_matches_runtime_surface() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    assert contract["openapi"] == "3.1.0"
    assert contract["info"]["version"] == "1.0.0-c3-frozen"

    frozen_paths = contract["paths"]
    runtime_paths = app.openapi()["paths"]
    assert set(frozen_paths) == set(FROZEN_TO_RUNTIME)

    for frozen_path, (runtime_path, expected_methods) in FROZEN_TO_RUNTIME.items():
        methods = _methods(expected_methods)
        assert runtime_path in runtime_paths
        for method in methods:
            assert method in frozen_paths[frozen_path]
            assert method in runtime_paths[runtime_path]


def test_c3_contract_freezes_audit_authority_and_history_invariants() -> None:
    contract_text = CONTRACT_PATH.read_text()
    assert "Human-created findings cannot self-declare Audit completion guards" in contract_text
    assert "Historical findings keep Audit Status FLAGS_RAISED" in contract_text
    assert "server-side Project/role policy remains authoritative" in contract_text
    assert "without exposing internal payloads or actor identifiers" in contract_text
    assert "blockingCompletion" not in contract_text
