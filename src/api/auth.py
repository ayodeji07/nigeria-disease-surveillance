"""
src/api/auth.py
────────────────────────────────────────────────────────────────
API authentication.

This module implements header-based API key authentication for
the FastAPI service.

Design decisions:
  • Public GET endpoints (surveillance data, charts) are open —
    no key required. This allows the Streamlit dashboard and
    any external client to query data freely.
  • Sensitive endpoints (pipeline trigger, admin stats) are
    protected and require a valid X-API-Key header.
  • Keys are compared using hmac.compare_digest() to prevent
    timing-attack side channels.
  • In development mode the placeholder key is allowed so
    developers can test without configuring a real key.

Usage in a route:
    @router.get("/admin/status",
                dependencies=[Depends(require_api_key)])
    def admin_status():
        ...
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# FastAPI's APIKeyHeader reads the key from the request header.
# auto_error=False means we handle missing keys ourselves rather
# than letting FastAPI return a generic 403.
_API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="API key for protected endpoints. Pass in X-API-Key header.",
)


def _keys_match(provided: str, expected: str) -> bool:
    """
    Compare two API key strings in constant time.

    hmac.compare_digest prevents timing attacks where an attacker
    could infer key length or prefix by measuring response time.

    Parameters
    ----------
    provided : str
        The key supplied by the caller.
    expected : str
        The correct key from settings.

    Returns
    -------
    bool
    """
    return hmac.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    )


def require_api_key(
    api_key: str | None = Security(_API_KEY_HEADER),
) -> str:
    """
    FastAPI dependency that enforces API key authentication.

    Raises HTTP 403 if the key is missing or incorrect.

    Usage:
        @router.post("/admin/trigger-etl",
                     dependencies=[Depends(require_api_key)])
        def trigger_etl():
            ...

    Parameters
    ----------
    api_key : str | None
        Extracted from the X-API-Key request header.

    Returns
    -------
    str
        The validated API key (rarely needed by the caller).

    Raises
    ------
    HTTPException
        403 Forbidden if the key is missing or invalid.
    """
    if not api_key:
        logger.warning("Request rejected — X-API-Key header missing")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing API key. Include X-API-Key in your request headers.",
        )

    if not _keys_match(api_key, settings.api_key):
        # Log the attempt but never log the actual key value
        logger.warning(
            "Request rejected — invalid API key (first 4 chars: %s...)",
            api_key[:4] if len(api_key) >= 4 else "****",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    return api_key


def optional_api_key(
    api_key: str | None = Security(_API_KEY_HEADER),
) -> str | None:
    """
    FastAPI dependency for endpoints that accept but don't require a key.

    When a key is provided it is validated — an invalid key still
    raises 403. When no key is provided, None is returned and the
    endpoint can apply reduced rate limits or feature restrictions.

    Parameters
    ----------
    api_key : str | None

    Returns
    -------
    str | None
        The validated key, or None if no key was provided.

    Raises
    ------
    HTTPException
        403 if a key was provided but is invalid.
    """
    if api_key is None:
        return None

    if not _keys_match(api_key, settings.api_key):
        logger.warning(
            "Optional auth — invalid key provided (first 4: %s...)",
            api_key[:4] if len(api_key) >= 4 else "****",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    return api_key
