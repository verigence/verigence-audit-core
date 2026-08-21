from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.errors import BusinessValidationError

_PROJECT_WIDE_ONLY_ROLES = frozenset({"TL", "PM", "Executive"})
_SUPPORTED_ROLES = frozenset({"PC", "TL", "PM", "CRM", "Executive"})


def install_role_mapping_policy(role_mappings: Any) -> None:
    """Install the owner-approved UC02 business-scope policy.

    Owner clarification (21-Aug-2026):
    - PC is Outlet-scoped with no artificial ONSITE/SATELLITE cardinality rule.
      A PC may be satellite-only and may cover multiple satellite locations.
    - CRM is intentionally flexible: Project-wide, Dealer-scoped, Outlet-scoped,
      or a union of selected Dealers and Outlets.
    - TL, PM and Executive remain Project-wide.
    """

    def mapping_from_rows(user_id: str, rows: list[Any]):
        roles = {str(row["business_role_code"]) for row in rows}
        if len(roles) != 1:
            raise RuntimeError("Active business assignments have inconsistent operating roles")
        role = next(iter(roles))
        if role not in _SUPPORTED_ROLES:
            raise RuntimeError("Active business assignment has unsupported operating role")

        outlet_ids = sorted(
            {UUID(str(row["outlet_id"])) for row in rows if row["outlet_id"] is not None},
            key=str,
        )
        direct_dealer_ids = sorted(
            {
                UUID(str(row["dealer_id"]))
                for row in rows
                if row["dealer_id"] is not None and row["outlet_id"] is None
            },
            key=str,
        )

        if role == "PC":
            if not outlet_ids or any(
                row["dealer_id"] is None or row["outlet_id"] is None for row in rows
            ):
                raise RuntimeError("PC business assignment scope is inconsistent")
            response_dealers: list[UUID] = []
            response_outlets = outlet_ids
        elif role in _PROJECT_WIDE_ONLY_ROLES:
            if any(
                row["dealer_id"] is not None or row["outlet_id"] is not None
                for row in rows
            ):
                raise RuntimeError("Project-wide business assignment scope is inconsistent")
            response_dealers = []
            response_outlets = []
        elif role == "CRM":
            project_wide = all(
                row["dealer_id"] is None and row["outlet_id"] is None for row in rows
            )
            scoped = all(row["dealer_id"] is not None for row in rows)
            if project_wide:
                response_dealers = []
                response_outlets = []
            elif scoped:
                response_dealers = direct_dealer_ids
                response_outlets = outlet_ids
            else:
                raise RuntimeError("CRM business assignment scope is inconsistent")
        else:  # pragma: no cover - guarded by _SUPPORTED_ROLES
            raise RuntimeError("Unsupported operating role")

        return role_mappings.RoleMappingResponse(
            userId=user_id,
            operatingRole=role,
            dealerIds=response_dealers,
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
                    detail="PC Role Mapping is Outlet-scoped; Dealer IDs must be empty."
                )
            if not outlet_ids:
                raise BusinessValidationError(
                    detail="PC Role Mapping requires at least one Dealer Outlet."
                )
        elif body.operatingRole in _PROJECT_WIDE_ONLY_ROLES:
            if dealer_ids or outlet_ids:
                raise BusinessValidationError(
                    detail=f"{body.operatingRole} Role Mapping is Project-wide and does not accept Dealer or Outlet IDs."
                )
        elif body.operatingRole != "CRM":
            raise BusinessValidationError(detail="Unsupported operating role.")

        with role_mappings._runtime_transaction(engine) as connection:
            connection.execute(text(f"SET LOCAL ROLE {role_mappings._RUNTIME_ROLE}"))
            set_tenant_context(connection, tenant_id)
            role_mappings._require_project(connection, tenant_id)

            if body.operatingRole in _PROJECT_WIDE_ONLY_ROLES:
                return [], [], [(None, None)]

            if body.operatingRole == "CRM" and not dealer_ids and not outlet_ids:
                return [], [], [(None, None)]

            if body.operatingRole == "CRM":
                for dealer_id in dealer_ids:
                    exists = connection.execute(
                        text(
                            "SELECT 1 FROM auditcore.dealers "
                            "WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id"
                        ),
                        {"tenant_id": tenant_id, "dealer_id": dealer_id},
                    ).scalar_one_or_none()
                    if exists is None:
                        raise BusinessValidationError(
                            detail="Each CRM Dealer scope must belong to the requested Project."
                        )

            outlet_scopes: list[tuple[UUID | None, UUID | None]] = []
            for outlet_id in outlet_ids:
                dealer_id = connection.execute(
                    text(
                        "SELECT dealer_id FROM auditcore.dealer_outlets "
                        "WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id"
                    ),
                    {"tenant_id": tenant_id, "outlet_id": outlet_id},
                ).scalar_one_or_none()
                if dealer_id is None:
                    raise BusinessValidationError(
                        detail="Each selected Dealer Outlet must belong to the requested Project."
                    )
                outlet_scopes.append((UUID(str(dealer_id)), outlet_id))

            if body.operatingRole == "PC":
                return [], outlet_ids, outlet_scopes

            scopes: list[tuple[UUID | None, UUID | None]] = [
                (dealer_id, None) for dealer_id in dealer_ids
            ]
            scopes.extend(outlet_scopes)
            return dealer_ids, outlet_ids, scopes

    role_mappings._mapping_from_rows = mapping_from_rows
    role_mappings._resolve_scope = resolve_scope
