"""
dashboard/api_client.py
────────────────────────────────────────────────────────────────
HTTP client for dashboard → API communication.

All API calls from the dashboard go through this module.
No page file ever calls requests.get() directly.

Benefits of centralising API calls here:
  • One place to change the base URL or auth headers.
  • Caching (via Streamlit's st.cache_data) is applied once,
    not duplicated across every page.
  • Error handling is consistent — a failed request returns
    an empty DataFrame or dict, never crashes the dashboard.
  • Easy to mock in tests.

Usage in a dashboard page:
    from dashboard.api_client import get_trend, get_summary
    summary = get_summary(year=2023)
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd
import requests

# ── Configuration ─────────────────────────────────────────────────
def _secret(key: str, default: str = "") -> str:
    """Read a config value from st.secrets (Streamlit Cloud) or os.environ."""
    try:
        import streamlit as st
        return str(st.secrets.get(key, os.environ.get(key, default)))
    except Exception:
        return os.environ.get(key, default)


_BASE_URL = _secret("API_BASE_URL", "http://localhost:8000").rstrip("/")
_API_V1   = f"{_BASE_URL}/api/v1"

# Request timeout in seconds — prevents the dashboard from hanging
# if the API is slow or temporarily unreachable.
_TIMEOUT = 10

# Optional API key for protected endpoints
_API_KEY  = _secret("API_KEY", "")
_HEADERS  = {"X-API-Key": _API_KEY} if _API_KEY else {}


# ── Internal helpers ─────────────────────────────────────────────

def _get(
    endpoint: str,
    params:   Optional[dict] = None,
    timeout:  int            = _TIMEOUT,
) -> dict | list:
    """
    Perform a GET request against the API and return parsed JSON.

    On any error (network, timeout, non-200 status), logs a warning
    and returns an empty dict so the calling page can degrade
    gracefully rather than showing an error traceback to the user.

    Parameters
    ----------
    endpoint : str
        Path relative to /api/v1, e.g. "/surveillance".
    params : dict, optional
        Query parameters.
    timeout : int
        Request timeout in seconds. Override for slow endpoints (e.g. forecast).

    Returns
    -------
    dict | list
        Parsed JSON response, or {} on failure.
    """
    url = f"{_API_V1}{endpoint}"
    try:
        response = requests.get(
            url,
            params  = {k: v for k, v in (params or {}).items() if v is not None},
            headers = _HEADERS,
            timeout = timeout,
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.Timeout:
        print(f"[api_client] Timeout calling {url}")
        return {}
    except requests.exceptions.ConnectionError:
        print(f"[api_client] Connection error — is the API running at {_BASE_URL}?")
        return {}
    except requests.exceptions.HTTPError as exc:
        print(f"[api_client] HTTP {exc.response.status_code} from {url}")
        return {}
    except Exception as exc:
        print(f"[api_client] Unexpected error calling {url}: {exc}")
        return {}


def _records_to_df(data: dict | list, records_key: str = "records") -> pd.DataFrame:
    """
    Convert a JSON response to a DataFrame.

    Handles both:
      - List responses:  [{"state": "Lagos", ...}, ...]
      - Wrapped responses: {"records": [...], "total": N}

    Parameters
    ----------
    data : dict | list
    records_key : str
        Key in a dict response that holds the list of records.

    Returns
    -------
    pd.DataFrame
        Empty DataFrame if data is empty or malformed.
    """
    if not data:
        return pd.DataFrame()

    if isinstance(data, list):
        return pd.DataFrame(data)

    if isinstance(data, dict):
        records = data.get(records_key, data.get("states", data.get("points", [])))
        if isinstance(records, list):
            return pd.DataFrame(records)

    return pd.DataFrame()


# ── Public API functions ─────────────────────────────────────────
# Each function corresponds to one API endpoint. Streamlit's
# @st.cache_data decorator is applied here so repeated calls
# (e.g. when a page re-renders) don't hit the API unnecessarily.

def get_health() -> dict:
    """Check API health. Returns {} if unreachable."""
    return _get("/health") or {}  # Note: /health is not under /api/v1


def get_diseases() -> list[str]:
    """Return the list of tracked disease names."""
    data = _get("/surveillance/diseases")
    return data.get("diseases", []) if isinstance(data, dict) else []


def get_states() -> list[str]:
    """Return the list of canonical state names."""
    data = _get("/surveillance/states")
    return data.get("states", []) if isinstance(data, dict) else []


def get_summary(year: Optional[int] = None) -> pd.DataFrame:
    """
    Return national KPI summary per disease.

    Parameters
    ----------
    year : int, optional

    Returns
    -------
    pd.DataFrame
        Columns: disease, total_cases, total_deaths, avg_cfr_pct, etc.
    """
    data = _get("/analytics/summary", params={"year": year})
    if isinstance(data, list):
        return pd.DataFrame(data)
    return pd.DataFrame()


def get_surveillance(
    disease:    Optional[str] = None,
    state:      Optional[str] = None,
    year:       Optional[int] = None,
    start_date: Optional[str] = None,
    end_date:   Optional[str] = None,
    limit:      int           = 10_000,
) -> pd.DataFrame:
    """
    Return surveillance records with optional filters.

    Parameters
    ----------
    disease : str, optional
    state : str, optional
    year : int, optional
    start_date : str, optional  "YYYY-MM-DD"
    end_date : str, optional    "YYYY-MM-DD"
    limit : int

    Returns
    -------
    pd.DataFrame
    """
    data = _get(
        "/surveillance",
        params={
            "disease":    disease,
            "state":      state,
            "year":       year,
            "start_date": start_date,
            "end_date":   end_date,
            "limit":      limit,
        },
    )
    return _records_to_df(data)


def get_latest_surveillance(disease: Optional[str] = None) -> pd.DataFrame:
    """Return the most recent week's data for all states."""
    data = _get("/surveillance/latest", params={"disease": disease})
    return _records_to_df(data)


