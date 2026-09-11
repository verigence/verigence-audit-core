import inspect
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.uc03_document_capture_v2 import (
    _authorize_booking_for_resync,
    _backfill_evidence_links_for_resync,
    _build_capture_response,
    _build_local_capture_response,
    _candidate_type_keys,
    _ensure_evidence_link_for_resync,
    _human_actor_id,
    _requirement_refs_by_document_type_key,
    resync_booking_capture_v2,
)

JOURNEY_ID = UUID("11111111-1111-1111-1111-111111111111")
DOCUMENT_ID = UUID("22222222-2222-2222-2222-222222222222")


def _requirement(
    *,
    key: str = "booking_docket",
    document_type: str = "booking_docket",
    level: str = "REQUIRED",
    condition: str | None = None,
) -> dict[str, object]:
    return {
        "requirement_key": key,
        "display_label": key.replace("_", " ").title(),
        "document_type_key": document_type,
        "requirement_level": level,
        "condition_key": condition,
    }


def _classified_document(
    *,
    document_type: str = "booking_docket",
) -> dict[str, object]:
    return {
        "documentId": str(DOCUMENT_ID),
        "clientUploadId": "client-upload-1",
        "state": "CLASSIFIED",
        "classifiedDocumentTypeKey": document_type,
        "originalFilename": "booking.pdf",
        "contentUrl": "https://example.test/signed-document",
        "processingStatus": "PROCESSING",
    }


def _response(
    *,
    requirements: list[dict[str, object]],
    declarations: dict[str, dict[str, object]] | None = None,
    audit_documents: list[dict[str, object]] | None = None,
    di_documents: list[dict[str, object]] | None = None,
):
    return _build_capture_response(
        journey_id=JOURNEY_ID,
        context_ref="journey:11111111-1111-1111-1111-111111111111",
        requirements=requirements,
        declaration_rows=declarations or {},
        audit_documents=audit_documents or [],
        di_documents=di_documents or [],
    )


def test_required_document_missing_is_non_blocking_audit_observation() -> None:
    result = _response(requirements=[_requirement()])

    assert result.canContinue is True
    assert result.requirements[0].state == "NOT_UPLOADED"
    assert result.requirements[0].blocksContinue is False


def test_required_classified_document_allows_screen_two() -> None:
    result = _response(
        requirements=[_requirement()],
        audit_documents=[
            {
                "di_document_id": DOCUMENT_ID,
                "requirement_key": "booking_docket",
            }
        ],
        di_documents=[_classified_document()],
    )

    assert result.canContinue is True
    assert result.requirements[0].state == "UPLOADED"
    assert result.requirements[0].canView is True
    assert result.requirements[0].canDelete is True


def test_unknown_upload_does_not_satisfy_requirement_but_does_not_block() -> None:
    unknown = _classified_document()
    unknown["state"] = "UNKNOWN"
    unknown["classifiedDocumentTypeKey"] = None

    result = _response(
        requirements=[_requirement()],
        audit_documents=[
            {
                "di_document_id": DOCUMENT_ID,
                "requirement_key": None,
            }
        ],
        di_documents=[unknown],
    )

    assert result.canContinue is True
    assert result.requirements[0].state == "NOT_UPLOADED"
    assert result.requirements[0].blocksContinue is False
    assert result.uploads[0].state == "UNKNOWN"


def test_unresolved_conditional_requirement_is_non_blocking() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="gst_certificate",
                document_type="gst_certificate",
                level="CONDITIONAL",
                condition="gstApplicable",
            )
        ]
    )

    assert result.canContinue is True
    assert result.requirements[0].applicabilityState == "UNRESOLVED"
    assert result.requirements[0].state == "NEEDS_DECISION"
    assert result.requirements[0].needsDecision is True
    assert result.requirements[0].blocksContinue is False


def test_applicable_available_conditional_document_missing_is_non_blocking() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="gst_certificate",
                document_type="gst_certificate",
                level="CONDITIONAL",
                condition="gstApplicable",
            )
        ],
        declarations={
            "gstApplicable": {
                "applicable": True,
                "document_available": True,
            }
        },
    )

    assert result.canContinue is True
    assert result.requirements[0].state == "NOT_UPLOADED"
    assert result.requirements[0].blocksContinue is False


