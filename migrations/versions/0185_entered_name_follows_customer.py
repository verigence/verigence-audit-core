"""0185_entered_name_follows_customer -- the entered name may follow the booking's own customer (2026-11-01).

The entered name (customers.display_name) is immutable once set
(protect_customer_entered_name, 0018), except the one-time move from the
Journey-id placeholder to the first verified legal name (0058). On a booking
another customer's file was dropped into, that first name was the other
customer's ("CHINMAYA SENAPATI" on Bikash Kumar Martha's ADI-RD-261022-018),
and it stayed after the file was taken out (uc03_p2_file_identity).

The guard now also lets the change through when the transaction set
``auditcore.entered_name_follows_customer`` to 'on' -- only
uc03_p2_customer.follow_entered_name does, for an entered name that is a
person of a file taken out of the booking. Everything else is unchanged.
"""
from __future__ import annotations

from alembic import op

revision = "0185_entered_name_follows"
down_revision = "0184_file_move_book_as"
branch_labels = None
depends_on = None


def _function(bypass: str) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION auditcore.protect_customer_entered_name()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_placeholder boolean := false;
        BEGIN
            {bypass}
            IF NEW.display_name IS DISTINCT FROM OLD.display_name
               AND EXISTS (
                    SELECT 1
                    FROM auditcore.journeys j
                    WHERE j.tenant_id = OLD.tenant_id
                      AND j.customer_id = OLD.customer_id
               ) THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM auditcore.journeys j
                    WHERE j.tenant_id = OLD.tenant_id
                      AND j.customer_id = OLD.customer_id
                      AND OLD.display_name = j.journey_id::text
                )
                INTO v_placeholder;

                IF NOT (
                    v_placeholder
                    AND NEW.legal_name IS NOT NULL
                    AND btrim(NEW.legal_name) <> ''
                    AND NEW.display_name = NEW.legal_name
                ) THEN
                    NEW.display_name := OLD.display_name;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """


def upgrade() -> None:
    op.execute(_function(
        "IF current_setting('auditcore.entered_name_follows_customer', true) = 'on' THEN\n"
        "                RETURN NEW;\n"
        "            END IF;"
    ))


def downgrade() -> None:
    op.execute(_function(""))
