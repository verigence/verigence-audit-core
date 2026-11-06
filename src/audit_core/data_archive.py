"""Data archive job: move rows nothing reads any more to R2, then remove them.

The shared database grew to 2.4 GB in three weeks (~125 MB a day). This job
moves rows that no reader needs to R2 as gzip JSON-lines files, reads every
file back and checks it, and only then removes exactly the rows it verified,
through ``auditcore.archive_delete_rows`` (migration 0201; the runtime role
holds no DELETE grant on these tables). Archives are kept: the job never
deletes an object.

Per table (one ``ArchiveRule``), per Project, per batch of ~5,000 rows:

    select -> JSON lines (columns in table order, ISO-8601 UTC timestamps,
    jsonb kept as JSON) -> gzip -> upload the data object and its manifest ->
    read both back, check sha256, row count and the exact primary keys ->
    one transaction: archive_delete_rows(those keys) + an archive_runs row.

Any failure stops that table's run; nothing that was not verified is
deleted. A batch's delete and its archive_runs row commit together, so no row
leaves the database without a record of the object it went to.

Modes:
  * DRY_RUN (default) counts the candidates per Project and records one
    archive_runs row each. It writes nothing to R2 and deletes nothing.
  * LIVE needs AUDIT_CORE_ARCHIVE_BUCKET *and* the table's own enable setting
    (off by default). Without them the run is refused and recorded as REFUSED.

Settings (environment):
  AUDIT_CORE_ARCHIVE_BUCKET                    R2 bucket for archives (unset: LIVE refused)
  AUDIT_CORE_ARCHIVE_BATCH_SIZE                rows per object, default 5000 (max 10000)
  AUDIT_CORE_ARCHIVE_MAX_ROWS_PER_TABLE        rows one run archives per table, default 500000
  AUDIT_CORE_ARCHIVE_<TABLE>_ENABLED           per-table LIVE switch, default off
  AUDIT_CORE_ARCHIVE_<TABLE>_AGE_DAYS          per-table age limit (rule_executions: 30)
  AUDIT_CORE_ARCHIVE_STORAGE_ENDPOINT / _ACCESS_KEY_ID / _SECRET_ACCESS_KEY / _REGION
      R2 credentials; each falls back to the Phase 2 storage setting, then the
      photo storage setting (the same R2 account the app already uses).

Entry point (GitHub Actions: .github/workflows/archive-dev.yml):
    python -m audit_core.data_archive --mode dry-run|live [--table rule_executions]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

import structlog
from sqlalchemy import Connection, Engine, create_engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

MODE_DRY_RUN = "DRY_RUN"
MODE_LIVE = "LIVE"
DEFAULT_BATCH_SIZE = 5000
MAX_BATCH_SIZE = 10000  # archive_delete_rows refuses more keys than this in one call
DEFAULT_MAX_ROWS_PER_TABLE = 500_000
_DELETE_FUNCTION_SQL = "SELECT auditcore.archive_delete_rows(:tenant_id, :table, CAST(:ids AS uuid[]))"
_TRUE = {"1", "true", "yes", "on"}


class ArchiveError(RuntimeError):
    """A batch could not be archived and verified; nothing unverified was deleted."""


class ArchiveRefused(RuntimeError):
    """A LIVE run is not allowed with the current settings."""


# ── Rules ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ArchiveRule:
    """One table the job may archive, and which of its rows.

    ``candidate_predicate`` is SQL on alias ``t``. The job adds the tenant and
    the age limit (``t.<date_column> < :cutoff``) itself. Rows are read in
    ``order_by`` order; ``resume_columns`` (a prefix of that order) lets the
    next batch start where the last one ended, since the rows of the last
    batch are gone by then. ``archive_delete_rows`` must carry a branch for
    ``qualified_name`` or every LIVE batch fails before deleting anything.
    """

    key: str
    schema: str
    table: str
    primary_key: str
    tenant_column: str
    date_column: str
    default_age_days: int
    description: str
    candidate_predicate: str
    order_by: tuple[str, ...]
    resume_columns: tuple[str, ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def setting_prefix(self) -> str:
        return f"AUDIT_CORE_ARCHIVE_{self.key.upper()}"


# rule_executions: the latest row of each (journey, rule) is what the status
# report, the P2 controls and the diagnostics read (DISTINCT ON ... ORDER BY
# evaluated_at_utc DESC / LIMIT 1), and MAX(evaluated_at_utc) per Journey is
# the derived-control fingerprint; the latest non-ERROR row is what "has this
# ONCE rule already run" finds (uc03_rule_orchestrator.has_already_run). A row
# is a candidate only when there is a strictly newer row of its pair and, unless
# it is an ERROR, a strictly newer non-ERROR row too -- so rows tied at the
# newest time are all kept.
RULE_EXECUTIONS_PREDICATE = """
    EXISTS (
        SELECT 1 FROM auditcore.rule_executions n
        WHERE n.tenant_id = t.tenant_id
          AND n.journey_id = t.journey_id
          AND n.rule_code = t.rule_code
          AND n.evaluated_at_utc > t.evaluated_at_utc
    )
    AND (
        t.outcome = 'ERROR'
        OR EXISTS (
            SELECT 1 FROM auditcore.rule_executions n
            WHERE n.tenant_id = t.tenant_id
              AND n.journey_id = t.journey_id
              AND n.rule_code = t.rule_code
              AND n.outcome <> 'ERROR'
              AND n.evaluated_at_utc > t.evaluated_at_utc
        )
    )
