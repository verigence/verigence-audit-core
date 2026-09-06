"""Keep every accepted Booking Review DI field in an Audit Core owner.

UC03 Review previously regressed to treating the lossless
``journey_document_extracted_fields`` copy as optional provenance.  For Journey
Details it is also the durable Audit Core owner for reviewed DI fields whose richer
typed business owner is not yet defined.  Proven Booking/PAN/Aadhaar/receipt fields
still materialize into their existing typed owners; every other accepted reviewed
field falls back to the reviewed-field owner instead of being rejected or lost.

``booking_docket`` continues to use the same typed Booking business owner as
``booking_form``. Both document types are alternate sales-contract evidence for
the same UC03 Booking business attributes.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import text

from audit_core import uc03_attribute_mapping as attribute_mapping
from audit_core import uc03_booking_review_decisions as decisions
from audit_core import uc03_v2_review_materialization as materialization
from audit_core.uc03_di_core_persistence import ReviewedDiField, has_persistable_value

_BOOKING_DOCKET_DOCUMENT_TYPE = "booking_docket"
_DOCKET_ONLY_FIELDS = (
    "deal_type",
    "out_of_scope_reasons",
    "dsa_commission_amount",
)

_installed = False


def _install_docket_attribute_specs() -> None:
    additions = (
        attribute_mapping.AttributeSpec(
            attribute_key="booking_deal_type",
            excel_field_no=None,
            label="Deal Type",
            stages=("BOOKING",),
            field_keys=frozenset({"deal_type"}),
            source_priority=("booking_docket",),
            operational_field="DEAL_TYPE",
        ),
        attribute_mapping.AttributeSpec(
            attribute_key="booking_out_of_scope_reasons",
            excel_field_no=None,
            label="Out Of Scope Reasons",
            stages=("BOOKING",),
            field_keys=frozenset({"out_of_scope_reasons"}),
            source_priority=("booking_docket",),
        ),
        attribute_mapping.AttributeSpec(
            attribute_key="booking_dsa_commission_amount",
            excel_field_no=None,
            label="DSA Commission Amount",
            stages=("BOOKING",),
            field_keys=frozenset({"dsa_commission_amount"}),
            source_priority=("booking_docket",),
        ),
    )
    existing_fields = set(attribute_mapping._FIELD_INDEX)
    selected = tuple(
        spec
        for spec in additions
        if not any(field.casefold() in existing_fields for field in spec.field_keys)
    )
    if not selected:
        return
    attribute_mapping.ATTRIBUTE_SPECS = (*attribute_mapping.ATTRIBUTE_SPECS, *selected)
    attribute_mapping._FIELD_INDEX = {
        field_key.casefold(): spec
        for spec in attribute_mapping.ATTRIBUTE_SPECS
        for field_key in spec.field_keys
    }


def _install_docket_typed_owner() -> None:
    materialization._BOOKING_FORM_FIELDS = tuple(
        dict.fromkeys((*materialization._BOOKING_FORM_FIELDS, *_DOCKET_ONLY_FIELDS))
    )
    materialization._BOOKING_DECIMAL_FIELDS = {
        *materialization._BOOKING_DECIMAL_FIELDS,
        "dsa_commission_amount",
    }
    materialization._COMMERCIAL_LINE_FIELDS = {
        *materialization._COMMERCIAL_LINE_FIELDS,
        "dsa_commission_amount",
    }

    original_owner = materialization.reviewed_field_core_owner

    def reviewed_field_core_owner(
        *,
        document_type_key: str | None,
        field_key: str,
        document_id,
    ) -> tuple[str, str] | None:
        document_type = str(document_type_key or "").strip().lower()
        normalized_field = str(field_key).strip().lower()
        if (
            document_type == _BOOKING_DOCKET_DOCUMENT_TYPE
            and normalized_field in materialization._BOOKING_FORM_FIELDS
        ):
            return "BOOKING_FORM_REVIEW_VALUE", str(document_id)

        typed_owner = original_owner(
            document_type_key=document_type_key,
            field_key=field_key,
            document_id=document_id,
        )
        if typed_owner is not None:
            return typed_owner

        # Every accepted DI business field must survive Review Confirm in Audit
        # Core even when a richer domain projection has not been implemented yet.
        # journey_document_extracted_fields is the durable reviewed-field owner and
        # is what Journey Details reads for complete DI coverage.
        if document_type and normalized_field:
            return "REVIEWED_DI_FIELD", str(document_id)
        return None

    materialization.reviewed_field_core_owner = reviewed_field_core_owner  # type: ignore[assignment]
    # Booking Review imported the helper directly, so update that live binding too.
    decisions.reviewed_field_core_owner = reviewed_field_core_owner  # type: ignore[assignment]

    original_materialize = materialization.materialize_reviewed_booking_form_values

    def materialize_reviewed_booking_values(
        connection,
        *,
        tenant_id: str,
        journey_id,
        documents: list[Any],
        rejected_review_keys: set[str],
        actor_id: str,
    ) -> dict[str, int]:
        normalized_documents: list[Any] = []
        docket_document_ids: list[str] = []
        for document in documents:
            document_type = str(document.documentTypeKey or "").strip().lower()
            if document_type == _BOOKING_DOCKET_DOCUMENT_TYPE:
                docket_document_ids.append(str(document.documentId))
                normalized_documents.append(
                    document.model_copy(update={"documentTypeKey": "booking_form"})
                )
            else:
                normalized_documents.append(document)

        result = original_materialize(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=normalized_documents,
            rejected_review_keys=rejected_review_keys,
            actor_id=actor_id,
        )

        # The legacy materializer labels commercial provenance as booking_form.
        # Restore the truthful source label for rows that actually came from Docket.
        for document_id in docket_document_ids:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.commercial_lines
                    SET source_reference=:docket_reference,
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id
                      AND journey_id=:journey_id
                      AND source_reference=:form_reference
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "form_reference": f"booking_form:{document_id}",
                    "docket_reference": f"booking_docket:{document_id}",
                },
            )
        return result

    materialization.materialize_reviewed_booking_form_values = materialize_reviewed_booking_values  # type: ignore[assignment]
    # materialize_reviewed_di_business_values resolves the module global at runtime,
    # so no second binding needs to be patched.


def _install_owner_guard() -> None:
    original_persist: Callable[..., int] = decisions.persist_reviewed_di_fields

    def persist_reviewed_di_fields_with_owner_guard(
        connection,
        *,
        tenant_id: str,
        journey_id,
        stage_code,
        actor_id: str,
        fields: list[ReviewedDiField],
    ) -> int:
        if stage_code == "BOOKING":
            for field in fields:
                # Rejected fields intentionally have no effective accepted value and
                # are retained as immutable DI extraction history; they need no
                # accepted-value owner.
                if not field.effective_value_is_set or not has_persistable_value(
                    field.effective_value
                ):
                    continue
                owner = materialization.reviewed_field_core_owner(
                    document_type_key=field.source_document_type_key,
                    field_key=field.field_key,
                    document_id=field.document_id,
                )
                if owner is None:
                    spec = attribute_mapping.spec_for_field(field.field_key)
                    raise decisions._missing_core_owner_error(
                        field_key=field.field_key,
                        document_type_key=field.source_document_type_key,
                        attribute_key=spec.attribute_key if spec is not None else None,
                    )

        return original_persist(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            actor_id=actor_id,
            fields=fields,
        )

    decisions.persist_reviewed_di_fields = persist_reviewed_di_fields_with_owner_guard  # type: ignore[assignment]


def install_uc03_strict_review_core_ownership() -> None:
    """Install typed owners plus the lossless reviewed-DI fallback owner."""

    global _installed
    if _installed:
        return
    _install_docket_attribute_specs()
    _install_docket_typed_owner()
    _install_owner_guard()
    _installed = True
