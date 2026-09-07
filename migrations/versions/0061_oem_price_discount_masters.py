"""OEM native price / discount master intake support tables.

Revision ID: 0061
Revises: 0060
Create Date: 2026-09-07

A Super Admin uploads an OEM's own price list + discount documents and this
service ingests them into the existing tenant-scoped versioned masters
(``price_lists`` / ``discount_schemes`` and the shared product catalogue).
Everything lands under the target project's ``tenant_id`` — there is no
OEM-level storage. This migration only adds the three support tables that
ingestion needs:

  * ``oem_model_aliases``       — deterministic "name in the document" ->
                                  "canonical model code" map (reference data,
                                  seeded for Mahindra). Nothing is guessed at
                                  ingest time; an unmapped name is surfaced,
                                  not resolved.
  * ``corporate_privilege_registry`` — the OEM's corporate customer list
                                  (code -> name / type / privilege category),
                                  from the Corporate Privilege Policy workbook.
  * ``oem_master_uploads``      — one row per uploaded file: sha256, kind,
                                  effective-from, target tenant, row counts,
                                  lifecycle. The audit trail for "which master
                                  is live and where did it come from".

Reference tables carry no ``tenant_id`` and no RLS; ``oem_master_uploads`` is
tenant-scoped like the masters it describes.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"

# alias in the discount / scheme documents  ->  the "Model" label the consolidated
# price list uses (verbatim from its Model column). Ingestion slugs both sides the
# same way to reach one product_models.model_code. Only unambiguous 1:1 names are
# seeded; anything not here is reported as unresolved at ingest, never guessed.
_MAHINDRA_MODEL_ALIASES: dict[str, str] = {
    "SCORPIO N": "SCORPIO N",
    "SCORPIO-N": "SCORPIO N",
    "NEW SCORPIO N": "NEW SCORPIO N",
    "SCORPIO CLASSIC": "SCORPIO CLASSIC",
    "SCORPIO": "SCORPIO CLASSIC",
    "THAR": "NEW THAR 2WD & 4WD",
    "NEW THAR": "NEW THAR 2WD & 4WD",
    "THAR ROXX": "THAR ROXX",
    "XUV 3XO": "XUV3XO",
    "XUV3XO": "XUV3XO",
    "3XO REVX": "XUV3XO",
    "XUV 3XO REVX": "XUV3XO",
    "XUV 7XO": "XUV 7XO",
    "XUV7XO": "XUV 7XO",
    "XUV700": "XUV 7XO",
    "XUV 700": "XUV 7XO",
    "XUV400": "XUV400",
    "XUV 400": "XUV400",
    "XUV3XO EV": "XUV3XO EV",
    "XUV 3XO EV": "XUV3XO EV",
    "MARAZZO": "MARAZZO",
    "BOLERO NEO": "THE BOSS- BOLERO+NEO",
    "BOLERO NEO+": "THE BOSS- BOLERO+NEO",
    "BOLERO NEO PLUS": "THE BOSS- BOLERO+NEO",
    "BOLERO": "THE BOSS- BOLERO+NEO",
    "MAXX CITY": "MAXX CITY",
    "MAXX HD": "MAXX HD",
    "PICK UP": "PICK UP",
    "PICKUP": "PICK UP",
    "BOLERO PIK-UP": "PICK UP",
    "CAMPER": "PICK UP",
    "BOLERO CAMPER": "PICK UP",
    "SUPRO": "SUPRO",
    "SUPRO MINI TRUCK": "SUPRO",
    "SUPRO MAXI TRUCK": "SUPRO",
    "SUPRO MINITRUCK": "SUPRO",
    "VEERO": "VEERO",
    "BE 6": "BE 6",
    "BE6": "BE 6",
    "BE6 SPORTEQ": "BE6 SPORTEQ",
    "XEV 9E": "XEV 9E",
    "XEV9E": "XEV 9E",
    "XEV 9S": "XEV9S",
    "XEV9S": "XEV9S",
}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.oem_model_aliases (
            oem_code              varchar(80)  NOT NULL,
            alias_text            varchar(240) NOT NULL,
            canonical_model_name  varchar(200) NOT NULL,
            note                  text,
            created_at_utc        timestamptz NOT NULL DEFAULT now(),
            updated_at_utc        timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (oem_code, alias_text)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auditcore.corporate_privilege_registry (
            oem_code             varchar(80)  NOT NULL,
            corporate_code       varchar(60)  NOT NULL,
            corporate_name       varchar(400) NOT NULL,
            corporate_type       varchar(200),
            privilege_category   varchar(20)  NOT NULL
                                 CHECK (privilege_category IN ('Z','Y','F','A','B')),
            source_upload_id     uuid,
            effective_from       date,
            effective_to         date,
            created_at_utc       timestamptz NOT NULL DEFAULT now(),
            updated_at_utc       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (oem_code, corporate_code)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_corporate_privilege_name "
        "ON auditcore.corporate_privilege_registry (oem_code, lower(corporate_name))"
    )
    op.execute(
        """
        CREATE TABLE auditcore.oem_master_uploads (
            upload_id            uuid NOT NULL DEFAULT gen_random_uuid(),
            tenant_id            varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
            oem_code             varchar(80)  NOT NULL,
            master_kind          varchar(40)  NOT NULL
                                 CHECK (master_kind IN (
                                     'PRICE_LIST','CONSUMER_SCHEME','EXCHANGE_SCHEME','CORPORATE_POLICY'
                                 )),
            source_filename      varchar(400) NOT NULL,
            source_sha256        char(64)     NOT NULL,
            effective_from       date         NOT NULL,
            status               varchar(20)  NOT NULL DEFAULT 'STAGED'
                                 CHECK (status IN ('STAGED','PUBLISHED','SUPERSEDED','FAILED')),
            row_counts           jsonb        NOT NULL DEFAULT '{}'::jsonb,
            preview              jsonb        NOT NULL DEFAULT '{}'::jsonb,
            error_detail         text,
            price_list_version_id        uuid,
            discount_scheme_summary      jsonb NOT NULL DEFAULT '{}'::jsonb,
            uploaded_by_actor_id varchar(160),
            uploaded_at_utc      timestamptz NOT NULL DEFAULT now(),
            published_at_utc     timestamptz,
            updated_at_utc       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, upload_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_oem_master_uploads_id ON auditcore.oem_master_uploads (upload_id)"
    )
    op.execute(
        "CREATE INDEX ix_oem_master_uploads_live "
        "ON auditcore.oem_master_uploads (tenant_id, oem_code, master_kind, effective_from DESC)"
    )

    # updated_at triggers (the baseline DO-block only covered tables that existed then)
    for table_name in (
        "oem_model_aliases",
        "corporate_privilege_registry",
        "oem_master_uploads",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_updated_at "
            f"BEFORE UPDATE ON auditcore.{table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()"
        )

    # oem_master_uploads has tenant_id -> tenant RLS; the Super Admin route sets
    # app.tenant_id to the target project before every read/write.
    op.execute("ALTER TABLE auditcore.oem_master_uploads ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.oem_master_uploads FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation_oem_master_uploads "
        "ON auditcore.oem_master_uploads "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )

    for table_name in (
        "oem_model_aliases",
        "corporate_privilege_registry",
        "oem_master_uploads",
    ):
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table_name} TO {_RUNTIME_ROLE}"
        )
        op.execute(f"REVOKE DELETE ON auditcore.{table_name} FROM {_RUNTIME_ROLE}")

    # corporate registry is a full monthly replacement per (oem, upload); allow the
    # runtime role to clear the prior set before reloading.
    op.execute(
        f"GRANT DELETE ON auditcore.corporate_privilege_registry TO {_RUNTIME_ROLE}"
    )

    connection = op.get_bind()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.oem_model_aliases (oem_code, alias_text, canonical_model_name)
            VALUES (:oem_code, :alias_text, :canonical_model_name)
            ON CONFLICT (oem_code, alias_text) DO UPDATE
              SET canonical_model_name = EXCLUDED.canonical_model_name
            """
        ),
        [
            {
                "oem_code": "MAHINDRA",
                "alias_text": alias,
                "canonical_model_name": canonical,
            }
            for alias, canonical in _MAHINDRA_MODEL_ALIASES.items()
        ],
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.oem_master_uploads")
    op.execute("DROP TABLE IF EXISTS auditcore.corporate_privilege_registry")
    op.execute("DROP TABLE IF EXISTS auditcore.oem_model_aliases")