"""

ARCHIVE_RULES: tuple[ArchiveRule, ...] = (
    ArchiveRule(
        key="rule_executions",
        schema="auditcore",
        table="rule_executions",
        primary_key="rule_execution_id",
        tenant_column="tenant_id",
        date_column="evaluated_at_utc",
        default_age_days=30,
        description=(
            "rule_executions older than the age limit that are neither the latest row "
            "nor the latest non-ERROR row of their (journey_id, rule_code)"
        ),
        candidate_predicate=RULE_EXECUTIONS_PREDICATE,
        order_by=("journey_id", "rule_code", "evaluated_at_utc", "rule_execution_id"),
        resume_columns=("journey_id", "rule_code"),
    ),
    # ── Extension points: NOT enabled. Each needs the product owner's policy
    # and, for LIVE, a branch in archive_delete_rows (a new migration). ─────
    #
    # Phase 2 one-off worker jobs, finished more than 30 days ago. The table
    # has no tenant_id: the rule needs a Project-less path (tenant_column=None,
    # archive_runs.tenant_id NULL) and a matching tenant-less delete branch.
    # ArchiveRule(
    #     key="p2_worker_jobs", schema="auditcore", table="p2_worker_jobs",
    #     primary_key="job_id", tenant_column=None, date_column="requested_at_utc",
    #     default_age_days=30,
    #     description="p2_worker_jobs DONE or FAILED, requested more than 30 days ago",
    #     candidate_predicate="t.status IN ('DONE', 'FAILED')",
    #     order_by=("requested_at_utc", "job_id"), resume_columns=(),
    # ),
    #
    # Rule engine findings that a later run superseded, and the runs nothing
    # current points at. These live in the rule engine's own schema ("audit"),
    # created by its migrations: the delete function belongs in that repository
    # (owned by that schema's owner), not in auditcore.archive_delete_rows.
    # ArchiveRule(
    #     key="re_audit_findings", schema="audit", table="audit_findings",
    #     primary_key="finding_id", tenant_column="tenant_id", date_column="created_at",
    #     default_age_days=30,
    #     description="rule engine findings with is_current = false (superseded by a later run)",
    #     candidate_predicate="t.is_current = false AND t.superseded_by_run_id IS NOT NULL",
    #     order_by=("created_at", "finding_id"), resume_columns=(),
    # ),
    # ArchiveRule(
    #     key="re_audit_runs", schema="audit", table="audit_runs", ...
    #     candidate_predicate=("NOT EXISTS (SELECT 1 FROM audit.audit_findings f "
    #                          "WHERE f.run_id = t.run_id AND f.is_current)"),
    # ),
)

RULES_BY_KEY: dict[str, ArchiveRule] = {rule.key: rule for rule in ARCHIVE_RULES}


# ── Settings ──────────────────────────────────────────────────────────────────
def _int_setting(environ: Mapping[str, str], name: str, default: int, *, low: int, high: int) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class ArchiveSettings:
    bucket: str | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    max_rows_per_table: int = DEFAULT_MAX_ROWS_PER_TABLE
    enabled_tables: frozenset[str] = frozenset()
    age_days: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ArchiveSettings:
        env = os.environ if environ is None else environ
        enabled: set[str] = set()
        ages: dict[str, int] = {}
        for rule in ARCHIVE_RULES:
            if (env.get(f"{rule.setting_prefix}_ENABLED") or "").strip().lower() in _TRUE:
                enabled.add(rule.key)
            ages[rule.key] = _int_setting(
                env, f"{rule.setting_prefix}_AGE_DAYS", rule.default_age_days, low=1, high=3650
            )
        return cls(
            bucket=(env.get("AUDIT_CORE_ARCHIVE_BUCKET") or "").strip() or None,
            batch_size=_int_setting(
                env, "AUDIT_CORE_ARCHIVE_BATCH_SIZE", DEFAULT_BATCH_SIZE, low=1, high=MAX_BATCH_SIZE
            ),
            max_rows_per_table=_int_setting(
                env, "AUDIT_CORE_ARCHIVE_MAX_ROWS_PER_TABLE", DEFAULT_MAX_ROWS_PER_TABLE,
                low=1, high=100_000_000,
            ),
            enabled_tables=frozenset(enabled),
            age_days=ages,
        )

    def age_for(self, rule: ArchiveRule) -> int:
        return int(self.age_days.get(rule.key, rule.default_age_days))


# ── Object store ──────────────────────────────────────────────────────────────
class ArchiveStore(Protocol):
    def put_object(self, object_key: str, data: bytes, *, content_type: str) -> None: ...

    def get_object(self, object_key: str) -> bytes: ...


def archive_store_from_env(bucket: str, environ: Mapping[str, str] | None = None) -> ArchiveStore:
    """The repo's R2 (S3) client, pointed at the archive bucket. Credentials
    fall back to the Phase 2 storage settings, then the photo storage ones."""
    from audit_core.uc03_p2_storage import P2DocumentStorage, P2DocumentStorageSettings

    env = os.environ if environ is None else environ

    def pick(suffix: str, default: str = "") -> str:
        for prefix in ("AUDIT_CORE_ARCHIVE_STORAGE", "AUDIT_CORE_P2_STORAGE", "AUDIT_CORE_PHOTO_STORAGE"):
            value = (env.get(f"{prefix}_{suffix}") or "").strip()
            if value:
                return value
        return default

    endpoint, key_id, secret = pick("ENDPOINT"), pick("ACCESS_KEY_ID"), pick("SECRET_ACCESS_KEY")
    if not endpoint or not key_id or not secret:
        raise ArchiveRefused("R2 storage credentials are not configured")
    return P2DocumentStorage(
        P2DocumentStorageSettings(
            endpoint_url=endpoint, access_key_id=key_id, secret_access_key=secret,
            bucket=bucket, region=pick("REGION", "auto"),
        )
    )


# ── Encoding ──────────────────────────────────────────────────────────────────
def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    raise TypeError(f"cannot archive a value of type {type(value).__name__}")


def encode_rows(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    """JSON lines, one object per row, keys in ``columns`` order."""
    lines = [
        json.dumps(
            {column: _json_value(row[column]) for column in columns},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        )
        for row in rows
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def gzip_bytes(raw: bytes) -> bytes:
    # mtime=0: the same rows always give the same bytes (and the same sha256).
    return gzip.compress(raw, compresslevel=6, mtime=0)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "_"


def object_key(rule: ArchiveRule, tenant_id: str, started_at: datetime, run_id: UUID, batch_no: int,
               *, manifest: bool = False) -> str:
    suffix = "manifest.json" if manifest else "jsonl.gz"
    return (
        f"archive/{rule.qualified_name}/{_safe_segment(tenant_id)}/{started_at:%Y-%m}/"
        f"{run_id}-{batch_no:05d}.{suffix}"
    )


# ── Results ───────────────────────────────────────────────────────────────────
@dataclass
class TenantResult:
    tenant_id: str | None
    status: str
    candidate_count: int = 0
    archived_count: int = 0
    object_keys: list[str] = field(default_factory=list)
    checksums: dict[str, str] = field(default_factory=dict)
    error: str | None = None


@dataclass
class TableResult:
    table_key: str
    mode: str
    run_id: UUID
    cutoff_utc: datetime | None
    tenants: list[TenantResult] = field(default_factory=list)
    refused: str | None = None

    @property
    def status(self) -> str:
        if self.refused:
            return "REFUSED"
        if any(t.status == "FAILED" for t in self.tenants):
            return "FAILED"
        return MODE_DRY_RUN if self.mode == MODE_DRY_RUN else "SUCCEEDED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table_key,
            "mode": self.mode,
            "runId": str(self.run_id),
            "status": self.status,
            "cutoffUtc": self.cutoff_utc.isoformat() if self.cutoff_utc else None,
            "refused": self.refused,
            "candidateCount": sum(t.candidate_count for t in self.tenants),
            "archivedCount": sum(t.archived_count for t in self.tenants),
            "tenants": [
                {"tenantId": t.tenant_id, "status": t.status, "candidateCount": t.candidate_count,
                 "archivedCount": t.archived_count, "objects": len(t.object_keys), "error": t.error}
                for t in self.tenants
            ],
        }


# ── SQL ───────────────────────────────────────────────────────────────────────
def _q(identifier: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", identifier):
        raise ValueError(f"unexpected identifier {identifier!r}")
    return f'"{identifier}"'


def candidate_where(rule: ArchiveRule) -> str:
    return (
        f"t.{_q(rule.tenant_column)} = :tenant_id "
        f"AND t.{_q(rule.date_column)} < :cutoff "
        f"AND ({rule.candidate_predicate})"
    )


def count_candidates_sql(rule: ArchiveRule) -> str:
    return f"SELECT count(*) FROM {rule.schema}.{rule.table} t WHERE {candidate_where(rule)}"


def select_batch_sql(rule: ArchiveRule, columns: Sequence[str], *, resume: bool) -> str:
    resume_sql = ""
    if resume and rule.resume_columns:
        left = ", ".join(f"t.{_q(c)}" for c in rule.resume_columns)
        right = ", ".join(f":resume_{i}" for i in range(len(rule.resume_columns)))
        resume_sql = f" AND ({left}) >= ({right})"
    return (
        f"SELECT {', '.join(f't.{_q(c)}' for c in columns)} "
        f"FROM {rule.schema}.{rule.table} t "
        f"WHERE {candidate_where(rule)}{resume_sql} "
        f"ORDER BY {', '.join(f't.{_q(c)}' for c in rule.order_by)} "
        "LIMIT :limit"
    )


def _tenants(connection: Connection, rule: ArchiveRule) -> list[str]:
    # A loose index scan over the leading tenant column: one index probe per
    # Project instead of reading the whole table for DISTINCT.
    column, table = _q(rule.tenant_column), f"{rule.schema}.{rule.table}"
    rows = connection.execute(
        text(
            f"""
            WITH RECURSIVE tenants(tid) AS (
                SELECT min({column}) FROM {table}
                UNION ALL
                SELECT (SELECT min({column}) FROM {table} WHERE {column} > tenants.tid)
                FROM tenants WHERE tenants.tid IS NOT NULL
            )
            SELECT tid FROM tenants WHERE tid IS NOT NULL ORDER BY tid
            """
        )
    ).scalars().all()
    return [str(row) for row in rows]


def _columns(connection: Connection, rule: ArchiveRule) -> list[str]:
    """Every column, in table order: an archive never leaves a column behind."""
    columns = connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"
        ),
        {"s": rule.schema, "t": rule.table},
    ).scalars().all()
    if not columns or rule.primary_key not in columns:
        raise ArchiveError(f"cannot read the columns of {rule.qualified_name}")
    return [str(c) for c in columns]


def _record(connection: Connection, *, run_id: UUID, tenant_id: str | None, rule: ArchiveRule, mode: str,
            batch_no: int | None, status: str, cutoff: datetime | None, candidate_count: int,
            archived_count: int, object_keys: Sequence[str], checksums: Mapping[str, str],
            error: str | None, started_by: str, started_at: datetime) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.archive_runs (
                run_id, tenant_id, table_key, mode, batch_no, status, rule_description, cutoff_utc,
                candidate_count, archived_count, object_keys, checksums, error, started_by,
                started_at_utc, finished_at_utc
            ) VALUES (
                :run_id, :tenant_id, :table_key, :mode, :batch_no, :status, :rule, :cutoff,
                :candidates, :archived, CAST(:keys AS jsonb), CAST(:checksums AS jsonb), :error,
                :started_by, :started_at, clock_timestamp()
            )
            """
        ),
        {
            "run_id": run_id, "tenant_id": tenant_id, "table_key": rule.qualified_name, "mode": mode,
            "batch_no": batch_no, "status": status, "rule": rule.description, "cutoff": cutoff,
            "candidates": candidate_count, "archived": archived_count,
            "keys": json.dumps(list(object_keys)), "checksums": json.dumps(dict(checksums)),
            "error": (error or None) and error[:4000], "started_by": started_by[:160],
            "started_at": started_at,
        },
    )


