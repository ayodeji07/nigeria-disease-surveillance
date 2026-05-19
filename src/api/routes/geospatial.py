"""
src/api/routes/geospatial.py
────────────────────────────────────────────────────────────────
Geospatial endpoints for map rendering and spatial analysis.

Endpoints:
  GET /api/v1/geospatial/choropleth       — GeoJSON for choropleth maps
  GET /api/v1/geospatial/facilities       — health facility locations
  GET /api/v1/geospatial/burden-index     — composite disease burden score
  GET /api/v1/geospatial/accessibility    — facility accessibility analysis
  GET /api/v1/geospatial/morans-i         — spatial autocorrelation test
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies import get_session
from src.api.schemas import (
    ChoroplethResponse,
    FacilityListResponse,
    FacilityRecord,
    MoransIResponse,
    AccessibilityResponse,
    AccessibilityRecord,
)
from src.db import repository
from src.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/geospatial",
    tags=["Geospatial & Maps"],
)


@router.get(
    "/choropleth",
    response_model=ChoroplethResponse,
    summary="Choropleth map data",
    description=(
        "Return a GeoJSON FeatureCollection with state boundaries and "
        "disease burden statistics. Consumed by the dashboard's Leaflet map."
    ),
)
def get_choropleth(
    disease: str = Query(..., description="Disease name."),
    year:    int = Query(..., ge=2000, le=2100, description="Year."),
    db:      Session = Depends(get_session),
) -> ChoroplethResponse:
    """
    Return GeoJSON FeatureCollection for choropleth rendering.

    Each feature is a Nigerian state polygon with total_cases
    and avg_incidence_per_100k as properties.

    Requires PostgreSQL + PostGIS. Returns an empty features list
    on non-PostgreSQL backends.
    """
    df = repository.get_choropleth_data(session=db, disease=disease, year=year)

    if df.empty:
        logger.info(
            "GET /geospatial/choropleth — no data for %s %d", disease, year
        )
        return ChoroplethResponse(
            disease=disease, year=year, features=[]
        )

    import json
    features = []
    for _, row in df.iterrows():
        geom_json = row.get("geometry_json")
        if not geom_json:
            continue
        try:
            geom_dict = json.loads(geom_json) if isinstance(geom_json, str) else geom_json
        except (json.JSONDecodeError, TypeError):
            logger.warning("Skipping row with invalid geometry: %s", row.get("state"))
            continue

        features.append(
            {
                "type":     "Feature",
                "geometry": geom_dict,
                "properties": {
                    "state":                  row.get("state"),
                    "zone":                   row.get("zone"),
                    "total_cases":            _safe_int(row.get("total_cases")),
                    "total_deaths":           _safe_int(row.get("total_deaths")),
                    "avg_incidence_per_100k": _safe_float(row.get("avg_incidence_per_100k")),
                },
            }
        )

    logger.info(
        "GET /geospatial/choropleth — %s %d → %d features",
        disease, year, len(features),
    )
    return ChoroplethResponse(
        disease=disease, year=year, features=features
    )


@router.get(
    "/facilities",
    response_model=FacilityListResponse,
    summary="Health facility locations",
    description="Return health facility locations, optionally filtered by state or type.",
)
def get_facilities(
    state:         Optional[str] = Query(None, description="Filter by state name."),
    facility_type: Optional[str] = Query(None, description="Filter by facility type."),
    db:            Session       = Depends(get_session),
) -> FacilityListResponse:
    """
    Return health facility locations for map overlay.

    Results include name, type, ownership, state, LGA, and
    lat/lon coordinates. Used as an overlay layer on choropleth maps.
    """
    df = repository.get_health_facilities(
        session       = db,
        state         = state,
        facility_type = facility_type,
    )

    logger.info(
        "GET /geospatial/facilities — state=%s type=%s rows=%d",
        state, facility_type, len(df),
    )

    if df.empty:
        return FacilityListResponse(total=0, state=state, facilities=[])

    facilities = [
        FacilityRecord(**_sanitise_row(row))
        for row in df.to_dict(orient="records")
    ]

    return FacilityListResponse(
        total      = len(facilities),
        state      = state,
        facilities = facilities,
    )


@router.get(
    "/burden-index",
    summary="Composite disease burden index",
    description=(
        "Return a composite burden score per state combining normalised "
        "incidence rates across all diseases. Higher = more disease burden."
    ),
)
def get_burden_index(
    year:     Optional[int]  = Query(None, ge=2000, le=2100),
    db:       Session        = Depends(get_session),
) -> dict:
    """
    Compute and return the composite Disease Burden Index for all states.

    The DBI combines normalised incidence rates for all tracked diseases
    into a single [0, 1] score per state, enabling a single-map view
    of overall health burden across Nigeria.
    """
    from src.analysis.geospatial import compute_burden_index

    # Pull all disease burden data from the DB
    df = repository.get_surveillance_records(
        session = db,
        year    = year,
        limit   = 50_000,
    )

    if df.empty:
        return {"states": [], "year": year}

    dbi_df = compute_burden_index(df, year=year)

    if dbi_df.empty:
        return {"states": [], "year": year}

    return {
        "year":   year,
        "states": dbi_df.to_dict(orient="records"),
    }


@router.get(
    "/accessibility",
    response_model=AccessibilityResponse,
    summary="Facility accessibility analysis",
    description=(
        "Identify states where healthcare access is poor relative to "
        "disease burden. Returns an access gap score per state."
    ),
)
def get_accessibility(
    disease: str = Query(..., description="Disease name for burden calculation."),
    year:    Optional[int] = Query(None, ge=2000, le=2100),
    db:      Session       = Depends(get_session),
) -> AccessibilityResponse:
    """
    Return the facility accessibility analysis for all states.

    States are scored by: disease incidence / facilities per 100k.
    Higher scores = more disease burden per available facility.
    Flagged as CRITICAL, POOR, ADEQUATE, or GOOD.
    """
    from src.analysis.geospatial import (
        analyse_facility_accessibility,
        accessibility_to_dataframe,
    )
    import pandas as pd

    burden_df    = repository.get_state_burden(session=db, disease=disease, year=year)
    facilities_df = repository.get_health_facilities(session=db)
    population_df = _get_population_from_db(db)

    results = analyse_facility_accessibility(
        burden_df     = burden_df,
        facilities_df = facilities_df,
        population_df = population_df,
        disease       = disease,
    )

    logger.info(
        "GET /geospatial/accessibility — %s %s → %d states",
        disease, year, len(results),
    )

    return AccessibilityResponse(
        disease = disease,
        states  = [
            AccessibilityRecord(
                state               = r.state,
                total_facilities    = r.total_facilities,
                facilities_per_100k = r.facilities_per_100k,
                disease_burden      = r.disease_burden,
                access_gap_score    = r.access_gap_score,
                flag                = r.flag,
            )
            for r in results
        ],
    )


@router.get(
    "/morans-i",
    response_model=MoransIResponse,
    summary="Spatial autocorrelation (Moran's I)",
    description=(
        "Test whether high-burden states cluster spatially using Moran's I. "
        "Requires PostGIS and libpysal."
    ),
)
def get_morans_i(
    disease: str = Query(..., description="Disease name."),
    year:    Optional[int] = Query(None, ge=2000, le=2100),
    db:      Session       = Depends(get_session),
) -> MoransIResponse:
    """
    Return Moran's I spatial autocorrelation for disease burden.

    A significant positive Moran's I means high-burden states
    neighbour each other — indicating a regional driver such as
    a shared water source, climate zone, or healthcare gap.
    """
    from src.analysis.geospatial import compute_morans_i
    from src.etl.extract import extract_shapefiles

    burden_df  = repository.get_state_burden(session=db, disease=disease, year=year)
    shapefiles = extract_shapefiles()
    states_gdf = shapefiles.get("states")

    if states_gdf is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "State shapefiles are not available. "
                "Run the ETL pipeline to load spatial data."
            ),
        )

    result = compute_morans_i(
        burden_df  = burden_df,
        states_gdf = states_gdf,
        disease    = disease,
        year       = year,
    )

    logger.info(
        "GET /geospatial/morans-i — %s %s → I=%.4f p=%.4f",
        disease, year, result.morans_i, result.p_value,
    )

    return MoransIResponse(
        disease        = result.disease,
        year           = result.year,
        morans_i       = result.morans_i,
        expected_i     = result.expected_i,
        z_score        = result.z_score,
        p_value        = result.p_value,
        significant    = result.significant,
        pattern        = result.pattern,
        interpretation = result.interpretation,
    )


# ── Internal helpers ─────────────────────────────────────────────

def _sanitise_row(row: dict) -> dict:
    """Replace NaN/NaT with None in a dict row."""
    import pandas as pd
    clean = {}
    for k, v in row.items():
        try:
            clean[k] = None if pd.isna(v) else v
        except (TypeError, ValueError):
            clean[k] = v
    return clean


def _safe_int(value) -> Optional[int]:
    """Convert to int safely, returning None for null values."""
    import pandas as pd
    try:
        return None if pd.isna(value) else int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> Optional[float]:
    """Convert to float safely, returning None for null values."""
    import pandas as pd
    try:
        return None if pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


def _get_population_from_db(db: Session) -> "pd.DataFrame":
    """
    Pull population data from dim_states for accessibility analysis.

    Parameters
    ----------
    db : Session

    Returns
    -------
    pd.DataFrame
        Columns: state, population.
    """
    import pandas as pd
    from sqlalchemy import text

    query = text("""
        SELECT state_name AS state, population
        FROM   dim_states
        WHERE  population IS NOT NULL
    """)
    try:
        return pd.read_sql(query, db.bind)
    except Exception as exc:
        logger.warning("Could not load population from DB: %s", exc)
        return pd.DataFrame(columns=["state", "population"])
