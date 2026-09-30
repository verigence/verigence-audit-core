"""Shared fixtures for DB-backed Phase 2 integration tests.

Every Journey created here belongs to a fresh test tenant and is removed with
conftest.delete_tenant_data after the test; P2 rows cascade from journeys.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from audit_core.db import set_tenant_context


@dataclass
class P2Journey:
    engine: Engine
    tenant_id: str
    journey_id: UUID
    actor_id: str
    dealer_id: UUID
    outlet_id: UUID
    customer_id: UUID


def database_engine() -> Engine:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for Phase 2 integration tests")
    return create_engine(database_url)


def create_p2_journey(engine: Engine, *, prefix: str = "p2") -> P2Journey:
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-{prefix}-{suffix}"
    actor_id = f"{prefix}-pc-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"P2-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"P2-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, "
                 "product_category_id, effective_start_date, timezone_name, project_status) "
                 "VALUES (:t, :c, 'P2 Project', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"),
            {"t": tenant_id, "c": f"P2-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"P2-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"P2-O-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, "
                 "business_role_code, dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": tenant_id, "a": actor_id, "d": dealer_id, "o": outlet_id},
        )
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, "
                 "display_name) VALUES (:t, :d, :o, 'INDIVIDUAL', 'P2 Customer') RETURNING customer_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                 "journey_reference) VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"P2-J-{suffix}"},
        ).scalar_one()
    return P2Journey(
        engine=engine,
        tenant_id=tenant_id,
        journey_id=UUID(str(journey_id)),
        actor_id=actor_id,
        dealer_id=UUID(str(dealer_id)),
        outlet_id=UUID(str(outlet_id)),
        customer_id=UUID(str(customer_id)),
    )


def add_page(
    journey: P2Journey,
    *,
    page_number: int = 1,
    status: str = "CLASSIFYING",
    di_document_id: UUID | None = None,
    submitted_minutes_ago: float = 0.0,
) -> tuple[UUID, UUID, UUID]:
    """Insert one upload batch + one queue page. Returns (batch, queue, di_document)."""
    di_document_id = di_document_id or uuid4()
    batch_id = uuid4()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, original_filename, content_type,
                    size_bytes, page_count, original_object_key, batch_status,
                    uploaded_by_actor_id
                ) VALUES (
                    :t, :b, :j, 'scan.pdf', 'application/pdf', 100, 1, :k,
                    'PROCESSING', :a
                )
                """
            ),
            {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id,
             "k": f"p2/{batch_id}", "a": journey.actor_id},
        )
        queue_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_document_queue (
                    tenant_id, batch_id, journey_id, page_number, page_sha256,
                    page_object_key, client_upload_id, di_document_id, queue_status,
                    di_submitted_at_utc
                ) VALUES (
                    :t, :b, :j, :p, :sha, :k, :c, :d, :s,
                    now() - (:ago * interval '1 minute')
                ) RETURNING queue_id
                """
            ),
            {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id, "p": page_number,
             "sha": uuid4().hex, "k": f"p2/{batch_id}/{page_number}", "c": f"p2-{uuid4().hex}",
             "d": di_document_id, "s": status, "ago": submitted_minutes_ago},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_capture_v2_documents (
                    tenant_id, journey_id, stage_code, di_document_id, client_upload_id,
                    capture_status, created_by_actor_id
                ) VALUES (:t, :j, 'BOOKING', :d, :c, 'CLASSIFYING', :a)
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": di_document_id,
             "c": f"cap-{uuid4().hex}", "a": journey.actor_id},
        )
    return batch_id, UUID(str(queue_id)), di_document_id


def add_extracted_field(
    journey: P2Journey,
    *,
    di_document_id: UUID,
    field_key: str,
    value: Any,
    confidence: float | None = 95.0,
    document_type: str = "booking_form",
    canonical_field_id: str | None = None,
) -> str:
    canonical = canonical_field_id or f"cf-{field_key}"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_extracted_fields (
                    tenant_id, journey_id, di_document_id, source_fact_version,
                    field_key, stage_code, source_canonical_field_id,
                    source_document_type_key, extracted_value, effective_value,
                    confidence_score, confidence_scale
                ) VALUES (
                    :t, :j, :d, 1, :f, 'BOOKING', :cf, :dt,
                    CAST(:v AS jsonb), CAST(:v AS jsonb), :c,
                    CASE WHEN CAST(:c AS numeric) IS NULL THEN NULL ELSE 'PERCENT' END
                )
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": di_document_id, "f": field_key,
             "cf": canonical, "dt": document_type, "v": json.dumps(value), "c": confidence},
        )
    return canonical


def principal(journey: P2Journey) -> SimpleNamespace:
    return SimpleNamespace(subject=journey.actor_id)


class AllowAllAuthorization:
    """Security authorization client double: allows everything, records calls."""

    def __init__(self, role_key: str = "PC") -> None:
        self.calls: list[str] = []
        self.role_key = role_key

    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        self.calls.append(permission_key)
        return SimpleNamespace(allowed=True, role_key=self.role_key)


def queue_row(journey: P2Journey, work_type: str, work_key: str) -> dict[str, Any] | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        row = connection.execute(
            text(
                """
                SELECT * FROM auditcore.p2_work_queue
                WHERE tenant_id=:t AND work_type=:wt AND work_key=:wk
                """
            ),
            {"t": journey.tenant_id, "wt": work_type, "wk": work_key},
        ).mappings().one_or_none()
    return dict(row) if row else None


def add_evidence(
    journey: P2Journey,
    *,
    di_document_id: UUID,
    document_type_key: str,
    process_area: str = "BOOKING",
    status: str = "ACTIVE",
) -> UUID:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return UUID(str(connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                    document_type_key, evidence_purpose, process_area, association_status
                ) VALUES (:t, :j, :c, :s, :d, :k, 'JOURNEY_DOCUMENT', :p, :st)
                RETURNING evidence_id
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id, "c": journey.customer_id,
             "s": uuid4(), "d": di_document_id, "k": document_type_key, "p": process_area,
             "st": status},
        ).scalar_one()))


def add_ready_document(
    journey: P2Journey, document_type_key: str, *, confidence: float = 99.0, **fields: Any,
) -> UUID:
    """ACTIVE evidence plus durable, high-confidence facts: the document is ready."""
    di_document_id = uuid4()
    add_evidence(journey, di_document_id=di_document_id, document_type_key=document_type_key)
    for key, value in (fields or {"marker": "x"}).items():
        add_extracted_field(
            journey, di_document_id=di_document_id, field_key=key, value=value,
            document_type=document_type_key, confidence=confidence,
        )
    return di_document_id


def add_receipt_payment(
    journey: P2Journey,
    *,
    amount: str,
    receipt_number: str | None,
    receipt_date: str | None,
    di_document_id: UUID | None = None,
) -> UUID:
    di_document_id = di_document_id or add_ready_document(journey, "dealer_receipt", amount_paid=amount)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        booking_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.bookings (tenant_id, journey_id) VALUES (:t, :j)
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET updated_at_utc=now()
                RETURNING booking_id
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.payments (
                    tenant_id, journey_id, amount, booking_id, payment_stage,
                    status_source, source_di_document_id, receipt_number, receipt_date
                ) VALUES (:t, :j, :a, :b, 'BOOKING', 'EVIDENCE', :d, :n, :rd)
                """
            ),
            {"t": journey.tenant_id, "j": journey.journey_id, "a": amount, "b": booking_id,
             "d": di_document_id, "n": receipt_number, "rd": receipt_date},
        )
    return di_document_id


