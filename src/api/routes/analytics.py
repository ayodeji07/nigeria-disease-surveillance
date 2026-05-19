"""
src/api/routes/analytics.py
────────────────────────────────────────────────────────────────
Analytics, statistics, and forecasting endpoints.

Endpoints:
  GET /api/v1/analytics/summary           — national KPI cards
  GET /api/v1/analytics/trends            — time series per disease
  GET /api/v1/analytics/hotspots          — top N states by burden
  GET /api/v1/analytics/forecast          — Prophet forecast
  GET /api/v1/analytics/outbreak-alerts   — CUSUM outbreak detection
  GET /api/v1/analytics/trend-test        — Mann-Kendall trend test
  GET /api/v1/analytics/clusters          — K-means state clustering
  GET /api/v1/analytics/cfr-benchmark     — CFR vs. national mean
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies import get_session
from src.api.schemas import (
    ForecastResponse,
    ForecastPoint,
    HotspotResponse,
    NationalSummaryRecord,
    OutbreakAlertResponse,
    TrendResponse,
    TrendPoint,
    TrendTestResponse,
    ClusterResponse,
    StatClusterRecord,
    StateBurdenRecord,
)
from src.db import repository
from src.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/analytics",
    tags=["Analytics & Forecasting"],
)


@router.get(
    "/summary",
    response_model=list[NationalSummaryRecord],
    summary="National KPI summary",
    description="Return high-level case/death/CFR totals per disease for dashboard KPI cards.",
)
def get_summary(
    year: Optional[int] = Query(None, ge=2000, le=2100,
                                description="Filter to a specific year."),
    db:   Session       = Depends(get_session),
) -> list[NationalSummaryRecord]:
    """
    Return one summary record per disease with total cases, deaths,
    average CFR, and highest single-week case count.
    """
    df = repository.get_national_summary(session=db, year=year)

    if df.empty:
        return []

    logger.info("GET /analytics/summary — year=%s → %d diseases", year, len(df))

    return [
        NationalSummaryRecord(
            disease          = row.get("disease", ""),
            total_cases      = _safe_int(row.get("total_cases", 0)),
            total_deaths     = _safe_int(row.get("total_deaths", 0)),
            avg_cfr_pct      = _safe_float(row.get("avg_cfr_pct")),
            peak_week_cases  = _safe_int(row.get("peak_week_cases")),
            states_affected  = _safe_int(row.get("states_affected")),
        )
        for row in df.to_dict(orient="records")
    ]


@router.get(
    "/trends",
    response_model=TrendResponse,
    summary="Disease trend time series",
    description=(
        "Return a time series of case counts for one disease. "
        "Aggregate by week (default) or month."
    ),
)
def get_trend(
    disease: str           = Query(..., description="Disease name."),
    state:   Optional[str] = Query(None, description="State name (omit for national)."),
    freq:    str           = Query("weekly", pattern="^(weekly|monthly)$",
                                   description="Time aggregation: weekly or monthly."),
    db:      Session       = Depends(get_session),
) -> TrendResponse:
    """
    Return a time-ordered series of case counts for charting.
    """
    df = repository.get_disease_trend(
        session = db,
        disease = disease,
        state   = state,
        freq    = freq,
    )

    logger.info(
        "GET /analytics/trends — disease=%s state=%s freq=%s rows=%d",
        disease, state, freq, len(df),
    )

    if df.empty:
        return TrendResponse(disease=disease, state=state, freq=freq, points=[])

    points = [
        TrendPoint(
            period          = str(row.get("period", "")),
            confirmed_cases = _safe_int(row.get("confirmed_cases", 0)),
            deaths          = _safe_int(row.get("deaths", 0)),
            cfr_pct         = _safe_float(row.get("cfr_pct")),
            avg_incidence   = _safe_float(row.get("avg_incidence")),
        )
        for row in df.to_dict(orient="records")
    ]

    return TrendResponse(disease=disease, state=state, freq=freq, points=points)


@router.get(
    "/hotspots",
    response_model=HotspotResponse,
    summary="Top states by disease burden",
    description="Return the top N states with the highest confirmed case counts.",
)
def get_hotspots(
    disease: str           = Query(..., description="Disease name."),
    year:    Optional[int] = Query(None, ge=2000, le=2100),
    top_n:   int           = Query(5, ge=1, le=37,
                                   description="Number of states to return."),
    db:      Session       = Depends(get_session),
) -> HotspotResponse:
    """
    Return the top N hotspot states ranked by total confirmed cases.
    """
    df = repository.get_hotspots(
        session = db,
        disease = disease,
        year    = year,
        top_n   = top_n,
    )

    logger.info(
        "GET /analytics/hotspots — disease=%s year=%s top_n=%d → %d states",
        disease, year, top_n, len(df),
    )

    states = [
        StateBurdenRecord(
            state                  = row.get("state", ""),
            zone                   = row.get("zone"),
            total_cases            = _safe_int(row.get("total_cases", 0)),
            total_deaths           = _safe_int(row.get("total_deaths", 0)),
            avg_incidence_per_100k = _safe_float(row.get("avg_incidence_per_100k")),
            cfr_pct                = _safe_float(row.get("cfr_pct")),
        )
        for row in df.to_dict(orient="records")
    ]

    return HotspotResponse(
        disease = disease,
        year    = year,
        top_n   = top_n,
        states  = states,
    )


@router.get(
    "/forecast",
    response_model=ForecastResponse,
    summary="Disease case count forecast",
    description=(
        "Generate a Prophet time-series forecast for a disease. "
        "Returns historical fitted values + 52-week ahead predictions "
        "with 95% confidence intervals."
    ),
)
def get_forecast(
    disease:       str           = Query(..., description="Disease name."),
    state:         Optional[str] = Query(None, description="State (omit for national)."),
    horizon_weeks: int           = Query(52, ge=4, le=104,
                                         description="Weeks to forecast ahead."),
    db:            Session       = Depends(get_session),
) -> ForecastResponse:
    """
    Fit a Prophet model and return a forecast for the specified disease.

    Note: model fitting takes 5–30 seconds depending on history length.
    For production use, consider pre-computing forecasts during the
    nightly ETL run and caching results.
    """
    from src.analysis.forecasting import forecast_disease

    # Pull full history for this disease
    df = repository.get_surveillance_records(
        session = db,
        disease = disease,
        state   = state,
        limit   = 50_000,
    )

    logger.info(
        "GET /analytics/forecast — disease=%s state=%s horizon=%d history=%d rows",
        disease, state, horizon_weeks, len(df),
    )

    result = forecast_disease(
        df            = df,
        disease       = disease,
        state         = state,
        horizon_weeks = horizon_weeks,
    )

    if result.is_empty:
        return ForecastResponse(
            disease       = disease,
            state         = state,
            horizon_weeks = horizon_weeks,
            points        = [],
            warnings      = result.warnings,
        )

    points = []
    for _, row in result.combined_df.iterrows():
        dt = row.get("ds")
        points.append(
            ForecastPoint(
                forecast_date = pd.Timestamp(dt).date() if pd.notna(dt) else None,
                y             = _safe_float(row.get("y")),
                yhat          = _safe_float(row.get("yhat")) or 0.0,
                yhat_lower    = _safe_float(row.get("yhat_lower")) or 0.0,
                yhat_upper    = _safe_float(row.get("yhat_upper")) or 0.0,
                is_forecast   = bool(row.get("is_forecast", False)),
            )
        )

    return ForecastResponse(
        disease       = disease,
        state         = state,
        horizon_weeks = horizon_weeks,
        mae           = result.model_metrics.get("mae"),
        rmse          = result.model_metrics.get("rmse"),
        points        = points,
        warnings      = result.warnings,
    )


@router.get(
    "/outbreak-alerts",
    response_model=list[OutbreakAlertResponse],
    summary="CUSUM outbreak detection alerts",
    description=(
        "Return states where the CUSUM algorithm detected an unusual "
        "spike in case counts above the historical baseline."
    ),
)
def get_outbreak_alerts(
    disease: str           = Query(..., description="Disease name."),
    year:    Optional[int] = Query(None, ge=2000, le=2100),
    db:      Session       = Depends(get_session),
) -> list[OutbreakAlertResponse]:
    """
    Apply CUSUM outbreak detection across all states for one disease.
    """
    from src.analysis.statistics import detect_outbreaks

    df = repository.get_surveillance_records(
        session = db,
        disease = disease,
        year    = year,
        limit   = 50_000,
    )

    alerts = detect_outbreaks(df, disease=disease)

    logger.info(
        "GET /analytics/outbreak-alerts — disease=%s → %d alerts",
        disease, len(alerts),
    )

    return [
        OutbreakAlertResponse(
            state          = a.state,
            disease        = a.disease,
            alert_date     = a.alert_date.date(),
            cases          = a.cases,
            cusum_score    = a.cusum_score,
            baseline_mean  = a.baseline_mean,
            interpretation = a.interpretation,
        )
        for a in alerts
    ]


@router.get(
    "/trend-test",
    response_model=TrendTestResponse,
    summary="Mann-Kendall trend test",
    description=(
        "Test whether confirmed case counts are statistically increasing "
        "or decreasing over time using the Mann-Kendall non-parametric test."
    ),
)
def get_trend_test(
    disease: str           = Query(..., description="Disease name."),
    state:   Optional[str] = Query(None, description="State (omit for national)."),
    db:      Session       = Depends(get_session),
) -> TrendTestResponse:
    """
    Apply the Mann-Kendall trend test to the full case count time series.
    """
    from src.analysis.statistics import test_trend

    df = repository.get_surveillance_records(
        session = db,
        disease = disease,
        state   = state,
        limit   = 50_000,
    )

    result = test_trend(df, disease=disease, state=state)

    logger.info(
        "GET /analytics/trend-test — disease=%s state=%s → %s (p=%.4f)",
        disease, state, result.trend, result.p_value,
    )

    return TrendTestResponse(
        disease        = result.disease,
        state          = result.state,
        trend          = result.trend,
        tau            = result.tau,
        p_value        = result.p_value,
        significant    = result.significant,
        interpretation = result.interpretation,
    )


@router.get(
    "/clusters",
    response_model=ClusterResponse,
    summary="K-means state clustering",
    description=(
        "Group Nigerian states into clusters based on disease burden profile "
        "(total cases, incidence rate, CFR). Returns cluster labels per state."
    ),
)
def get_clusters(
    disease:    str           = Query(..., description="Disease name."),
    year:       Optional[int] = Query(None, ge=2000, le=2100),
    n_clusters: int           = Query(4, ge=2, le=8,
                                      description="Number of clusters (2–8)."),
    db:         Session       = Depends(get_session),
) -> ClusterResponse:
    """
    Apply K-means clustering to group states by disease burden profile.
    """
    from src.analysis.statistics import cluster_states

    df = repository.get_surveillance_records(
        session = db,
        disease = disease,
        year    = year,
        limit   = 50_000,
    )

    result = cluster_states(df, disease=disease, year=year, n_clusters=n_clusters)

    logger.info(
        "GET /analytics/clusters — disease=%s year=%s → %d clusters",
        disease, year, result.n_clusters,
    )

    if result.state_clusters.empty:
        return ClusterResponse(
            disease=disease, year=year,
            n_clusters=0, states=[],
        )

    states = [
        StatClusterRecord(
            state         = str(row.get("state", "")),
            cluster_id    = int(row.get("cluster_id", 0)),
            cluster_label = str(row.get("cluster_label", "")),
            total_cases   = float(row.get("total_cases", 0)),
            avg_incidence = float(row.get("avg_incidence", 0)),
            avg_cfr       = float(row.get("avg_cfr", 0)),
        )
        for row in result.state_clusters.to_dict(orient="records")
    ]

    return ClusterResponse(
        disease    = disease,
        year       = year,
        n_clusters = result.n_clusters,
        states     = states,
    )


@router.get(
    "/cfr-benchmark",
    summary="CFR benchmarking against national mean",
    description=(
        "Compare each state's Case Fatality Rate to the national mean "
        "and flag states with significantly higher mortality."
    ),
)
def get_cfr_benchmark(
    disease: str           = Query(..., description="Disease name."),
    year:    Optional[int] = Query(None, ge=2000, le=2100),
    db:      Session       = Depends(get_session),
) -> dict:
    """
    Return per-state CFR benchmarked against the national mean.

    States more than 1 std dev above the mean are flagged HIGH.
    """
    from src.analysis.statistics import benchmark_cfr

    df = repository.get_surveillance_records(
        session = db,
        disease = disease,
        year    = year,
        limit   = 50_000,
    )

    cfr_df = benchmark_cfr(df, disease=disease, year=year)

    logger.info(
        "GET /analytics/cfr-benchmark — disease=%s year=%s → %d states",
        disease, year, len(cfr_df),
    )

    if cfr_df.empty:
        return {"disease": disease, "year": year, "states": []}

    return {
        "disease": disease,
        "year":    year,
        "states":  cfr_df.to_dict(orient="records"),
    }


# ── Internal helpers ─────────────────────────────────────────────

def _safe_int(value) -> Optional[int]:
    """Convert to int, returning None for null values."""
    try:
        return None if pd.isna(value) else int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> Optional[float]:
    """Convert to float, returning None for null values."""
    try:
        return None if pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None
