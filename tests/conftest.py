import os

os.environ.setdefault("APP_ENV", "test")

# Direct user correction (2026-09-23): DB-backed integration test fixtures
# across this suite (unified_capture_setup, evidence_backfill_setup, the
# `journey` fixtures in test_uc03_deal_reconciliation.py and
# test_uc03_duplicate_receipt_detection.py, and others with the same shape)
# each INSERT a real tenant/project/journey into the live database the test
# run points DATABASE_URL at, then only ever call engine.dispose() -- which
# closes the connection, not the data. No teardown existed anywhere, so
# every test run left its rows behind permanently; 145 such rows
# accumulated in one day of running these suites repeatedly. Every fixture
# of this shape must now call delete_tenant_data(engine, tenant_id) after
# its own yield.
_TENANT_DATA_DELETE_ORDER = (
    "activity_records", "administrative_operations", "audit_chain_heads", "audit_events",
    "audit_finding_events", "audit_state_events", "booking_form_review_values", "business_assignments",
    "commercial_line_source_values", "commercial_lines", "crm_interactions", "customer_identity_index",
    "customer_identity_review_values", "daily_ops_items", "dealer_receipt_review_values",
    "delivery_status_history", "di_subject_mappings", "discount_applications", "discount_policy_parameters",
    "discount_scheme_benefits", "discount_scheme_eligibility", "document_capture_v2_declarations",
    "document_capture_v2_documents", "escalations", "evidence_ingestion_operations", "finance_records",
    "finding_evidence", "finding_remarks", "idempotency_records", "inbox_events", "insurance_records",
    "invoice_review_values", "journey_addons", "journey_attribute_resolutions",
    "journey_attribute_review_decisions", "journey_capture_proposals", "journey_delivery_audit_facts",
    "journey_document_assessments", "journey_document_field_correction_proposals", "journey_products",
    "journey_stage_states", "journey_workflow_events", "model_selection_correction_proposals",
    "oem_master_uploads", "outbox_events", "payment_bank_matches", "payment_verification_events",
    "pc_daily_notes", "project_master_import_rows", "project_product_master_items", "project_segments",
    "registration_records", "review_decisions", "rule_executions", "scrappage_certificate_review_values",
    "tenant_rule_config", "trade_in_cases", "user_feedback", "vehicle_records", "work_item_finding_detail",
    "work_item_task_detail", "workflow_dead_letters", "workflow_task_attempts", "workflow_task_events",
    "bank_statement_lines", "discount_policy_versions", "discount_scheme_versions", "evidence_facts",
    "journey_document_extracted_fields", "payments", "price_list_items", "project_master_imports",
    "project_product_master_versions", "work_items", "workflow_tasks", "audit_findings", "deliveries",
    "discount_schemes", "project_product_masters", "workflow_instances", "audit_evaluations", "bookings",
    "daily_ops_runs", "audit_control_versions", "dealership_staff", "audit_controls",
    # This group has a real circular FK dependency among itself (journeys <->
    # document_requirement_profile_versions via journeys' own pointer column;
    # customers <-> evidence via customers' own pointer column) -- both
    # pointer columns are nulled below before this group is deleted, which
    # is what makes this fixed order safe.
    "evidence", "journey_document_requirements", "document_requirement_items",
    "document_requirement_profile_versions", "document_requirement_profiles", "journeys", "customers",
    "dealer_outlets", "dealers", "price_list_versions", "price_lists", "project_policy_versions",
    "business_status_codes", "projects",
)


# Every trigger in auditcore whose job is to protect real production data
# from mutation -- version-controlled masters (protect_version_child_
# mutation, protect_published_version, protect_project_product_master_
# version) and append-only audit trails (prevent_append_only_mutation),
# plus one field-level guard (protect_customer_entered_name). A fixture
# doing ordinary test setup (publishing a requirement profile version,
# writing a workflow/audit event row) legitimately triggers all of these,
# by design -- verified against the live pg_trigger catalog, not guessed,
# after two rounds of a fixture teardown failing on ones missed the first
# time. Disabled only for this one tenant's cleanup.
#
# No try/finally re-enabling these: ALTER TABLE ... DISABLE/ENABLE TRIGGER
# is transactional DDL. If everything below succeeds, the ENABLE at the end
# commits atomically with it. If anything raises, the whole `with engine.
# begin()` block rolls back -- which undoes the DISABLE too, automatically,
# via ordinary Postgres transaction semantics. A manual finally-re-enable
# was tried first and made things worse: it ran more statements against an
# already-aborted transaction, masking the real error with a second,
# unrelated one (InFailedSqlTransaction) instead of surfacing it.
_PROTECTED_TABLES = (
    "discount_policy_parameters", "discount_scheme_benefits", "discount_scheme_eligibility",
    "document_requirement_items", "price_list_items", "project_product_master_items",
    "audit_control_versions", "discount_policy_versions", "discount_scheme_versions",
    "document_requirement_profile_versions", "price_list_versions", "project_policy_versions",
    "project_product_master_versions", "customers",
    "audit_events", "audit_finding_events", "audit_state_events", "delivery_status_history",
    "finding_remarks", "journey_workflow_events", "payment_verification_events",
    "review_decisions", "workflow_task_events",
)


def delete_tenant_data(engine, tenant_id: str) -> None:
    """Delete every row for one test-created tenant_id, in dependency order,
    verifying nothing is left in any tenant_id-scoped table afterward. Only
    ever call this with a tenant_id a fixture just created itself in this
    same test run -- never with an externally-supplied or pre-existing one."""
    from sqlalchemy import text

    with engine.begin() as connection:
        for table in _PROTECTED_TABLES:
            connection.execute(text(f"ALTER TABLE auditcore.{table} DISABLE TRIGGER USER"))

        connection.execute(
            text("UPDATE auditcore.journeys SET document_requirement_profile_version_id=NULL "
                 "WHERE tenant_id=:t"),
            {"t": tenant_id},
        )
        connection.execute(
            text("UPDATE auditcore.customers SET legal_name_source_evidence_id=NULL WHERE tenant_id=:t"),
            {"t": tenant_id},
        )
        for table in _TENANT_DATA_DELETE_ORDER:
            connection.execute(text(f"DELETE FROM auditcore.{table} WHERE tenant_id=:t"), {"t": tenant_id})

        tenant_tables = connection.execute(
            text("SELECT table_name FROM information_schema.columns "
                 "WHERE table_schema='auditcore' AND column_name='tenant_id'")
        ).scalars().all()
        leftover = {}
        for table in tenant_tables:
            count = connection.execute(
                text(f"SELECT count(*) FROM auditcore.{table} WHERE tenant_id=:t"),
                {"t": tenant_id},
            ).scalar_one()
            if count:
                leftover[table] = count
        if leftover:
            raise AssertionError(
                f"delete_tenant_data left rows behind for {tenant_id}: {leftover} "
                f"-- add the missing table(s) to _TENANT_DATA_DELETE_ORDER"
            )

        for table in _PROTECTED_TABLES:
            connection.execute(text(f"ALTER TABLE auditcore.{table} ENABLE TRIGGER USER"))
