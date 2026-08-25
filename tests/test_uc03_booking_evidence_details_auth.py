from fastapi.routing import APIRoute

from audit_core.dependencies import get_human_principal, get_principal
from audit_core.uc03_booking_evidence_details import router


def _route(method: str, suffix: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and method in route.methods
            and route.path.endswith(suffix)
        ):
            return route
    raise AssertionError(f"Missing {method} route ending with {suffix}")


def _dependency_calls(route: APIRoute) -> set[object]:
    return {dependency.call for dependency in route.dependant.dependencies}


def test_uc03_booking_evidence_refresh_uses_human_auth() -> None:
    route = _route("POST", "/{evidence_id}/refresh")
    calls = _dependency_calls(route)
    assert get_human_principal in calls
    assert get_principal not in calls


def test_uc03_booking_evidence_facts_uses_human_auth() -> None:
    route = _route("GET", "/{evidence_id}/facts")
    calls = _dependency_calls(route)
    assert get_human_principal in calls
    assert get_principal not in calls
