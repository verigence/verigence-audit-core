"""DMS dumps: dates taken from the DMS on a sure match, and model differences listed.

Revision ID: 0252_dms_strong_match_sync
Revises: 0251_veero_g1_model_alias

Product owner 2026-11-21: when a DMS row matches a Journey on the OTF / booking number (booking
dump) or the chassis (delivery dump), the DMS date is the Journey's date -- corrected on the
booking form / gate pass through the field correction path, logged as DMS_SYNC (the DMS wins
over a hand correction, the earlier value kept in the log). The mobile number and the name are
never changed. A model that differs is listed for the person who uploaded the dump.

- p2_dms_dump_rows.journey_model: the Journey's model on a sure match
- p2_dms_dump_rows.model_differs: the DMS model is not the Journey's
- p2_dms_dump_rows.date_sync: the date taken from the DMS, or why it was not
- journey_field_change_log.change_reason: DMS_SYNC
- p2_tenant_alerts.alert_kind: DMS_MODEL_DIFFERS (to the person who uploaded the dump)
"""
from __future__ import annotations

from alembic import op

revision = "0252_dms_strong_match_sync"
down_revision = "0251_veero_g1_model_alias"
branch_labels = None
depends_on = None

_KINDS_BEFORE = ("SAME_FILE_UPLOADED", "DUPLICATE_ACROSS_OUTLETS", "DUPLICATE_SAME_DEALERSHIP",
                 "MY_DOCUMENTS_ATTENTION", "DMS_DUMP_UPLOADED")
_KINDS = (*_KINDS_BEFORE, "DMS_MODEL_DIFFERS")


def _alert_kinds(kinds: tuple[str, ...]) -> None:
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    listed = ", ".join(f"'{k}'" for k in kinds)
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
               f"CHECK (alert_kind IN ({listed}))")


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_dms_dump_rows
          ADD COLUMN IF NOT EXISTS journey_model varchar(160),
          ADD COLUMN IF NOT EXISTS model_differs boolean,
          ADD COLUMN IF NOT EXISTS date_sync jsonb;

        ALTER TABLE auditcore.journey_field_change_log
          DROP CONSTRAINT IF EXISTS journey_field_change_log_change_reason_check;
        ALTER TABLE auditcore.journey_field_change_log
          ADD CONSTRAINT journey_field_change_log_change_reason_check
          CHECK (change_reason IN ('CORRECTION', 'RESYNC_OVERWRITE', 'DMS_SYNC'));
        """
    )
    _alert_kinds(_KINDS)


def downgrade() -> None:
    op.execute("DELETE FROM auditcore.p2_tenant_alerts WHERE alert_kind = 'DMS_MODEL_DIFFERS'")
    _alert_kinds(_KINDS_BEFORE)
    op.execute(
        """
        ALTER TABLE auditcore.journey_field_change_log
          DROP CONSTRAINT IF EXISTS journey_field_change_log_change_reason_check;
        ALTER TABLE auditcore.journey_field_change_log
          ADD CONSTRAINT journey_field_change_log_change_reason_check
          CHECK (change_reason IN ('CORRECTION', 'RESYNC_OVERWRITE'));
        ALTER TABLE auditcore.p2_dms_dump_rows
          DROP COLUMN IF EXISTS date_sync,
          DROP COLUMN IF EXISTS model_differs,
          DROP COLUMN IF EXISTS journey_model;
        """
    )