def get_trend(
    disease: str,
    state:   Optional[str] = None,
    freq:    str            = "weekly",
) -> pd.DataFrame:
    """
    Return a disease trend time series.

    Parameters
    ----------
    disease : str
    state : str, optional
    freq : str
        "weekly" or "monthly"

    Returns
    -------
    pd.DataFrame
        Columns: period, confirmed_cases, deaths, cfr_pct.
    """
    data = _get(
        "/analytics/trends",
        params={"disease": disease, "state": state, "freq": freq},
    )
    return _records_to_df(data, records_key="points")


def get_hotspots(
    disease: str,
    year:    Optional[int] = None,
    top_n:   int           = 5,
) -> pd.DataFrame:
    """Return the top N states by disease burden."""
    data = _get(
        "/analytics/hotspots",
        params={"disease": disease, "year": year, "top_n": top_n},
    )
    return _records_to_df(data, records_key="states")


def get_choropleth(disease: str, year: int) -> dict:
    """
    Return a GeoJSON FeatureCollection for choropleth map rendering.

    Parameters
    ----------
    disease : str
    year : int

    Returns
    -------
    dict
        GeoJSON FeatureCollection, or {} on failure.
    """
    return _get(
        "/geospatial/choropleth",
        params={"disease": disease, "year": year},
    )


def get_facilities(
    state:         Optional[str] = None,
    facility_type: Optional[str] = None,
) -> pd.DataFrame:
    """Return health facility locations."""
    data = _get(
        "/geospatial/facilities",
        params={"state": state, "facility_type": facility_type},
    )
    return _records_to_df(data, records_key="facilities")


def get_forecast(
    disease:       str,
    state:         Optional[str] = None,
    horizon_weeks: int           = 52,
) -> pd.DataFrame:
    """
    Return a Prophet forecast as a DataFrame.

    Parameters
    ----------
    disease : str
    state : str, optional
    horizon_weeks : int

    Returns
    -------
    pd.DataFrame
        Columns: forecast_date, y, yhat, yhat_lower, yhat_upper, is_forecast.
    """
    data = _get(
        "/analytics/forecast",
        params={
            "disease":       disease,
            "state":         state,
            "horizon_weeks": horizon_weeks,
        },
        timeout=60,
    )
    return _records_to_df(data, records_key="points")


def get_outbreak_alerts(
    disease: str,
    year:    Optional[int] = None,
) -> pd.DataFrame:
    """Return CUSUM outbreak detection alerts."""
    data = _get(
        "/analytics/outbreak-alerts",
        params={"disease": disease, "year": year},
    )
    if isinstance(data, list):
        return pd.DataFrame(data)
    return pd.DataFrame()


def get_burden_index(year: Optional[int] = None) -> pd.DataFrame:
    """Return the composite Disease Burden Index per state."""
    data = _get("/geospatial/burden-index", params={"year": year})
    return _records_to_df(data, records_key="states")


def get_accessibility(
    disease: str,
    year:    Optional[int] = None,
) -> pd.DataFrame:
    """Return facility accessibility analysis per state."""
    data = _get(
        "/geospatial/accessibility",
        params={"disease": disease, "year": year},
    )
    return _records_to_df(data, records_key="states")


def get_trend_test(
    disease: str,
    state:   Optional[str] = None,
) -> dict:
    """Return Mann-Kendall trend test result."""
    return _get(
        "/analytics/trend-test",
        params={"disease": disease, "state": state},
    )


def get_clusters(
    disease:    str,
    year:       Optional[int] = None,
    n_clusters: int           = 4,
) -> pd.DataFrame:
    """Return K-means state clustering result."""
    data = _get(
        "/analytics/clusters",
        params={"disease": disease, "year": year, "n_clusters": n_clusters},
    )
    return _records_to_df(data, records_key="states")


def get_cfr_benchmark(
    disease: str,
    year:    Optional[int] = None,
) -> pd.DataFrame:
    """Return CFR benchmarking per state."""
    data = _get(
        "/analytics/cfr-benchmark",
        params={"disease": disease, "year": year},
    )
    return _records_to_df(data, records_key="states")
