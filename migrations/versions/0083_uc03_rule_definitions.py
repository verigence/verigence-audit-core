"""0083_uc03_rule_definitions — unified rule registry (Definition).

Phase 1 of the unified rule-engine platform: one table listing every rule
the platform has, regardless of which engine actually executes it --
``finding_types`` (migration 0060) is a category->class->owner mapping,
useful but not a rule registry (many rules share one finding_type_code,
and it has no notion of trigger events, rerun policy, or which engine
executes a rule). ``rule_definitions`` supersedes it as the authoritative
Definition; ``finding_types`` itself is left untouched -- other code still
reads it -- but stops being the thing new work extends.

Seeded here with every audit-core CODE-executor rule this session's own
inventory pass verified (see the plan doc / PR description for the full
audit trail this seed data was built from -- every row below traces to a
specific producer function and trigger call site, not guessed). Every
finding_class/owner/mode value matches its rule's existing finding_type_code
row in ``finding_types`` exactly (cross-checked against migrations 0060,
0063, 0064, 0067, 0069, 0074, 0077, 0081, 0082) -- no new classification
decisions made here, only formalized into a per-rule record.

RULE_ENGINE-executor rows (the external rule-engine's 85 rules) are
deliberately NOT seeded here -- a schema migration must never depend on a
live network call to another service. Those rows are synced at runtime via
``uc03_rule_registry.py``'s fetch-through cache against the rule-engine's
own ``GET /audit/rules`` catalog.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0083_uc03_rule_definitions"
down_revision = "0082_uc03_duplicate_receipt"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"

# rule_code -> (category, title, finding_class, severity, owner, mode,
#               trigger_events, rerun_policy, description)
_AUDIT_CORE_RULES: dict[str, tuple] = {
    "WRONG_DOCUMENT": (
        "Customer & Dealer Identity", "Document name/dealer does not match KYC",
        "VIOLATION", "HIGH", "TL", "ADJUDICATED",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("Every named document (Booking Form, Insurance, invoices, receipts) is "
        "checked against the customer's KYC name; every receipt's dealer name is "
        "checked against this journey's own dealer."),
    ),
    "DUPLICATE_RECEIPT": (
        "Payments & Reconciliation", "Possible duplicate receipt upload",
        "VIOLATION", "HIGH", "TL", "ADJUDICATED",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("Two or more receipts of the same type on this journey show the same "
        "amount (and receipt number, or same date with none legible)."),
    ),
    "MANUAL_VERIFICATION": (
        "Document Extraction & Completeness", "Low-confidence extracted value needs review",
        "DATA_GAP", "LOW", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED", "CAPTURE_SCREEN_READ"], "RERUNNABLE",
        ("One or more machine-read values on a document are below the 90% "
        "confidence threshold."),
    ),
    "MODEL_NOT_IDENTIFIED": (
        "Vehicle & Model Resolution", "Vehicle model could not be matched to a SKU",
        "DATA_GAP", "MEDIUM", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED", "DELIVERY_COMPLETED"], "RERUNNABLE",
        ("Booking model text matched zero or more than one price-master SKU; a "
        "Delivery invoice's own model/SKU text is tried as a fallback."),
    ),
    "PAYMENT_BANK_UNMATCHED": (
        "Payments & Reconciliation", "Payment not evidenced in a bank statement",
        "DATA_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("A non-cash payment could not be matched to a bank credit on reference, "
        "amount and date."),
    ),
    "BK_DISCOUNT_EVIDENCE_MISSING": (
        "Document Extraction & Completeness", "Discount evidence document missing",
        "DOCUMENT_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("The Booking Form shows a corporate/exchange/scrappage discount with no "
        "supporting document on file."),
    ),
    "BK_MIN_BOOKING_AMOUNT_NOT_MET": (
        "Payments & Reconciliation", "Booking below minimum payment",
        "VIOLATION", "HIGH", "TL", "ADJUDICATED",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("Total Booking payments received are below the configured minimum "
        "booking amount."),
    ),
    "WF_BOOKING_INCOMPLETE_AT_DELIVERY_START": (
        "Delivery Process", "Booking incomplete at Delivery Start",
        "VIOLATION", "HIGH", "TL", "ADJUDICATED",
        ["DELIVERY_STARTED"], "ONCE",
        ("Delivery was started while Booking had not been closed with Proceed to "
        "Delivery."),
    ),
    "DL_VIN_RECONCILIATION": (
        "Vehicle & Model Resolution", "VIN/chassis mismatch",
        "VIOLATION", "CRITICAL", "TL", "ADJUDICATED",
        ["DELIVERY_VEHICLE_OBSERVATION_RECORDED"], "ONCE",
        ("Comparable full vehicle identifiers conflict at physical delivery "
        "observation."),
    ),
    "DL_NOT_INTIMATED": (
        "Delivery Process", "Delivery not intimated",
        "DATA_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DELIVERY_COMPLETED"], "ONCE",
        "Delivery was completed without recording customer intimation.",
    ),
    "DOC_REQUIRED_ANSWER_NO": (
        "Document Extraction & Completeness", "Required Delivery document answered No",
        "DOCUMENT_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DELIVERY_DOCUMENT_ANSWERED_NO", "DELIVERY_COMPLETED"], "ONCE",
        "A configured mandatory Delivery document was explicitly answered No.",
    ),
    "PAY_UNVERIFIED_RECEIPT": (
        "Delivery Process", "Delivery completed with unverified payment",
        "DATA_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DELIVERY_COMPLETED"], "ONCE",
        ("One or more captured payments do not have a VERIFIED realization status "
        "at Delivery completion."),
    ),
    "WF_DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE": (
        "Delivery Process", "Delivery completed with audit incomplete",
        "VIOLATION", "HIGH", "TL", "ADJUDICATED",
        ["DELIVERY_COMPLETED"], "ONCE",
        ("Physical Delivery was completed and recorded while configured audit "
        "gaps remained open."),
    ),
    "DOCUMENT_UNRECOGNIZED": (
        "Document Extraction & Completeness", "Unrecognized document",
        "DATA_GAP", "LOW", "PC", "SELF_SERVICE",
        ["CAPTURE_SCREEN_READ"], "RERUNNABLE",
        ("Document Intelligence could not confidently identify an uploaded "
        "document as any known type."),
    ),
    "DOCUMENT_MISSING": (
        "Document Extraction & Completeness", "Document could not be processed",
        "DATA_GAP", "MEDIUM", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("Document Intelligence could not process an uploaded document -- "
        "unreadable, corrupt, or the wrong document."),
    ),
    "AUTOMATED_SYNC_FAILURE": (
        "System Health", "Automated background step failed",
        "DATA_GAP", "MEDIUM", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("SKU resolution or payment reconciliation hit an unexpected internal "
        "error (not the expected zero/many-match outcome)."),
    ),
    "BK_DOCKET_PRESENT": (
        "Document Extraction & Completeness", "Booking Form / OTF present",
        "DOCUMENT_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED", "BOOKING_REVIEW_CONFIRMED", "BOOKING_SUBMITTED"], "RERUNNABLE",
        "Booking checkpoint rule: the Booking Form/OTF document must be on file.",
    ),
    "BK_PAN_PRESENT": (
        "Customer & Dealer Identity", "PAN card present",
        "VIOLATION", "HIGH", "TL", "ADJUDICATED",
        ["DOCUMENT_SYNCED", "BOOKING_REVIEW_CONFIRMED", "BOOKING_SUBMITTED"], "RERUNNABLE",
        "Booking checkpoint rule: a PAN card must be on file for KYC.",
    ),
    "BK_MIN_BOOKING_PROOF_PRESENT": (
        "Payments & Reconciliation", "Minimum booking payment proof present",
        "DATA_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED", "BOOKING_REVIEW_CONFIRMED", "BOOKING_SUBMITTED"], "RERUNNABLE",
        "Booking checkpoint rule: at least one payment receipt must be on file.",
    ),
    "BK_CONDITIONAL_DOCS_ADDRESSED": (
        "Document Extraction & Completeness", "Conditional documents addressed",
        "DOCUMENT_GAP", "HIGH", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED", "BOOKING_REVIEW_CONFIRMED", "BOOKING_SUBMITTED"], "RERUNNABLE",
        ("Booking checkpoint rule: every conditionally-required document (GST, "
        "corporate ID, exchange RC, ...) must be resolved or declared N/A."),
    ),
    "BK_REQUIRED_CAPTURE_COMPLETE": (
        "Document Extraction & Completeness", "Required capture fields complete",
        "VIOLATION", "HIGH", "TL", "ADJUDICATED",
        ["DOCUMENT_SYNCED", "BOOKING_REVIEW_CONFIRMED", "BOOKING_SUBMITTED"], "RERUNNABLE",
        ("Booking checkpoint rule: every required capture field must have a "
        "reviewed value."),
    ),
    "DL_V2_REQUIRED_DOCUMENT_MISSING": (
        "Document Extraction & Completeness", "Delivery required document missing",
        "DOCUMENT_GAP", "MEDIUM", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        "A configured mandatory Delivery document has not been uploaded.",
    ),
    "DL_V2_DOCUMENT_PROCESSING_FAILED": (
        "Document Extraction & Completeness", "Delivery document processing failed",
        "DOCUMENT_GAP", "MEDIUM", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("A submitted Delivery document could not be processed successfully by "
        "Document Intelligence."),
    ),
    "FINANCE_HYPOTHECATION_MISSING": (
        "Payments & Reconciliation", "Financed deal missing hypothecation charges",
        "DATA_GAP", "MEDIUM", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("This deal is financed but no hypothecation (HP) charges amount has "
        "been captured."),
    ),
    "UC03_DI_LOW_CONFIDENCE_POST_SUBMIT": (
        "Document Extraction & Completeness", "Low-confidence value after Booking submit",
        "DOCUMENT_GAP", "INFO", "PC", "SELF_SERVICE",
        ["DOCUMENT_SYNCED"], "RERUNNABLE",
        ("A document confirmed after Booking submission still has unreviewed "
        "low-confidence fields."),
    ),
}


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS auditcore.rule_definitions (
                rule_code           varchar(160) PRIMARY KEY,
                category            varchar(120) NOT NULL,
                title               varchar(300) NOT NULL,
                description         text,
                executor            varchar(20) NOT NULL
                                    CHECK (executor IN ('AUDIT_CORE','RULE_ENGINE')),
                execution_kind      varchar(20) NOT NULL
                                    CHECK (execution_kind IN ('CODE','DECLARATIVE')),
                trigger_events      text[] NOT NULL DEFAULT '{}',
                rerun_policy        varchar(20) NOT NULL
                                    CHECK (rerun_policy IN ('RERUNNABLE','ONCE')),
                finding_class       varchar(30)
                                    CHECK (finding_class IS NULL OR finding_class IN
                                           ('DATA_GAP','DOCUMENT_GAP','VIOLATION')),
                default_severity    varchar(20),
                default_owner_role  varchar(80),
                resolution_mode     varchar(20)
                                    CHECK (resolution_mode IS NULL OR resolution_mode IN
                                           ('SELF_SERVICE','ADJUDICATED')),
                enabled             boolean NOT NULL DEFAULT true,
                source_ref          varchar(160),
                version_no          integer NOT NULL DEFAULT 1 CHECK (version_no > 0),
                created_at_utc      timestamptz NOT NULL DEFAULT now(),
                updated_at_utc      timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER trg_rule_definitions_updated "
            "BEFORE UPDATE ON auditcore.rule_definitions "
            "FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()"
        )
    )
    conn.execute(
        text(f"GRANT SELECT, INSERT, UPDATE ON auditcore.rule_definitions TO {_RUNTIME_ROLE}")
    )
    conn.execute(text(f"REVOKE DELETE ON auditcore.rule_definitions FROM {_RUNTIME_ROLE}"))

    for rule_code, (
        category, title, finding_class, severity, owner, mode,
        trigger_events, rerun_policy, description,
    ) in _AUDIT_CORE_RULES.items():
        conn.execute(
            text(
                """
                INSERT INTO auditcore.rule_definitions (
                    rule_code, category, title, description,
                    executor, execution_kind, trigger_events, rerun_policy,
                    finding_class, default_severity, default_owner_role,
                    resolution_mode, enabled
                ) VALUES (
                    :rule_code, :category, :title, :description,
                    'AUDIT_CORE', 'CODE', :trigger_events, :rerun_policy,
                    :finding_class, :severity, :owner,
                    :mode, true
                )
                ON CONFLICT (rule_code) DO UPDATE SET
                    category = EXCLUDED.category,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    trigger_events = EXCLUDED.trigger_events,
                    rerun_policy = EXCLUDED.rerun_policy,
                    finding_class = EXCLUDED.finding_class,
                    default_severity = EXCLUDED.default_severity,
                    default_owner_role = EXCLUDED.default_owner_role,
                    resolution_mode = EXCLUDED.resolution_mode,
                    updated_at_utc = now()
                """
            ),
            {
                "rule_code": rule_code,
                "category": category,
                "title": title,
                "description": description,
                "trigger_events": trigger_events,
                "rerun_policy": rerun_policy,
                "finding_class": finding_class,
                "severity": severity,
                "owner": owner,
                "mode": mode,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS auditcore.rule_definitions CASCADE"))
