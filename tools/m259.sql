
        WITH carried AS (
            SELECT ub.tenant_id, ub.batch_id, ub.uploaded_by_actor_id AS was,
                   CAST(substr(a.details->>'job', 11) AS uuid) AS copy_journey_id
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.p2_activity_events a
              ON a.tenant_id = ub.tenant_id AND a.journey_id = ub.journey_id
             AND a.subject_id = CAST(ub.batch_id AS text)
             AND a.event_type = 'DOCUMENT_COPIED_FROM_DUPLICATE'
            WHERE ub.correlation_id LIKE 'p2-duplicate-merge:%'
              AND a.details->>'job' ~ '^same-file:[0-9a-fA-F-]{36}$'
        ),
        who AS (
            SELECT c.tenant_id, c.batch_id, c.was,
                   COALESCE(
                       (SELECT bu.uploaded_by_actor_id FROM auditcore.p2_bulk_files bf
                          JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
                         WHERE bf.tenant_id = c.tenant_id AND bf.journey_id = c.copy_journey_id
                         ORDER BY bf.created_at_utc LIMIT 1),
                       (SELECT d.pc_actor_id FROM auditcore.p2_journey_deletions d
                         WHERE d.tenant_id = c.tenant_id AND d.journey_id = c.copy_journey_id
                           AND d.pc_actor_id IS NOT NULL
                         ORDER BY d.requested_at_utc DESC LIMIT 1),
                       (SELECT j.created_by_actor_id FROM auditcore.journeys j
                         WHERE j.tenant_id = c.tenant_id AND j.journey_id = c.copy_journey_id)
                   ) AS uploader
            FROM carried c
        )
        UPDATE auditcore.p2_upload_batches ub
           SET uploaded_by_actor_id = w.uploader, updated_at_utc = now()
          FROM who w
         WHERE ub.tenant_id = w.tenant_id AND ub.batch_id = w.batch_id
           AND w.uploader IS NOT NULL AND w.uploader <> ''
           AND ub.uploaded_by_actor_id IS DISTINCT FROM w.uploader
    