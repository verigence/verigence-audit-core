"""0151_bolero_split_model_aliases — the Bolero, Bolero Neo and Bolero Neo Plus
are three models (2026-10-20 product owner rules).

September's price list printed all three as one model, "THE BOSS-
BOLERO+NEO"; October's prints "Bolero", "Bolero neo" and "Bolero Neo Plus",
and October's schemes and corporate policy give them different benefits.
The price-list ingest files both months' rows under three models, BOLERO,
BOLERO NEO and BOLERO NEO PLUS, by the variant's series letter (B4/B6/B8,
N4..N11, P4/P10).

1. The aliases that pointed every Bolero spelling at the combined model now
   point at the three.
2. The catalogue already loaded is split IN PLACE, so nothing pinned to it
   is orphaned: every variant of the combined model moves to the model of
   its series with its SKUs (same product_sku_id, same sku_code, so every
   Journey stays pinned and every published price row still prices it),
   and the configurations follow their variant. Discount-scheme
   eligibility rows move with their variant; a model-wide row goes to the
   model of the single series its own variant texts name, else to all
   three (what it covered before). A dealer-grid row goes to the model its
   alias names ("BOLERO NEO" -> BOLERO NEO), and a model of the three that
   no row of that grid version names takes the nearest row's terms. The
   combined model keeps no variants and is deactivated.

Idempotent: a second run finds no variants left on a combined model. Safe on
a database with none of these rows. Published scheme versions are immutable
by trigger; the eligibility trigger is disabled for the move only, because
the move changes which model row a scheme points at, never what it grants.
"""
from __future__ import annotations

import json
import re

from alembic import op
from sqlalchemy import text

revision = "0151_bolero_split_model_aliases"
down_revision = "0150_more_mahindra_model_aliases"
branch_labels = None
depends_on = None

_ALIASES = {
    "BOLERO": "BOLERO",
    "BOLERO NEO": "BOLERO NEO",
    "BOLERO NEO+": "BOLERO NEO PLUS",
    "BOLERO NEO PLUS": "BOLERO NEO PLUS",
    "BOL NEO": "BOLERO NEO",
    "BOL NEO+": "BOLERO NEO PLUS",
    "BOL NEO PLUS": "BOLERO NEO PLUS",
}
_PREVIOUS = "THE BOSS- BOLERO+NEO"

# series letter -> (squashed identity, model name, model code)
_TARGETS = {
    "B": ("BOLERO", "BOLERO", "BOLERO"),
    "N": ("BOLERONEO", "BOLERO NEO", "BOLERO_NEO"),
    "P": ("BOLERONEOPLUS", "BOLERO NEO PLUS", "BOLERO_NEO_PLUS"),
}
_SERIES_RE = re.compile(r"^\s*([BNP])\s*\d")