def _record_tenant(engine: Engine, tenant_id: str, **kwargs: Any) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        _record(connection, tenant_id=tenant_id, **kwargs)


# ── One batch ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Batch:
    keys: tuple[str, ...]  # data object, manifest
    sha256: str
    ids: tuple[str, ...]
    last_row: Mapping[str, Any]


def _upload_and_verify(store: ArchiveStore, *, rule: ArchiveRule, columns: Sequence[str],
                       rows: Sequence[Mapping[str, Any]], tenant_id: str, run_id: UUID, batch_no: int,
                       started_at: datetime, cutoff: datetime) -> _Batch:
    raw = encode_rows(columns, rows)
    body = gzip_bytes(raw)
    digest = sha256_hex(body)
    selected_ids = [str(row[rule.primary_key]) for row in rows]
    dates = [row[rule.date_column] for row in rows]
    data_key = object_key(rule, tenant_id, started_at, run_id, batch_no)
    manifest_key = object_key(rule, tenant_id, started_at, run_id, batch_no, manifest=True)
    manifest = {
        "format": "jsonl.gz",
        "formatVersion": 1,
        "table": rule.qualified_name,
        "tenantId": tenant_id,
        "runId": str(run_id),
        "batchNo": batch_no,
        "objectKey": data_key,
        "rowCount": len(rows),
        "sha256": digest,
        "uncompressedSha256": sha256_hex(raw),
        "columns": list(columns),
        "primaryKey": rule.primary_key,
        "dateColumn": rule.date_column,
        "minDate": _json_value(min(dates)),
        "maxDate": _json_value(max(dates)),
        "rule": rule.description,
        "cutoffUtc": cutoff.isoformat(),
        "createdAtUtc": datetime.now(UTC).isoformat(),
    }
    manifest_body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    store.put_object(data_key, body, content_type="application/gzip")
    store.put_object(manifest_key, manifest_body, content_type="application/json")

    # Read back: the object in R2, not what we meant to send, decides what may go.
    stored = store.get_object(data_key)
    if sha256_hex(stored) != digest:
        raise ArchiveError(f"{data_key}: sha256 read back does not match what was written")
    try:
        lines = gzip.decompress(stored).decode("utf-8").splitlines()
        stored_ids = [str(json.loads(line)[rule.primary_key]) for line in lines]
    except (OSError, ValueError, KeyError) as exc:
        raise ArchiveError(f"{data_key}: object read back is not valid gzip JSON lines") from exc
    if len(stored_ids) != len(rows):
        raise ArchiveError(f"{data_key}: {len(stored_ids)} rows read back, {len(rows)} written")
    if stored_ids != selected_ids:
        raise ArchiveError(f"{data_key}: primary keys read back differ from the rows selected")
    try:
        stored_manifest = json.loads(store.get_object(manifest_key))
    except ValueError as exc:
        raise ArchiveError(f"{manifest_key}: manifest read back is not valid JSON") from exc
    if stored_manifest.get("sha256") != digest or stored_manifest.get("rowCount") != len(rows):
        raise ArchiveError(f"{manifest_key}: manifest read back does not match the data object")
    return _Batch(keys=(data_key, manifest_key), sha256=digest, ids=tuple(stored_ids), last_row=rows[-1])


