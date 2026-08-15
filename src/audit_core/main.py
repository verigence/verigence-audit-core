from fastapi import FastAPI

from audit_core.config import load_settings
from audit_core.errors import install_error_handlers
from audit_core.observability import install_observability

settings = load_settings()

app = FastAPI(
    title=settings.service_name,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
install_error_handlers(app)
install_observability(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
