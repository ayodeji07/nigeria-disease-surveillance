"""
src/analysis/forecasting.py
────────────────────────────────────────────────────────────────
Time series forecasting for disease surveillance data.

This module wraps Facebook Prophet to produce 52-week ahead
forecasts of confirmed case counts per disease and (optionally)
per state.

Why Prophet?
  - Handles the irregular weekly cadence of NCDC data well.
  - Automatically detects yearly seasonality — important for
    diseases like cholera (rainy season) and meningitis (dry).
  - Provides uncertainty intervals, not just point estimates.
  - Robust to missing values and outliers (common in real
    surveillance data).
  - No manual parameter tuning required for a first-pass model.

Output:
  Each forecast is returned as a plain DataFrame so it can be
  stored in the database, served via the API, and visualised
  in the dashboard without any Prophet dependency downstream.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)

# Minimum number of historical data points required for a
# meaningful forecast — Prophet needs enough history to detect
# seasonality patterns.
_MIN_HISTORY_WEEKS = 52


# ── Result dataclass ─────────────────────────────────────────────

@dataclass
class ForecastResult:
    """
    The complete output of a single forecast run.

    Attributes
    ----------
    disease : str
        Disease being forecast.
    state : str | None
        State being forecast. None = national aggregate.
    horizon_weeks : int
        Number of weeks ahead forecasted.
    history_df : pd.DataFrame
        Historical actuals used to fit the model.
        Columns: ds (date), y (cases), yhat (fitted values).
    forecast_df : pd.DataFrame
        Forecast output.
        Columns: ds, yhat, yhat_lower, yhat_upper.
    combined_df : pd.DataFrame
        History + forecast joined for easy charting.
    model_metrics : dict
        MAE and RMSE on the training data (in-sample).
    warnings : list[str]
        Any non-fatal issues encountered during fitting.
    """
    disease:       str
    state:         Optional[str]
    horizon_weeks: int
    history_df:    pd.DataFrame  = field(default_factory=pd.DataFrame)
    forecast_df:   pd.DataFrame  = field(default_factory=pd.DataFrame)
    combined_df:   pd.DataFrame  = field(default_factory=pd.DataFrame)
    model_metrics: dict          = field(default_factory=dict)
    warnings:      list[str]     = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.forecast_df.empty


# ── Main forecast function ───────────────────────────────────────

def forecast_disease(
    df: pd.DataFrame,
    disease: str,
    state: Optional[str] = None,
    horizon_weeks: int = 52,
    yearly_seasonality: bool = True,
    weekly_seasonality: bool = False,
    changepoint_prior_scale: float = 0.05,
) -> ForecastResult:
    """
    Fit a Prophet model and forecast confirmed case counts.

    The function:
      1. Aggregates the input data to a national or state-level
         weekly time series.
      2. Prepares the Prophet-required (ds, y) format.
      3. Fits the model with Nigerian seasonal patterns in mind.
      4. Forecasts `horizon_weeks` into the future.
      5. Returns a clean ForecastResult with no Prophet objects —
         just plain DataFrames.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: disease, confirmed_cases, report_date.
        If state is provided, must also contain: state.
    disease : str
        The disease to forecast.
    state : str, optional
        If provided, forecast for this state only.
        If None, forecast the national (summed) series.
    horizon_weeks : int
        Number of weeks to forecast ahead. Default: 52 (one year).
    yearly_seasonality : bool
        Whether to model yearly seasonal patterns.
        True is appropriate for cholera and meningitis.
    weekly_seasonality : bool
        Whether to model within-week patterns.
        False is correct for weekly surveillance data.
    changepoint_prior_scale : float
        Controls how flexible the trend is. Lower values (0.01–0.05)
        produce smoother trends; higher values (0.1–0.5) allow
        more abrupt changes. Default: 0.05 (conservative).

    Returns
    -------
    ForecastResult
    """
    result = ForecastResult(
        disease=disease,
        state=state,
        horizon_weeks=horizon_weeks,
    )

    # ── Step 1: Prepare time series ───────────────────────────────
    series_df = _prepare_series(df, disease, state)

    if series_df.empty:
        result.warnings.append(
            f"No data found for disease='{disease}' state='{state}'"
        )
        logger.warning(
            "No data for forecast: disease=%s, state=%s", disease, state
        )
        return result

    if len(series_df) < _MIN_HISTORY_WEEKS:
        result.warnings.append(
            f"Only {len(series_df)} weeks of history available. "
            f"Need at least {_MIN_HISTORY_WEEKS} for a reliable forecast."
        )
        logger.warning(
            "Insufficient history for %s (%s): %d weeks (need %d)",
            disease, state or "national",
            len(series_df), _MIN_HISTORY_WEEKS,
        )
        # Proceed anyway — Prophet can still fit with less data,
        # but uncertainty will be high. We flag it in warnings.

    # ── Step 2: Fit Prophet model ─────────────────────────────────
    try:
        from prophet import Prophet
    except ImportError:
        result.warnings.append(
            "prophet package not installed. Run: pip install prophet"
        )
        logger.error("prophet not installed")
        return result

    # Suppress Prophet's verbose logging — it's very noisy by default
    import logging as _logging
    _logging.getLogger("prophet").setLevel(_logging.WARNING)
    _logging.getLogger("cmdstanpy").setLevel(_logging.WARNING)

    model = Prophet(
        yearly_seasonality       = yearly_seasonality,
        weekly_seasonality       = weekly_seasonality,
        daily_seasonality        = False,
        changepoint_prior_scale  = changepoint_prior_scale,
        # Nigerian harmattan season (dry / meningitis risk) peaks
        # December–February. Rainy season (cholera risk) peaks
        # June–September. These custom seasonalities improve
        # accuracy over the generic yearly Fourier terms.
        seasonality_mode         = "multiplicative",
        interval_width           = 0.95,   # 95% confidence intervals
    )

    # Add Nigerian seasonal regressors if we have enough data
    if yearly_seasonality and len(series_df) >= 52:
        model.add_seasonality(
            name="nigerian_dry_season",
            period=365.25,
            fourier_order=3,
        )

    try:
        model.fit(series_df)
    except Exception as exc:
        result.warnings.append(f"Prophet fit failed: {exc}")
        logger.error("Prophet fit error for %s/%s: %s", disease, state, exc)
        return result

    # ── Step 3: Generate forecast ─────────────────────────────────
    future    = model.make_future_dataframe(periods=horizon_weeks, freq="W")
    raw_forecast = model.predict(future)

    # ── Step 4: Clean and clip forecast ──────────────────────────
    # Case counts cannot be negative — clip lower bound at 0
    forecast_cols = ["ds", "yhat", "yhat_lower", "yhat_upper"]
    forecast_raw  = raw_forecast[forecast_cols].copy()

    for col in ["yhat", "yhat_lower", "yhat_upper"]:
        forecast_raw[col] = forecast_raw[col].clip(lower=0).round(2)

    # Split into history (has actuals) and future (forecast only)
    cutoff_date  = series_df["ds"].max()
    history_pred = forecast_raw[forecast_raw["ds"] <= cutoff_date].copy()
    future_pred  = forecast_raw[forecast_raw["ds"] >  cutoff_date].copy()

    # Attach actuals to the history portion
    history_merged = history_pred.merge(
        series_df[["ds", "y"]], on="ds", how="left"
    )

    # ── Step 5: Compute in-sample metrics ─────────────────────────
    metrics = _compute_metrics(
        actual    = history_merged["y"].dropna(),
        predicted = history_merged.loc[
            history_merged["y"].notna(), "yhat"
        ],
    )

    # ── Step 6: Build combined chart DataFrame ────────────────────
    # Combine history (with actuals) and forecast (future only)
    # into one DataFrame for easy plotting.
    history_merged["is_forecast"] = False
    future_pred["is_forecast"]    = True
    future_pred["y"]              = np.nan

    combined = pd.concat(
        [history_merged, future_pred],
        ignore_index=True,
    ).sort_values("ds")

    result.history_df    = history_merged.reset_index(drop=True)
    result.forecast_df   = future_pred.reset_index(drop=True)
    result.combined_df   = combined.reset_index(drop=True)
    result.model_metrics = metrics

    logger.info(
        "Forecast complete: disease=%s state=%s horizon=%dw "
        "MAE=%.1f RMSE=%.1f",
        disease,
        state or "national",
        horizon_weeks,
        metrics.get("mae", 0),
        metrics.get("rmse", 0),
    )
    return result


# ── Batch forecasting ────────────────────────────────────────────

def forecast_all_diseases(
    df: pd.DataFrame,
    horizon_weeks: int = 52,
    state: Optional[str] = None,
) -> dict[str, ForecastResult]:
    """
    Run forecasts for all diseases in the DataFrame.

    Useful for pre-computing forecasts during the ETL pipeline
    so the dashboard can serve them without waiting for model
    fitting on each request.

    Parameters
    ----------
    df : pd.DataFrame
        Full surveillance DataFrame.
    horizon_weeks : int
        Forecast horizon in weeks.
    state : str, optional
        Forecast for a specific state. None = national.

    Returns
    -------
    dict[str, ForecastResult]
        Keys are disease names.
    """
    diseases   = df["disease"].unique().tolist()
    results    = {}

    for disease in diseases:
        logger.info(
            "Forecasting %s (%s)...",
            disease, state or "national"
        )
        results[disease] = forecast_disease(
            df            = df,
            disease       = disease,
            state         = state,
            horizon_weeks = horizon_weeks,
        )

    successes = sum(1 for r in results.values() if not r.is_empty)
    logger.info(
        "Batch forecast complete: %d/%d diseases succeeded",
        successes, len(diseases),
    )
    return results


def forecast_result_to_dataframe(result: ForecastResult) -> pd.DataFrame:
    """
    Convert a ForecastResult to a flat DataFrame suitable for
    database storage or API serialisation.

    Parameters
    ----------
    result : ForecastResult

    Returns
    -------
    pd.DataFrame
        Columns: disease, state, ds, y (actual), yhat, yhat_lower,
                 yhat_upper, is_forecast, mae, rmse.
    """
    if result.is_empty:
        return pd.DataFrame()

    df = result.combined_df.copy()
    df["disease"] = result.disease
    df["state"]   = result.state or "National"
    df["mae"]     = result.model_metrics.get("mae")
    df["rmse"]    = result.model_metrics.get("rmse")

    # Rename ds → forecast_date for clarity in DB context
    df = df.rename(columns={"ds": "forecast_date"})

    return df[[
        "disease", "state", "forecast_date",
        "y", "yhat", "yhat_lower", "yhat_upper",
        "is_forecast", "mae", "rmse",
    ]]


# ── Internal helpers ─────────────────────────────────────────────

def _prepare_series(
    df: pd.DataFrame,
    disease: str,
    state: Optional[str],
) -> pd.DataFrame:
    """
    Extract and format a weekly time series in Prophet's (ds, y) format.

    Handles:
      - Filtering by disease and optional state.
      - Aggregating to weekly national totals if no state specified.
      - Dropping NaN and negative values.
      - Removing duplicate dates by summing.

    Parameters
    ----------
    df : pd.DataFrame
    disease : str
    state : str | None

    Returns
    -------
    pd.DataFrame
        Columns: ds (datetime), y (float). Sorted by ds.
        Empty DataFrame if no data found.
    """
    data = df[df["disease"] == disease].copy()

    if state:
        data = data[data["state"] == state]

    if data.empty:
        return pd.DataFrame()

    # Aggregate to one row per week
    agg = (
        data.groupby("report_date")["confirmed_cases"]
        .sum()
        .reset_index()
        .rename(columns={"report_date": "ds", "confirmed_cases": "y"})
    )

    # Ensure ds is datetime
    agg["ds"] = pd.to_datetime(agg["ds"])

    # Drop rows with null or negative y
    agg = agg[agg["y"].notna() & (agg["y"] >= 0)]

    # Handle duplicate dates — can occur after a transform bug
    agg = (
        agg.groupby("ds")["y"]
        .sum()
        .reset_index()
        .sort_values("ds")
        .reset_index(drop=True)
    )

    return agg


def _compute_metrics(
    actual: pd.Series,
    predicted: pd.Series,
) -> dict[str, float]:
    """
    Compute Mean Absolute Error and Root Mean Squared Error
    between actual and in-sample predicted values.

    Parameters
    ----------
    actual : pd.Series
        Observed case counts.
    predicted : pd.Series
        Model-fitted values for the same time points.

    Returns
    -------
    dict[str, float]
        Keys: 'mae', 'rmse', 'n_obs'.
    """
    if actual.empty or predicted.empty:
        return {"mae": None, "rmse": None, "n_obs": 0}

    # Align by index in case they differ
    actual    = actual.reset_index(drop=True)
    predicted = predicted.reset_index(drop=True)

    # Drop pairs where either is NaN
    mask      = actual.notna() & predicted.notna()
    actual    = actual[mask]
    predicted = predicted[mask]

    if len(actual) == 0:
        return {"mae": None, "rmse": None, "n_obs": 0}

    residuals = actual - predicted
    mae       = float(np.abs(residuals).mean())
    rmse      = float(np.sqrt((residuals ** 2).mean()))

    return {
        "mae":   round(mae,  2),
        "rmse":  round(rmse, 2),
        "n_obs": len(actual),
    }