# ── Runs ──────────────────────────────────────────────────────────────────────
StoreFactory = Callable[[str], ArchiveStore]


def _archive_tenant(engine: Engine, store: ArchiveStore, *, rule: ArchiveRule, columns: Sequence[str],
                    tenant_id: str, run_id: UUID, started_at: datetime, started_by: str,
                    cutoff: datetime, batch_size: int, budget: int) -> TenantResult:
    result = TenantResult(tenant_id=tenant_id, status="SUCCEEDED")
    common: dict[str, Any] = {"run_id": run_id, "rule": rule, "mode": MODE_LIVE, "cutoff": cutoff,
                              "started_by": started_by, "started_at": started_at}
    pending: str | None = None  # a batch written to R2 whose rows are not deleted yet
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            result.candidate_count = int(
                connection.execute(text(count_candidates_sql(rule)),
                                   {"tenant_id": tenant_id, "cutoff": cutoff}).scalar_one()
            )
        resume: Mapping[str, Any] | None = None
        batch_no = 0
        while result.archived_count < budget:
            limit = min(batch_size, budget - result.archived_count)
            params: dict[str, Any] = {"tenant_id": tenant_id, "cutoff": cutoff, "limit": limit}
            if resume is not None:
                params.update({f"resume_{i}": resume[c] for i, c in enumerate(rule.resume_columns)})
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                rows = connection.execute(
                    text(select_batch_sql(rule, columns, resume=resume is not None)), params
                ).mappings().all()
            if not rows:
                break
            batch_no += 1
            pending = object_key(rule, tenant_id, started_at, run_id, batch_no)
            batch = _upload_and_verify(store, rule=rule, columns=columns, rows=rows, tenant_id=tenant_id,
                                       run_id=run_id, batch_no=batch_no, started_at=started_at,
                                       cutoff=cutoff)
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                deleted = int(
                    connection.execute(
                        text(_DELETE_FUNCTION_SQL),
                        {"tenant_id": tenant_id, "table": rule.qualified_name, "ids": list(batch.ids)},
                    ).scalar_one()
                )
                if deleted != len(batch.ids):  # the function raises first; belt and braces
                    raise ArchiveError(f"deleted {deleted} of {len(batch.ids)} verified rows")
                _record(connection, tenant_id=tenant_id, batch_no=batch_no, status="BATCH_ARCHIVED",
                        candidate_count=result.candidate_count, archived_count=deleted,
                        object_keys=batch.keys, checksums={batch.keys[0]: batch.sha256}, error=None,
                        **common)
            pending = None
            result.archived_count += deleted
            result.object_keys.extend(batch.keys)
            result.checksums[batch.keys[0]] = batch.sha256
            resume = batch.last_row
            logger.info("data_archive_batch", table=rule.qualified_name, tenant_id=tenant_id,
                        run_id=str(run_id), batch_no=batch_no, rows=deleted, object_key=batch.keys[0])
    except Exception as exc:  # noqa: BLE001 -- any failure stops this table's run
        result.status = "FAILED"
        # A database error's own message, not the statement and its 5,000 keys.
        cause = getattr(exc, "orig", None) or exc
        result.error = f"{type(cause).__name__}: {str(cause).strip()}"
        if pending:
            result.error += f" (batch object {pending} may exist in R2; its rows were kept)"
        logger.error("data_archive_failed", table=rule.qualified_name, tenant_id=tenant_id,
                     run_id=str(run_id), error=result.error, archived=result.archived_count)
    _record_tenant(engine, tenant_id, batch_no=None, status=result.status,
                   candidate_count=result.candidate_count, archived_count=result.archived_count,
                   object_keys=result.object_keys, checksums=result.checksums, error=result.error, **common)
    return result


