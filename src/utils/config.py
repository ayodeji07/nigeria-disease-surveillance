"""
src/utils/config.py
────────────────────────────────────────────────────────────────
Central configuration module.

All environment variables, file paths, and project constants
live here. No other module should import `os.environ` directly
or hardcode paths — they import from here instead.

This keeps the project backend-agnostic: to switch from
PostgreSQL to another database, change DATABASE_URL in .env
and nothing else needs updating.

Usage:
    from src.utils.config import settings, Paths, Diseases
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── Load .env file ────────────────────────────────────────────────
# dotenv silently does nothing if .env does not exist, which is
# correct behaviour in CI/CD and production where env vars come
# from the platform.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_FILE)


# ── Project root & paths ─────────────────────────────────────────

# Root is two levels up from this file: src/utils/config.py → root
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Paths:
    """All filesystem paths used across the project."""
    root: Path = PROJECT_ROOT
    data: Path = PROJECT_ROOT / "data"
    raw: Path = PROJECT_ROOT / "data" / "raw"
    processed: Path = PROJECT_ROOT / "data" / "processed"
    shapefiles: Path = PROJECT_ROOT / "data" / "shapefiles"
    sql: Path = PROJECT_ROOT / "sql"
    reports: Path = PROJECT_ROOT / "reports"
    logs: Path = PROJECT_ROOT / "logs"

    # Sub-folders for raw NCDC PDFs, one per disease
    ncdc_cholera:    Path = PROJECT_ROOT / "data" / "raw" / "ncdc_pdfs" / "cholera"
    ncdc_lassa:      Path = PROJECT_ROOT / "data" / "raw" / "ncdc_pdfs" / "lassa_fever"
    ncdc_mpox:       Path = PROJECT_ROOT / "data" / "raw" / "ncdc_pdfs" / "mpox"
    ncdc_meningitis: Path = PROJECT_ROOT / "data" / "raw" / "ncdc_pdfs" / "meningitis"
    ncdc_yellow:     Path = PROJECT_ROOT / "data" / "raw" / "ncdc_pdfs" / "yellow_fever"

    def ensure_all(self) -> None:
        """Create all directories if they do not already exist."""
        for attr_name in self.__dataclass_fields__:
            path: Path = getattr(self, attr_name)
            path.mkdir(parents=True, exist_ok=True)


# Singleton path registry
Paths = _Paths()


# ── Disease constants ─────────────────────────────────────────────

@dataclass(frozen=True)
class _Diseases:
    """
    Disease names exactly as they should appear in the database.

    Keeping names as typed constants (rather than bare strings)
    catches typos at import time.
    """
    CHOLERA:    str = "Cholera"
    LASSA:      str = "Lassa Fever"
    MPOX:       str = "Mpox"
    MENINGITIS: str = "Meningitis"
    YELLOW_FEVER: str = "Yellow Fever"

    @property
    def all(self) -> list[str]:
        """Return all disease names as a list."""
        return [
            self.CHOLERA,
            self.LASSA,
            self.MPOX,
            self.MENINGITIS,
            self.YELLOW_FEVER,
        ]

    # Maps the disease name to the folder containing its raw PDFs
    @property
    def pdf_folder_map(self) -> dict[str, str]:
        return {
            self.CHOLERA:     "cholera",
            self.LASSA:       "lassa_fever",
            self.MPOX:        "mpox",
            self.MENINGITIS:  "meningitis",
            self.YELLOW_FEVER: "yellow_fever",
        }


Diseases = _Diseases()


# ── Application settings (from environment) ───────────────────────

@dataclass
class _Settings:
    """
    Runtime settings resolved from environment variables.

    Attributes have sensible defaults so the application starts
    correctly in a minimal local environment.
    """
    # Database — the only thing that changes between environments
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:localdev@localhost:5432/nigeria_health",
        )
    )

    # API security — fail loudly in production if not set
    api_key: str = field(
        default_factory=lambda: os.environ.get("API_KEY", "dev-api-key")
    )

    # Application environment: "development" | "production"
    app_env: str = field(
        default_factory=lambda: os.environ.get("APP_ENV", "development")
    )

    # Log level: DEBUG | INFO | WARNING | ERROR
    log_level: str = field(
        default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO")
    )

    # Email notification (optional — used by GitHub Actions)
    notify_email: str = field(
        default_factory=lambda: os.environ.get("NOTIFY_EMAIL", "")
    )
    sendgrid_api_key: str = field(
        default_factory=lambda: os.environ.get("SENDGRID_API_KEY", "")
    )

    # Dashboard → API connection
    api_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "API_BASE_URL", "http://localhost:8000"
        )
    )

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() == "development"

    @property
    def notifications_enabled(self) -> bool:
        """True only when both email and SendGrid key are configured."""
        return bool(self.notify_email and self.sendgrid_api_key)

    def validate(self) -> None:
        """
        Raise a clear error if critical settings are missing.

        Call this at application startup so misconfiguration
        surfaces immediately rather than at the first DB query.
        """
        if not self.database_url:
            raise EnvironmentError(
                "DATABASE_URL is not set. "
                "Copy .env.example to .env and fill in your database URL."
            )
        if self.is_production and self.api_key == "dev-api-key":
            raise EnvironmentError(
                "API_KEY is still set to the development default. "
                "Set a strong secret key in production."
            )
        logger.info(
            "Settings validated — env=%s db=%s",
            self.app_env,
            # Log only the DB host, never the password
            self.database_url.split("@")[-1] if "@" in self.database_url else "configured",
        )


# Singleton settings object
settings = _Settings()


# ── Data collection constants ─────────────────────────────────────

# Earliest year of NCDC data we attempt to collect
DATA_START_YEAR: int = 2015

# Latest year (inclusive) — update annually
DATA_END_YEAR: int = 2024

# NASA POWER API — seconds to wait between state requests
# to stay within the rate limit (~30 req/min)
NASA_API_DELAY_SECONDS: float = 2.2

# Incidence rate denominator (standard in epidemiology)
INCIDENCE_PER_N: int = 100_000

# Consecutive missing weeks threshold:
#   <= SHORT_GAP_WEEKS  → forward-fill
#   >  SHORT_GAP_WEEKS  → linear interpolation
SHORT_GAP_WEEKS: int = 2
