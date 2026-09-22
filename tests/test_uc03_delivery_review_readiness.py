"""Pure-function coverage for the Delivery review-readiness handoff:
submit_delivery_capture_v2 stays unconditional (PC's own click always
succeeds and records its own completed PC_DELIVERY_CAPTURE task).
TL_DELIVERY_REVIEW is raised by raise_tl_delivery_review_if_ready, called
from the exact same per-document sync chain every other Delivery self-heal
check already runs from (_run_delivery_checkpoint_once, off the DI
document-link webhook) -- not a separate poll -- plus once directly from
Submit itself, since submitting is the event that first makes readiness
meaningful (capture_completed_at_utc goes from null to set right then).

raise_tl_delivery_review_if_ready and delivery_review_readiness_blockers
both need a live connection (they query journey_stage_states,
audit_findings, workflow_tasks, evidence), so they aren't covered here --
what's tested at the direct-function-call level, no DB needed, is that the
task-type/effect-key building blocks stay consistent with each other.
"""
from __future__ import annotations

from uuid import uuid4

from audit_core.uc03_delivery_capture_v2 import (
    PC_DELIVERY_CAPTURE_TASK_TYPE,
    TL_DELIVERY_REVIEW_TASK_TYPE,
    _pc_delivery_capture_effect_key,
    tl_delivery_review_effect_key,
)


def test_task_types_are_distinct_and_named_for_their_own_role() -> None:
    assert PC_DELIVERY_CAPTURE_TASK_TYPE == "PC_DELIVERY_CAPTURE"
    assert TL_DELIVERY_REVIEW_TASK_TYPE == "TL_DELIVERY_REVIEW"
    assert PC_DELIVERY_CAPTURE_TASK_TYPE != TL_DELIVERY_REVIEW_TASK_TYPE


def test_pc_and_tl_effect_keys_are_distinct_per_journey() -> None:
    tenant_id = "tenant-1"
    journey_id = uuid4()
    assert _pc_delivery_capture_effect_key(tenant_id, journey_id) != tl_delivery_review_effect_key(
        tenant_id, journey_id
    )


def test_tl_effect_key_is_tenant_and_journey_scoped() -> None:
    journey_id = uuid4()
    assert tl_delivery_review_effect_key("tenant-1", journey_id) != tl_delivery_review_effect_key(
        "tenant-2", journey_id
    )
    assert tl_delivery_review_effect_key("tenant-1", journey_id) != tl_delivery_review_effect_key(
        "tenant-1", uuid4()
    )
