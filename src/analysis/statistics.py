"""
src/analysis/statistics.py
────────────────────────────────────────────────────────────────
Statistical analysis for disease surveillance data.

This module provides all the statistical methods used in the
analysis notebooks and the API's /analytics endpoints. Every
function takes a plain pandas DataFrame and returns a plain
DataFrame or a typed result object — no database access, no
side effects.

Methods implemented:
  • Mann-Kendall trend test       — is incidence rising or falling?
  • Kruskal-Wallis seasonality    — is there a seasonal pattern?
  • Spearman correlation          — rainfall vs. disease burden
  • CUSUM outbreak detection      — flag unusual weekly spikes
  • K-means state clustering      — group states by burden profile
  • Rolling metrics               — 4-week averages, WoW change
  • CFR benchmarking              — compare state CFR to national mean

Design:
  All functions are pure — same input always gives same output.
  Results include the test statistic, p-value, and a plain-English
  interpretation so dashboard users don't need a statistics degree
  to read the output.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

from src.utils.logger import get_logger

logger = get_logger(__name__)

# Significance threshold used throughout the module
_ALPHA = 0.05


# ── Result dataclasses ───────────────────────────────────────────

@dataclass
class TrendResult:
    """Outcome of a Mann-Kendall trend test."""
    disease:       str
    state:         Optional[str]     # None = national
    trend:         str               # 'increasing' | 'decreasing' | 'no trend'
    tau:           float             # Kendall's tau (-1 to +1)
    p_value:       float
    significant:   bool
    interpretation: str


@dataclass
class SeasonalityResult:
    """Outcome of a Kruskal-Wallis seasonality test."""
    disease:       str
    state:         Optional[str]
    h_statistic:   float
    p_value:       float
    significant:   bool
    peak_season:   str               # 'Dry' | 'Rainy' | 'None'
    interpretation: str


@dataclass
class CorrelationResult:
    """Outcome of a Spearman correlation test."""
    disease:       str
    variable:      str               # e.g. 'rainfall_mm'
    rho:           float             # Spearman rho (-1 to +1)
    p_value:       float
    significant:   bool
    direction:     str               # 'positive' | 'negative' | 'none'
    interpretation: str


@dataclass
class OutbreakAlert:
    """A single CUSUM-detected outbreak alert."""
    state:         str
    disease:       str
    alert_date:    pd.Timestamp
    cases:         int
    cusum_score:   float
    baseline_mean: float
    interpretation: str


@dataclass
class ClusterResult:
    """K-means clustering outcome for states."""
    disease:       str
    year:          Optional[int]
    n_clusters:    int
    state_clusters: pd.DataFrame    # columns: state, cluster_id, cluster_label
    cluster_profiles: pd.DataFrame  # columns: cluster_id, mean_cases, mean_incidence


# ── Rolling metrics ──────────────────────────────────────────────

def _primary_col(df: pd.DataFrame) -> str:
    """Return 'primary_cases' if present, else fall back to 'confirmed_cases'."""
    return "primary_cases" if "primary_cases" in df.columns else "confirmed_cases"


def add_rolling_metrics(
    df: pd.DataFrame,
    case_col: str | None = None,
    window: int = 4,
) -> pd.DataFrame:
    """
    Add rolling average and week-on-week change columns to a
    time-ordered surveillance DataFrame.

    The DataFrame must be sorted by date before calling this
    function. We sort internally per (state, disease) group to
    be safe.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: state, disease, report_date, confirmed_cases.
    case_col : str
        Column to compute rolling metrics on.
    window : int
        Rolling window size in weeks. Default: 4.

    Returns
    -------
    pd.DataFrame
        Input with two new columns:
        - cases_4wk_avg   : rolling mean over `window` weeks
        - pct_change_wow  : week-on-week percentage change
    """
    if df.empty:
        return df

    if case_col is None:
        case_col = _primary_col(df)

    data = df.copy().sort_values(["state", "disease", "report_date"])

    grp = data.groupby(["state", "disease"])[case_col]

    data["cases_4wk_avg"] = (
        grp.transform(lambda g: g.rolling(window=window, min_periods=1).mean())
        .round(2)
    )
    data["pct_change_wow"] = (
        grp.transform(lambda g: g.pct_change() * 100)
        .replace([np.inf, -np.inf], np.nan)
        .round(2)
    )

    logger.debug(
        "Rolling metrics added: %d rows, window=%d weeks", len(data), window
    )
    return data.reset_index(drop=True)


# ── Mann-Kendall trend test ──────────────────────────────────────

def test_trend(
    df: pd.DataFrame,
    disease: str,
    state: Optional[str] = None,
) -> TrendResult:
    """
    Apply the Mann-Kendall non-parametric trend test to determine
    whether confirmed case counts are increasing or decreasing
    over time.

    Mann-Kendall is preferred over linear regression here because:
      - It does not assume a normal distribution of residuals.
      - It is robust to outliers (common in disease counts).
      - It handles missing values better than OLS.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: disease, confirmed_cases, and (if state is
        provided) state column. Sorted by date.
    disease : str
        Disease name to test.
    state : str, optional
        If provided, test state-level series. Default: national total.

    Returns
    -------
    TrendResult
    """
    try:
        import pymannkendall as mk
    except ImportError:
        logger.warning(
            "pymannkendall not installed — returning inconclusive result. "
            "Run: pip install pymannkendall"
        )
        return TrendResult(
            disease=disease, state=state,
            trend="unknown", tau=0.0, p_value=1.0,
            significant=False,
            interpretation="pymannkendall package not available.",
        )

    series = _extract_series(df, disease, state, _primary_col(df))

    if len(series) < 8:
        return TrendResult(
            disease=disease, state=state,
            trend="insufficient data", tau=0.0, p_value=1.0,
            significant=False,
            interpretation=(
                f"Need at least 8 data points for a reliable trend test "
                f"(got {len(series)})."
            ),
        )

    result = mk.original_test(series.values)

    trend_direction = (
        "increasing" if result.trend == "increasing"
        else "decreasing" if result.trend == "decreasing"
        else "no trend"
    )

    location_label = state if state else "Nigeria (national)"
    significance   = "statistically significant" if result.h else "not significant"

    interpretation = (
        f"{disease} in {location_label} shows a "
        f"{trend_direction} trend "
        f"(τ={result.Tau:.3f}, p={result.p:.4f}) — {significance} "
        f"at α={_ALPHA}."
    )

    return TrendResult(
        disease       = disease,
        state         = state,
        trend         = trend_direction,
        tau           = round(float(result.Tau), 4),
        p_value       = round(float(result.p), 6),
        significant   = bool(result.h),
        interpretation = interpretation,
    )


# ── Kruskal-Wallis seasonality test ─────────────────────────────

def test_seasonality(
    df: pd.DataFrame,
    disease: str,
    state: Optional[str] = None,
) -> SeasonalityResult:
    """
    Test whether case counts differ significantly between seasons
    using the Kruskal-Wallis H-test.

    Kruskal-Wallis is the non-parametric equivalent of one-way ANOVA.
    We use it rather than ANOVA because weekly case counts are often
    right-skewed and do not satisfy the normality assumption.

    Nigerian seasons:
      Dry   (November–March)  — harmattan, meningitis belt risk
      Rainy (April–October)   — cholera and malaria risk

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: disease, confirmed_cases, season columns.
    disease : str
    state : str, optional

    Returns
    -------
    SeasonalityResult
    """
    data = _filter_disease_state(df, disease, state)

    if "season" not in data.columns:
        return SeasonalityResult(
            disease=disease, state=state,
            h_statistic=0.0, p_value=1.0,
            significant=False, peak_season="None",
            interpretation="'season' column not present in data.",
        )

    col = _primary_col(data)
    dry_cases   = data[data["season"] == "Dry"][col].dropna()
    rainy_cases = data[data["season"] == "Rainy"][col].dropna()

    if len(dry_cases) < 3 or len(rainy_cases) < 3:
        return SeasonalityResult(
            disease=disease, state=state,
            h_statistic=0.0, p_value=1.0,
            significant=False, peak_season="None",
            interpretation="Insufficient data for seasonality test.",
        )

    h_stat, p_value = stats.kruskal(dry_cases, rainy_cases)

    significant = p_value < _ALPHA
    peak_season = (
        "Dry"   if dry_cases.mean() > rainy_cases.mean()
        else "Rainy"
    )

    location_label = state if state else "Nigeria (national)"

    if significant:
        interpretation = (
            f"{disease} in {location_label} shows statistically significant "
            f"seasonal variation (H={h_stat:.2f}, p={p_value:.4f}). "
            f"Case counts are higher in the {peak_season} season."
        )
    else:
        interpretation = (
            f"No statistically significant seasonal pattern detected for "
            f"{disease} in {location_label} (H={h_stat:.2f}, p={p_value:.4f})."
        )

    return SeasonalityResult(
        disease       = disease,
        state         = state,
        h_statistic   = round(float(h_stat), 4),
        p_value       = round(float(p_value), 6),
        significant   = significant,
        peak_season   = peak_season if significant else "None",
        interpretation = interpretation,
    )


# ── Spearman correlation ─────────────────────────────────────────

def test_correlation(
    df: pd.DataFrame,
    disease: str,
    covariate_col: str = "rainfall_mm",
    covariate_label: str = "monthly rainfall",
    state: Optional[str] = None,
) -> CorrelationResult:
    """
    Compute Spearman rank correlation between disease burden
    and a continuous covariate (e.g. rainfall).

    Spearman is used rather than Pearson because:
      - It does not assume linearity.
      - It is robust to outliers.
      - Case count distributions are typically skewed.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: disease, confirmed_cases, covariate_col.
    disease : str
    covariate_col : str
        Column name for the covariate variable.
    covariate_label : str
        Human-readable label for the covariate (used in interpretation).
    state : str, optional

    Returns
    -------
    CorrelationResult
    """
    data = _filter_disease_state(df, disease, state)

    if covariate_col not in data.columns:
        return CorrelationResult(
            disease=disease, variable=covariate_label,
            rho=0.0, p_value=1.0, significant=False,
            direction="none",
            interpretation=f"Column '{covariate_col}' not found in data.",
        )

    col = _primary_col(data)
    paired = data[[col, covariate_col]].dropna()

    if len(paired) < 10:
        return CorrelationResult(
            disease=disease, variable=covariate_label,
            rho=0.0, p_value=1.0, significant=False,
            direction="none",
            interpretation=(
                f"Need at least 10 paired observations "
                f"(got {len(paired)})."
            ),
        )

    rho, p_value = stats.spearmanr(paired[col], paired[covariate_col])

    significant = p_value < _ALPHA
    direction   = (
        "positive" if rho > 0.1
        else "negative" if rho < -0.1
        else "none"
    )

    location_label = state if state else "Nigeria (national)"

    if significant:
        strength = (
            "strong"    if abs(rho) > 0.6
            else "moderate" if abs(rho) > 0.3
            else "weak"
        )
        interpretation = (
            f"{disease} in {location_label} has a {strength} "
            f"{direction} correlation with {covariate_label} "
            f"(ρ={rho:.3f}, p={p_value:.4f}). "
            f"Higher {covariate_label} is associated with "
            f"{'more' if direction == 'positive' else 'fewer'} cases."
        )
    else:
        interpretation = (
            f"No significant correlation between {disease} and "
            f"{covariate_label} in {location_label} "
            f"(ρ={rho:.3f}, p={p_value:.4f})."
        )

    return CorrelationResult(
        disease       = disease,
        variable      = covariate_label,
        rho           = round(float(rho), 4),
        p_value       = round(float(p_value), 6),
        significant   = significant,
        direction     = direction,
        interpretation = interpretation,
    )


# ── CUSUM outbreak detection ─────────────────────────────────────

def detect_outbreaks(
    df: pd.DataFrame,
    disease: str,
    threshold_multiplier: float = 2.0,
    baseline_weeks: int = 52,
) -> list[OutbreakAlert]:
    """
    Apply CUSUM (Cumulative Sum) control charts to detect weeks
    where case counts exceed the expected baseline by a meaningful
    amount.

    CUSUM is preferred over simple threshold rules because it
    accumulates small deviations over time, making it sensitive
    to sustained moderate increases (not just single-week spikes).

    Algorithm:
      1. Compute baseline mean (μ) and std (σ) from the first
         `baseline_weeks` of data.
      2. Set the decision threshold as μ + threshold_multiplier × σ.
      3. Compute the CUSUM score: S_t = max(0, S_{t-1} + x_t - μ - k)
         where k = threshold_multiplier × σ / 2 (the allowable slack).
      4. Flag weeks where S_t > threshold as outbreak alerts.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: state, disease, report_date, confirmed_cases.
        Should be sorted by date.
    disease : str
        Disease to analyse.
    threshold_multiplier : float
        How many standard deviations above baseline triggers an alert.
        Default: 2.0 (standard epidemiological practice).
    baseline_weeks : int
        Number of initial weeks used to establish the baseline.

    Returns
    -------
    list[OutbreakAlert]
        One alert per (state, week) where CUSUM exceeded the threshold.
        Empty list if no outbreaks detected.
    """
    alerts: list[OutbreakAlert] = []
    disease_df = df[df["disease"] == disease].copy()

    if disease_df.empty:
        logger.warning("No data for disease '%s' in outbreak detection", disease)
        return alerts

    case_col = _primary_col(disease_df)
    for state_name in disease_df["state"].unique():
        state_series = (
            disease_df[disease_df["state"] == state_name]
            .sort_values("report_date")[case_col]
            .reset_index(drop=True)
        )
        state_dates = (
            disease_df[disease_df["state"] == state_name]
            .sort_values("report_date")["report_date"]
            .reset_index(drop=True)
        )

        if len(state_series) < baseline_weeks:
            # Not enough history for a meaningful baseline
            continue

        # Establish baseline statistics from the first N weeks
        baseline    = state_series.iloc[:baseline_weeks]
        mu          = baseline.mean()
        sigma       = baseline.std()

        if sigma == 0:
            # No variability — cannot compute CUSUM meaningfully
            continue

        # CUSUM parameters
        k         = threshold_multiplier * sigma / 2   # allowable slack
        threshold = threshold_multiplier * sigma        # decision threshold

        # Run CUSUM over the full series
        cusum_score = 0.0
        for i in range(baseline_weeks, len(state_series)):
            x_t         = float(state_series.iloc[i])
            cusum_score = max(0.0, cusum_score + x_t - mu - k)

            if cusum_score > threshold:
                alerts.append(
                    OutbreakAlert(
                        state         = state_name,
                        disease       = disease,
                        alert_date    = pd.Timestamp(state_dates.iloc[i]),
                        cases         = int(x_t),
                        cusum_score   = round(cusum_score, 2),
                        baseline_mean = round(mu, 2),
                        interpretation = (
                            f"Outbreak signal: {state_name} reported "
                            f"{int(x_t)} {disease} cases "
                            f"(baseline mean: {mu:.1f}). "
                            f"CUSUM score {cusum_score:.1f} exceeds "
                            f"threshold {threshold:.1f}."
                        ),
                    )
                )
                # Reset CUSUM after flagging — avoids cascading alerts
                cusum_score = 0.0

    logger.info(
        "Outbreak detection (%s): %d alerts found across %d states",
        disease,
        len(alerts),
        len({a.state for a in alerts}),
    )
    return alerts


def outbreak_alerts_to_dataframe(alerts: list[OutbreakAlert]) -> pd.DataFrame:
    """
    Convert a list of OutbreakAlert objects to a DataFrame.

    Parameters
    ----------
    alerts : list[OutbreakAlert]

    Returns
    -------
    pd.DataFrame
        Sorted by alert_date descending.
    """
    if not alerts:
        return pd.DataFrame(
            columns=[
                "state", "disease", "alert_date", "cases",
                "cusum_score", "baseline_mean", "interpretation",
            ]
        )

    rows = [
        {
            "state":          a.state,
            "disease":        a.disease,
            "alert_date":     a.alert_date,
            "cases":          a.cases,
            "cusum_score":    a.cusum_score,
            "baseline_mean":  a.baseline_mean,
            "interpretation": a.interpretation,
        }
        for a in alerts
    ]
    return (
        pd.DataFrame(rows)
        .sort_values("alert_date", ascending=False)
        .reset_index(drop=True)
    )


# ── K-means state clustering ─────────────────────────────────────

def cluster_states(
    df: pd.DataFrame,
    disease: str,
    year: Optional[int] = None,
    n_clusters: int = 4,
) -> ClusterResult:
    """
    Group states into clusters based on their disease burden profile
    using K-means clustering.

    Features used per state:
      - Total confirmed cases
      - Average incidence rate per 100k
      - Average CFR

    Clustering reveals which states share similar epidemiological
    patterns — useful for targeted intervention planning.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: state, disease, confirmed_cases,
        incidence_per_100k, cfr_pct.
    disease : str
    year : int, optional
        Filter to a specific year.
    n_clusters : int
        Number of clusters. Default: 4 (High / Medium-High /
        Medium-Low / Low burden).

    Returns
    -------
    ClusterResult
    """
    from sklearn.preprocessing import StandardScaler
    from sklearn.cluster import KMeans

    data = _filter_disease_state(df, disease, state=None)
    if year:
        if "year" in data.columns:
            data = data[data["year"] == year]

    if data.empty:
        return ClusterResult(
            disease=disease, year=year,
            n_clusters=0,
            state_clusters=pd.DataFrame(),
            cluster_profiles=pd.DataFrame(),
        )

    col = _primary_col(data)
    # Aggregate to one row per state
    agg_dict: dict = {
        col:                 "sum",
        "incidence_per_100k": "mean",
    }
    if "deaths" in data.columns:
        agg_dict["deaths"] = "sum"
    state_agg = (
        data.groupby("state")
        .agg(**{
            "total_cases":   (col,                   "sum"),
            "avg_incidence": ("incidence_per_100k",  "mean"),
            **( {"total_deaths": ("deaths", "sum")} if "deaths" in data.columns else {} ),
        })
        .reset_index()
        .fillna(0)
    )
    # Compute CFR as sum(deaths)/sum(cases) to avoid per-row averaging artefacts
    if "total_deaths" in state_agg.columns:
        state_agg["avg_cfr"] = np.where(
            state_agg["total_cases"] > 0,
            (state_agg["total_deaths"] / state_agg["total_cases"] * 100).round(4),
            0.0,
        )
    else:
        state_agg["avg_cfr"] = 0.0

    if len(state_agg) < n_clusters:
        n_clusters = max(2, len(state_agg) // 2)
        logger.warning(
            "Reduced n_clusters to %d (only %d states available)",
            n_clusters, len(state_agg),
        )

    # Standardise features — K-means is sensitive to scale
    feature_cols = ["total_cases", "avg_incidence", "avg_cfr"]
    scaler       = StandardScaler()
    features_scaled = scaler.fit_transform(state_agg[feature_cols])

    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    state_agg["cluster_id"] = kmeans.fit_predict(features_scaled)

    # Label clusters by their average total_cases rank
    # (cluster with most cases → "High Burden", etc.)
    cluster_means = (
        state_agg.groupby("cluster_id")["total_cases"]
        .mean()
        .sort_values(ascending=False)
        .reset_index()
    )
    burden_labels = ["High Burden", "Medium-High", "Medium-Low", "Low Burden"]
    label_map = {
        row["cluster_id"]: burden_labels[min(i, len(burden_labels) - 1)]
        for i, row in cluster_means.iterrows()
    }
    state_agg["cluster_label"] = state_agg["cluster_id"].map(label_map)

    # Build cluster profile summary
    cluster_profiles = (
        state_agg.groupby(["cluster_id", "cluster_label"])
        .agg(
            state_count      = ("state",          "count"),
            mean_cases       = ("total_cases",    "mean"),
            mean_incidence   = ("avg_incidence",  "mean"),
            mean_cfr         = ("avg_cfr",        "mean"),
        )
        .round(2)
        .reset_index()
    )

    logger.info(
        "State clustering (%s, year=%s): %d clusters across %d states",
        disease, year, n_clusters, len(state_agg),
    )

    return ClusterResult(
        disease          = disease,
        year             = year,
        n_clusters       = n_clusters,
        state_clusters   = state_agg[
            ["state", "cluster_id", "cluster_label",
             "total_cases", "avg_incidence", "avg_cfr"]
        ].sort_values("total_cases", ascending=False),
        cluster_profiles = cluster_profiles,
    )


# ── CFR benchmarking ─────────────────────────────────────────────

def benchmark_cfr(
    df: pd.DataFrame,
    disease: str,
    year: Optional[int] = None,
) -> pd.DataFrame:
    """
    Compare each state's CFR against the national mean and flag
    states with significantly higher mortality rates.

    A state is flagged if its CFR is more than one standard
    deviation above the national mean — a simple but clinically
    meaningful threshold.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: state, disease, cfr_pct.
    disease : str
    year : int, optional

    Returns
    -------
    pd.DataFrame
        Columns: state, avg_cfr, national_mean_cfr,
                 cfr_z_score, flag.
    """
    data = _filter_disease_state(df, disease, state=None)
    if year and "year" in data.columns:
        data = data[data["year"] == year]

    col = _primary_col(data)
    if data.empty or col not in data.columns:
        return pd.DataFrame()

    if "deaths" in data.columns:
        state_agg = data.groupby("state").agg(
            total_cases  = (col,       "sum"),
            total_deaths = ("deaths",  "sum"),
        ).reset_index().fillna(0)
        state_agg["avg_cfr"] = np.where(
            state_agg["total_cases"] > 0,
            (state_agg["total_deaths"] / state_agg["total_cases"] * 100).round(4),
            0.0,
        )
        state_cfr = state_agg[["state", "avg_cfr"]]
    else:
        state_cfr = (
            data.groupby("state")["cfr_pct"]
            .mean()
            .reset_index()
            .rename(columns={"cfr_pct": "avg_cfr"})
        )

    national_mean = state_cfr["avg_cfr"].mean()
    national_std  = state_cfr["avg_cfr"].std()

    state_cfr["national_mean_cfr"] = round(national_mean, 3)
    if national_std > 0:
        state_cfr["cfr_z_score"] = (
            (state_cfr["avg_cfr"] - national_mean) / national_std
        ).round(3)
    else:
        state_cfr["cfr_z_score"] = 0.0
    state_cfr["flag"] = state_cfr["cfr_z_score"].apply(
        lambda z: "HIGH" if z > 1.0 else ("LOW" if z < -1.0 else "NORMAL")
    )

    return state_cfr.sort_values("avg_cfr", ascending=False).reset_index(drop=True)


# ── Internal helpers ─────────────────────────────────────────────

def _filter_disease_state(
    df: pd.DataFrame,
    disease: str,
    state: Optional[str],
) -> pd.DataFrame:
    """
    Filter a DataFrame to the specified disease and optionally a state.

    Parameters
    ----------
    df : pd.DataFrame
    disease : str
    state : str | None

    Returns
    -------
    pd.DataFrame
    """
    data = df[df["disease"] == disease].copy()
    if state:
        data = data[data["state"] == state]
    return data


def _extract_series(
    df: pd.DataFrame,
    disease: str,
    state: Optional[str],
    value_col: str,
) -> pd.Series:
    """
    Extract a time-ordered numeric series for one disease/state.

    If state is None, sums across all states (national series).

    Parameters
    ----------
    df : pd.DataFrame
    disease : str
    state : str | None
    value_col : str

    Returns
    -------
    pd.Series
        Values sorted by report_date. Index is reset.
    """
    data = _filter_disease_state(df, disease, state)

    if state is None:
        # National aggregate — sum all states per date
        data = (
            data.groupby("report_date")[value_col]
            .sum()
            .reset_index()
            .sort_values("report_date")
        )
        return data[value_col].reset_index(drop=True)

    return (
        data.sort_values("report_date")[value_col]
        .reset_index(drop=True)
    )