def test_applicable_but_document_unavailable_is_recorded_and_does_not_block() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="corporate_id",
                document_type="corporate_id",
                level="CONDITIONAL",
                condition="corporateCustomer",
            )
        ],
        declarations={
            "corporateCustomer": {
                "applicable": True,
                "document_available": False,
            }
        },
    )

    assert result.canContinue is True
    assert result.requirements[0].applicabilityState == "APPLICABLE"
    assert result.requirements[0].state == "ACKNOWLEDGED_MISSING"
    assert result.requirements[0].blocksContinue is False
    assert result.declarations[0].applicable is True
    assert result.declarations[0].documentAvailable is False
    assert result.declarations[0].source == "PC"


def test_not_applicable_conditional_requirement_does_not_block() -> None:
    result = _response(
        requirements=[
            _requirement(
                key="trade_in_vehicle_rc",
                document_type="vehicle_rc",
                level="CONDITIONAL",
                condition="exchangeTaken",
            )
        ],
        declarations={
            "exchangeTaken": {
                "applicable": False,
                "document_available": None,
            }
        },
    )

    assert result.canContinue is True
    assert result.requirements[0].applicabilityState == "NOT_APPLICABLE"
    assert result.requirements[0].state == "NOT_APPLICABLE"
    assert result.requirements[0].blocksContinue is False


def test_v2_actor_id_uses_human_principal_subject() -> None:
    principal = HumanPrincipal(subject="pc-user-123")

    assert _human_actor_id(principal) == "pc-user-123"


def test_v2_completion_route_is_additive() -> None:
    assert "/v2/tenants/{tenant_id}/journeys/{journey_id}/booking/complete" in app.openapi()["paths"]


def test_booking_docket_requirement_sends_canonical_booking_form_to_di() -> None:
    # DI's schema + Core materialisation both key on "booking_form"; the legacy
    # "booking_docket" requirement type must be sent to DI as "booking_form" so
    # the sales contract is extracted against the real Booking Form schema.
    requirements = [
        {"document_type_key": "booking_docket"},
        {"document_type_key": "pan_card"},
    ]
    assert _candidate_type_keys(requirements) == ["booking_form", "pan_card"]


def test_requirement_ref_map_uses_only_the_canonical_key() -> None:
    # Regression test: this map feeds DI's create_upload_intents call alongside
    # candidate_document_type_keys (_candidate_type_keys, always canonical). DI
    # rejects the whole request with "Requirement-ref mapping contains a
    # non-candidate document type." if this map has a key outside the candidate
    # list -- a live production failure on every single Booking upload, because
    # this map used to also carry the legacy "booking_docket" key, which is
    # never a candidate once _candidate_type_keys canonicalizes it away.
    requirements = [{"document_type_key": "booking_docket", "requirement_ref": "req-1"}]
    refs = _requirement_refs_by_document_type_key(requirements)

    assert refs == {"booking_form": "req-1"}
    assert set(refs) <= set(_candidate_type_keys(requirements))


def test_local_completion_check_uses_reconciled_classified_links() -> None:
    requirements = [_requirement()]
    audit_documents = [{
        "di_document_id": DOCUMENT_ID,
        "client_upload_id": "client-upload-1",
        "capture_status": "CLASSIFIED",
        "classified_document_type_key": "booking_docket",
        "requirement_key": "booking_docket",
        "original_filename": "booking.pdf",
    }]

    result = _build_local_capture_response(
        journey_id=JOURNEY_ID,
        requirements=requirements,
        declaration_rows={},
        audit_documents=audit_documents,
    )

    assert result.canContinue is True
    assert result.requirements[0].state == "UPLOADED"


def test_booking_resync_only_includes_classified_documents() -> None:
    # Same filter as uc03_delivery_capture_v2._resyncable_document_ids,
    # duplicated (not imported, to avoid a circular import between the two
    # modules) -- kept in lockstep by this test asserting on the actual
    # source, not a restated copy of the logic.
    classified_id = uuid4()
    documents = [
        {"di_document_id": classified_id, "capture_status": "CLASSIFIED"},
        {"di_document_id": uuid4(), "capture_status": "UNKNOWN"},
        {"di_document_id": uuid4(), "capture_status": None},
        {"di_document_id": uuid4()},
    ]
    resyncable = [
        row["di_document_id"] for row in documents
        if str(row.get("capture_status") or "").upper() == "CLASSIFIED"
    ]
    assert resyncable == [classified_id]


