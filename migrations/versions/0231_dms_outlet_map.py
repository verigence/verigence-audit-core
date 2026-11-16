"""0231_dms_outlet_map -- how a dealer's DMS locations map to the Verigence outlets
(product owner 2026-11-16).

"We need to give the report outlet wise, but there is small catch ... for shivnath,
no issues, DMS will give you mapping for two outlets it has, for utkal there is only
one deviation that anandpur is mapped under keonjhar ... for Aditya its quite complex,
Jajpur is one and rest all are in one, so DMS computation should be as per that, but
you can show verigence count and total count across."

p2_dms_outlet_map, one row per Verigence outlet that needs one (an outlet with no row
is its own DMS location, named as the outlet is):

  dms_outlet_id   the outlet under whose DMS location this outlet's bookings and
                  deliveries appear (itself when the DMS keeps it apart). The DMS
                  figures of a location are compared with the Verigence figures of
                  every outlet under it, together.
  dms_labels      the names the DMS / the dealer's register uses for this outlet
                  ("CSA_SEC_10_SH" for CDA, "PURIBYPASS" for Kesora / Puri Bypass),
                  matched letters and digits only, case ignored.
  catch_all       a dump row of the dealership whose location names no outlet falls
                  here (Aditya: everything but Jajpur is Cuttack's).

Seeded by outlet code for the Mahindra Orissa outlets (any Project carrying those
codes): Utkal ANANDAPUR under KEONJHAR; every Aditya outlet but JAJPUR under CUTTACK
(Cuttack the catch-all); the location names the 5-16 Nov dumps used that the outlet
names alone did not find. A Team Lead changes it on the Data uploads screen.
"""
from __future__ import annotations

from alembic import op

revision = "0231_dms_outlet_map"
down_revision = "0230_shown_customer_name"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"

# outlet code -> (code of the outlet it reports under in the DMS, location names, catch-all)
_SEED: dict[str, tuple[str, list[str], bool]] = {
    "UM-MAH-ANANDAPUR": ("UM-MAH-KEONJHAR", ["ANANDAPUR", "ANANDPUR"], False),
    "UM-MAH-CDA": ("UM-MAH-CDA", ["CDA", "CSA_SEC_10_SH", "CDA_SEC_10_SH"], False),
    "UM-MAH-MANCHESWAR": ("UM-MAH-MANCHESWAR", ["MANCHESWAR", "MANCHESWAR & CUBE"], False),
    "UM-MAH-MANGULI": ("UM-MAH-MANGULI", ["MANGULI", "MANGULI_SH", "CUTTACK 3S"], False),
    "UM-MAH-RAGHUNATHPUR": ("UM-MAH-RAGHUNATHPUR", ["RAGHUNATHPUR", "RAGHUNATH_PUR_SH"], False),
    "UM-MAH-PALASUNI": ("UM-MAH-PALASUNI", ["PALASUNI", "PALASUNI COMMERCIAL"], False),
    "UM-MAH-CUBEUNIT3": ("UM-MAH-CUBEUNIT3", ["CUBE", "CUBE_UNIT3"], False),
    "SM-MAH-KESORA": ("SM-MAH-KESORA", ["PURI_BY_PASS_SH", "PURIBYPASS", "PURI BY PASS", "KESORA"], False),
    "SM-MAH-TAMANDO": ("SM-MAH-TAMANDO", ["TAMANDO", "TAMANDO_3S"], False),
    "AM-MAH-JAJPUR": ("AM-MAH-JAJPUR", ["JAJPUR", "JHAJPUR"], False),
    "AM-MAH-CUTTACK": ("AM-MAH-CUTTACK", ["CUTTACK"], True),
    "AM-MAH-CUBE": ("AM-MAH-CUTTACK", ["BHUBANESHWAR_SH", "BHUBANESWAR", "CUBE"], False),
    "AM-MAH-JAGATSINGHPUR": ("AM-MAH-CUTTACK", ["JAGATSINGHPUR"], False),
    "AM-MAH-KENDRAPARA": ("AM-MAH-CUTTACK", ["KENDRAPARA", "KENDRAPARA_SZS"], False),
    "AM-MAH-KHORDHA": ("AM-MAH-CUTTACK", ["KHORDHA", "KHORDHA_SZS"], False),
    "AM-MAH-NUAPATANA": ("AM-MAH-CUTTACK", ["NUAPATANA", "NUAPATNA"], False),
    "AM-MAH-PURI": ("AM-MAH-CUTTACK", ["PURI"], False),
}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_dms_outlet_map (
            tenant_id        varchar(128) NOT NULL,
            outlet_id        uuid         NOT NULL,
            dealer_id        uuid         NOT NULL,
            dms_outlet_id    uuid         NOT NULL,
            dms_labels       text[]       NOT NULL DEFAULT '{}',
            catch_all        boolean      NOT NULL DEFAULT false,
            updated_by       varchar(200),
            updated_at_utc   timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, outlet_id)
        )
        """
    )
    op.execute("ALTER TABLE auditcore.p2_dms_outlet_map ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.p2_dms_outlet_map FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_p2_dms_outlet_map ON auditcore.p2_dms_outlet_map")
    op.execute(
        "CREATE POLICY tenant_isolation_p2_dms_outlet_map ON auditcore.p2_dms_outlet_map "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.p2_dms_outlet_map TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.p2_dms_outlet_map FROM {_RUNTIME_ROLE}")
    for code, (under, labels, catch_all) in _SEED.items():
        literal = "ARRAY[" + ", ".join("'" + label.replace("'", "''") + "'" for label in labels) + "]::text[]"
        op.execute(
            f"""
            INSERT INTO auditcore.p2_dms_outlet_map (tenant_id, outlet_id, dealer_id, dms_outlet_id, dms_labels,
                                                     catch_all, updated_by)
            SELECT o.tenant_id, o.outlet_id, o.dealer_id, a.outlet_id, {literal}, {str(catch_all).lower()}, 'migration'
            FROM auditcore.dealer_outlets o
            JOIN auditcore.dealer_outlets a
              ON a.tenant_id = o.tenant_id AND a.dealer_id = o.dealer_id AND a.outlet_code = '{under}'
            WHERE o.outlet_code = '{code}'
            ON CONFLICT (tenant_id, outlet_id) DO NOTHING
            """
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_dms_outlet_map")
