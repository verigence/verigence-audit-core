from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.errors import BusinessValidationError

_PROJECT_WIDE_ROLES = frozenset({"TL", "PM", "CRM", "Executive"})


def install_role_mapping_policy(role_mappings: Any) -> None:
    """Install the corrected UC02 business-scope policy on the Role Mapping router.

    UC02 owner correction (21-Aug-2026):
    - TL and CRM are Project-wide, not Dealer-scoped.
    - PC is assigned to exactly one ONSITE Dealer Outlet and may additionally
      cover one SATELLITE Dealer Outlet.
    """

    def mapping_from_rows(user_id: str, rows: list[Any]):
        roles = {str(row["business_role_code"]) for row in rows}
        if len(roles) != 1:
            raise RuntimeError("Active business assignments have inconsistent operating roles")
        role = next(iter(roles))
        if role not in {"PC", "TL", "PM", "CRM", "Executive"}:
            raise RuntimeError("Active business assignment has unsupported operating role")

        dealer_ids = sorted(
            {UUID(str(row["dealer_id"])) for row in rows if row["dealer_id"] is not None},
            key=str,
        )
        outlet_ids = sorted(
            {UUID(str(row["outlet_id"])) for row in rows if row["outlet_id"] is not None},
            key=str,
        )

        if role == "PC":
            if (
                not outlet_ids
                or len(outlet_ids) > 2
                or any(row["dealer_id"] is None for row in rows)
            ):
                raise RuntimeError("PC business assignment scope is inconsistent")
            response_outlets = outlet_ids
        else:
            if role not in _PROJECT_WIDE_ROLES or dealer_ids or outlet_ids:
                raise RuntimeError("Project-wide business assignment scope is inconsistent")
            response_outlets = []

        return role_mappings.RoleMappingResponse(
            userId=user_id,
            operatingRole=role,
            dealerIds=[],
            outletIds=response_outlets,
        )

    def resolve_scope(
        engine,
        *,
        tenant_id: str,
        body,
    ) -> tuple[list[UUID], list[UUID], list[tuple[UUID | None, UUID | None]]]:
        dealer_ids = sorted(set(body.dealerIds), key=str)
        outlet_ids = sorted(set(body.outletIds), key=str)

        if body.operatingRole == "PC":
            if dealer_ids:
                raise BusinessValidationError(
                    detail="PC Role Mapping derives Dealer from the selected Dealer Outlet; Dealer IDs must be empty."
                )
            if not 1 <= len(outlet_ids) <= 2:
                raise BusinessValidationError(
                    detail="PC Role Mapping requires one ONSITE Dealer Outlet and allows at most one additional SATELLITE Dealer Outlet."
                )
        elif body.operatingRole in _PROJECT_WIDE_ROLES:
            if dealer_ids or outlet_ids:
                raise BusinessValidationError(
                    detail=f"{body.operatingRole} Role Mapping is Project-wide and does not accept Dealer or Outlet IDs."
                )
        else:
            raise BusinessValidationError(detail="Unsupported operating role.")

        with role_mappings._runtime_transaction(engine) as connection:
            connection.execute(text(f"SET LOCAL ROLE {role_mappings._RUNTIME_ROLE}"))
            set_tenant_context(connection, tenant_id)
            role_mappings._require_project(connection, tenant_id)

            if body.operatingRole != "PC":
                return [], [], [(None, None)]

            scopes: list[tuple[UUID | None, UUID | None]] = []
            classifications: list[str] = []
            for outlet_id in outlet_ids:
                row = connection.execute(
                    text(
                        "SELECT dealer_id, outlet_classification "
                        "FROM auditcore.dealer_outlets "
                        "WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id"
                    ),
                    {"tenant_id": tenant_id, "outlet_id": outlet_id},
                ).mappings().one_or_none()
                if row is None:
                    raise BusinessValidationError(
                        detail="Each PC Dealer Outlet must belong to the requested Project."
                    )
                dealer_id = UUID(str(row["dealer_id"]))
                classification = str(row["outlet_classification"])
                scopes.append((dealer_id, outlet_id))
                classifications.append(classification)

            onsite_count = classifications.count("ONSITE")
            satellite_count = classifications.count("SATELLITE")
            if onsite_count != 1 or satellite_count > 1 or onsite_count + satellite_count != len(classifications):
                raise BusinessValidationError(
                    detail="A PC must map to exactly one ONSITE Dealer Outlet and may additionally map to one SATELLITE Dealer Outlet."
                )

            return [], outlet_ids, scopes

    role_mappings._mapping_from_rows = mapping_from_rows
    role_mappings._resolve_scope = resolve_scope
