"""0211_journey_classifications -- Deal Type, Customer Type and the insurance decision, stored (2026-11-11).

Product owner 2026-11-11: every Journey carries a Deal Type (Inscope, Outscope, MR, Lease, Bulk)
and a Customer Type (Individual, Corporate, Company), filled for the Journeys already on file; and
"some of the journeys don't have updated insurance either self or dealership".

Found on Mahindra Orissa the same day: ``bookings.deal_type_code`` is empty and
``customers.customer_type_code`` is PENDING on all 1,268 Journeys (nothing ever wrote them), and
``insurance_records.insurance_source`` holds only a PC's own answer (108 of 795 Journeys in
delivery) -- what the documents decide (the dealer's Agent / MISP code on 288 policies, the premium
on the dealer's statement) was worked out again on every screen and never kept, so every list and
report reading the column saw nothing.

``journey_classifications`` is the one stored copy of the three facts, written by
``uc03_p2_journey_classification.refresh`` on every recompute of a Journey, with what each was
decided on. The Deal Type stays empty while the Journey has no VIN to read the vehicle's age from;
a Team Lead's own Deal Type is kept on the same row and wins over the rule.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0211_journey_classifications"
down_revision = "0210_geography_masters"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "journey_classifications"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id               varchar(128) NOT NULL,
            journey_id              uuid         NOT NULL,
            -- Empty until the VIN is read or the Team Lead decides (product owner 2026-11-11).
            deal_type               varchar(20)
                CHECK (deal_type IN ('INSCOPE', 'OUTSCOPE', 'MR', 'LEASE', 'BULK')),
            deal_type_decided_by    varchar(20)  CHECK (deal_type_decided_by IN ('RULE', 'TL')),
            deal_type_basis         text,
            vin_pending             boolean      NOT NULL DEFAULT false,
            booking_date_used       date,
            vin_used                varchar(32),
            vin_manufacture_month   date,
            age_reference_date      date,
            age_reference_kind      varchar(20),
            vehicle_age_days        integer,
            tl_deal_type            varchar(20)
                CHECK (tl_deal_type IN ('INSCOPE', 'OUTSCOPE', 'MR', 'LEASE', 'BULK')),
            tl_reason               text,
            tl_set_by_actor_id      varchar(128),
            tl_set_by_role          varchar(20),
            tl_set_at_utc           timestamptz,
            customer_type           varchar(20)  NOT NULL
                CHECK (customer_type IN ('INDIVIDUAL', 'CORPORATE', 'COMPANY')),
            customer_type_basis     text,
            insurance_type          varchar(20)  CHECK (insurance_type IN ('DEALERSHIP', 'SELF')),
            insurance_decided_by    varchar(20)
                CHECK (insurance_decided_by IN ('PC', 'DOCUMENT', 'UNCONFIRMED', 'DEFAULT')),
            insurance_basis         text,
            computed_at_utc         timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id),
            FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys (tenant_id, journey_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deal_type ON auditcore.{_TABLE} (tenant_id, deal_type)")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
