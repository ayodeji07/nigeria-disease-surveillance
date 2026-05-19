"""
src/api/dependencies.py
────────────────────────────────────────────────────────────────
FastAPI dependency injection functions.

Dependencies are reusable components that FastAPI injects into
route handlers via Depends(). They handle cross-cutting concerns
like database sessions, pagination, and request validation so
route handlers stay focused on their business logic.

Usage in a route:
    @router.get("/surveillance")
    def get_data(
        db:    Session     = Depends(get_session),
        page:  Pagination  = Depends(get_pagination),
    ):
        ...
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generator

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from src.db.connection import get_db_session_dependency
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ── Database session ─────────────────────────────────────────────

def get_session() -> Generator[Session, None, None]:
    """
    Provide a database session for the duration of one HTTP request.

    The session is committed on success and rolled back on exception.
    FastAPI calls this as a generator dependency — it yields once,
    then the cleanup code after yield runs when the request completes.

    Yields
    ------
    Session
        An active SQLAlchemy database session.
    """
    yield from get_db_session_dependency()


# ── Pagination ───────────────────────────────────────────────────

@dataclass
class Pagination:
    """
    Standard pagination parameters.

    Injected into any endpoint that returns a list of items.
    Validated ranges prevent clients from requesting unreasonably
    large result sets.
    """
    limit:  int
    offset: int

    @property
    def page(self) -> int:
        """Current page number (1-indexed)."""
        return (self.offset // self.limit) + 1


def get_pagination(
    limit:  int = Query(default=100, ge=1, le=10_000,
                        description="Maximum number of records to return."),
    offset: int = Query(default=0,   ge=0,
                        description="Number of records to skip."),
) -> Pagination:
    """
    Validate and return pagination parameters from query string.

    Parameters
    ----------
    limit : int
        Maximum records in this response. Default: 100, max: 10,000.
    offset : int
        Records to skip (for cursor-style pagination). Default: 0.

    Returns
    -------
    Pagination
    """
    return Pagination(limit=limit, offset=offset)


# ── Disease filter ───────────────────────────────────────────────

def valid_disease(
    disease: str = Query(..., description="Disease name (case-sensitive)."),
) -> str:
    """
    Validate that the disease query parameter is a known disease.

    Raises HTTP 422 (handled by FastAPI) if the value is not
    in the known disease list.

    Parameters
    ----------
    disease : str

    Returns
    -------
    str
        The validated disease name.
    """
    from src.utils.config import Diseases
    from fastapi import HTTPException, status

    if disease not in Diseases.all:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown disease: '{disease}'. "
                f"Valid options: {Diseases.all}"
            ),
        )
    return disease


# ── State filter ─────────────────────────────────────────────────

def valid_state(
    state: str = Query(..., description="Canonical state name."),
) -> str:
    """
    Validate that the state query parameter is a canonical state.

    Parameters
    ----------
    state : str

    Returns
    -------
    str
        The validated state name.
    """
    from src.utils.state_maps import CANONICAL_STATE_SET
    from fastapi import HTTPException, status

    if state not in CANONICAL_STATE_SET:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown state: '{state}'. "
                f"Use canonical state names (e.g. 'Lagos', 'FCT', 'Cross River')."
            ),
        )
    return state


# ── Year range ───────────────────────────────────────────────────

def valid_year(
    year: int = Query(
        ...,
        ge=2000,
        le=2100,
        description="Four-digit year.",
    ),
) -> int:
    """
    Validate a year query parameter is in a sensible range.

    Parameters
    ----------
    year : int

    Returns
    -------
    int
    """
    return year
