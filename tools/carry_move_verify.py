"""Read only: did carries since the 24 Nov deploy move their reading (no double reading)?"""
import os

from sqlalchemy import create_engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context

SINCE = "2026-11-24 12:30+00"
engine = create_engine(os.environ["DATABASE_URL"])
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_platform_super_admin_context(c)
    tenants = c.execute(text("SELECT tenant_id, project_name FROM auditcore.projects WHERE project_status='ACTIVE'")).all()
for tid, name in tenants:
    with engine.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(c, tid)
        rows = c.execute(text("""
            SELECT j.journey_reference, ub.batch_id, ub.created_at_utc, ub.batch_status, ub.uploaded_by_actor_id,
                   pn.display_name AS uploader, j.created_by_actor_id AS booking_pc,
                   q.client_upload_id AS q_client, q.queue_status, q.unit_kind, q.di_document_id,
                   (SELECT count(*) FROM auditcore.p2_work_queue w WHERE w.tenant_id=ub.tenant_id
                      AND w.work_type='SPLIT_BATCH' AND w.work_key=CAST(ub.batch_id AS text)) AS splits,
                   (SELECT e.journey_id = ub.journey_id FROM auditcore.evidence e WHERE e.tenant_id=ub.tenant_id
                      AND e.di_document_id=q.di_document_id) AS evidence_here,
                   (SELECT e.association_status FROM auditcore.evidence e WHERE e.tenant_id=ub.tenant_id
                      AND e.di_document_id=q.di_document_id) AS evidence_status
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.journeys j ON j.tenant_id=ub.tenant_id AND j.journey_id=ub.journey_id
            LEFT JOIN auditcore.people_names pn ON pn.actor_id=ub.uploaded_by_actor_id
            LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id=ub.tenant_id AND q.batch_id=ub.batch_id
                 AND q.merged_into_queue_id IS NULL
            WHERE ub.tenant_id=:t AND ub.correlation_id LIKE 'p2-duplicate-merge:%' AND ub.created_at_utc >= :s
            ORDER BY ub.created_at_utc"""), {"t": tid, "s": SINCE}).mappings().all()
        if not rows:
            print(f"[{name}] no carries since {SINCE}")
            continue
        moved = [r for r in rows if (r["q_client"] or "").startswith("carried-")]
        read_again = [r for r in rows if r["splits"]]
        print(f"[{name}] carried uploads: {len(rows)}, moved with reading: {len(moved)}, split/read again: {len(read_again)}")
        from collections import Counter
        print("   queue status of moved:", Counter(r["queue_status"] for r in moved))
        print("   evidence on target:", Counter((r["evidence_here"], r["evidence_status"]) for r in moved))
        print("   uploader is not booking PC:", sum(1 for r in rows if r["uploaded_by_actor_id"] != r["booking_pc"]))
        for r in rows:
            if r not in moved or r["queue_status"] not in ("READY", "NEEDS_REVIEW", "SUPPORTING") or not r["evidence_here"]:
                print("   CHECK", {k: str(v) for k, v in r.items()})
        tasks = c.execute(text("""
            SELECT t.task_type, t.task_status, t.assigned_actor_id, ub.uploaded_by_actor_id, j.created_by_actor_id
            FROM auditcore.p2_tasks t
            JOIN auditcore.p2_upload_batches ub ON ub.tenant_id=t.tenant_id AND CAST(ub.batch_id AS text)=t.reference->>'batchId'
            JOIN auditcore.journeys j ON j.tenant_id=t.tenant_id AND j.journey_id=t.journey_id
            WHERE t.tenant_id=:t AND t.created_at_utc >= :s AND upper(t.assigned_role_code)='PC'
              AND ub.uploaded_by_actor_id IS DISTINCT FROM j.created_by_actor_id"""), {"t": tid, "s": SINCE}).mappings().all()
        print(f"   file tasks since on another PC's booking: {len(tasks)}, to the uploader: "
              f"{sum(1 for x in tasks if x['assigned_actor_id'] == x['uploaded_by_actor_id'])}")
        for x in tasks:
            if x["assigned_actor_id"] != x["uploaded_by_actor_id"]:
                print("   TASK CHECK", dict(x))
