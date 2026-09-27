from audit_core.main import app


def test_p2_route_contract_is_mounted() -> None:
    paths = app.openapi()["paths"]
    required = {
        "/p2/v1/tenants/{tenant_id}/journeys",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/uploads:init",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/uploads/{batch_id}:finalize",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/documents",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/documents/{document_id}/review",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/documents/{document_id}/content",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/documents/{document_id}/field-corrections",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/events",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/stage",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/overview",
        "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/tasks",
        "/p2/v1/tenants/{tenant_id}/tasks",
        "/p2/v1/tenants/{tenant_id}/tasks/{task_id}/actions",
    }
    assert required <= set(paths)


def test_legacy_uc03_routes_remain_available() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/tenants/{tenant_id}/uc03/journeys/{journey_id}/overview" in paths
