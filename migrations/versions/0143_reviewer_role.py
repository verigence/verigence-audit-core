"""0143_reviewer_role — what a Reviewer opens, reviews, claims and changes.

Four tables behind the Reviewer operating role:

`p2_document_views` is the open ledger. A Reviewer may only mark a stage's
documents reviewed once they have personally opened every one of them, which
can happen over several days, so the opens have to be durable and per actor --
browser state would be lost and would not be evidence of anything. One row per
(document, actor); the first open is the one that matters.

`p2_stage_document_reviews` holds the Booking/Delivery documents-reviewed
statuses. It is write-once per Journey and stage: marked once, never revoked,
and a document uploaded afterwards does not reopen it (owner decision
2026-10-18). The primary key makes that true by construction, and the grants
below withhold UPDATE and DELETE so no later code can quietly overwrite a
review. Because the rows are immutable they double as the event log the daily
and weekly counters read, which is why the dealer and outlet are denormalised
here: counts are segregated by outlet, and a count must not change when
reference data does.

`p2_journey_claims` is advisory. It records who is working a Journey so two
Reviewers do not duplicate the effort, and it deliberately does not gate the
review -- a claim nobody releases would otherwise lock the Journey and need
expiry, reassignment and an override to administer.

`journey_field_change_log` is append-only and exists because
`journey_document_extracted_fields` cannot answer "who changed this, in what
role, from what". It records no role for the modifier, and a resync overwrites
`modified_value`, `modified_by_actor_id` and `modified_at_utc` in place, so
without this log the evidence that a correction was ever made is destroyed
along with the value. Reviewers correct high-confidence values without a
second person approving, so this log is the control rather than a convenience;
the grants withhold UPDATE and DELETE accordingly.

"""
from __future__ import annotations

from alembic import op

revision = "0143_reviewer_role"
down_revision = "0142_oem_master_source_object"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_document_views (
            tenant_id              varchar(128) NOT NULL,
            di_document_id         uuid NOT NULL,
            actor_id               varchar(160) NOT NULL,
            journey_id             uuid NOT NULL,
            first_opened_at_utc    timestamptz NOT NULL DEFAULT now(),
            last_opened_at_utc     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, di_document_id, actor_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
              ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_p2_document_views_journey_actor
          ON auditcore.p2_document_views(tenant_id, journey_id, actor_id);

        CREATE TABLE IF NOT EXISTS auditcore.p2_stage_document_reviews (
            tenant_id              varchar(128) NOT NULL,
            journey_id             uuid NOT NULL,
            stage_code             varchar(20) NOT NULL
                                   CHECK (stage_code IN ('BOOKING', 'DELIVERY')),
            reviewed_by_actor_id   varchar(160) NOT NULL,
            reviewed_by_role_code  varchar(80) NOT NULL,
            reviewed_at_utc        timestamptz NOT NULL DEFAULT now(),
            document_count         integer NOT NULL
                                   CHECK (document_count >= 0),
            dealer_id              uuid NOT NULL,
            outlet_id              uuid NOT NULL,
            correlation_id         varchar(128),
            PRIMARY KEY (tenant_id, journey_id, stage_code),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
              ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_p2_stage_reviews_actor_day
          ON auditcore.p2_stage_document_reviews(
            tenant_id, reviewed_by_actor_id, reviewed_at_utc DESC
          );
        CREATE INDEX IF NOT EXISTS ix_p2_stage_reviews_outlet_day
          ON auditcore.p2_stage_document_reviews(
            tenant_id, outlet_id, reviewed_at_utc DESC
          );

        CREATE TABLE IF NOT EXISTS auditcore.p2_journey_claims (
            tenant_id              varchar(128) NOT NULL,
            journey_id             uuid NOT NULL,
            claimed_by_actor_id    varchar(160) NOT NULL,
            claimed_by_role_code   varchar(80) NOT NULL,
            claimed_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
              ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_p2_journey_claims_actor
          ON auditcore.p2_journey_claims(tenant_id, claimed_by_actor_id);

        CREATE TABLE IF NOT EXISTS auditcore.journey_field_change_log (
            tenant_id                 varchar(128) NOT NULL,
            change_id                 uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id                uuid NOT NULL,
            stage_code                varchar(20) NOT NULL
                                      CHECK (stage_code IN ('BOOKING', 'DELIVERY')),
            di_document_id            uuid,
            field_key                 varchar(160) NOT NULL,
            source_canonical_field_id varchar(160),
            source_fact_version       integer,
            previous_value            jsonb,
            new_value                 jsonb,
            confidence_score          numeric(6,3),
            change_reason             varchar(40) NOT NULL
                                      CHECK (change_reason IN (
                                        'CORRECTION', 'RESYNC_OVERWRITE'
                                      )),
            actor_id                  varchar(160) NOT NULL,
            actor_role_code           varchar(80) NOT NULL,
            remarks                   varchar(2000),
            changed_at_utc            timestamptz NOT NULL DEFAULT now(),
            correlation_id            varchar(128),
            PRIMARY KEY (tenant_id, change_id),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id)
              ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_journey_field_change_log_journey
          ON auditcore.journey_field_change_log(
            tenant_id, journey_id, changed_at_utc DESC
          );
        CREATE INDEX IF NOT EXISTS ix_journey_field_change_log_actor
          ON auditcore.journey_field_change_log(
            tenant_id, actor_id, changed_at_utc DESC
          );

        ALTER TABLE auditcore.p2_document_views ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_document_views FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_p2_document_views ON auditcore.p2_document_views
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        ALTER TABLE auditcore.p2_stage_document_reviews ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_stage_document_reviews FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_p2_stage_document_reviews
          ON auditcore.p2_stage_document_reviews
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        ALTER TABLE auditcore.p2_journey_claims ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_journey_claims FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_p2_journey_claims ON auditcore.p2_journey_claims
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        ALTER TABLE auditcore.journey_field_change_log ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.journey_field_change_log FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_journey_field_change_log
          ON auditcore.journey_field_change_log
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        -- An open ledger is upserted (the last open moves), so it takes UPDATE.
        GRANT SELECT, INSERT, UPDATE ON
          auditcore.p2_document_views
        TO audit_core_runtime;

        -- A claim is advisory: it moves to another Reviewer and is released.
        GRANT SELECT, INSERT, UPDATE, DELETE ON
          auditcore.p2_journey_claims
        TO audit_core_runtime;

        -- A completed review and a recorded change are facts. Withholding
        -- UPDATE and DELETE makes write-once and append-only a privilege rather
        -- than a convention, so later code cannot quietly rewrite either one.
        GRANT SELECT, INSERT ON
          auditcore.p2_stage_document_reviews,
          auditcore.journey_field_change_log
        TO audit_core_runtime;

        REVOKE UPDATE, DELETE ON
          auditcore.p2_stage_document_reviews,
          auditcore.journey_field_change_log
        FROM audit_core_runtime;

        REVOKE DELETE ON
          auditcore.p2_document_views
        FROM audit_core_runtime;

        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auditcore TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS auditcore.journey_field_change_log;
        DROP TABLE IF EXISTS auditcore.p2_journey_claims;
        DROP TABLE IF EXISTS auditcore.p2_stage_document_reviews;
        DROP TABLE IF EXISTS auditcore.p2_document_views;
        """
    )
