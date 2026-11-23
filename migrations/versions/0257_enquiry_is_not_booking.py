"""0257_enquiry_is_not_booking -- an enquiry number kept as the booking number is re-filed (2026-11-23).

Product owner, 23 Nov 2026: "enquiry number cant be booking number". The reading had put
the enquiry number in the booking-number slot of the booking form on many journeys
("EN 0038100001", "ENQ - 0038100002", "0038100003"). From now on such a value is kept as
"enquiry_number" (uc03_enquiry_number.filed_as); this re-files what was kept before, with the
same test in SQL (uc03_enquiry_number.ENQUIRY_SQL), and clears a Journey's booking reference
where it held an enquiry number. The DMS re-match afterwards matches them by the dump's
ENQUIRY NUMBER.
"""
from __future__ import annotations

from alembic import op

revision = "0257_enquiry_is_not_booking"
down_revision = "0256_dms_not_in_dms"
branch_labels = None
depends_on = None

# uc03_enquiry_number.ENQUIRY_SQL, written out: a migration must not change when the module does.
_ENQUIRY = (
    "((v ~* '^\\s*(ENQUIRY|ENQ|EN)([^A-Za-z]|$)' AND v ~ '[0-9]')"
    " OR regexp_replace(v, '[^0-9]', '', 'g') ~ '^00[0-9]{8}$'"
    " OR (ltrim(regexp_replace(v, '[^0-9]', '', 'g'), '0') ~ '^3[89][0-9]{6}$'"
    "     AND v !~* '^\\s*B([^A-Za-z]|$)'))"
)


def upgrade() -> None:
    op.execute(f"""
        UPDATE auditcore.journey_document_extracted_fields f
        SET field_key = 'enquiry_number', updated_at_utc = now()
        FROM (SELECT extracted_field_id, effective_value #>> '{{}}' AS v
              FROM auditcore.journey_document_extracted_fields
              WHERE field_key = 'booking_reference_number'
                AND jsonb_typeof(effective_value) IN ('string', 'number')) x
        WHERE f.extracted_field_id = x.extracted_field_id AND {_ENQUIRY}
    """)
    op.execute(f"""
        UPDATE auditcore.bookings b SET booking_reference = NULL, updated_at_utc = now()
        FROM (SELECT booking_id, booking_reference AS v FROM auditcore.bookings
              WHERE booking_reference IS NOT NULL) x
        WHERE b.booking_id = x.booking_id AND {_ENQUIRY}
    """)


def downgrade() -> None:
    # Not reversible by value: which enquiry numbers were once kept as booking numbers is not
    # recorded. Re-filing them back would bring the defect back.
    pass
