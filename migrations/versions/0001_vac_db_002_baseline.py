import hashlib
from pathlib import Path

from alembic import op

revision = "0001_vac_db_002"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "database" / "AUDIT_CORE_POSTGRESQL_SCHEMA_v2.1.sql"
_SCHEMA_GIT_BLOB_SHA = "5eb7c935e54a730a62a63b6825d19c71733553b7"


def _load_frozen_schema() -> str:
    data = _SCHEMA_PATH.read_bytes()
    git_blob = f"blob {len(data)}\0".encode() + data
    actual_sha = hashlib.sha1(git_blob, usedforsecurity=False).hexdigest()
    if actual_sha != _SCHEMA_GIT_BLOB_SHA:
        raise RuntimeError(
            "VAC-DB-002 source changed after migration baseline creation; "
            "create a new migration instead of mutating the baseline"
        )

    lines = data.decode("utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "BEGIN;":
            del lines[index]
            break

    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == "COMMIT;":
            del lines[index]
            break

    return "\n".join(lines)


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_load_frozen_schema())


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP SCHEMA IF EXISTS auditcore CASCADE")
