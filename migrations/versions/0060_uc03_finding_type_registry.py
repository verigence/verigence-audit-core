"""UC03 finding-type registry: finding_type_code -> class / owner / mode.

Revision ID: 0060
Revises: 0059
Create Date: 2026-09-07

0059 added finding_class / owner_role_code / sla_due_at_utc to audit_findings but
only two of the nine producers stamp them. This adds a small reference table that
maps every finding_type_code the platform emits to a class, so:

  * every producer resolves the same way (audit_core.uc03_finding_classification)
  * a Super Admin can reclassify a type at runtime, no deploy
  * a type nobody classified is auto-inserted here with status='UNCLASSIFIED'
    (and metered), so "did we classify everything?" is a query, not a guess.

Reference data — no tenant_id, no RLS. Seeded with the known vocabulary and used
to backfill any still-open finding that a non-stamping producer left NULL.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"

# finding_type_code -> finding_class. Owner role + resolution mode are derived
# from the class (PC/SELF_SERVICE for gaps, TL/ADJUDICATED for violations) but
# stored so they can be overridden per type.
_SEED: dict[str, str] = {
    # ── document gaps (PC self-serve) ──────────────────────────────────────────
    "DOCUMENT_EXCEPTION": "DOCUMENT_GAP",
    "DELIVERY_DOCUMENT_MISSING": "DOCUMENT_GAP",
    "REQUIRED_DOCUMENT_ANSWER_NO": "DOCUMENT_GAP",
    # ── data gaps (PC self-serve) ─────────────────────────────────────────────
    "PAYMENT_EXCEPTION": "DATA_GAP",
    "PAYMENT_UNVERIFIED": "DATA_GAP",
    "DELIVERY_NOT_INTIMATED": "DATA_GAP",
    # ── violations (TL / PM adjudicate) ───────────────────────────────────────
    "VIN_RECONCILIATION_MISMATCH": "VIOLATION",
    "DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE": "VIOLATION",
    "BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY": "VIOLATION",
    "DUPLICATE_BOOKING": "VIOLATION",
    "COMMERCIAL_EXCEPTION": "VIOLATION",
    "PROCESS_NON_COMPLIANCE": "VIOLATION",
    "CUSTOMER_IDENTITY_CONCERN": "VIOLATION",
    "PHYSICAL_OBSERVATION": "VIOLATION",
    "DELIVERY_EXCEPTION": "VIOLATION",
    "OTHER": "VIOLATION",
    # rule-engine anomaly categories (materialised by uc03_rule_engine_findings)
    "PRICING_ANOMALY": "VIOLATION",
    "DISCOUNT_ANOMALY": "VIOLATION",
    "ACCESSORY_ANOMALY": "VIOLATION",
    "INSURANCE_ANOMALY": "VIOLATION",
    "RTO_ANOMALY": "VIOLATION",
    "VEHICLE_IDENTITY_ANOMALY": "VIOLATION",
    "CROSS_CASE_DUPLICATE": "VIOLATION",
    "RULE_ENGINE_ANOMALY": "VIOLATION",
}

_OWNER = {"DATA_GAP": "PC", "DOCUMENT_GAP": "PC", "VIOLATION": "TL"}
_MODE = {"DATA_GAP": "SELF_SERVICE", "DOCUMENT_GAP": "SELF_SERVICE", "VIOLATION": "ADJUDICATED"}


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS auditcore.finding_types (
                finding_type_code   varchar(100) PRIMARY KEY,
                finding_class       varchar(30) NOT NULL
                                    CHECK (finding_class IN ('DATA_GAP','DOCUMENT_GAP','VIOLATION')),
                default_owner_role  varchar(80) NOT NULL,
                resolution_mode     varchar(20) NOT NULL
                                    CHECK (resolution_mode IN ('SELF_SERVICE','ADJUDICATED')),
                status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
                                    CHECK (status IN ('ACTIVE','UNCLASSIFIED','RETIRED')),
                description         text,
                created_at_utc      timestamptz NOT NULL DEFAULT now(),
                updated_at_utc      timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER trg_finding_types_updated "
            "BEFORE UPDATE ON auditcore.finding_types "
            "FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()"
        )
    )
    conn.execute(
        text(f"GRANT SELECT, INSERT, UPDATE ON auditcore.finding_types TO {_RUNTIME_ROLE}")
    )
    conn.execute(text(f"REVOKE DELETE ON auditcore.finding_types FROM {_RUNTIME_ROLE}"))

    for code, cls in _SEED.items():
        conn.execute(
            text(
                """
                INSERT INTO auditcore.finding_types
                    (finding_type_code, finding_class, default_owner_role, resolution_mode, status)
                VALUES (:code, :cls, :owner, :mode, 'ACTIVE')
                ON CONFLICT (finding_type_code) DO UPDATE SET
                    finding_class = EXCLUDED.finding_class,
                    default_owner_role = EXCLUDED.default_owner_role,
                    resolution_mode = EXCLUDED.resolution_mode,
                    status = 'ACTIVE',
                    updated_at_utc = now()
                """
            ),
            {"code": code, "cls": cls, "owner": _OWNER[cls], "mode": _MODE[cls]},
        )

    # Backfill any still-open finding a non-stamping producer left NULL.
    conn.execute(
        text(
            """
            UPDATE auditcore.audit_findings f
            SET finding_class   = ft.finding_class,
                owner_role_code  = ft.default_owner_role,
                sla_due_at_utc   = f.created_at_utc + make_interval(hours =>
                    CASE
                      WHEN ft.finding_class = 'VIOLATION' THEN
                        CASE upper(coalesce(f.severity,'MEDIUM'))
                          WHEN 'CRITICAL' THEN 8 WHEN 'HIGH' THEN 24 WHEN 'MEDIUM' THEN 48
                          WHEN 'LOW' THEN 96 WHEN 'INFO' THEN 120 ELSE 48 END
                      ELSE
                        CASE upper(coalesce(f.severity,'MEDIUM'))
                          WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 8 WHEN 'MEDIUM' THEN 24
                          WHEN 'LOW' THEN 48 WHEN 'INFO' THEN 72 ELSE 24 END
                    END)
            FROM auditcore.finding_types ft
            WHERE f.finding_type_code = ft.finding_type_code
              AND f.finding_class IS NULL
              AND f.finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS auditcore.finding_types CASCADE"))