def test_booking_resync_endpoint_authorizes_and_queues_the_shared_sync_task() -> None:
    # Source-inspected: exercising the full route (auth, a real
    # BackgroundTasks dispatch) needs infrastructure this file's other
    # tests don't set up. What matters for the regression this endpoint
    # exists to fix (available to any role, opens the same door Delivery's
    # own /resync already has) is that it (a) re-authorizes against the
    # journey WITHOUT requiring an active Booking (a resync on a Journey
    # that has moved on to Delivery, where Booking is CLOSED, must still
    # work -- this is the exact bug a closed Booking triggered live), (b)
    # refreshes classification status from DI's own live state before
    # filtering, so a stale local cache can't silently resync 0 documents,
    # (c) lists documents the same way the capture screen itself does, (d)
    # filters to classified documents, and (e) queues the same
    # _run_sync_booking_document_task the DI webhook itself uses -- a
    # manually-triggered resync goes through the identical, already-tested
    # pipeline (including every producer wired into it: SKU resolution,
    # identity/dealer checks, duplicate-receipt detection) rather than a
    # parallel one.
    source = inspect.getsource(resync_booking_capture_v2)
    assert "_authorize_booking_for_resync(" in source
    assert "_authorize_booking(" not in source
    assert "_ensure_di_context(" in source
    assert "_reconcile_documents(" in source
    assert "_linked_documents(" in source
    assert '"CLASSIFIED"' in source
    assert "_backfill_evidence_links_for_resync(" in source
    assert "background_tasks.add_task(" in source
    assert "_run_sync_booking_document_task" in source
    assert 'stage_code="BOOKING"' in source


def test_authorize_booking_for_resync_does_not_require_active_booking() -> None:
    # The regression itself: _authorize_booking (used by ordinary capture
    # writes) calls _require_active_booking and rejects a CLOSED Booking --
    # correct for a capture edit, wrong for a resync/repair action on a
    # Journey that has since progressed to Delivery. The resync-specific
    # authorizer must scope-check and load state without that gate.
    source = inspect.getsource(_authorize_booking_for_resync)
    assert "_scope(" in source
    assert "_capture_phase_state(" in source
    assert "_require_active_booking(" not in source


