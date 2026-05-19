"""
src/api/routes/surveillance.py
────────────────────────────────────────────────────────────────
REST endpoints for disease surveillance data.

Endpoints:
  GET /api/v1/surveillance          — query with filters, CSV download
  GET /api/v1/surveillance/latest   — most recent week nationwide
  GET /api/v1/surveillance/state/{state}  — full history for one state
  GET /api/v1/surveillance/disease/{disease} — full history for one disease

All endpoints read from the database via the repository layer.
No business logic lives here — routes are thin wrappers that
validate input, call the repository, and serialise the response.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import io

import pandas as pd
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.api.dependencies import get_session, get_pagination, Pagination
from src.api.schemas import (
    SurveillanceRecord,
    SurveillanceListResponse,
)
from src.db import repository
from src.utils.config import Diseases
from src.utils.state_maps import CANONICAL_STATES
from src.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/surveillance",
    tags=["Surveillance Data"],
)


@router.get(
    "",
    response_model=SurveillanceListResponse,
    summary="Query surveillance records",
    description=(
        "Return disease surveillance records with optional filtering "
        "by disease, state, year, and date range. "
        "Supports CSV download via ?format=csv."
    ),
)
def get_surveillance(
    disease:    str | None   = Query(None, description="Filter by disease name."),
    state:      str | None   = Query(None, description="Filter by state name."),
    year:       int | None   = Query(None, ge=2000, le=2100, description="Filter by year."),
    start_date: str | None   = Query(None, description="Start date YYYY-MM-DD."),
    end_date:   str | None   = Query(None, description="End date YYYY-MM-DD."),
    format:     str          = Query("json", pattern="^(json|csv)$",
                                     description="Response format: json or csv."),
    pagination: Pagination   = Depends(get_pagination),
    db:         Session      = Depends(get_session),
) -> SurveillanceListResponse | StreamingResponse:
    """
    Fetch surveillance records with flexible filtering.

    Returns JSON by default. Pass ?format=csv for a downloadable
    CSV file — useful for analysts who want to work in Excel or R.
    """
    # Parse optional date strings
    start = pd.Timestamp(start_date).date() if start_date else None
    end   = pd.Timestamp(end_date).date()   if end_date   else None

    df = repository.get_surveillance_records(
        session    = db,
        disease    = disease,
        state      = state,
        year       = year,
        start_date = start,
        end_date   = end,
        limit      = pagination.limit,
    )

    logger.info(
        "GET /surveillance — disease=%s state=%s year=%s rows=%d",
        disease, state, year, len(df),
    )

    # ── CSV download path ─────────────────────────────────────────
    if format == "csv":
        buffer = io.StringIO()
        df.to_csv(buffer, index=False)
        buffer.seek(0)

        filename = _build_csv_filename("surveillance", disease, state, year)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    # ── JSON path ─────────────────────────────────────────────────
    records = _dataframe_to_records(df, SurveillanceRecord)
    return SurveillanceListResponse(
        total   = len(records),
        limit   = pagination.limit,
        records = records,
    )


@router.get(
    "/latest",
    response_model=SurveillanceListResponse,
    summary="Latest surveillance data",
    description="Return the most recent reporting week's data for all diseases and states.",
)
def get_latest_surveillance(
    disease: str | None = Query(None, description="Filter by disease."),
    db:      Session    = Depends(get_session),
) -> SurveillanceListResponse:
    """
    Return the single most recent week of data across all states.

    Used by the dashboard's 'Current Situation' cards.
    """
    df = repository.get_surveillance_records(
        session = db,
        disease = disease,
        limit   = 200,     # Up to 37 states × 5 diseases = 185 rows max
    )

    if not df.empty and "report_date" in df.columns:
        # Keep only the latest available date
        latest_date = df["report_date"].max()
        df = df[df["report_date"] == latest_date]

    logger.info("GET /surveillance/latest — %d rows", len(df))

    records = _dataframe_to_records(df, SurveillanceRecord)
    return SurveillanceListResponse(
        total   = len(records),
        limit   = 200,
        records = records,
    )


@router.get(
    "/state/{state_name}",
    response_model=SurveillanceListResponse,
    summary="Surveillance history for one state",
    description="Return the full surveillance history for a single state across all diseases.",
)
def get_state_surveillance(
    state_name: str,
    disease:    str | None = Query(None, description="Filter by disease."),
    format:     str        = Query("json", pattern="^(json|csv)$"),
    db:         Session    = Depends(get_session),
) -> SurveillanceListResponse | StreamingResponse:
    """
    Full time series for one state — all diseases by default, or
    filtered to one disease.
    """
    df = repository.get_surveillance_records(
        session = db,
        state   = state_name,
        disease = disease,
        limit   = 50_000,
    )

    logger.info(
        "GET /surveillance/state/%s — disease=%s rows=%d",
        state_name, disease, len(df),
    )

    if format == "csv":
        buffer = io.StringIO()
        df.to_csv(buffer, index=False)
        buffer.seek(0)
        filename = _build_csv_filename("surveillance", disease, state_name, None)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    records = _dataframe_to_records(df, SurveillanceRecord)
    return SurveillanceListResponse(
        total   = len(records),
        limit   = 50_000,
        records = records,
    )


@router.get(
    "/disease/{disease_name}",
    response_model=SurveillanceListResponse,
    summary="Surveillance history for one disease",
    description="Return the full surveillance history for a single disease across all states.",
)
def get_disease_surveillance(
    disease_name: str,
    state:        str | None = Query(None, description="Filter to a single state."),
    year:         int | None = Query(None, ge=2000, le=2100),
    format:       str        = Query("json", pattern="^(json|csv)$"),
    db:           Session    = Depends(get_session),
) -> SurveillanceListResponse | StreamingResponse:
    """
    Full time series for one disease across all states (or one state).
    """
    df = repository.get_surveillance_records(
        session = db,
        disease = disease_name,
        state   = state,
        year    = year,
        limit   = 50_000,
    )

    logger.info(
        "GET /surveillance/disease/%s — state=%s year=%s rows=%d",
        disease_name, state, year, len(df),
    )

    if format == "csv":
        buffer = io.StringIO()
        df.to_csv(buffer, index=False)
        buffer.seek(0)
        filename = _build_csv_filename("surveillance", disease_name, state, year)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    records = _dataframe_to_records(df, SurveillanceRecord)
    return SurveillanceListResponse(
        total   = len(records),
        limit   = 50_000,
        records = records,
    )


@router.get(
    "/diseases",
    summary="List available diseases",
    description="Return the list of diseases tracked in this system.",
)
def list_diseases() -> dict:
    """Return all tracked disease names."""
    return {"diseases": Diseases.all}


@router.get(
    "/states",
    summary="List available states",
    description="Return all 37 Nigerian administrative units (36 states + FCT).",
)
def list_states() -> dict:
    """Return all canonical state names."""
    return {"states": CANONICAL_STATES, "total": len(CANONICAL_STATES)}


# ── Internal helpers ─────────────────────────────────────────────

def _dataframe_to_records(
    df: pd.DataFrame,
    schema_class,
) -> list:
    """
    Convert a DataFrame to a list of Pydantic model instances.

    Replaces NaN with None so Pydantic's Optional fields
    serialise as JSON null rather than raising a validation error.

    Parameters
    ----------
    df : pd.DataFrame
    schema_class : type
        A Pydantic BaseModel subclass.

    Returns
    -------
    list
    """
    if df.empty:
        return []

    # Convert each row to a dict and replace every NaN/NaT variant
    # with None. df.where() misses some edge cases in pandas 3.x
    # when columns are mixed-type, so we sanitise at the dict level.
    def _sanitise(value):
        """Return None for any null-like value, else the value itself."""
        if value is None:
            return None
        try:
            # Catches float('nan'), numpy.nan, and pandas NaT
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        return value

    records = []
    for row_dict in df.to_dict(orient="records"):
        clean_row = {k: _sanitise(v) for k, v in row_dict.items()}
        records.append(schema_class(**clean_row))
    return records


def _build_csv_filename(
    prefix:  str,
    disease: str | None,
    state:   str | None,
    year:    int | None,
) -> str:
    """
    Build a descriptive filename for CSV downloads.

    Parameters
    ----------
    prefix : str
    disease : str | None
    state : str | None
    year : int | None

    Returns
    -------
    str
        e.g. "surveillance_cholera_lagos_2023.csv"
    """
    parts = [prefix]
    if disease:
        parts.append(disease.lower().replace(" ", "_"))
    if state:
        parts.append(state.lower().replace(" ", "_"))
    if year:
        parts.append(str(year))
    return "_".join(parts) + ".csv"
