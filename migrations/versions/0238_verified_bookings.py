"""0238_verified_bookings -- bookings a Team Lead or Reviewer has checked against their
papers, and every later comparison of their figures (product owner, 2026-11-17).

Why: a change made for one reported booking kept moving figures on others unseen
("why so many mistakes ... can we do introspection"). A booking whose figures a
person has checked against its documents is kept with those figures; each night,
after each deploy and on demand the system works them out again and says, per
booking, Matches or Changed -- and whether its papers changed since (verify it
again) or not (the system moved it).

p2_verified_bookings        one row per booking marked verified: who, when, the
                            comment, the figures as verified and the fingerprint of
                            the papers they rest on; withdrawn, never deleted
p2_verified_booking_checks  each comparison: when, why (daily, deploy, on demand,
                            on marking), the code version, Matches / Changed /
                            Papers changed / Error, and the figures that moved

Additive; idempotent. Tenant RLS; the runtime role cannot DELETE.
"""
from __future__ import annotations

from alembic import op

revision = "0238_verified_bookings"
down_revision = "0237_scheme_dumps"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON auditcore.{table}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON auditcore.{table} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table} TO {_RUNTIME_ROLE}")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_verified_bookings (
            tenant_id          varchar(128) NOT NULL,
            journey_id         uuid         NOT NULL,
            journey_reference  varchar(80),
            status             varchar(20)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'WITHDRAWN')),
            verified_by        varchar(200) NOT NULL,
            verified_by_name   varchar(200),
            verified_role      varchar(40),
            verified_at_utc    timestamptz  NOT NULL DEFAULT now(),
            comment            text,
            figures            jsonb        NOT NULL,
            papers             varchar(64)  NOT NULL,
            code_version       varchar(40),
            withdrawn_by       varchar(200),
            withdrawn_at_utc   timestamptz,
            withdrawn_reason   text,
            PRIMARY KEY (tenant_id, journey_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_verified_booking_checks (
            tenant_id          varchar(128) NOT NULL,
            check_id           uuid         NOT NULL DEFAULT gen_random_uuid(),
            journey_id         uuid         NOT NULL,
            checked_at_utc     timestamptz  NOT NULL DEFAULT now(),
            trigger            varchar(20)  NOT NULL,
            code_version       varchar(40),
            result             varchar(20)  NOT NULL
                               CHECK (result IN ('MATCHES', 'CHANGED', 'PAPERS_CHANGED', 'ERROR')),
            papers_changed     boolean      NOT NULL DEFAULT false,
            changes            jsonb        NOT NULL DEFAULT '[]'::jsonb,
            error              text,
            PRIMARY KEY (tenant_id, check_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_verified_booking_checks_journey "
        "ON auditcore.p2_verified_booking_checks (tenant_id, journey_id, checked_at_utc DESC)"
    )
    _rls("p2_verified_bookings")
    _rls("p2_verified_booking_checks")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_verified_booking_checks")
    op.execute("DROP TABLE IF EXISTS auditcore.p2_verified_bookings")