def set_minimum_booking_amount(journey: P2Journey, amount: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.tenant_rule_config (tenant_id, minimum_booking_amount)
                VALUES (:t, :a)
                ON CONFLICT (tenant_id) DO UPDATE SET minimum_booking_amount=EXCLUDED.minimum_booking_amount
                """
            ),
            {"t": journey.tenant_id, "a": amount},
        )


def add_batch_pages(
    journey: P2Journey,
    pages: list[tuple[str | None, str]],
    *,
    grouping_status: str = "PENDING",
) -> tuple[UUID, list[dict[str, Any]]]:
    """One multi-page upload whose pages DI has already classified.

    ``pages`` is a list of (di_type or None, queue_status)."""
    batch_id = uuid4()
    rows: list[dict[str, Any]] = []
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, original_filename, content_type,
                    size_bytes, page_count, original_object_key, batch_status,
                    uploaded_by_actor_id, grouping_status
                ) VALUES (:t, :b, :j, 'packet.pdf', 'application/pdf', 100, :n, :k,
                          'PROCESSING', :a, :g)
                """
            ),
            {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id, "n": len(pages),
             "k": f"p2/{batch_id}", "a": journey.actor_id, "g": grouping_status},
        )
        for number, (di_type, status) in enumerate(pages, start=1):
            di_document_id = uuid4()
            queue_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_document_queue (
                        tenant_id, batch_id, journey_id, page_number, page_numbers, page_sha256,
                        page_object_key, client_upload_id, di_document_id, queue_status,
                        classified_document_type, business_stage, di_submitted_at_utc
                    ) VALUES (:t, :b, :j, :p, ARRAY[:p], :sha, :k, :c, :d, :s, :dt, 'BOOKING', now())
                    RETURNING queue_id
                    """
                ),
                {"t": journey.tenant_id, "b": batch_id, "j": journey.journey_id, "p": number,
                 "sha": uuid4().hex, "k": f"p2/{batch_id}/pages/{number:04d}.pdf",
                 "c": f"p2-{uuid4().hex}", "d": di_document_id, "s": status, "dt": di_type},
            ).scalar_one()
            rows.append({"queue_id": UUID(str(queue_id)), "di_document_id": di_document_id,
                         "page_number": number, "object_key": f"p2/{batch_id}/pages/{number:04d}.pdf"})
    return batch_id, rows


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, key: str) -> bytes:
        return self.objects[key]

    def put_object(self, key: str, payload: bytes, *, content_type: str) -> None:
        self.objects[key] = payload


def one_page_pdf(width: int = 200) -> bytes:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=width, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
