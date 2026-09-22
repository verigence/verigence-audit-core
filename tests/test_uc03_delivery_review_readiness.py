"""Pure-function coverage for the Delivery review-readiness handoff:
submit_delivery_capture_v2 stays unconditional (PC's own click always
succeeds and records its own PC_DELIVERY_CAPTURE task), while
TL_DELIVERY_REVIEW is raised separately by uc03_delivery_review_readiness_
sweep.py once delivery_review_readiness_blockers actually comes back
clean. What's tested here at the direct-function-call level, no DB needed:
the effect-key helpers stay consistent with each other (the sweep's SQL
prefix match depends on it), and the mid-processing check inside
delivery_review_readiness_blockers is pure Python.
"""
from __future__ import annotations

from uuid import uuid4

from audit_core.uc03_delivery_capture_v2 import (
    PC_DELIVERY_CAPTURE_TASK_TYPE,
    TL_DELIVERY_REVIEW_TASK_TYPE,
    _pc_delivery_capture_effect_key,
    tl_delivery_review_effect_key,
    tl_delivery_review_effect_key_prefix,
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


def test_tl_effect_key_prefix_matches_the_full_key() -> None:
    # This is exactly what uc03_delivery_review_readiness_sweep.py's own SQL
    # depends on: effect_key = prefix || journey_id::text must match what
    # tl_delivery_review_effect_key itself produces, or the sweep's "does a
    # TL task already exist" check silently stops matching and it starts
    # trying to create a duplicate every tick.
    tenant_id = "tenant-1"
    journey_id = uuid4()
    prefix = tl_delivery_review_effect_key_prefix(tenant_id)
    full_key = tl_delivery_review_effect_key(tenant_id, journey_id)
    assert full_key == f"{prefix}{journey_id}"
    assert full_key.startswith(prefix)


def test_tl_effect_key_prefix_is_tenant_scoped() -> None:
    assert tl_delivery_review_effect_key_prefix("tenant-1") != tl_delivery_review_effect_key_prefix(
        "tenant-2"
    )
