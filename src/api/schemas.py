"""
src/api/schemas.py
────────────────────────────────────────────────────────────────
Pydantic models for all API request parameters and responses.

Why explicit schemas?
  - FastAPI validates incoming query parameters automatically.
  - Outgoing responses are serialised to exactly the documented
    shape — no accidental leakage of internal DB columns.
  - The auto-generated Swagger docs at /docs are built from
    these models, so they stay accurate by construction.

Naming convention:
  • *Request  — query parameters or request body models
  • *Response — a single item in a response
  • *ListResponse — a paginated or batched list response
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Shared primitives ────────────────────────────────────────────

class HealthCheck(BaseModel):
    """Response for the GET /health endpoint."""
    status:  str = "healthy"
    version: str
    environment: str


# ── Surveillance schemas ─────────────────────────────────────────

class SurveillanceRecord(BaseModel):
    """
    A single disease surveillance observation.
    One row from the fact_disease_surveillance table.
    """
    state:               str
    zone:                Optional[str]   = None
    disease:             str
    report_date:         Optional[date]  = None
    epi_week:            Optional[int]   = None
    year:                Optional[int]   = None
    season:              Optional[str]   = None
    suspected_cases:     int             = 0
    confirmed_cases:     int             = 0
    deaths:              int             = 0
    cfr_pct:             Optional[float] = None
    incidence_per_100k:  Optional[float] = None
    cases_4wk_avg:       Optional[float] = None
    pct_change_wow:      Optional[float] = None
    data_quality_flag:   Optional[str]   = None
    data_source:         Optional[str]   = None

    model_config = {"from_attributes": True}


class SurveillanceListResponse(BaseModel):
    """Paginated list of surveillance records."""
    total:   int
    limit:   int
    records: list[SurveillanceRecord]


# ── Analytics schemas ────────────────────────────────────────────

class NationalSummaryRecord(BaseModel):
    """High-level KPI for one disease — used on the Overview page."""
    disease:             str
    total_cases:         int
    total_deaths:        int
    avg_cfr_pct:         Optional[float] = None
    peak_week_cases:     Optional[int]   = None
    states_affected:     Optional[int]   = None

    model_config = {"from_attributes": True}


class TrendPoint(BaseModel):
    """A single point in a disease trend time series."""
    period:           str              # "2023-01-02" or "2023-01"
    confirmed_cases:  int
    deaths:           int
    cfr_pct:          Optional[float] = None
    avg_incidence:    Optional[float] = None


class TrendResponse(BaseModel):
    """Full trend time series for one disease (and optional state)."""
    disease:  str
    state:    Optional[str] = None
    freq:     str
    points:   list[TrendPoint]


class StateBurdenRecord(BaseModel):
    """Disease burden metrics for one state."""
    state:                 str
    zone:                  Optional[str]   = None
    total_cases:           int
    total_deaths:          int
    avg_incidence_per_100k: Optional[float] = None
    cfr_pct:               Optional[float] = None

    model_config = {"from_attributes": True}


class HotspotResponse(BaseModel):
    """Top N states by disease burden."""
    disease:  str
    year:     Optional[int]
    top_n:    int
    states:   list[StateBurdenRecord]


class OutbreakAlertResponse(BaseModel):
    """A single CUSUM outbreak alert."""
    state:         str
    disease:       str
    alert_date:    date
    cases:         int
    cusum_score:   float
    baseline_mean: float
    interpretation: str


class StatClusterRecord(BaseModel):
    """Clustering result for one state."""
    state:         str
    cluster_id:    int
    cluster_label: str
    total_cases:   float
    avg_incidence: float
    avg_cfr:       float


class ClusterResponse(BaseModel):
    """K-means clustering result for all states."""
    disease:    str
    year:       Optional[int]
    n_clusters: int
    states:     list[StatClusterRecord]


# ── Geospatial schemas ───────────────────────────────────────────

class GeoFeatureProperties(BaseModel):
    """Properties attached to each GeoJSON feature."""
    state:                  str
    zone:                   Optional[str]   = None
    total_cases:            Optional[int]   = None
    total_deaths:           Optional[int]   = None
    avg_incidence_per_100k: Optional[float] = None


class GeoFeature(BaseModel):
    """A single GeoJSON feature (state polygon + properties)."""
    type:       str = "Feature"
    geometry:   dict[str, Any]
    properties: GeoFeatureProperties


class ChoroplethResponse(BaseModel):
    """GeoJSON FeatureCollection for choropleth map rendering."""
    type:     str = "FeatureCollection"
    disease:  str
    year:     int
    features: list[GeoFeature]


class FacilityRecord(BaseModel):
    """A single health facility record."""
    facility_name:  Optional[str] = None
    facility_type:  Optional[str] = None
    ownership:      Optional[str] = None
    state:          Optional[str] = None
    lga_name:       Optional[str] = None
    latitude:       Optional[float] = None
    longitude:      Optional[float] = None

    model_config = {"from_attributes": True}


class FacilityListResponse(BaseModel):
    """List of health facility locations."""
    total:      int
    state:      Optional[str]
    facilities: list[FacilityRecord]


# ── Forecast schemas ─────────────────────────────────────────────

class ForecastPoint(BaseModel):
    """A single point in a forecast time series."""
    forecast_date:  date
    y:              Optional[float] = None   # actual (None for future)
    yhat:           float                    # predicted value
    yhat_lower:     float                    # lower confidence bound
    yhat_upper:     float                    # upper confidence bound
    is_forecast:    bool                     # True = future, False = fitted


class ForecastResponse(BaseModel):
    """Complete forecast result for one disease."""
    disease:       str
    state:         Optional[str] = None
    horizon_weeks: int
    mae:           Optional[float] = None
    rmse:          Optional[float] = None
    points:        list[ForecastPoint]
    warnings:      list[str] = []


# ── Moran's I schema ─────────────────────────────────────────────

class MoransIResponse(BaseModel):
    """Spatial autocorrelation test result."""
    disease:        str
    year:           Optional[int]
    morans_i:       float
    expected_i:     float
    z_score:        float
    p_value:        float
    significant:    bool
    pattern:        str
    interpretation: str


# ── Accessibility schema ─────────────────────────────────────────

class AccessibilityRecord(BaseModel):
    """Facility accessibility analysis for one state."""
    state:               str
    total_facilities:    int
    facilities_per_100k: float
    disease_burden:      float
    access_gap_score:    float
    flag:                str


class AccessibilityResponse(BaseModel):
    """Facility accessibility analysis for all states."""
    disease: str
    states:  list[AccessibilityRecord]


# ── Trend test schema ────────────────────────────────────────────

class TrendTestResponse(BaseModel):
    """Mann-Kendall trend test result."""
    disease:        str
    state:          Optional[str]
    trend:          str
    tau:            float
    p_value:        float
    significant:    bool
    interpretation: str


# ── Validators ───────────────────────────────────────────────────

class SurveillanceQueryParams(BaseModel):
    """
    Validated query parameters for GET /api/v1/surveillance.

    FastAPI uses this as a dependency to validate and coerce
    all query parameters in one place.
    """
    disease:    Optional[str]  = Field(None, description="Filter by disease name")
    state:      Optional[str]  = Field(None, description="Filter by state name")
    year:       Optional[int]  = Field(None, ge=2000, le=2100)
    start_date: Optional[date] = Field(None, description="Start date (YYYY-MM-DD)")
    end_date:   Optional[date] = Field(None, description="End date (YYYY-MM-DD)")
    limit:      int            = Field(1000, ge=1, le=50_000)
    format:     str            = Field("json", pattern="^(json|csv)$")

    @field_validator("end_date")
    @classmethod
    def end_date_after_start(
        cls, end_date: Optional[date], info
    ) -> Optional[date]:
        """Ensure end_date is not before start_date."""
        start_date = info.data.get("start_date")
        if end_date and start_date and end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        return end_date
