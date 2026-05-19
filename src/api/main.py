"""
src/api/main.py
────────────────────────────────────────────────────────────────
FastAPI application — entry point for the REST API.

This module:
  1. Creates and configures the FastAPI app instance.
  2. Registers all route routers under /api/v1/.
  3. Adds CORS middleware for dashboard and external access.
  4. Defines the /health endpoint.
  5. Runs startup/shutdown lifecycle events.

Running locally:
    uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000

The Swagger UI is available at: http://localhost:8000/docs
The ReDoc UI is available at:   http://localhost:8000/redoc
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.routes import surveillance, geospatial, analytics
from src.api.schemas import HealthCheck
from src.db.connection import verify_connection, dispose_engine
from src.utils.config import settings
from src.utils.logger import get_logger, set_log_level

logger = get_logger(__name__)


# ── Application metadata ─────────────────────────────────────────
# These values populate the Swagger UI at /docs.

_APP_TITLE       = "Nigeria Disease Surveillance API"
_APP_DESCRIPTION = """
## Nigeria Disease Surveillance Dashboard — REST API

Provides access to disease surveillance data for Nigeria covering
**Cholera**, **Lassa Fever**, **Mpox**, **Meningitis**, and **Yellow Fever**
across all 36 states and the FCT (2015–present).

### Data sources
- **NCDC** Nigeria — weekly Situation Reports
- **WHO AFRO** — cross-validation data
- **NASA POWER** — monthly rainfall per state
- **HDX** — health facility locations

### Authentication
Most `GET` endpoints are **public** — no API key required.
Admin/trigger endpoints require an `X-API-Key` header.

### Response format
All endpoints return JSON by default.
Surveillance endpoints support `?format=csv` for direct download.

Built by **Ayodeji** — HealthTech Data Scientist.
"""

_APP_VERSION     = "1.0.0"
_CONTACT         = {
    "name":  "Ayodeji",
    "url":   "https://github.com/ayodeji/nigeria-disease-surveillance",
}
_LICENSE         = {
    "name": "MIT",
    "url":  "https://opensource.org/licenses/MIT",
}


# ── Lifespan (startup / shutdown) ────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handle application startup and shutdown.

    FastAPI calls the code before `yield` on startup and the code
    after `yield` on shutdown. This replaces the deprecated
    @app.on_event("startup") pattern.
    """
    # ── Startup ──────────────────────────────────────────────────
    set_log_level(settings.log_level)
    logger.info("Starting Nigeria Disease Surveillance API v%s", _APP_VERSION)
    logger.info("Environment: %s", settings.app_env)

    # Verify database connectivity on startup — fail fast if broken
    if not verify_connection():
        logger.error(
            "Database connectivity check failed at startup. "
            "Check DATABASE_URL in .env and ensure the DB is running."
        )
        # We don't raise here — the app still starts, but individual
        # requests will fail with appropriate 503 errors.

    logger.info("API ready — docs at /docs")

    yield   # Application runs here

    # ── Shutdown ─────────────────────────────────────────────────
    logger.info("Shutting down API — disposing database connections")
    dispose_engine()


# ── FastAPI app instance ─────────────────────────────────────────

app = FastAPI(
    title         = _APP_TITLE,
    description   = _APP_DESCRIPTION,
    version       = _APP_VERSION,
    contact       = _CONTACT,
    license_info  = _LICENSE,
    docs_url      = "/docs",
    redoc_url     = "/redoc",
    lifespan      = lifespan,
)


# ── CORS middleware ──────────────────────────────────────────────
# Allow the Streamlit dashboard (and any other web client) to call
# the API from a browser without CORS errors.
#
# In production, replace allow_origins=["*"] with the actual
# dashboard URL (e.g. "https://your-app.streamlit.app") for security.

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"] if settings.is_development else [
        "https://*.streamlit.app",
        "http://localhost:8501",
    ],
    allow_credentials = False,      # Cannot be True when allow_origins=["*"]
    allow_methods     = ["GET"],    # This API is read-only for public endpoints
    allow_headers     = ["*"],
)


# ── Health check ─────────────────────────────────────────────────

@app.get(
    "/health",
    response_model=HealthCheck,
    tags=["System"],
    summary="API health check",
    description="Returns the API status. Used by Docker and UptimeRobot monitoring.",
)
def health_check() -> HealthCheck:
    """
    Confirm the API is running and responsive.

    Does not check database connectivity — that is intentional.
    A database outage should not cause the health endpoint to fail,
    because load balancers may stop routing to a healthy API instance
    just because the DB is momentarily unreachable.
    """
    return HealthCheck(
        status      = "healthy",
        version     = _APP_VERSION,
        environment = settings.app_env,
    )


@app.get(
    "/health/db",
    tags=["System"],
    summary="Database connectivity check",
    description="Verifies the API can reach the database. Returns 503 if not.",
)
def health_check_db() -> JSONResponse:
    """
    Check database connectivity explicitly.

    Returns 200 if the database is reachable, 503 otherwise.
    Useful for debugging deployment issues.
    """
    db_ok = verify_connection()
    if db_ok:
        return JSONResponse(
            status_code = 200,
            content     = {"status": "healthy", "database": "connected"},
        )
    return JSONResponse(
        status_code = 503,
        content     = {"status": "degraded", "database": "unreachable"},
    )


# ── Route registration ───────────────────────────────────────────
# All routes are prefixed with /api/v1 so future breaking changes
# can be introduced as /api/v2 without removing existing endpoints.

API_PREFIX = "/api/v1"

app.include_router(
    surveillance.router,
    prefix = API_PREFIX,
)
app.include_router(
    geospatial.router,
    prefix = API_PREFIX,
)
app.include_router(
    analytics.router,
    prefix = API_PREFIX,
)


# ── Root redirect ─────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root() -> JSONResponse:
    """
    Root endpoint — redirect browsers to Swagger docs.
    Not included in the OpenAPI schema to keep docs clean.
    """
    return JSONResponse(
        content = {
            "message": "Nigeria Disease Surveillance API",
            "docs":    "/docs",
            "health":  "/health",
            "version": _APP_VERSION,
        }
    )
