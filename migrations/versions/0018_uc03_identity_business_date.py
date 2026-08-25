"""Add UC03 Legal Name and explicit business-date semantics.

Entered Name remains customers.display_name and is immutable after Journey
creation. Legal Name is derived only from validated PAN/Aadhaar extraction
proposals. bookings.booking_date is the Actual Booking Date; journeys.created_at_utc
remains the immutable Audit Captured At timestamp.
"""
from alembic import op

revision = "0017_uc03_identity_business_date"
down_revision = "0016_uc02_project_delete_uc03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.customers
            ADD COLUMN legal_name varchar(240),
            ADD COLUMN legal_name_status varchar(20) NOT NULL DEFAULT 'PENDING',
            ADD COLUMN legal_name_source_evidence_id uuid,
            ADD COLUMN legal_name_verified_by_actor_id varchar(160),
            ADD COLUMN legal_name_verified_at_utc timestamptz
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.customers
        ADD CONSTRAINT ck_customers_legal_name_status
        CHECK (legal_name_status IN ('PENDING','VERIFIED','CONFLICT'))
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.customers
        ADD CONSTRAINT fk_customers_legal_name_evidence
        FOREIGN KEY (tenant_id, legal_name_source_evidence_id)
        REFERENCES auditcore.evidence(tenant_id, evidence_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_customers_legal_name_status
        ON auditcore.customers(tenant_id, legal_name_status, customer_id)
        """
    )

    # The name keyed at Journey creation is an observation made by the PC. Once the
    # Journey exists it is historical audit input and must not be overwritten by DI.
    # Existing proposal-decision code still writes the legacy CUSTOMER_NAME typed
    # target; the write is intentionally neutralised here and the identity proposal
    # trigger below applies the value to legal_name instead.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.protect_customer_entered_name()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.display_name IS DISTINCT FROM OLD.display_name THEN
                NEW.display_name := OLD.display_name;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_customer_entered_name_immutable
        BEFORE UPDATE OF display_name ON auditcore.customers
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.protect_customer_entered_name()
        """
    )

    # Apply only identity-authoritative proposal fields. Booking-form customer_name
    # is deliberately excluded by the Audit Core publication installer and therefore
    # cannot establish Legal Name.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.apply_uc03_legal_name_proposal()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_customer_id uuid;
            v_value jsonb;
            v_name text;
            v_existing text;
            v_existing_norm text;
            v_candidate_norm text;
        BEGIN
            IF NEW.proposal_status NOT IN ('ACCEPTED','CORRECTED')
               OR OLD.proposal_status IN ('ACCEPTED','CORRECTED') THEN
                RETURN NEW;
            END IF;

            IF NOT (
                (lower(COALESCE(NEW.source_document_type_key, '')) IN ('pan','pan_card')
                 AND lower(NEW.field_key) = 'pan_name')
                OR
                (lower(COALESCE(NEW.source_document_type_key, '')) = 'aadhaar'
                 AND lower(NEW.field_key) = 'aadhaar_name')
            ) THEN
                RETURN NEW;
            END IF;

            v_value := COALESCE(NEW.accepted_value, NEW.proposed_value);
            IF jsonb_typeof(v_value) = 'string' THEN
                v_name := v_value #>> '{}';
            ELSIF jsonb_typeof(v_value) = 'object' THEN
                v_name := COALESCE(v_value->>'value', v_value->>'text', v_value->>'name');
            ELSE
                v_name := v_value #>> '{}';
            END IF;
            v_name := regexp_replace(trim(COALESCE(v_name, '')), '[[:space:]]+', ' ', 'g');
            IF v_name = '' THEN
                RAISE EXCEPTION 'Validated identity proposal cannot establish a blank Legal Name';
            END IF;

            SELECT j.customer_id
            INTO v_customer_id
            FROM auditcore.journeys j
            WHERE j.tenant_id = NEW.tenant_id
              AND j.journey_id = NEW.journey_id;

            IF v_customer_id IS NULL THEN
                RAISE EXCEPTION 'UC03 Legal Name proposal is not linked to a Customer';
            END IF;

            SELECT c.legal_name
            INTO v_existing
            FROM auditcore.customers c
            WHERE c.tenant_id = NEW.tenant_id
              AND c.customer_id = v_customer_id
            FOR UPDATE;

            v_existing_norm := lower(regexp_replace(COALESCE(v_existing, ''), '[[:space:][:punct:]]+', '', 'g'));
            v_candidate_norm := lower(regexp_replace(v_name, '[[:space:][:punct:]]+', '', 'g'));

            IF v_existing IS NULL OR v_existing_norm = '' OR v_existing_norm = v_candidate_norm THEN
                UPDATE auditcore.customers
                SET legal_name = v_name,
                    legal_name_status = 'VERIFIED',
                    legal_name_source_evidence_id = NEW.source_evidence_id,
                    legal_name_verified_by_actor_id = NEW.accepted_by_actor_id,
                    legal_name_verified_at_utc = COALESCE(NEW.accepted_at_utc, now()),
                    updated_by_actor_id = NEW.accepted_by_actor_id,
                    updated_at_utc = now(),
                    version_no = version_no + 1
                WHERE tenant_id = NEW.tenant_id
                  AND customer_id = v_customer_id;
            ELSE
                UPDATE auditcore.customers
                SET legal_name_status = 'CONFLICT',
                    updated_by_actor_id = NEW.accepted_by_actor_id,
                    updated_at_utc = now(),
                    version_no = version_no + 1
                WHERE tenant_id = NEW.tenant_id
                  AND customer_id = v_customer_id;
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_uc03_apply_legal_name_proposal
        AFTER UPDATE OF proposal_status ON auditcore.journey_capture_proposals
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.apply_uc03_legal_name_proposal()
        """
    )

    # Actual Booking Date may legitimately pre-date Audit Captured At for delayed
    # cases. It may not be in the future relative to the Project's local timezone.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.validate_actual_booking_date()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_timezone varchar;
            v_local_today date;
        BEGIN
            IF NEW.booking_date IS NULL THEN
                RETURN NEW;
            END IF;

            SELECT p.timezone_name
            INTO v_timezone
            FROM auditcore.journeys j
            JOIN auditcore.projects p ON p.tenant_id = j.tenant_id
            WHERE j.tenant_id = NEW.tenant_id
              AND j.journey_id = NEW.journey_id;

            IF v_timezone IS NULL THEN
                RAISE EXCEPTION 'Actual Booking Date cannot be validated without Project timezone';
            END IF;

            SELECT (now() AT TIME ZONE v_timezone)::date INTO v_local_today;
            IF NEW.booking_date > v_local_today THEN
                RAISE EXCEPTION 'Actual Booking Date cannot be in the future';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_actual_booking_date
        BEFORE INSERT OR UPDATE OF booking_date ON auditcore.bookings
        FOR EACH ROW
        EXECUTE FUNCTION auditcore.validate_actual_booking_date()
        """
    )

    op.execute("COMMENT ON COLUMN auditcore.customers.display_name IS 'Entered Name captured by the Process Coordinator at Journey creation; immutable after creation.'")
    op.execute("COMMENT ON COLUMN auditcore.customers.legal_name IS 'Legal Name validated from an approved identity document (PAN/Aadhaar).'")
    op.execute("COMMENT ON COLUMN auditcore.bookings.booking_date IS 'Actual Booking Date: real-world date on which the dealer/customer made the Booking.'")
    op.execute("COMMENT ON COLUMN auditcore.journeys.created_at_utc IS 'Audit Captured At: immutable timestamp when Verigence created the Journey.'")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_validate_actual_booking_date ON auditcore.bookings")
    op.execute("DROP FUNCTION IF EXISTS auditcore.validate_actual_booking_date()")
    op.execute("DROP TRIGGER IF EXISTS trg_uc03_apply_legal_name_proposal ON auditcore.journey_capture_proposals")
    op.execute("DROP FUNCTION IF EXISTS auditcore.apply_uc03_legal_name_proposal()")
    op.execute("DROP TRIGGER IF EXISTS trg_customer_entered_name_immutable ON auditcore.customers")
    op.execute("DROP FUNCTION IF EXISTS auditcore.protect_customer_entered_name()")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_customers_legal_name_status")
    op.execute("ALTER TABLE auditcore.customers DROP CONSTRAINT IF EXISTS fk_customers_legal_name_evidence")
    op.execute("ALTER TABLE auditcore.customers DROP CONSTRAINT IF EXISTS ck_customers_legal_name_status")
    op.execute(
        """
        ALTER TABLE auditcore.customers
            DROP COLUMN IF EXISTS legal_name_verified_at_utc,
            DROP COLUMN IF EXISTS legal_name_verified_by_actor_id,
            DROP COLUMN IF EXISTS legal_name_source_evidence_id,
            DROP COLUMN IF EXISTS legal_name_status,
            DROP COLUMN IF EXISTS legal_name
        """
    )
