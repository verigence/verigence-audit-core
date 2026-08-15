from fastapi import FastAPI

from audit_core.config import load_settings
from audit_core.errors import install_error_handlers

settings = load_settings()

app = FastAPI(
    title=settings.service_name,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
install_error_handlers(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