@pytest.fixture
def evidence_backfill_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for evidence-link-backfill integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-evbf-{suffix}"
    document_id = uuid4()
    with engine.begin() as connection:
        category_id = connection.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"EVBF-CAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"EVBF-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'EVBF', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"EVBF-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"EVBF-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"EVBF-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO auditcore.di_subject_mappings (
                tenant_id, customer_id, di_subject_id, di_subject_type, mapping_status
            ) VALUES (:t, :cu, :subj, 'OTHER', 'ACTIVE')"""),
            {"t": tenant_id, "cu": customer_id, "subj": uuid4()},
        )
        journey_id = connection.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"EVBF-J-{suffix}"},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status,
                audit_state, audit_status, first_started_at_utc,
                latest_activity_at_utc, version_no
            ) VALUES (
                :t, :j, 'BOOKING', 'BOOKING_CLOSED',
                'IN_PROGRESS', 'NOT_EVALUATED', now(), now(), 1
            )"""),
            {"t": tenant_id, "j": journey_id},
        )
        requirement_ref = connection.execute(
            text("""INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, requirement_key, document_type_key,
                process_area, requirement_level
            ) VALUES (
                :t, :j, 'booking_docket', 'booking_form', 'BOOKING', 'REQUIRED'
            ) RETURNING journey_document_requirement_id"""),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO auditcore.document_capture_v2_documents (
                tenant_id, journey_id, stage_code, di_document_id, client_upload_id,
                original_filename, content_type, requirement_key,
                classified_document_type_key, capture_status, created_by_actor_id
            ) VALUES (
                :t, :j, 'BOOKING', :doc, :upload,
                'booking-form.pdf', 'application/pdf', 'booking_docket',
                'booking_form', 'CLASSIFIED', 'test-actor'
            )"""),
            {"t": tenant_id, "j": journey_id, "doc": document_id, "upload": uuid4()},
        )
    yield {
        "engine": engine,
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "requirement_ref": requirement_ref,
        "requirement_key": "booking_docket",
        "document_id": document_id,
    }
    engine.dispose()


def _evidence_row(engine, *, tenant_id, document_id):
    with engine.begin() as connection:
        return connection.execute(
            text(
                "SELECT association_status FROM auditcore.evidence "
                "WHERE tenant_id=:t AND di_document_id=:d"
            ),
            {"t": tenant_id, "d": document_id},
        ).mappings().one_or_none()


def test_ensure_evidence_link_for_resync_creates_missing_link(evidence_backfill_setup) -> None:
    # The regression this closes: a document DI has genuinely classified
    # (document_capture_v2_documents.capture_status='CLASSIFIED', confirmed
    # by _reconcile_documents against DI's own live state) can still have NO
    # evidence row at all, if DI's one-time async "link" webhook callback for
    # that document never landed. _sync_booking_document's first check
    # ("link is None ... return 0") would then silently no-op forever on
    # every resync -- this function is what closes that gap.
    setup = evidence_backfill_setup
    assert _evidence_row(setup["engine"], tenant_id=setup["tenant_id"], document_id=setup["document_id"]) is None

    with setup["engine"].begin() as connection:
        created = _ensure_evidence_link_for_resync(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            requirement_ref=setup["requirement_ref"],
            document_id=setup["document_id"],
            service_id="manual-resync:test",
        )
    assert created is True

    row = _evidence_row(setup["engine"], tenant_id=setup["tenant_id"], document_id=setup["document_id"])
    assert row is not None
    assert row["association_status"] == "ACTIVE"


def test_ensure_evidence_link_for_resync_is_a_no_op_when_already_active(evidence_backfill_setup) -> None:
    setup = evidence_backfill_setup
    with setup["engine"].begin() as connection:
        _ensure_evidence_link_for_resync(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            requirement_ref=setup["requirement_ref"],
            document_id=setup["document_id"],
            service_id="manual-resync:test",
        )
    # Calling it again must not raise, and must not create a second row --
    # this is exactly what a real resync click does every time it's pressed.
    with setup["engine"].begin() as connection:
        result = _ensure_evidence_link_for_resync(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            requirement_ref=setup["requirement_ref"],
            document_id=setup["document_id"],
            service_id="manual-resync:test",
        )
    assert result is True

    with setup["engine"].begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM auditcore.evidence "
                "WHERE tenant_id=:t AND di_document_id=:d"
            ),
            {"t": setup["tenant_id"], "d": setup["document_id"]},
        ).scalar_one()
    assert count == 1


def test_backfill_evidence_links_for_resync_resolves_ref_from_requirement_key(evidence_backfill_setup) -> None:
    # This is the shape resync_booking_capture_v2/resync_delivery_capture_v2
    # actually have on hand: _linked_documents-style rows (requirement_key,
    # a string) and the _base_requirements-style requirements list
    # (requirement_key + requirement_ref). The helper must resolve one from
    # the other itself -- callers never look up requirement_ref directly.
    setup = evidence_backfill_setup
    documents = [{"di_document_id": setup["document_id"], "requirement_key": setup["requirement_key"]}]
    requirements = [{"requirement_key": setup["requirement_key"], "requirement_ref": setup["requirement_ref"]}]

    with setup["engine"].begin() as connection:
        _backfill_evidence_links_for_resync(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            documents=documents,
            document_ids=[setup["document_id"]],
            requirements=requirements,
            service_id="manual-resync:test",
        )

    row = _evidence_row(setup["engine"], tenant_id=setup["tenant_id"], document_id=setup["document_id"])
    assert row is not None
    assert row["association_status"] == "ACTIVE"


def test_backfill_evidence_links_for_resync_skips_document_with_no_resolvable_requirement(
    evidence_backfill_setup,
) -> None:
    # A document reconciled with no matching requirement_key (classified as
    # something not on this Journey's requirement list) has nothing to link
    # against -- must be skipped quietly, not raise.
    setup = evidence_backfill_setup
    documents = [{"di_document_id": setup["document_id"], "requirement_key": None}]

    with setup["engine"].begin() as connection:
        _backfill_evidence_links_for_resync(
            connection,
            tenant_id=setup["tenant_id"],
            journey_id=setup["journey_id"],
            documents=documents,
            document_ids=[setup["document_id"]],
            requirements=[],
            service_id="manual-resync:test",
        )

    assert _evidence_row(setup["engine"], tenant_id=setup["tenant_id"], document_id=setup["document_id"]) is None
