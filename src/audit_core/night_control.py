"""Night control enforcement (product owner, 6 Nov 2026).

From 20:00 to 08:30 IST nobody may open Journey 360 or download any Verigence
file unless SuperAdmin gave them access; SuperAdmin and Executive are never
blocked. Security owns the policy (window, exemptions, grants) and answers per
USER and feature; Audit Core enforces that answer on its own human requests.

One mechanism covers every route: an application-wide dependency classifies the
matched route's path template against ``NIGHT_CONTROLLED_ROUTES`` and, only for
a covered route, asks Security. A new feature is one more row in that table.
Routes are matched by template, not by the raw URL, so a list such as
``/journeys/search`` can never be mistaken for one Journey.

Only human requests are checked: a request whose bearer token is not a valid
Security human USER token (worker, ServiceIntegration, missing or bad token) is
left to the route's own authentication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import anyio.to_thread
import structlog
from fastapi import Request

from audit_core.dependencies import _token_validator
from audit_core.errors import NightModeError
from audit_core.security_authorization import (
    NightControlDecision,
    get_security_authorization_client,
)

logger = structlog.get_logger(__name__)

JOURNEY_360 = "JOURNEY_360"
FILE_DOWNLOAD = "FILE_DOWNLOAD"


@dataclass(frozen=True)
class NightControlledRoutes:
    """Routes whose path template matches ``pattern`` belong to ``feature_key``."""

    feature_key: str
    pattern: re.Pattern[str]
    covers: str


# The feature mapping. A route may belong to several features (a Journey's
# document content is both Journey 360 and a file download); it is refused when
# any of them is closed to the caller.
NIGHT_CONTROLLED_ROUTES: tuple[NightControlledRoutes, ...] = (
    NightControlledRoutes(
        JOURNEY_360,
        # /journeys/{journey_id} followed by the end, "/" or a ":action", and any
        # other route keyed by a journey_id (the TL /cases/{journey_id} views).
        re.compile(r"/journeys/\{[^/}]+\}(?:[/:]|$)|\{journey_id(?::[^}]*)?\}"),
        "Every request that addresses one Journey: Journey 360 and everything inside it.",
    ),
    NightControlledRoutes(
        FILE_DOWNLOAD,
        re.compile(
            r"/(?:content|file|image-url|open|screenshot|template|error-report"
            r"|validation-report|workbook)$"
        ),
        "File bytes or a signed link to one file: documents, pages, uploads, exports, "
        "gate pass images, master documents, templates and import reports.",
    ),
    NightControlledRoutes(
        FILE_DOWNLOAD,
        re.compile(r"/vehicle-photos(?::finalize)?$"),
        "Vehicle photo lists and uploads answer with signed photo links.",
    ),
    NightControlledRoutes(
        FILE_DOWNLOAD,
        re.compile(r"/reports/(?:daily-processing|failed-pages)$|/di-dashboard$"),
        "Reports that carry signed links to the uploaded files and failed pages.",
    ),
)


@lru_cache(maxsize=1024)
def night_controlled_features(path_template: str) -> tuple[str, ...]:
    """The night-controlled features of a route, by its path template."""
    features: list[str] = []
    for rule in NIGHT_CONTROLLED_ROUTES:
        if rule.feature_key not in features and rule.pattern.search(path_template):
            features.append(rule.feature_key)
    return tuple(features)


class NightControlClient(Protocol):
    def check_night_control(self, *, user_id: str, feature_key: str) -> NightControlDecision: ...


def get_night_control_client() -> NightControlClient | None:
    """Security's night-control client: the shared Security authorization client,
    reusing its ServiceIntegration token. None when Security is not configured."""
    try:
        return get_security_authorization_client()
    except RuntimeError:
        logger.warning("night_control_check_failed", reason="security_not_configured", outcome="allowed")
        return None


def night_control_user_id(request: Request) -> str | None:
    """The human USER behind the request, or None when it is not a human request."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        return _token_validator().validate_human(token).subject
    except Exception:  # noqa: BLE001 - not a valid human token: the route authenticates it
        return None


def _enforce(request: Request, features: tuple[str, ...]) -> None:
    # Resolved here rather than as sub-dependencies so that routes outside the
    # table pay nothing; app.dependency_overrides is honoured like Depends would.
    overrides = request.app.dependency_overrides
    user_id = overrides.get(night_control_user_id, night_control_user_id)(request)
    if not user_id:
        return
    client = overrides.get(get_night_control_client, get_night_control_client)()
    if client is None:
        return
    for feature_key in features:
        try:
            decision = client.check_night_control(user_id=user_id, feature_key=feature_key)
        except Exception as exc:  # noqa: BLE001 - Security unavailable: allow (see module doc)
            logger.warning(
                "night_control_check_failed",
                reason=type(exc).__name__,
                feature_key=feature_key,
                outcome="allowed",
            )
            continue
        if decision.blocked:
            raise NightModeError(
                feature_key=feature_key,
                message=decision.message,
                starts_at=decision.starts_at,
                ends_at=decision.ends_at,
            )


async def enforce_night_control(request: Request) -> None:
    """Application-wide dependency: refuse a night-controlled route with NIGHT_MODE."""
    route = request.scope.get("route")
    path_template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not isinstance(path_template, str):
        return
    features = night_controlled_features(path_template)
    if not features:
        return
    await anyio.to_thread.run_sync(_enforce, request, features)
