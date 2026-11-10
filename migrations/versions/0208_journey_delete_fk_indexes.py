"""0208_journey_delete_fk_indexes -- index every foreign key a journey delete has to check (2026-11-09).

Deleting a journey (ADI-RD-261022-026, multiple customers' documents in one file) failed with a
statement timeout: deleting its evidence made Postgres check journey_document_extracted_fields
(520k rows, 690 MB) for references with no index on (tenant_id, evidence_id) -- a full scan per
evidence row. The same gap existed on every foreign key below, each pointing at a table the journey
delete removes rows from (journeys, evidence, audit findings, tasks, workflow, payments, ...).
tests/test_journey_delete_fk_indexes.py keeps new tables from bringing it back.

Plain CREATE INDEX IF NOT EXISTS: on DEV the indexes were built beforehand with CONCURRENTLY under
the same names, so this is a no-op there; a fresh database has small tables. Tables or columns that
do not exist in a database are skipped.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0208_journey_delete_fk_indexes"
down_revision = "0207_people_employment"
branch_labels = None
depends_on = None

# (table, foreign key columns), all in schema auditcore.
FOREIGN_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('activity_records', ('tenant_id', 'journey_id')),
    ('audit_evaluations', ('tenant_id', 'journey_id')),
    ('audit_finding_events', ('tenant_id', 'journey_id')),
    ('audit_findings', ('tenant_id', 'audit_evaluation_id')),
    ('audit_state_events', ('tenant_id', 'journey_id')),
    ('bank_statement_lines', ('tenant_id', 'source_evidence_id')),
    ('booking_form_review_values', ('tenant_id', 'source_evidence_id')),
    ('commercial_line_source_values', ('tenant_id', 'source_evidence_id')),
    ('commercial_lines', ('tenant_id', 'source_evidence_id')),
    ('crm_interactions', ('tenant_id', 'journey_id')),
    ('crm_interactions', ('tenant_id', 'workflow_task_id')),
    ('customer_identity_index', ('tenant_id', 'customer_id')),
    ('customer_identity_review_values', ('tenant_id', 'source_evidence_id')),
    ('customers', ('tenant_id', 'legal_name_source_evidence_id')),
    ('daily_ops_items', ('tenant_id', 'evidence_id')),
    ('daily_ops_items', ('tenant_id', 'journey_id')),
    ('dealer_receipt_review_values', ('tenant_id', 'source_evidence_id')),
    ('deliveries', ('tenant_id', 'source_evidence_id')),
    ('delivery_status_history', ('tenant_id', 'delivery_id')),
    ('delivery_status_history', ('tenant_id', 'journey_id')),
    ('delivery_status_history', ('tenant_id', 'source_evidence_id')),
    ('discount_applications', ('tenant_id', 'journey_id')),
    ('discount_applications', ('tenant_id', 'source_evidence_id')),
    ('escalations', ('tenant_id', 'journey_id')),
    ('evidence', ('tenant_id', 'customer_id')),
    ('evidence', ('tenant_id', 'journey_document_requirement_id')),
    ('evidence', ('tenant_id', 'supersedes_evidence_id')),
    ('evidence_facts', ('tenant_id', 'evidence_id')),
    ('evidence_ingestion_operations', ('tenant_id', 'customer_id')),
    ('evidence_ingestion_operations', ('tenant_id', 'evidence_id')),
    ('finance_records', ('tenant_id', 'journey_id')),
    ('finance_records', ('tenant_id', 'loan_disbursement_payment_id')),
    ('finance_records', ('tenant_id', 'source_evidence_id')),
    ('finding_evidence', ('tenant_id', 'audit_finding_id')),
    ('finding_evidence', ('tenant_id', 'evidence_fact_id')),
    ('finding_evidence', ('tenant_id', 'evidence_id')),
    ('finding_remarks', ('tenant_id', 'audit_finding_id')),
    ('insurance_records', ('tenant_id', 'source_evidence_id')),
    ('invoice_review_values', ('tenant_id', 'source_evidence_id')),
    ('journey_addons', ('tenant_id', 'journey_id')),
    ('journey_addons', ('tenant_id', 'source_evidence_id')),
    ('journey_attribute_resolutions', ('tenant_id', 'source_evidence_id')),
    ('journey_delivery_audit_facts', ('tenant_id', 'observed_source_evidence_id')),
    ('journey_document_assessments', ('tenant_id', 'evidence_id')),
    ('journey_document_assessments', ('tenant_id', 'journey_document_requirement_id')),
    ('journey_document_extracted_fields', ('tenant_id', 'evidence_id')),
    ('journey_document_field_correction_proposals', ('tenant_id', 'audit_finding_id')),
    ('outbox_events', ('tenant_id', 'journey_id')),
    ('p2_file_identity_enforcement', ('tenant_id', 'journey_id')),
    ('p2_file_moves', ('tenant_id', 'source_journey_id')),
    ('p2_journey_snapshots', ('tenant_id', 'journey_id')),
    ('p2_task_events', ('tenant_id', 'journey_id')),
    ('p2_task_events', ('tenant_id', 'task_id')),
    ('p2_upload_batches', ('tenant_id', 'replaces_evidence_id')),
    ('p2_work_queue', ('tenant_id', 'journey_id')),
    ('payment_bank_matches', ('tenant_id', 'bank_statement_line_id')),
    ('payment_verification_events', ('tenant_id', 'journey_id')),
    ('payment_verification_events', ('tenant_id', 'payment_id')),
    ('payment_verification_events', ('tenant_id', 'source_evidence_id')),
    ('payments', ('tenant_id', 'source_evidence_id')),
    ('registration_records', ('tenant_id', 'source_evidence_id')),
    ('review_decisions', ('tenant_id', 'journey_id')),
    ('rule_executions', ('tenant_id', 'audit_finding_id')),
    ('scrappage_certificate_review_values', ('tenant_id', 'source_evidence_id')),
    ('trade_in_cases', ('tenant_id', 'source_evidence_id')),
    ('vehicle_records', ('tenant_id', 'source_evidence_id')),
    ('workflow_dead_letters', ('tenant_id', 'journey_id')),
    ('workflow_instances', ('tenant_id', 'journey_id')),
    ('workflow_task_events', ('tenant_id', 'journey_id')),
    ('workflow_task_events', ('tenant_id', 'workflow_instance_id')),
    ('workflow_task_events', ('tenant_id', 'workflow_task_id')),
    ('workflow_tasks', ('tenant_id', 'workflow_instance_id')),
)


def index_name(table: str, columns: tuple[str, ...]) -> str:
    import hashlib

    name = "ix_fk_" + table + "_" + "_".join(c for c in columns if c != "tenant_id")
    if len(name) > 63:
        name = name[:54] + "_" + hashlib.sha1(name.encode()).hexdigest()[:8]
    return name


def _exists(table: str, columns: tuple[str, ...]) -> bool:
    found = op.get_bind().execute(
        text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'auditcore' AND table_name = :t AND column_name = ANY(:c)"
        ),
        {"t": table, "c": list(columns)},
    ).scalar()
    return found == len(columns)


def upgrade() -> None:
    for table, columns in FOREIGN_KEYS:
        if not _exists(table, columns):
            continue
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name(table, columns)} "
            f"ON auditcore.{table} ({', '.join(columns)})"
        )


def downgrade() -> None:
    for table, columns in FOREIGN_KEYS:
        op.execute(f"DROP INDEX IF EXISTS auditcore.{index_name(table, columns)}")
