"""Alembic keeps the revision id in alembic_version.version_num, a varchar(32):
a longer id fails the migration on every database it runs against."""
from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def test_every_migration_revision_id_fits_alembic_version_column():
    too_long = []
    for path in sorted(VERSIONS.glob("*.py")):
        match = re.search(r'^revision\s*=\s*["\']([^"\']+)["\']', path.read_text(), re.MULTILINE)
        if match and len(match.group(1)) > 32:
            too_long.append(f"{path.name}: {match.group(1)} ({len(match.group(1))})")
    assert not too_long, too_long