def _refuse(engine: Engine, result: TableResult, rule: ArchiveRule, reason: str, *, started_by: str,
            started_at: datetime) -> TableResult:
    result.refused = reason
    with engine.begin() as connection:
        _record(connection, run_id=result.run_id, tenant_id=None, rule=rule, mode=result.mode, batch_no=None,
                status="REFUSED", cutoff=result.cutoff_utc, candidate_count=0, archived_count=0,
                object_keys=[], checksums={}, error=reason, started_by=started_by, started_at=started_at)
    logger.warning("data_archive_refused", table=rule.qualified_name, mode=result.mode, reason=reason)
    return result


def archive_table(engine: Engine, rule: ArchiveRule, *, mode: str, settings: ArchiveSettings,
                  started_by: str, store_factory: StoreFactory | None = None,
                  tenant_ids: Sequence[str] | None = None, now: datetime | None = None) -> TableResult:
    """Archive (LIVE) or count (DRY_RUN) one rule's candidates, Project by
    Project. ``tenant_ids`` limits the run to those Projects."""
    if mode not in (MODE_DRY_RUN, MODE_LIVE):
        raise ValueError(f"unknown mode {mode!r}")
    started_at = now or datetime.now(UTC)
    cutoff = started_at - timedelta(days=settings.age_for(rule))
    result = TableResult(table_key=rule.qualified_name, mode=mode, run_id=uuid4(), cutoff_utc=cutoff)

    store: ArchiveStore | None = None
    if mode == MODE_LIVE:
        if not settings.bucket:
            return _refuse(engine, result, rule, "AUDIT_CORE_ARCHIVE_BUCKET is not set",
                           started_by=started_by, started_at=started_at)
        if rule.key not in settings.enabled_tables:
            return _refuse(engine, result, rule, f"{rule.setting_prefix}_ENABLED is not on",
                           started_by=started_by, started_at=started_at)
        try:
            store = (store_factory or archive_store_from_env)(settings.bucket)
        except ArchiveRefused as exc:
            return _refuse(engine, result, rule, str(exc), started_by=started_by, started_at=started_at)

    # One run per table at a time (a second dispatch while one runs is refused).
    lock = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        lock_key = f"audit_core.data_archive:{rule.qualified_name}"
        if not lock.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": lock_key}).scalar_one():
            return _refuse(engine, result, rule, "another archive run for this table is in progress",
                           started_by=started_by, started_at=started_at)
        try:
            with engine.begin() as connection:
                tenants = _tenants(connection, rule)
                columns = _columns(connection, rule)
            if tenant_ids is not None:
                wanted = set(tenant_ids)
                tenants = [tenant for tenant in tenants if tenant in wanted]
            budget = settings.max_rows_per_table
            for tenant_id in tenants:
                if mode == MODE_DRY_RUN:
                    with engine.begin() as connection:
                        set_tenant_context(connection, tenant_id)
                        count = int(connection.execute(text(count_candidates_sql(rule)),
                                                       {"tenant_id": tenant_id, "cutoff": cutoff}).scalar_one())
                        _record(connection, run_id=result.run_id, tenant_id=tenant_id, rule=rule, mode=mode,
                                batch_no=None, status=MODE_DRY_RUN, cutoff=cutoff, candidate_count=count,
                                archived_count=0, object_keys=[], checksums={}, error=None,
                                started_by=started_by, started_at=started_at)
                    result.tenants.append(TenantResult(tenant_id=tenant_id, status=MODE_DRY_RUN,
                                                       candidate_count=count))
                    continue
                if budget <= 0:
                    break
                if store is None:  # LIVE always has one by now
                    raise ArchiveRefused("no archive store")
                tenant_result = _archive_tenant(
                    engine, store, rule=rule, columns=columns, tenant_id=tenant_id, run_id=result.run_id,
                    started_at=started_at, started_by=started_by, cutoff=cutoff,
                    batch_size=settings.batch_size, budget=budget,
                )
                result.tenants.append(tenant_result)
                budget -= tenant_result.archived_count
                if tenant_result.status == "FAILED":
                    break  # a failure stops this table's run
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": lock_key})
    finally:
        lock.close()
    logger.info("data_archive_table_done", **result.as_dict() | {"tenants": len(result.tenants)})
    return result


