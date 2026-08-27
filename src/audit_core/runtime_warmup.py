from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable

import structlog
from sqlalchemy import text

from audit_core.dependencies import _token_validator, get_engine
from audit_core.security_authorization import get_security_authorization_client

logger = structlog.get_logger(__name__)


def _warm_database() -> None:
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1")).scalar_one()


def _warm_jwks() -> None:
    _token_validator().warm()


def _warm_security_service_token() -> None:
    get_security_authorization_client().warm_service_token()


def warm_runtime_dependencies() -> None:
    """Best-effort warm of cold dependencies before the first user request.

    The warmers run concurrently so startup pays roughly the slowest dependency once,
    rather than serially. Failures are logged but never prevent Audit Core from
    starting; request-time validation and authorization remain authoritative.
    """

    warmers: dict[str, Callable[[], None]] = {
        "database": _warm_database,
        "jwks": _warm_jwks,
        "security_service_token": _warm_security_service_token,
    }

    with ThreadPoolExecutor(max_workers=len(warmers), thread_name_prefix="audit-warm") as executor:
        futures = {executor.submit(warmer): name for name, warmer in warmers.items()}
        for future in as_completed(futures):
            dependency = futures[future]
            try:
                future.result()
            except Exception as exc:  # best-effort startup optimization
                logger.warning(
                    "runtime_dependency_warmup_failed",
                    dependency=dependency,
                    exception_type=type(exc).__name__,
                )
            else:
                logger.info("runtime_dependency_warmup_complete", dependency=dependency)
