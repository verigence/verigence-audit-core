from dataclasses import dataclass

from audit_core.security import Principal


@dataclass(frozen=True)
class AuthorizationError(RuntimeError):
    error_code: str
    status_code: int
    title: str

    def __str__(self) -> str:
        return self.title


def require_tenant(principal: Principal, tenant_id: str) -> None:
    if principal.tenant_id != tenant_id:
        raise AuthorizationError(
            error_code="VAC-AUTH-003",
            status_code=403,
            title="Tenant mismatch",
        )


def require_permission(principal: Principal, permission: str) -> None:
    if permission not in principal.permissions:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def authorize(principal: Principal, *, tenant_id: str, permission: str) -> None:
    require_tenant(principal, tenant_id)
    require_permission(principal, permission)
