import os

import pytest
from sqlalchemy import create_engine, text


def test_p2_and_legacy_task_journey_ids_are_uuid_compatible() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for P2 UUID contract integration test")

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            # Planning/type-checking these statements is enough to catch the
            # regression where p2_tasks.journey_id (uuid) was cast to text
            # before UNIONing with work_items.subject_ref (uuid).
            connection.execute(
                text(
                    """
                    SELECT journey_id
                    FROM auditcore.p2_tasks
                    WHERE false
                    UNION ALL
                    SELECT subject_ref
                    FROM auditcore.work_items
                    WHERE false
                    """
                )
            ).all()

            connection.execute(
                text(
                    """
                    SELECT 1
                    FROM auditcore.journeys j
                    JOIN auditcore.work_items wi
                      ON wi.tenant_id=j.tenant_id
                     AND wi.subject_ref=j.journey_id
                    WHERE false
                    """
                )
            ).all()
    finally:
        engine.dispose()
