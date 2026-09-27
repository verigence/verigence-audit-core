import os

import pytest
from sqlalchemy import create_engine, text

from audit_core.main import app


def test_p2_design_route_contract() -> None:
    paths = app.openapi()["paths"]
    document = "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/documents/{document_id}"
    field = document + "/fields/{field_key}"
    replace = document + "/replace"
    batch = "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/uploads/{batch_id}"

    assert {"get", "delete"} <= set(paths[document])
    assert "patch" in paths[field]
    assert "post" in paths[replace]
    assert "get" in paths[batch]
    assert "get" in paths["/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/documents"]
    assert "get" in paths["/p2/v1/tenants/{tenant_id}/tasks"]


def test_p2_replacement_schema_is_migrated() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for P2 replacement schema test")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            columns = set(
                connection.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema='auditcore'
                          AND table_name='p2_upload_batches'
                          AND column_name IN (
                            'replaces_document_id',
                            'replaces_evidence_id',
                            'replacement_applied_at_utc'
                          )
                        """
                    )
                ).scalars()
            )
            assert columns == {
                "replaces_document_id",
                "replaces_evidence_id",
                "replacement_applied_at_utc",
            }
    finally:
        engine.dispose()
