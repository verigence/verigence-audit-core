"""0221_lookup_indexes -- indexes for the discount scheme and same-customer lookups.

Product owner, 13 Nov 2026: fix the slow queries found in the Neon cost review.
Measured on the live database (Mahindra Orissa):

- The deal's discount schemes in force (69,700 calls since 1 Nov, ~17,000 pages
  each): discount_scheme_eligibility and discount_scheme_benefits had no index
  on their scheme version, so each version in force re-read the whole table.
- The same-file check's "same PAN or Aadhaar" lookup (17,200 calls, 140 ms):
  every PAN and Aadhaar of the Project was cleaned up and compared at read
  time over a full scan of the 667,000 extracted fields. Two partial indexes
  hold the cleaned-up values (about 4,000 rows); the expressions are the very
  ones the lookup compares (uc03_p2_same_file._SAME_CUSTOMER_SQL).
"""
from __future__ import annotations

from alembic import op

revision = "0221_lookup_indexes"
down_revision = "0220_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_discount_scheme_eligibility_version "
        "ON auditcore.discount_scheme_eligibility (tenant_id, discount_scheme_version_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_discount_scheme_benefits_version "
        "ON auditcore.discount_scheme_benefits (tenant_id, discount_scheme_version_id)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_extracted_fields_pan_clean
          ON auditcore.journey_document_extracted_fields
             (tenant_id, (upper(regexp_replace(COALESCE(effective_value #>> '{}', ''), '[^A-Za-z0-9]', '', 'g'))))
          WHERE field_key = 'pan_number' AND effective_value IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_extracted_fields_aadhaar_clean
          ON auditcore.journey_document_extracted_fields
             (tenant_id, (regexp_replace(COALESCE(effective_value #>> '{}', ''), '[^0-9]', '', 'g')))
          WHERE field_key = 'aadhaar_number' AND effective_value IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_extracted_fields_aadhaar_clean")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_extracted_fields_pan_clean")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_discount_scheme_benefits_version")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_discount_scheme_eligibility_version")
