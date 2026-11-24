"""Read only: the open per-file PC tasks migration 0260 would hand to the file's uploader."""
import os

from sqlalchemy import create_engine, text

engine = create_engine(os.environ["DATABASE_URL"])
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    rows = c.execute(text("""
        SELECT t.task_type, pj.display_name AS booking_pc, pu.display_name AS uploader, count(*) AS tasks,
               string_agg(DISTINCT j.journey_reference, ', ') AS journeys
        FROM auditcore.p2_tasks t
        JOIN auditcore.p2_upload_batches b ON b.tenant_id=t.tenant_id AND b.journey_id=t.journey_id
             AND t.reference->>'batchId' = CAST(b.batch_id AS text)
        JOIN auditcore.journeys j ON j.tenant_id=b.tenant_id AND j.journey_id=b.journey_id
        LEFT JOIN auditcore.people_names pj ON pj.actor_id=j.created_by_actor_id
        LEFT JOIN auditcore.people_names pu ON pu.actor_id=b.uploaded_by_actor_id
        WHERE t.origin_kind='SYSTEM' AND upper(t.assigned_role_code)='PC' AND t.assigned_actor_id IS NULL
          AND t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
          AND b.uploaded_by_actor_id IS NOT NULL AND b.uploaded_by_actor_id NOT IN ('', 'SYSTEM')
          AND b.uploaded_by_actor_id IS DISTINCT FROM j.created_by_actor_id
        GROUP BY 1,2,3 ORDER BY tasks DESC""")).mappings().all()
    print(f"0260 would assign {sum(r['tasks'] for r in rows)} open tasks")
    for r in rows:
        print("  ", dict(r))
    head = c.execute(text("SELECT version_num FROM auditcore.alembic_version")).scalar() \
        if c.execute(text("SELECT to_regclass('auditcore.alembic_version')")).scalar() else \
        c.execute(text("SELECT version_num FROM public.alembic_version")).scalar()
    print("live migration head:", head)
# rerun after #958 deploy