def _squash(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def series_of_text(value: str | None) -> str | None:
    """The series a model's wording names, or None for the combined name."""
    upper = (value or "").upper()
    if "BOSS" in upper or "BOLERO+NEO" in upper.replace(" ", ""):
        return None
    if re.search(r"NEO\s*(\+|PLUS)", upper):
        return "P"
    if "NEO" in upper:
        return "N"
    if "BOLERO" in upper:
        return "B"
    return None


def split_bolero_family(bind) -> dict[str, int]:
    """Move the combined Bolero model's catalogue onto the three models.
    Returns counts (also used by the tests)."""
    counts = {"models": 0, "variants": 0, "merged": 0, "skus": 0, "eligibility": 0, "grid": 0}
    combined = bind.execute(text(
        "SELECT model_id, oem_id, segment_id FROM auditcore.product_models "
        "WHERE upper(regexp_replace(model_name, '[^A-Za-z0-9]', '', 'g')) = 'THEBOSSBOLERONEO'"
    )).mappings().all()
    if not combined:
        return counts
    bind.execute(text("SELECT set_config('app.platform_super_admin', 'true', true)"))
    # Transactional: a failure rolls the disable back with everything else,
    # so there is no finally (it would run in the aborted transaction and
    # hide the original error).
    bind.execute(text("ALTER TABLE auditcore.discount_scheme_eligibility DISABLE TRIGGER trg_discount_eligibility_draft_only"))
    for model in combined:
        # The projects of this OEM: scheme and grid rows are tenant rows.
        tenants = [r[0] for r in bind.execute(
            text("SELECT tenant_id FROM auditcore.projects WHERE oem_id = :o"), {"o": model["oem_id"]}
        ).all()]
        targets = _ensure_targets(bind, model, counts)
        _move_variants(bind, model, targets, tenants, counts)
        _route_model_rows(bind, model, targets, tenants, counts)
        bind.execute(text("UPDATE auditcore.product_models SET is_active = false WHERE model_id = :m "
                          "AND NOT EXISTS (SELECT 1 FROM auditcore.product_variants v WHERE v.model_id = :m)"),
                     {"m": model["model_id"]})
    bind.execute(text("ALTER TABLE auditcore.discount_scheme_eligibility ENABLE TRIGGER trg_discount_eligibility_draft_only"))
    bind.execute(text("SELECT set_config('app.tenant_id', '', true)"))
    return counts


def _ensure_targets(bind, model, counts) -> dict[str, object]:
    """series -> model_id of the BOLERO / BOLERO NEO / BOLERO NEO PLUS model
    of the same OEM: the one already owning price-list SKUs, else the
    oldest, else a new one in the combined model's segment."""
    existing = bind.execute(text(
        """
        SELECT pm.model_id, pm.model_name, pm.model_code,
               (SELECT count(*) FROM auditcore.product_skus s WHERE s.model_id = pm.model_id) AS skus
        FROM auditcore.product_models pm WHERE pm.oem_id = :o
        ORDER BY pm.created_at_utc, pm.model_code
        """
    ), {"o": model["oem_id"]}).mappings().all()
    codes = {r["model_code"] for r in existing}
    out: dict[str, object] = {}
    for series, (key, name, code) in _TARGETS.items():
        candidates = [r for r in existing if _squash(r["model_name"]) == key]
        if candidates:
            out[series] = max(candidates, key=lambda r: r["skus"])["model_id"]
            continue
        if code in codes:
            code = f"{code}_SERIES"
        out[series] = bind.execute(text(
            "INSERT INTO auditcore.product_models (oem_id, model_code, model_name, segment_id) "
            "VALUES (:o, :c, :n, :s) RETURNING model_id"
        ), {"o": model["oem_id"], "c": code, "n": name, "s": model["segment_id"]}).scalar_one()
        codes.add(code)
        counts["models"] += 1
    return out


def _move_variants(bind, model, targets, tenants, counts) -> None:
    variants = bind.execute(text(
        "SELECT variant_id, variant_code, variant_name FROM auditcore.product_variants WHERE model_id = :m"
    ), {"m": model["model_id"]}).mappings().all()
    moves: list[dict[str, object]] = []
    for variant in variants:
        match = _SERIES_RE.match((variant["variant_name"] or "").upper())
        if not match:
            continue
        target = targets[match.group(1)]
        twin = bind.execute(text(
            "SELECT variant_id FROM auditcore.product_variants WHERE model_id = :t AND variant_code = :c"
        ), {"t": target, "c": variant["variant_code"]}).scalar_one_or_none()
        keep = twin or variant["variant_id"]
        counts["skus"] += bind.execute(text(
            "UPDATE auditcore.product_skus SET model_id = :t, variant_id = :keep WHERE variant_id = :v"
        ), {"t": target, "keep": keep, "v": variant["variant_id"]}).rowcount
        bind.execute(text(
            """
            UPDATE auditcore.product_configurations c SET model_id = :t, variant_id = :keep
            WHERE c.variant_id = :v AND (CAST(:keep AS uuid) = CAST(:v AS uuid) OR NOT EXISTS (
                SELECT 1 FROM auditcore.product_configurations k
                WHERE k.variant_id = :keep AND k.configuration_code = c.configuration_code))
            """
        ), {"t": target, "keep": keep, "v": variant["variant_id"]})
        moves.append({"v": variant["variant_id"], "t": target, "keep": keep})
        if twin:
            _move_eligibility(bind, tenants, [moves.pop()], counts)
            # Removed once nothing points at it any more; else it stays,
            # empty of SKUs, on the deactivated combined model.
            bind.execute(text(
                "DELETE FROM auditcore.product_variants v WHERE v.variant_id = :v "
                "AND NOT EXISTS (SELECT 1 FROM auditcore.product_configurations c WHERE c.variant_id = :v)"
            ), {"v": variant["variant_id"]})
            counts["merged"] += 1
        else:
            bind.execute(text("UPDATE auditcore.product_variants SET model_id = :t WHERE variant_id = :v"),
                         {"t": target, "v": variant["variant_id"]})
            counts["variants"] += 1
    _move_eligibility(bind, tenants, moves, counts)


def _move_eligibility(bind, tenants, moves, counts) -> None:
    """Variant-level scheme eligibility follows its variant (one statement
    per tenant)."""
    if not moves:
        return
    variant_ids = [m["v"] for m in moves]
    target_by = {str(m["v"]): str(m["t"]) for m in moves}
    keep_by = {str(m["v"]): str(m["keep"]) for m in moves}
    for tenant in tenants:
        bind.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        counts["eligibility"] += bind.execute(text(
            """
            UPDATE auditcore.discount_scheme_eligibility e
            SET model_id = CAST(CAST(:targets AS jsonb) ->> CAST(e.variant_id AS text) AS uuid),
                variant_id = CAST(CAST(:keeps AS jsonb) ->> CAST(e.variant_id AS text) AS uuid)
            WHERE e.tenant_id = :tenant AND e.variant_id = ANY(:variants)
            """
        ), {"targets": json.dumps(target_by), "keeps": json.dumps(keep_by), "tenant": tenant,
            "variants": variant_ids}).rowcount


def _route_model_rows(bind, model, targets, tenants, counts) -> None:
    """Model-wide eligibility rows and dealer-grid rows of the combined model:
    to the model their wording names, else to all three."""
    for tenant in tenants:
        bind.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        rows = bind.execute(text(
            """
            SELECT e.*, ds.scheme_name
            FROM auditcore.discount_scheme_eligibility e
            JOIN auditcore.discount_scheme_versions dv
              ON dv.tenant_id = e.tenant_id AND dv.discount_scheme_version_id = e.discount_scheme_version_id
            JOIN auditcore.discount_schemes ds
              ON ds.tenant_id = dv.tenant_id AND ds.discount_scheme_id = dv.discount_scheme_id
            WHERE e.tenant_id = :t AND e.model_id = :m AND e.variant_id IS NULL
            """
        ), {"t": tenant, "m": model["model_id"]}).mappings().all()
        for row in rows:
            criteria = row["criteria"] or {}
            texts = [*(criteria.get("variantTexts") or []), *(criteria.get("unresolvedVariantTexts") or [])]
            series = {m.group(1) for m in (_SERIES_RE.match(t.upper()) for t in texts) if m}
            # Only the row's own variant texts narrow it. A scheme NAME is not
            # evidence: the dev ingest named one scheme after the first of
            # three bulletin rows ("Mahindra Exchange — Bolero") while its
            # model-wide rows covered the Bolero, the Neo and the Neo Plus
            # (re-review of PR 468, B3). Otherwise: all three, as before.
            chosen = sorted(series) if len(series) == 1 else sorted(targets)
            first, *rest = [targets[s] for s in chosen]
            bind.execute(text("UPDATE auditcore.discount_scheme_eligibility SET model_id = :t "
                              "WHERE tenant_id = :tn AND eligibility_id = :e"),
                         {"t": first, "tn": tenant, "e": row["eligibility_id"]})
            for other in rest:
                bind.execute(text(
                    """
                    INSERT INTO auditcore.discount_scheme_eligibility (
                        tenant_id, discount_scheme_version_id, dealer_id, outlet_id, model_id, variant_id,
                        colour_id, product_sku_id, customer_type_code, criteria
                    ) VALUES (:tenant_id, :discount_scheme_version_id, :dealer_id, :outlet_id, :model_id, :variant_id,
                              :colour_id, :product_sku_id, :customer_type_code, CAST(:criteria AS jsonb))
                    """
                ), {**{k: row[k] for k in ("tenant_id", "discount_scheme_version_id", "dealer_id", "outlet_id",
                                           "variant_id", "colour_id", "product_sku_id", "customer_type_code")},
                    "model_id": other, "criteria": json.dumps(criteria)})
            counts["eligibility"] += len(chosen)

        grid = bind.execute(text(
            "SELECT * FROM auditcore.dealer_discount_grid_rows WHERE tenant_id = :t AND model_id = :m"
        ), {"t": tenant, "m": model["model_id"]}).mappings().all()
        placed: dict[object, dict[str, object]] = {}
        for row in grid:
            series = series_of_text(row["model_alias"])
            chosen = [series] if series else sorted(targets)
            for s_ in chosen:
                placed.setdefault(row["grid_version_id"], {}).setdefault(s_, row)
            first, *rest = [targets[s] for s in chosen]
            bind.execute(text("UPDATE auditcore.dealer_discount_grid_rows SET model_id = :t "
                              "WHERE tenant_id = :tn AND grid_row_id = :g"),
                         {"t": first, "tn": tenant, "g": row["grid_row_id"]})
            for other in rest:
                bind.execute(text(
                    """
                    INSERT INTO auditcore.dealer_discount_grid_rows (
                        tenant_id, grid_version_id, model_alias, model_id, in_scope, booking_protection_days,
                        agreed_buffer_amount, insurance_od_percent, out_of_territory_amount, raw
                    ) VALUES (:tenant_id, :grid_version_id, :model_alias, :model_id, :in_scope,
                              :booking_protection_days, :agreed_buffer_amount, :insurance_od_percent,
                              :out_of_territory_amount, CAST(:raw AS jsonb))
                    """
                ), {**{k: row[k] for k in ("tenant_id", "grid_version_id", "model_alias", "in_scope",
                                           "booking_protection_days", "agreed_buffer_amount",
                                           "insurance_od_percent", "out_of_territory_amount")},
                    "model_id": other, "raw": json.dumps(row["raw"] or {})})
            counts["grid"] += len(chosen)
        # A grid version whose rows covered the combined model covered all
        # three: a model none of its rows names (the Neo Plus, when the grid
        # prints "BOLERO" and "BOLERO NEO") takes the nearest row's terms.
        for rows_by_series in placed.values():
            for series in sorted(set(targets) - set(rows_by_series)):
                source = next(rows_by_series[s_] for s_ in _NEAREST[series] if s_ in rows_by_series)
                _copy_grid_row(bind, source, targets[series])
                counts["grid"] += 1


_NEAREST = {"P": ("N", "B"), "N": ("P", "B"), "B": ("N", "P")}


def _copy_grid_row(bind, row, model_id) -> None:
    bind.execute(text(
        """
        INSERT INTO auditcore.dealer_discount_grid_rows (
            tenant_id, grid_version_id, model_alias, model_id, in_scope, booking_protection_days,
            agreed_buffer_amount, insurance_od_percent, out_of_territory_amount, raw
        ) VALUES (:tenant_id, :grid_version_id, :model_alias, :model_id, :in_scope,
                  :booking_protection_days, :agreed_buffer_amount, :insurance_od_percent,
                  :out_of_territory_amount, CAST(:raw AS jsonb))
        """
    ), {**{k: row[k] for k in ("tenant_id", "grid_version_id", "model_alias", "in_scope",
                               "booking_protection_days", "agreed_buffer_amount",
                               "insurance_od_percent", "out_of_territory_amount")},
        "model_id": model_id, "raw": json.dumps({**(row["raw"] or {}), "copiedForBoleroSplit": True})})


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            """
            INSERT INTO auditcore.oem_model_aliases (oem_code, alias_text, canonical_model_name)
            VALUES ('MAHINDRA', :alias_text, :canonical_model_name)
            ON CONFLICT (oem_code, alias_text) DO UPDATE
              SET canonical_model_name = EXCLUDED.canonical_model_name
            """
        ),
        [{"alias_text": a, "canonical_model_name": c} for a, c in _ALIASES.items()],
    )
    split_bolero_family(bind)
    # The durable claim of the one-off price-master re-load
    # (audit_core.oem_master_reload): one row per tag and tenant.
    bind.execute(text(
        """
        CREATE TABLE IF NOT EXISTS auditcore.oem_master_reload_runs (
            reload_tag       varchar(120) NOT NULL,
            tenant_id        varchar(128) NOT NULL,
            mode             varchar(10)  NOT NULL CHECK (mode IN ('DRY_RUN', 'COMMIT')),
            status           varchar(20)  NOT NULL DEFAULT 'CLAIMED'
                             CHECK (status IN ('CLAIMED', 'DRY_RUN_PASSED', 'DRY_RUN_FAILED', 'COMMITTED', 'FAILED')),
            summary          jsonb        NOT NULL DEFAULT '{}'::jsonb,
            claimed_at_utc   timestamptz  NOT NULL DEFAULT now(),
            finished_at_utc  timestamptz,
            PRIMARY KEY (reload_tag, tenant_id)
        )
        """
    ))
    bind.execute(text("GRANT SELECT, INSERT, UPDATE ON auditcore.oem_master_reload_runs TO audit_core_runtime"))


def downgrade() -> None:
    op.get_bind().execute(text("DROP TABLE IF EXISTS auditcore.oem_master_reload_runs"))
    # The catalogue split is not reversed (it would orphan SKUs priced under
    # the three models since); only the aliases go back.
    op.get_bind().execute(
        text(
            "UPDATE auditcore.oem_model_aliases SET canonical_model_name = :previous "
            "WHERE oem_code = 'MAHINDRA' AND alias_text = ANY(:a)"
        ),
        {"previous": _PREVIOUS, "a": list(_ALIASES)},
    )
