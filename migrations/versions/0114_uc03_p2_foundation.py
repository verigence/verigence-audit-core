"""0114_uc03_p2_foundation — isolated Phase 2 runtime.

Additive only. Existing UC03 tables/routes remain unchanged. Phase 2 owns
its upload batches, per-page document queue, generic durable work queue,
journey stage gates, task lifecycle, control state, and activity feed under
p2_* names so the complete Phase 2 track can be removed without disturbing
the legacy runtime.
"""
from __future__ import annotations

from alembic import op

revision = "0114_uc03_p2_foundation"
down_revision = "0113_delivery_vin_proposals"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.p2_upload_batches (
            tenant_id              varchar(128) NOT NULL,
            batch_id               uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id             uuid NOT NULL,
            original_filename      varchar(500) NOT NULL,
            content_type           varchar(160),
            size_bytes             bigint NOT NULL CHECK (size_bytes >= 0),
            sha256                 varchar(64) NOT NULL,
            page_count             integer NOT NULL CHECK (page_count > 0),
            original_payload       bytea NOT NULL,
            batch_status           varchar(40) NOT NULL DEFAULT 'ACCEPTED'
                                   CHECK (batch_status IN (
                                     'ACCEPTED','PROCESSING','COMPLETED',
                                     'PARTIAL_FAILURE','FAILED','CANCELLED'
                                   )),
            uploaded_by_actor_id   varchar(160) NOT NULL,
            correlation_id         varchar(128),
            created_at_utc         timestamptz NOT NULL DEFAULT now(),
            updated_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, batch_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE INDEX ix_p2_upload_batches_journey
          ON auditcore.p2_upload_batches(tenant_id, journey_id, created_at_utc DESC);

        CREATE TABLE auditcore.p2_document_queue (
            tenant_id                  varchar(128) NOT NULL,
            queue_id                   uuid NOT NULL DEFAULT gen_random_uuid(),
            batch_id                   uuid NOT NULL,
            journey_id                 uuid NOT NULL,
            page_number                integer NOT NULL CHECK (page_number > 0),
            page_sha256                varchar(64) NOT NULL,
            page_payload               bytea NOT NULL,
            client_upload_id           varchar(160) NOT NULL,
            di_document_id             uuid,
            classified_document_type   varchar(160),
            business_stage             varchar(40),
            queue_status               varchar(40) NOT NULL DEFAULT 'QUEUED'
                                       CHECK (queue_status IN (
                                         'QUEUED','PREPARING_PAGE','DI_UPLOAD_PREPARING',
                                         'DI_UPLOADING','DI_FINALIZING','CLASSIFYING',
                                         'EXTRACTING','SYNCING_TO_AUDIT_CORE','READY',
                                         'NEEDS_REVIEW','RETRY_WAIT','FAILED','DEAD_LETTER',
                                         'CANCELLED'
                                       )),
            attempt_count              integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at_utc        timestamptz,
            extracted_field_count      integer NOT NULL DEFAULT 0 CHECK (extracted_field_count >= 0),
            last_error                 text,
            correlation_id             varchar(128),
            created_at_utc             timestamptz NOT NULL DEFAULT now(),
            updated_at_utc             timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, queue_id),
            UNIQUE (tenant_id, batch_id, page_number),
            UNIQUE (tenant_id, client_upload_id),
            FOREIGN KEY (tenant_id, batch_id)
              REFERENCES auditcore.p2_upload_batches(tenant_id, batch_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE INDEX ix_p2_document_queue_journey
          ON auditcore.p2_document_queue(tenant_id, journey_id, created_at_utc DESC);
        CREATE INDEX ix_p2_document_queue_work
          ON auditcore.p2_document_queue(queue_status, next_attempt_at_utc, created_at_utc);

        CREATE TABLE auditcore.p2_work_queue (
            tenant_id              varchar(128) NOT NULL,
            work_id                uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id             uuid NOT NULL,
            work_type              varchar(80) NOT NULL
                                   CHECK (work_type IN (
                                     'DOCUMENT_INGEST','DOCUMENT_RECONCILE',
                                     'STAGE_RECOMPUTE','CONTROL_EVALUATE',
                                     'TASK_VERIFY'
                                   )),
            work_key               varchar(300) NOT NULL,
            payload                jsonb NOT NULL DEFAULT '{}'::jsonb,
            requested_version      bigint,
            processed_version      bigint,
            work_status            varchar(30) NOT NULL DEFAULT 'PENDING'
                                   CHECK (work_status IN (
                                     'PENDING','CLAIMED','PROCESSING',
                                     'RETRY_WAIT','COMPLETED','DEAD_LETTER','CANCELLED'
                                   )),
            attempt_count          integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at_utc    timestamptz,
            lease_expires_at_utc   timestamptz,
            last_error             text,
            correlation_id         varchar(128),
            created_at_utc         timestamptz NOT NULL DEFAULT now(),
            updated_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, work_id),
            UNIQUE (tenant_id, work_type, work_key),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE INDEX ix_p2_work_queue_claim
          ON auditcore.p2_work_queue(work_status, next_attempt_at_utc, created_at_utc);

        CREATE TABLE auditcore.p2_journey_runtime (
            tenant_id                       varchar(128) NOT NULL,
            journey_id                      uuid NOT NULL,
            current_stage                   varchar(50) NOT NULL DEFAULT 'BOOKING_DOCUMENT_UPLOAD'
                                            CHECK (current_stage IN (
                                              'BOOKING_DOCUMENT_UPLOAD',
                                              'BOOKING_VERIFY_DOCUMENTS',
                                              'BOOKING_COMPLETE',
                                              'DELIVERY_DOCUMENT_UPLOAD',
                                              'DELIVERY_VERIFY_DOCUMENTS',
                                              'DELIVERY_COMPLETE'
                                            )),
            booking_minimum_amount          numeric(18,2),
            booking_receipt_total           numeric(18,2) NOT NULL DEFAULT 0,
            booking_completion_state        varchar(30) NOT NULL DEFAULT 'IN_PROGRESS'
                                            CHECK (booking_completion_state IN (
                                              'IN_PROGRESS','BLOCKED','COMPLETE'
                                            )),
            delivery_completion_state       varchar(30) NOT NULL DEFAULT 'IN_PROGRESS'
                                            CHECK (delivery_completion_state IN (
                                              'IN_PROGRESS','BLOCKED','COMPLETE'
                                            )),
            manual_verification_pending_count integer NOT NULL DEFAULT 0 CHECK (manual_verification_pending_count >= 0),
            fact_version                    bigint NOT NULL DEFAULT 0 CHECK (fact_version >= 0),
            created_at_utc                  timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE TABLE auditcore.p2_stage_gate_state (
            tenant_id              varchar(128) NOT NULL,
            journey_id             uuid NOT NULL,
            stage_code             varchar(40) NOT NULL,
            gate_key               varchar(120) NOT NULL,
            gate_status            varchar(20) NOT NULL
                                   CHECK (gate_status IN ('WAITING','PASS','FAIL')),
            details                jsonb NOT NULL DEFAULT '{}'::jsonb,
            evaluated_at_utc       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id, stage_code, gate_key),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE TABLE auditcore.p2_control_state (
            tenant_id              varchar(128) NOT NULL,
            journey_id             uuid NOT NULL,
            control_code           varchar(180) NOT NULL,
            executor_type          varchar(30) NOT NULL
                                   CHECK (executor_type IN ('NATIVE','RULE_ENGINE')),
            control_status         varchar(40) NOT NULL DEFAULT 'WAITING_FOR_FACTS'
                                   CHECK (control_status IN (
                                     'WAITING_FOR_FACTS','READY','EVALUATING',
                                     'PASS','FAIL','NOT_APPLICABLE',
                                     'RETRY_PENDING','ERROR_TERMINAL'
                                   )),
            evaluated_fact_version bigint,
            finding_id             uuid,
            last_error             text,
            last_evaluated_at_utc  timestamptz,
            updated_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id, control_code),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE TABLE auditcore.p2_tasks (
            tenant_id              varchar(128) NOT NULL,
            task_id                uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id             uuid NOT NULL,
            root_task_id           uuid,
            parent_task_id         uuid,
            round_number           integer NOT NULL DEFAULT 1 CHECK (round_number > 0),
            task_type              varchar(160) NOT NULL,
            category               varchar(120) NOT NULL,
            origin_kind            varchar(20) NOT NULL CHECK (origin_kind IN ('SYSTEM','HUMAN')),
            source_type            varchar(80) NOT NULL,
            source_code            varchar(180),
            dedupe_key             varchar(400) NOT NULL,
            title                  varchar(300) NOT NULL,
            description            text NOT NULL,
            reference              jsonb NOT NULL DEFAULT '{}'::jsonb,
            severity               varchar(20) NOT NULL DEFAULT 'MEDIUM'
                                   CHECK (severity IN ('CRITICAL','HIGH','MEDIUM','LOW','INFO')),
            priority               varchar(20) NOT NULL DEFAULT 'NORMAL'
                                   CHECK (priority IN ('URGENT','HIGH','NORMAL','LOW')),
            assigned_role_code     varchar(80) NOT NULL,
            assigned_actor_id      varchar(160),
            raised_by_actor_id     varchar(160),
            raised_by_role_code    varchar(80),
            allowed_actions        jsonb NOT NULL DEFAULT '[]'::jsonb,
            completion_protocol    varchar(40) NOT NULL
                                   CHECK (completion_protocol IN (
                                     'MACHINE_VERIFIED','REQUESTER_CONFIRMED'
                                   )),
            task_status            varchar(40) NOT NULL DEFAULT 'READY'
                                   CHECK (task_status IN (
                                     'READY','IN_PROGRESS','ACTION_COMPLETED',
                                     'VERIFYING','AWAITING_REQUESTER_REVIEW',
                                     'RETURNED','VERIFIED_COMPLETE',
                                     'CANCELLED','FAILED','DEAD_LETTER'
                                   )),
            due_at_utc             timestamptz,
            completion_result      jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at_utc         timestamptz NOT NULL DEFAULT now(),
            updated_at_utc         timestamptz NOT NULL DEFAULT now(),
            verified_at_utc        timestamptz,
            PRIMARY KEY (tenant_id, task_id),
            UNIQUE (tenant_id, dedupe_key),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE INDEX ix_p2_tasks_queue
          ON auditcore.p2_tasks(tenant_id, assigned_role_code, task_status, due_at_utc);

        CREATE TABLE auditcore.p2_task_events (
            tenant_id              varchar(128) NOT NULL,
            task_event_id          bigserial NOT NULL,
            task_id                uuid NOT NULL,
            journey_id             uuid NOT NULL,
            event_type             varchar(80) NOT NULL,
            actor_id               varchar(160),
            actor_role_code        varchar(80),
            comment                text,
            details                jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, task_event_id),
            FOREIGN KEY (tenant_id, task_id)
              REFERENCES auditcore.p2_tasks(tenant_id, task_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE TABLE auditcore.p2_activity_events (
            tenant_id              varchar(128) NOT NULL,
            event_id               bigserial NOT NULL,
            journey_id             uuid NOT NULL,
            event_type             varchar(100) NOT NULL,
            subject_type           varchar(80),
            subject_id             varchar(180),
            details                jsonb NOT NULL DEFAULT '{}'::jsonb,
            correlation_id         varchar(128),
            created_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, event_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE INDEX ix_p2_activity_events_journey
          ON auditcore.p2_activity_events(tenant_id, journey_id, event_id);

        GRANT SELECT, INSERT, UPDATE, DELETE ON
          auditcore.p2_upload_batches,
          auditcore.p2_document_queue,
          auditcore.p2_work_queue,
          auditcore.p2_journey_runtime,
          auditcore.p2_stage_gate_state,
          auditcore.p2_control_state,
          auditcore.p2_tasks,
          auditcore.p2_task_events,
          auditcore.p2_activity_events
        TO audit_core_runtime;

        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auditcore TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS auditcore.p2_activity_events;
        DROP TABLE IF EXISTS auditcore.p2_task_events;
        DROP TABLE IF EXISTS auditcore.p2_tasks;
        DROP TABLE IF EXISTS auditcore.p2_control_state;
        DROP TABLE IF EXISTS auditcore.p2_stage_gate_state;
        DROP TABLE IF EXISTS auditcore.p2_journey_runtime;
        DROP TABLE IF EXISTS auditcore.p2_work_queue;
        DROP TABLE IF EXISTS auditcore.p2_document_queue;
        DROP TABLE IF EXISTS auditcore.p2_upload_batches;
        """
    )
