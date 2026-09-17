from __future__ import annotations

import inspect
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core import uc03_post_extraction_materialization
from audit_core.uc03_booking_rule_trigger import (
    run_booking_review_rule_task,
    schedule_booking_checkpoint_rules,
)


def _seed_booking_journey(engine, *, tenant_id: str, suffix: str, version_no: int = 1):
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"CKCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Checkpoint OEM') RETURNING oem_id"
            ),
            {"code": f"CKOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Checkpoint Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"CKP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Checkpoint Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"CKD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Checkpoint Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"CKO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Checkpoint Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'CHECKPOINT-JOURNEY'
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status, audit_state,
                    audit_status, first_started_at_utc, latest_activity_at_utc, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS',
                    'NOT_EVALUATED', now(), now(), :version_no
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version_no": version_no},
        )
    return journey_id


def test_schedule_booking_checkpoint_rules_dedupes_by_aggregate_version() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the checkpoint-rule scheduling test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-checkpoint-{suffix}"
    journey_id = _seed_booking_journey(engine, tenant_id=tenant_id, suffix=suffix, version_no=3)

    # Two calls of the SAME kind for the SAME aggregate version (e.g. a
    # retried Review Confirm, or two confirms racing) must collapse to
    # exactly one workflow_task, not one per call. Async (raise_new=False)
    # and confirm (raise_new=True) are a DIFFERENT case -- see
    # test_async_and_confirm_get_independent_tasks_for_the_same_version --
    # they deliberately do NOT share a task, so both calls here are the
    # confirm-style default (raise_new=True) to test same-kind dedup only.
    schedule_booking_checkpoint_rules(
        engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        correlation_id="test-1",
        trigger="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
    )
    schedule_booking_checkpoint_rules(
        engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        correlation_id="test-2",
        trigger="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
    )

    with engine.begin() as connection:
        tasks = connection.execute(
            text(
                """
                SELECT task_status, task_type
                FROM auditcore.workflow_tasks
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND task_type='BOOKING_RULE_EVALUATION'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().all()
    assert len(tasks) == 1
    assert tasks[0]["task_status"] == "COMPLETED"

    engine.dispose()


def test_async_and_confirm_get_independent_tasks_for_the_same_version() -> None:
    """The exact fix: async document sync (raise_new=False) and PC Review
    Confirm (raise_new=True) for the SAME unchanged version must NOT
    collapse to one task -- otherwise whichever fires first "wins" the
    task, and if that's the async trigger (raise_new=False, the common
    case), the confirm-time call would find the task already COMPLETED and
    silently skip the one evaluation that was actually supposed to raise
    anything. Two distinct tasks, and only the confirm-style one raises."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the checkpoint-rule scheduling test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-checkpoint-indep-{suffix}"
    journey_id = _seed_booking_journey(engine, tenant_id=tenant_id, suffix=suffix, version_no=1)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, requirement_key, document_type_key,
                    process_area, requirement_level
                ) VALUES (
                    :tenant_id, :journey_id, 'booking_docket', 'booking_form',
                    'BOOKING', 'REQUIRED'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )

    # The async trigger fires first (the common, primary path) -- must not
    # raise, and must not claim the version's ONE task in a way that blocks
    # the confirm-time evaluation from running at all.
    schedule_booking_checkpoint_rules(
        engine, tenant_id=tenant_id, journey_id=journey_id,
        correlation_id="async", trigger="ASYNC_DOCUMENT_SYNC", raise_new=False,
    )
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT 1 FROM auditcore.audit_findings "
                "WHERE tenant_id=:t AND journey_id=:j AND rule_key='BK_DOCKET_PRESENT'"
            ),
            {"t": tenant_id, "j": journey_id},
        ).first() is None

    # PC Review Confirm, same version -- must still actually run and raise.
    schedule_booking_checkpoint_rules(
        engine, tenant_id=tenant_id, journey_id=journey_id,
        correlation_id="confirm", trigger="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
        raise_new=True,
    )
    with engine.begin() as connection:
        status = connection.execute(
            text(
                "SELECT finding_status FROM auditcore.audit_findings "
                "WHERE tenant_id=:t AND journey_id=:j AND rule_key='BK_DOCKET_PRESENT'"
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
        assert status == "OPEN"

        tasks = connection.execute(
            text(
                "SELECT task_status FROM auditcore.workflow_tasks "
                "WHERE tenant_id=:t AND journey_id=:j AND task_type='BOOKING_RULE_EVALUATION'"
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalars().all()
        assert len(tasks) == 2, "async (self-heal) and confirm (genuine check) must be independent tasks"
        assert set(tasks) == {"COMPLETED"}

    engine.dispose()


def test_schedule_booking_checkpoint_rules_is_a_clean_skip_with_no_stage_state() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the checkpoint-rule scheduling test")

    engine = create_engine(database_url)
    # No journey_stage_states row exists for this journey_id -- the function
    # must return quietly, not raise, exactly like a document syncing for a
    # journey mid-provisioning.
    schedule_booking_checkpoint_rules(
        engine,
        tenant_id=f"tenant-checkpoint-missing-{uuid4().hex}",
        journey_id=uuid4(),
        correlation_id="test",
        trigger="ASYNC_DOCUMENT_SYNC",
    )
    engine.dispose()


def test_confirm_handler_schedules_checkpoint_rules_as_a_safety_net() -> None:
    # Regression test for the bug this fix closes: confirm_booking_review_v2_
    # confidence_policy is the actually-live handler for POST .../booking/
    # review/confirm (confirmed by install order in uc03_document_capture_v2_
    # rules.py), but until this fix it never called schedule_booking_
    # checkpoint_rules (nor its predecessor) anywhere in its body -- the
    # Booking checkpoint rules and the external rule-engine phase silently
    # never ran on a live confirm. Source-inspected rather than exercised
    # end-to-end because the full confirm flow needs a large fixture
    # (attributes, documents, decisions) that adds nothing to this specific
    # assertion.
    source = inspect.getsource(confidence_policy.confirm_booking_review_v2_confidence_policy)
    assert "background_tasks: BackgroundTasks" in source
    assert "background_tasks.add_task(" in source
    assert "schedule_booking_checkpoint_rules" in source


def test_booking_submit_schedules_checkpoint_rules_with_raise_new() -> None:
    # The third real caller (docstring on schedule_booking_checkpoint_rules
    # says "three places") -- Submit is PC declaring Booking complete, the
    # same genuine-gap-check moment as Review Confirm, not the async
    # trigger's self-heal-only pass.
    source = inspect.getsource(
        uc03_post_extraction_materialization.close_booking_ready_with_lazy_v2_sync
    )
    assert "schedule_booking_checkpoint_rules" in source
    assert "raise_new=True" in source


def test_async_document_sync_schedules_checkpoint_rules_for_booking() -> None:
    # The primary trigger this fix adds: every Booking document confirming
    # through the DI webhook's background sync evaluates checkpoint rules on
    # its own, so the PC never has to remember to click Confirm for rule
    # findings to surface.
    source = inspect.getsource(confidence_policy._run_sync_booking_document_task)
    assert 'stage_code == "BOOKING"' in source
    assert "schedule_booking_checkpoint_rules(" in source
    # raise_new=False: never raise off a partial Booking -- the genuine gap
    # check runs at Review Confirm instead. See
    # test_async_and_confirm_get_independent_tasks_for_the_same_version for
    # the behavioral proof; this locks in that the wiring stays in place.
    assert "raise_new=False" in source


def test_checkpoint_rule_self_heals_once_evidence_lands() -> None:
    """Regression: _run_booking_rules only ever called _machine_flag for
    rules that fired this pass -- nothing resolved a rule that fired on an
    earlier pass and no longer applies. A finding raised while a required
    document was missing stayed OPEN forever even after the PC uploaded and
    the document was linked, confirmed directly against a live journey where
    the PC had reviewed everything and 3 flags still would not clear."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the checkpoint-rule scheduling test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-checkpoint-heal-{suffix}"
    journey_id = _seed_booking_journey(engine, tenant_id=tenant_id, suffix=suffix, version_no=1)

    with engine.begin() as connection:
        requirement_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, requirement_key, document_type_key,
                    process_area, requirement_level
                ) VALUES (
                    :tenant_id, :journey_id, 'booking_docket', 'booking_form',
                    'BOOKING', 'REQUIRED'
                ) RETURNING journey_document_requirement_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()

    # Pass 1: PC confirms with no evidence linked yet -- the genuine gap
    # check (raise_new=True, PC Review Confirm's own trigger) raises
    # BK_DOCKET_PRESENT. (Not ASYNC_DOCUMENT_SYNC here -- that trigger is
    # raise_new=False by design and must never raise on its own; see
    # test_async_and_confirm_get_independent_tasks_for_the_same_version.)
    schedule_booking_checkpoint_rules(
        engine, tenant_id=tenant_id, journey_id=journey_id,
        correlation_id="pass-1", trigger="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
    )
    with engine.begin() as connection:
        status = connection.execute(
            text(
                "SELECT finding_status FROM auditcore.audit_findings "
                "WHERE tenant_id=:t AND journey_id=:j AND rule_key='BK_DOCKET_PRESENT'"
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
        assert status == "OPEN"

        # The Booking Form is uploaded and linked; a new document sync bumps
        # the aggregate version, same as the real async pipeline.
        customer_id = connection.execute(
            text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                    document_type_key, evidence_purpose, journey_document_requirement_id
                ) VALUES (
                    :t, :j, :cu, :subject, :doc, 'booking_form', 'BOOKING', :req_id
                )
                """
            ),
            {
                "t": tenant_id, "j": journey_id, "cu": customer_id,
                "subject": uuid4(), "doc": uuid4(), "req_id": requirement_id,
            },
        )
        connection.execute(
            text(
                "UPDATE auditcore.journey_stage_states SET version_no=version_no+1 "
                "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"
            ),
            {"t": tenant_id, "j": journey_id},
        )

    # Pass 2: evidence now exists -- even the async trigger's own
    # raise_new=False must still self-heal (never raise, but always
    # resolve), matching what production actually passes here.
    schedule_booking_checkpoint_rules(
        engine, tenant_id=tenant_id, journey_id=journey_id,
        correlation_id="pass-2", trigger="ASYNC_DOCUMENT_SYNC", raise_new=False,
    )
    with engine.begin() as connection:
        status = connection.execute(
            text(
                "SELECT finding_status FROM auditcore.audit_findings "
                "WHERE tenant_id=:t AND journey_id=:j AND rule_key='BK_DOCKET_PRESENT'"
            ),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
        assert status == "RESOLVED"

    engine.dispose()


def test_losing_the_claim_race_is_handled_as_an_expected_outcome_not_a_failure() -> None:
    """Regression: schedule_booking_checkpoint_rules is called from two
    independent triggers by design (the async document-sync path and PC
    Review Confirm's safety net) for the same task. claim_worker_task's
    UPDATE is atomic -- READY, WHERE-guarded -- so exactly one of two
    genuinely concurrent callers wins the claim; the loser's UPDATE matches
    zero rows and claim_worker_task raises AuditCoreError. Before this fix
    that fell through to the function's broad `except Exception`, logged at
    error level with a full traceback as uc03_booking_rule_evaluation_failed
    -- for what is actually an expected, routine outcome of this module's
    own two-trigger design, not a defect.

    The true TOCTOU window (both callers reading READY before either commits
    its claim) needs real concurrent transactions to force deterministically
    -- this file's own precedent for exactly this class of trigger behavior
    (test_confirm_handler_schedules_checkpoint_rules_as_a_safety_net,
    test_async_document_sync_schedules_checkpoint_rules_for_booking) is
    source inspection rather than an end-to-end race, so this follows suit:
    assert the specific claim_worker_task call is guarded by its own
    AuditCoreError handler, ahead of (not inside) the broad except Exception.
    """
    source = inspect.getsource(run_booking_review_rule_task)
    claim_call_index = source.index("claim_worker_task(")
    guard_index = source.index("except AuditCoreError:")
    broad_except_index = source.index("except Exception:")

    assert claim_call_index < guard_index < broad_except_index
    assert '"uc03_booking_rule_task_already_claimed"' in source
    assert "return" in source[guard_index:broad_except_index]
