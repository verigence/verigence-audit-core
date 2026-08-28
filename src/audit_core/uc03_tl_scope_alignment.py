from __future__ import annotations

from audit_core.uc03_tl_supervisory import _SCOPE_WHERE_SQL, _require_tl_case_scope

TL_SCOPE_WHERE_SQL = _SCOPE_WHERE_SQL
require_project_wide_tl_case_scope = _require_tl_case_scope


def install_tl_scope_alignment() -> None:
    """Compatibility no-op: TL scope is implemented directly in the router."""
