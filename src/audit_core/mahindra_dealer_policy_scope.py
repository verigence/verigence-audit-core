from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import ValidationError

_DEALER_SCOPE = "DEALER"


def _dealer_by_code(connection: Connection, tenant_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT dealer_id, dealer_code, dealer_name
            FROM auditcore.dealers
            WHERE tenant_id = :tenant_id
            ORDER BY dealer_code
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()
    return {str(row["dealer_code"]): dict(row) for row in rows}


def install_dealer_policy_scope(mahindra_masters_module) -> None:
    """Extend the generic Mahindra Discount & Policy Master with Dealer scope.

    The persisted row references ``dealer_id``. ``dealer_code`` exists only in the
    workbook contract so administrators can maintain values without handling UUIDs.
    """

    policy_columns = list(mahindra_masters_module._POLICY_COLUMNS)
    if "dealer_code" not in policy_columns:
        policy_columns.insert(1, "dealer_code")
    mahindra_masters_module._POLICY_COLUMNS = tuple(policy_columns)
    mahindra_masters_module._POLICY_SCOPE_TYPES = frozenset(
        {*mahindra_masters_module._POLICY_SCOPE_TYPES, _DEALER_SCOPE}
    )

    def _validate_policy_rows(connection: Connection, tenant_id: str, rows):
        selected_by_code = {
            str(row["segment_code"]): row
            for row in mahindra_masters_module._selected_segments(connection, tenant_id)
        }
        dealers_by_code = _dealer_by_code(connection, tenant_id)
        validated: list[tuple[int, dict[str, Any], list[str]]] = []
        seen: set[tuple[str, str | None, str | None, str | None, str]] = set()

        for row_number, row in rows:
            messages: list[str] = []
            scope_type = (mahindra_masters_module._text_value(row, "scope_type") or "").upper()
            value_type = (mahindra_masters_module._text_value(row, "value_type") or "").upper()
            parameter_key = mahindra_masters_module._text_value(row, "parameter_key")
            dealer_code = mahindra_masters_module._text_value(row, "dealer_code")
            segment_code = mahindra_masters_module._text_value(row, "segment_code")
            scope_key = mahindra_masters_module._text_value(row, "scope_key")

            if scope_type not in mahindra_masters_module._POLICY_SCOPE_TYPES:
                messages.append(
                    "scope_type must be PROJECT, DEALER, SEGMENT, MODEL, TRIM or "
                    "CONFIGURATION."
                )
            if value_type not in mahindra_masters_module._POLICY_VALUE_TYPES:
                messages.append("value_type must be NUMBER, TEXT or BOOLEAN.")
            if not parameter_key:
                messages.append("parameter_key is required.")

            if scope_type == _DEALER_SCOPE:
                if not dealer_code:
                    messages.append("dealer_code is required for DEALER policy scope.")
                elif dealer_code not in dealers_by_code:
                    messages.append("dealer_code must identify a Dealer in this Project.")
                if segment_code:
                    messages.append("segment_code must be empty for DEALER policy scope.")
                if scope_key:
                    messages.append("scope_key must be empty for DEALER policy scope.")
            else:
                if dealer_code:
                    messages.append("dealer_code is allowed only for DEALER policy scope.")
                if scope_type != "PROJECT" and not segment_code:
                    messages.append("segment_code is required for non-PROJECT policy scope.")
                if segment_code and segment_code not in selected_by_code:
                    messages.append(
                        "segment_code must be one of the Segments selected for this Project."
                    )
                if scope_type in {"MODEL", "TRIM", "CONFIGURATION"} and not scope_key:
                    messages.append(
                        "scope_key is required for MODEL, TRIM and CONFIGURATION scope."
                    )

            value_number = mahindra_masters_module._decimal(row.get("value_number"))
            value_text = mahindra_masters_module._text_value(row, "value_text")
            if value_type == "NUMBER":
                if value_number is None:
                    messages.append("value_number is required and must be numeric for NUMBER parameters.")
                if value_text:
                    messages.append("value_text must be empty for NUMBER parameters.")
            elif value_type == "TEXT":
                if not value_text:
                    messages.append("value_text is required for TEXT parameters.")
                if value_number is not None:
                    messages.append("value_number must be empty for TEXT parameters.")
            elif value_type == "BOOLEAN":
                boolean_values = {"true", "false", "yes", "no", "1", "0"}
                if not value_text or value_text.lower() not in boolean_values:
                    messages.append("value_text must be true/false for BOOLEAN parameters.")
                else:
                    row["value_text"] = (
                        "true" if value_text.lower() in {"true", "yes", "1"} else "false"
                    )
                if value_number is not None:
                    messages.append("value_number must be empty for BOOLEAN parameters.")

            effective_to = mahindra_masters_module._date(row.get("effective_to"))
            if row.get("effective_to") not in (None, "") and effective_to is None:
                messages.append("effective_to must be YYYY-MM-DD.")
            if effective_to is not None:
                row["effective_to"] = effective_to.isoformat()

            row["scope_type"] = scope_type
            row["value_type"] = value_type
            if dealer_code:
                row["dealer_code"] = dealer_code
            if value_number is not None:
                row["value_number"] = str(value_number)

            if parameter_key:
                dedupe = (
                    scope_type,
                    dealer_code,
                    segment_code,
                    scope_key,
                    parameter_key.upper(),
                )
                if dedupe in seen:
                    messages.append("The same policy parameter/scope appears more than once in this upload.")
                else:
                    seen.add(dedupe)
                row["parameter_key"] = parameter_key.upper()
            validated.append((row_number, row, messages))
        return validated

    def _confirm_discount_policy(
        connection: Connection,
        *,
        tenant_id: str,
        import_row,
        actor_id: str,
    ):
        rows = mahindra_masters_module._staged_rows(
            connection,
            tenant_id,
            import_row["import_id"],
        )
        selected_by_code = {
            str(row["segment_code"]): row
            for row in mahindra_masters_module._selected_segments(connection, tenant_id)
        }
        dealers_by_code = _dealer_by_code(connection, tenant_id)
        version_no = int(
            connection.execute(
                text(
                    "SELECT COALESCE(MAX(version_no),0)+1 "
                    "FROM auditcore.discount_policy_versions "
                    "WHERE tenant_id=:tenant_id"
                ),
                {"tenant_id": tenant_id},
            ).scalar_one()
        )
        effective_to_values: set[date | None] = {
            mahindra_masters_module._date(row.get("effective_to"))
            for row in rows
            if row.get("effective_to") not in (None, "")
        }
        if len(effective_to_values) > 1:
            raise ValidationError(
                detail="One Discount & Policy version must use one Effective To date."
            )
        effective_to = next(iter(effective_to_values), None)
        version_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.discount_policy_versions (
                    tenant_id, version_no, effective_from, effective_to,
                    source_import_id, created_by_actor_id
                ) VALUES (
                    :tenant_id, :version_no, :effective_from, :effective_to,
                    :source_import_id, :actor_id
                ) RETURNING discount_policy_version_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_no": version_no,
                "effective_from": import_row["effective_from"],
                "effective_to": effective_to,
                "source_import_id": import_row["import_id"],
                "actor_id": actor_id,
            },
        ).scalar_one()

        for index, row in enumerate(rows, start=2):
            segment_code = mahindra_masters_module._text_value(row, "segment_code")
            segment_id = selected_by_code[segment_code]["segment_id"] if segment_code else None
            dealer_code = mahindra_masters_module._text_value(row, "dealer_code")
            dealer_id: UUID | None = None
            if str(row["scope_type"]).upper() == _DEALER_SCOPE:
                dealer = dealers_by_code.get(str(dealer_code))
                if dealer is None:
                    raise ValidationError(detail="Dealer policy row references an unknown Dealer.")
                dealer_id = dealer["dealer_id"]

            value_type = str(row["value_type"]).upper()
            value_number = (
                Decimal(str(row["value_number"])) if value_type == "NUMBER" else None
            )
            value_text = (
                str(row["value_text"]) if value_type in {"TEXT", "BOOLEAN"} else None
            )
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.discount_policy_parameters (
                        tenant_id, discount_policy_version_id, scope_type,
                        dealer_id, segment_id, scope_key, parameter_key, value_type,
                        value_number, value_text, unit, notes, source_import_row_no
                    ) VALUES (
                        :tenant_id, :version_id, :scope_type,
                        :dealer_id, :segment_id, :scope_key, :parameter_key, :value_type,
                        :value_number, :value_text, :unit, :notes, :row_no
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "version_id": version_id,
                    "scope_type": row["scope_type"],
                    "dealer_id": dealer_id,
                    "segment_id": segment_id,
                    "scope_key": mahindra_masters_module._text_value(row, "scope_key"),
                    "parameter_key": row["parameter_key"],
                    "value_type": value_type,
                    "value_number": value_number,
                    "value_text": value_text,
                    "unit": mahindra_masters_module._text_value(row, "unit"),
                    "notes": mahindra_masters_module._text_value(row, "notes"),
                    "row_no": index,
                },
            )
        return version_id

    mahindra_masters_module._validate_policy_rows = _validate_policy_rows
    mahindra_masters_module._confirm_discount_policy = _confirm_discount_policy
