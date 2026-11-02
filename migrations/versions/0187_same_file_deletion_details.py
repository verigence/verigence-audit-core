"""0187_same_file_deletion_details -- what a copy deleted as the same file keeps (2026-11-02).

Product owner, 2026-11-02: a new Journey is deleted as the same file
uploaded again only when its PAN and/or Aadhaar, mobile, model and booking
reference number are an earlier Journey's (uc03_p2_same_file). Every booking
deleted by that rule is listed under Duplicate bookings, tab "Deleted", with
the PC, outlet, model, PAN, Aadhaar, mobile and booking reference number.
The Journey's own rows are gone by then, so the deletion record keeps them:

- deletion_kind            SAME_FILE for a copy this rule deleted (NULL: a
                           Team Lead's delete, or the duplicate purge)
- customer_pan             the PAN as read, in full
- customer_aadhaar_masked  the Aadhaar's last four digits only, "XXXX XXXX
                           1234" -- the full number is never stored
- customer_mobile          the mobile, in full (10 digits)
- booking_reference        the booking reference number as read
- model_name               the model
- kept_journey_id / kept_journey_reference   the copy that was kept

All nullable, additive; the runtime role's INSERT already covers them and
its UPDATE stays limited to finishing a deletion.

Backfill, past same-file deletions (reason "Same file uploaded again: ...",
deleted by SYSTEM): deletion_kind, the model and the copy kept from the
SAME_FILE_UPLOADED alert, and the PAN and mobile as that alert holds them --
masked ("XXXXXX234F"), the only form still on file. Their Aadhaar and
booking reference number were never kept and stay blank. Row security
applies to the migration role too (FORCE): the Projects are listed as the
platform SuperAdmin and each is updated as itself.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0187_same_file_deletion_details"
down_revision = "0186_journey_snapshots"
branch_labels = None
depends_on = None

_TABLE = "p2_journey_deletions"
_COLUMNS = (
    ("deletion_kind", "varchar(40)"),
    ("customer_pan", "varchar(20)"),
    ("customer_aadhaar_masked", "varchar(20)"),
    ("customer_mobile", "varchar(20)"),
    ("booking_reference", "varchar(200)"),
    ("model_name", "varchar(400)"),
    ("kept_journey_id", "uuid"),
    ("kept_journey_reference", "varchar(200)"),
)


def upgrade() -> None:
    for name, sql_type in _COLUMNS:
        op.execute(f"ALTER TABLE auditcore.{_TABLE} ADD COLUMN IF NOT EXISTS {name} {sql_type}")
    # The Aadhaar column only ever holds the masked form.
    op.execute(f"ALTER TABLE auditcore.{_TABLE} DROP CONSTRAINT IF EXISTS ck_{_TABLE}_aadhaar_masked")
    op.execute(
        f"ALTER TABLE auditcore.{_TABLE} ADD CONSTRAINT ck_{_TABLE}_aadhaar_masked "
        "CHECK (customer_aadhaar_masked IS NULL OR customer_aadhaar_masked ~ '^XXXX XXXX [0-9]{4}$')"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_kind_time ON auditcore.{_TABLE} "
        "(tenant_id, deletion_kind, requested_at_utc DESC) WHERE deletion_kind IS NOT NULL"
    )
    _backfill(op.get_bind())


def _backfill(connection) -> None:
    connection.execute(text("SELECT set_config('app.platform_super_admin', 'true', true)"))
    tenants = connection.execute(text("SELECT tenant_id FROM auditcore.projects ORDER BY tenant_id")).scalars().all()
    connection.execute(text("SELECT set_config('app.platform_super_admin', '', true)"))
    for tenant_id in tenants:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        connection.execute(
            text(
                f"""
                UPDATE auditcore.{_TABLE} d
                SET deletion_kind = 'SAME_FILE',
                    model_name = COALESCE(d.model_name, a.payload ->> 'model'),
                    kept_journey_id = COALESCE(d.kept_journey_id, a.related_journey_id),
                    kept_journey_reference = COALESCE(d.kept_journey_reference,
                                                      a.payload -> 'existingJourney' ->> 'reference'),
                    customer_pan = COALESCE(d.customer_pan, (
                        SELECT left(m ->> 'newValue', 20) FROM jsonb_array_elements(
                            CASE WHEN jsonb_typeof(a.payload -> 'matchedFields') = 'array'
                                 THEN a.payload -> 'matchedFields' ELSE '[]'::jsonb END) m
                        WHERE m ->> 'field' = 'pan' LIMIT 1)),
                    customer_mobile = COALESCE(d.customer_mobile, (
                        SELECT left(m ->> 'newValue', 20) FROM jsonb_array_elements(
                            CASE WHEN jsonb_typeof(a.payload -> 'matchedFields') = 'array'
                                 THEN a.payload -> 'matchedFields' ELSE '[]'::jsonb END) m
                        WHERE m ->> 'field' = 'mobile' LIMIT 1))
                FROM auditcore.p2_tenant_alerts a
                WHERE d.tenant_id = :t AND d.deletion_kind IS NULL
                  AND d.deleted_by_role = 'SYSTEM' AND d.reason LIKE 'Same file uploaded again:%'
                  AND a.tenant_id = d.tenant_id AND a.alert_kind = 'SAME_FILE_UPLOADED'
                  AND a.subject_journey_id = d.journey_id
                """
            ),
            {"t": tenant_id},
        )
        # A same-file deletion whose alert is gone: still marked as one.
        connection.execute(
            text(
                f"""
                UPDATE auditcore.{_TABLE} SET deletion_kind = 'SAME_FILE'
                WHERE tenant_id = :t AND deletion_kind IS NULL
                  AND deleted_by_role = 'SYSTEM' AND reason LIKE 'Same file uploaded again:%'
                """
            ),
            {"t": tenant_id},
        )
    connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS auditcore.ix_{_TABLE}_kind_time")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} DROP CONSTRAINT IF EXISTS ck_{_TABLE}_aadhaar_masked")
    for name, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE auditcore.{_TABLE} DROP COLUMN IF EXISTS {name}")