def run_archive(engine: Engine, *, mode: str, settings: ArchiveSettings, started_by: str,
                tables: Sequence[str] | None = None, store_factory: StoreFactory | None = None,
                tenant_ids: Sequence[str] | None = None, now: datetime | None = None) -> list[TableResult]:
    keys = list(tables) if tables else [rule.key for rule in ARCHIVE_RULES]
    unknown = [key for key in keys if key not in RULES_BY_KEY]
    if unknown:
        raise ValueError(f"unknown archive table(s): {', '.join(unknown)}")
    return [
        archive_table(engine, RULES_BY_KEY[key], mode=mode, settings=settings, started_by=started_by,
                      store_factory=store_factory, tenant_ids=tenant_ids, now=now)
        for key in keys
    ]


# ── CLI ───────────────────────────────────────────────────────────────────────
def job_engine(database_url: str) -> Engine:
    """The job runs as the runtime role, like the API: it can delete only
    through archive_delete_rows. Statements get more time than an API call."""
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith(("postgresql", "postgres")):
        options["connect_args"] = {
            "connect_timeout": 10,
            "options": "-c statement_timeout=600000 -c role=audit_core_runtime",
        }
    return create_engine(database_url, **options)


def _default_started_by() -> str:
    actor = (os.environ.get("GITHUB_ACTOR") or "").strip()
    if actor:
        run = (os.environ.get("GITHUB_RUN_ID") or "").strip()
        return f"github:{actor}" + (f" run {run}" if run else "")
    return f"cli:{os.environ.get('USER') or 'unknown'}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m audit_core.data_archive", description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    parser.add_argument("--table", action="append", choices=sorted(RULES_BY_KEY),
                        help="archive only this table (repeatable); default: every rule")
    parser.add_argument("--tenant", action="append", help="only this Project (tenant id; repeatable)")
    parser.add_argument("--started-by", default=None)
    args = parser.parse_args(argv)

    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        settings = ArchiveSettings.from_env()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    mode = MODE_LIVE if args.mode == "live" else MODE_DRY_RUN
    engine = job_engine(database_url)
    try:
        results = run_archive(engine, mode=mode, settings=settings, tables=args.table,
                              tenant_ids=args.tenant, started_by=args.started_by or _default_started_by())
    finally:
        engine.dispose()
    print(json.dumps([r.as_dict() for r in results], indent=2))
    if any(r.status == "FAILED" for r in results):
        return 1
    if any(r.status == "REFUSED" for r in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
