from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.security_integration import SecurityOAuthClient

# confirm_delivery_review_v2, _lossless_delivery_fields, and
# install_uc03_delivery_review_confirm were removed here (uniform-confidence-
# policy pass): install_uc03_review_effective_values() ran after this
# module's own install call in the app-startup cascade, so its
# _replace_confirm_route() always discarded this route in favor of
# confirm_delivery_review_v2_effective_values -- this handler, its
# lossless-copy helper, and its installer had zero live traffic despite each
# having its own passing test (route-registration correctness in this
# codebase cannot be verified by test count alone, the same lesson learned
# fixing Booking's confirm chain earlier this session). The live handler,
# now decorated directly on this same path, lives in
# uc03_review_effective_values.py and additionally carries the low-confidence
# blocking gate this module never had.
#
# DeliveryReviewV2ConfirmResponse (the response model) and
# _delivery_review_documents (the shared document-loading helper) are kept:
# both are still imported directly by the live handler.


class DeliveryReviewV2ConfirmResponse(BaseModel):
    journeyId: UUID
    pcVerificationStatus: Literal["VERIFIED"] = "VERIFIED"
    aggregateVersion: int
    storedFieldCount: int


def _delivery_review_documents(
    *,
    connection: Connection,
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    security_client: SecurityOAuthClient,
    di_client: review_v2.DiClient,
    v2_client: review_v2.DiCaptureV2Client,
) -> list[review_v2.ReviewV2Document]:
    context_ref, token = review_v2._ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    return review_v2._all_review_documents(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        token=token,
        context_ref=context_ref,
        di_client=di_client,
        v2_client=v2_client,
        stages=("DELIVERY",),
    )
