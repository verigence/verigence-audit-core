"""0142_oem_master_source_object — keep the master document that was uploaded.

An OEM master upload is parsed and the file then thrown away: only its
name and SHA-256 survive in ``oem_master_uploads``. So nobody can open the
price list or scheme bulletin a figure was read from, and a PC questioning
a price has nothing to check it against.

``source_object_key`` records where the uploaded bytes were put in object
storage, under

    {tenant_id}/oem-masters/{OEM_CODE}/{YYYY-MM}/{MASTER_KIND}/{upload_id}_{filename}

keyed on the document's own effective month rather than the day it was
uploaded, because that is the month it applies to. The upload id in the
object name keeps a re-upload of the same month from overwriting the file
it supersedes -- uploads are not idempotent, so two files for one month is
a normal state, not a fault.

Nullable, and left null for every upload made before this: those files
were never kept and cannot be recovered. A reader sees the document was
not stored rather than a broken link.
"""
from alembic import op

revision = "0142_oem_master_source_object"
down_revision = "0141_p2_runtime_price_variance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.oem_master_uploads "
        "ADD COLUMN IF NOT EXISTS source_object_key varchar(600)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.oem_master_uploads DROP COLUMN IF EXISTS source_object_key"
    )
