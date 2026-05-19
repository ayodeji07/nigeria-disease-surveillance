"""
src/analysis/geospatial.py
────────────────────────────────────────────────────────────────
Geospatial analysis for disease surveillance data.

This module provides spatial analysis functions used in the
geospatial notebooks and the API's /geospatial endpoints.

Methods implemented:
  • Choropleth data preparation   — merge disease burden with state geometries
  • Moran's I spatial autocorrelation — does high burden cluster spatially?
  • Facility accessibility analysis  — which states are underserved?
  • Disease burden index             — composite score across diseases
  • Hotspot detection                — spatial clusters of high burden

All functions that need geopandas import it locally so the rest
of the application works even if geopandas is not installed
(e.g. in a minimal API-only deployment).
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ── Result dataclasses ───────────────────────────────────────────

@dataclass
class MoranResult:
    """
    Outcome of a Moran's I spatial autocorrelation test.

    Moran's I ranges from -1 (perfect dispersion) to +1
    (perfect clustering). Values near 0 indicate randomness.
    """
    disease:        str
    year:           Optional[int]
    morans_i:       float
    expected_i:     float          # Expected I under randomness = -1/(n-1)
    variance:       float
    z_score:        float
    p_value:        float
    significant:    bool
    pattern:        str            # 'clustered' | 'dispersed' | 'random'
    interpretation: str


@dataclass
class AccessibilityResult:
    """
    Health facility accessibility analysis for one state.
    """
    state:              str
    total_facilities:   int
    facilities_per_100k: float
    disease_burden:     float      # avg incidence per 100k
    access_gap_score:   float      # higher = worse access relative to burden
    flag:               str        # 'CRITICAL' | 'POOR' | 'ADEQUATE' | 'GOOD'


# ── Choropleth data preparation ───────────────────────────────────

def prepare_choropleth(
    burden_df: pd.DataFrame,
    states_gdf: "gpd.GeoDataFrame",
    value_col: str = "total_cases",
    state_name_col: str = "state",
) -> "gpd.GeoDataFrame":
    """
    Merge disease burden statistics with state boundary geometries
    to produce a GeoDataFrame ready for choropleth rendering.

    Parameters
    ----------
    burden_df : pd.DataFrame
        State-level aggregated burden data — output of
        repository.get_state_burden() or similar.
        Must contain a column matching state_name_col.
    states_gdf : gpd.GeoDataFrame
        State boundary polygons from extract.extract_shapefiles().
        Must contain a 'state_name' column or similar.
    value_col : str
        The column to visualise in the choropleth.
    state_name_col : str
        Column in burden_df holding canonical state names.

    Returns
    -------
    gpd.GeoDataFrame
        Merged GeoDataFrame with burden statistics and geometry.
        States with no data get NaN values (rendered as grey on maps).
    """
    try:
        import geopandas as gpd
    except ImportError:
        logger.error("geopandas is required for choropleth preparation")
        raise

    if burden_df.empty:
        logger.warning("Empty burden DataFrame passed to prepare_choropleth")
        return states_gdf.copy()

    # Find the state name column in the GeoDataFrame
    geo_name_col = _find_geo_name_column(states_gdf)
    if geo_name_col is None:
        raise ValueError(
            "Cannot find a state name column in the GeoDataFrame. "
            "Expected one of: state_name, statename, name, adm1name."
        )

    # Normalise state names in the GeoDataFrame to match our canonical list
    from src.utils.state_maps import normalise_state_name
    states_gdf = states_gdf.copy()
    states_gdf["_canonical_name"] = states_gdf[geo_name_col].apply(
        normalise_state_name
    )

    # Left join: keep all state geometries, attach burden where available
    merged = states_gdf.merge(
        burden_df.rename(columns={state_name_col: "_canonical_name"}),
        on="_canonical_name",
        how="left",
    )

    # Drop the working column
    merged = merged.drop(columns=["_canonical_name"], errors="ignore")

    missing_data = merged[value_col].isna().sum()
    if missing_data > 0:
        logger.info(
            "%d states have no burden data — will render as grey on map",
            missing_data,
        )

    logger.info(
        "Choropleth prepared: %d states, value_col='%s'",
        len(merged),
        value_col,
    )
    return merged


def geodataframe_to_geojson(
    gdf: "gpd.GeoDataFrame",
    properties: Optional[list[str]] = None,
) -> dict:
    """
    Convert a GeoDataFrame to a GeoJSON FeatureCollection dict.

    The API's choropleth endpoint returns this format, which is
    consumed directly by Folium / Leaflet in the dashboard.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        Must have a valid geometry column.
    properties : list[str], optional
        Columns to include as feature properties.
        Default: all non-geometry columns.

    Returns
    -------
    dict
        A GeoJSON FeatureCollection.
    """
    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}

    import geopandas as gpd

    if properties is None:
        properties = [c for c in gdf.columns if c != gdf.geometry.name]

    features = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        # Convert geometry to GeoJSON dict
        geom_dict = json.loads(geom.__geo_interface__.__str__()
                               if hasattr(geom, '__geo_interface__')
                               else json.dumps(geom.__geo_interface__))

        # Replace NaN with None so JSON serialisation works
        props = {}
        for col in properties:
            val = row.get(col)
            if isinstance(val, float) and np.isnan(val):
                props[col] = None
            elif hasattr(val, 'item'):
                # Convert numpy scalars to native Python types
                props[col] = val.item()
            else:
                props[col] = val

        features.append({
            "type":       "Feature",
            "geometry":   geom_dict,
            "properties": props,
        })

    return {"type": "FeatureCollection", "features": features}


# ── Moran's I spatial autocorrelation ───────────────────────────

def compute_morans_i(
    burden_df: pd.DataFrame,
    states_gdf: "gpd.GeoDataFrame",
    disease: str,
    value_col: str = "total_cases",
    year: Optional[int] = None,
) -> MoranResult:
    """
    Compute Moran's I to test whether high-burden states cluster
    spatially or are randomly distributed across Nigeria.

    Interpretation:
      I > E[I] and p < 0.05 → significant spatial clustering
                               (high-burden states neighbour each other)
      I < E[I] and p < 0.05 → significant spatial dispersion
                               (high-burden states are spread out)
      p ≥ 0.05              → no significant spatial pattern

    This is clinically important: spatial clustering suggests a
    common environmental driver (water source, climate), whereas
    dispersion might point to nationwide systemic issues.

    Parameters
    ----------
    burden_df : pd.DataFrame
        State-level burden data with a 'state' column.
    states_gdf : gpd.GeoDataFrame
        State boundary geometries.
    disease : str
    value_col : str
        Column representing the spatial value (e.g. total_cases).
    year : int, optional

    Returns
    -------
    MoranResult
    """
    try:
        import geopandas as gpd
        from libpysal.weights import Queen
        from esda.moran import Moran
    except ImportError as exc:
        logger.warning(
            "Spatial libraries not available for Moran's I: %s", exc
        )
        return MoranResult(
            disease=disease, year=year,
            morans_i=0.0, expected_i=0.0, variance=0.0,
            z_score=0.0, p_value=1.0, significant=False,
            pattern="unknown",
            interpretation="libpysal / esda not installed.",
        )

    # Filter burden to requested year if provided
    data = burden_df.copy()
    if year and "year" in data.columns:
        data = data[data["year"] == year]

    # Merge burden with geometries
    merged = prepare_choropleth(data, states_gdf, value_col)

    # Drop states with missing values — Moran's I cannot handle NaN
    merged = merged.dropna(subset=[value_col])

    if len(merged) < 4:
        return MoranResult(
            disease=disease, year=year,
            morans_i=0.0, expected_i=0.0, variance=0.0,
            z_score=0.0, p_value=1.0, significant=False,
            pattern="insufficient data",
            interpretation=(
                f"Need at least 4 states with data "
                f"(got {len(merged)})."
            ),
        )

    # Build spatial weights matrix using Queen contiguity
    # (states that share a border or corner are considered neighbours)
    try:
        weights = Queen.from_dataframe(merged, silence_warnings=True)
        weights.transform = "r"   # Row-standardise weights
    except Exception as exc:
        logger.warning("Could not build spatial weights: %s", exc)
        return MoranResult(
            disease=disease, year=year,
            morans_i=0.0, expected_i=0.0, variance=0.0,
            z_score=0.0, p_value=1.0, significant=False,
            pattern="error",
            interpretation=f"Spatial weights error: {exc}",
        )

    # Compute Moran's I
    moran = Moran(merged[value_col].values, weights)

    n           = len(merged)
    expected_i  = -1.0 / (n - 1)
    significant = moran.p_sim < 0.05

    if significant:
        pattern = (
            "clustered"  if moran.I > expected_i
            else "dispersed"
        )
    else:
        pattern = "random"

    interpretation = (
        f"{disease} burden in Nigeria shows a {pattern} spatial pattern "
        f"(I={moran.I:.4f}, E[I]={expected_i:.4f}, "
        f"z={moran.z_sim:.3f}, p={moran.p_sim:.4f}). "
    )
    if significant and pattern == "clustered":
        interpretation += (
            "High-burden states tend to neighbour each other — "
            "suggests a shared environmental or infrastructural driver."
        )
    elif significant and pattern == "dispersed":
        interpretation += (
            "High-burden states are more spread out than expected by chance — "
            "suggests nationwide systemic factors rather than localised drivers."
        )
    else:
        interpretation += (
            "No statistically significant spatial pattern detected — "
            "disease burden is distributed randomly across states."
        )

    return MoranResult(
        disease       = disease,
        year          = year,
        morans_i      = round(float(moran.I), 4),
        expected_i    = round(expected_i, 4),
        variance      = round(float(moran.VI_sim), 6),
        z_score       = round(float(moran.z_sim), 4),
        p_value       = round(float(moran.p_sim), 6),
        significant   = significant,
        pattern       = pattern,
        interpretation = interpretation,
    )


# ── Facility accessibility analysis ─────────────────────────────

def analyse_facility_accessibility(
    burden_df: pd.DataFrame,
    facilities_df: pd.DataFrame,
    population_df: pd.DataFrame,
    disease: str,
) -> list[AccessibilityResult]:
    """
    Identify states where healthcare access is poor relative to
    disease burden — the "healthcare desert" analysis.

    Access gap score = disease burden (incidence) / facilities per 100k
    Higher scores mean more disease per available facility.

    Parameters
    ----------
    burden_df : pd.DataFrame
        State burden — must contain: state, avg_incidence_per_100k.
    facilities_df : pd.DataFrame
        Facility counts — must contain: state, facility_id or count.
    population_df : pd.DataFrame
        Population data — must contain: state, population.
    disease : str

    Returns
    -------
    list[AccessibilityResult]
        One result per state, sorted by access_gap_score descending.
    """
    if burden_df.empty or facilities_df.empty or population_df.empty:
        logger.warning(
            "One or more inputs are empty — accessibility analysis skipped"
        )
        return []

    # Facility counts per state
    if "state" in facilities_df.columns:
        facility_counts = (
            facilities_df.groupby("state")
            .size()
            .reset_index(name="total_facilities")
        )
    else:
        logger.warning("facilities_df has no 'state' column")
        return []

    # Merge: burden + facilities + population
    merged = (
        burden_df[["state", "avg_incidence_per_100k"]]
        .merge(facility_counts, on="state", how="left")
        .merge(population_df[["state", "population"]], on="state", how="left")
    )

    merged["total_facilities"]   = merged["total_facilities"].fillna(0)
    merged["population"]         = merged["population"].fillna(1)

    # Facilities per 100,000 population
    merged["facilities_per_100k"] = (
        merged["total_facilities"] / merged["population"] * 100_000
    ).round(2)

    # Access gap: incidence relative to facility density
    # Avoid division by zero — states with 0 facilities get a very high score
    merged["access_gap_score"] = np.where(
        merged["facilities_per_100k"] > 0,
        merged["avg_incidence_per_100k"] / merged["facilities_per_100k"],
        merged["avg_incidence_per_100k"] * 100,  # large penalty for zero facilities
    ).round(3)

    # Classify access level based on facilities per 100k
    # Thresholds from WHO primary health care benchmarks
    def _classify(row: pd.Series) -> str:
        fac = row["facilities_per_100k"]
        if fac < 0.5:
            return "CRITICAL"
        elif fac < 1.0:
            return "POOR"
        elif fac < 2.0:
            return "ADEQUATE"
        else:
            return "GOOD"

    results = []
    for _, row in merged.iterrows():
        results.append(
            AccessibilityResult(
                state               = row["state"],
                total_facilities    = int(row["total_facilities"]),
                facilities_per_100k = float(row["facilities_per_100k"]),
                disease_burden      = float(row["avg_incidence_per_100k"]),
                access_gap_score    = float(row["access_gap_score"]),
                flag                = _classify(row),
            )
        )

    # Sort by access gap — worst first
    results.sort(key=lambda r: r.access_gap_score, reverse=True)

    critical_count = sum(1 for r in results if r.flag == "CRITICAL")
    logger.info(
        "Accessibility analysis (%s): %d states analysed, "
        "%d flagged as CRITICAL",
        disease, len(results), critical_count,
    )
    return results


def accessibility_to_dataframe(
    results: list[AccessibilityResult],
) -> pd.DataFrame:
    """
    Convert a list of AccessibilityResult objects to a DataFrame.

    Parameters
    ----------
    results : list[AccessibilityResult]

    Returns
    -------
    pd.DataFrame
    """
    if not results:
        return pd.DataFrame()

    return pd.DataFrame(
        [
            {
                "state":               r.state,
                "total_facilities":    r.total_facilities,
                "facilities_per_100k": r.facilities_per_100k,
                "disease_burden":      r.disease_burden,
                "access_gap_score":    r.access_gap_score,
                "flag":                r.flag,
            }
            for r in results
        ]
    )


# ── Disease burden index ─────────────────────────────────────────

def compute_burden_index(
    df: pd.DataFrame,
    diseases: Optional[list[str]] = None,
    year: Optional[int] = None,
    weights: Optional[dict[str, float]] = None,
) -> pd.DataFrame:
    """
    Compute a composite Disease Burden Index (DBI) per state
    by combining normalised incidence rates across multiple diseases.

    The DBI allows a single choropleth to show overall disease
    burden rather than requiring the user to switch between
    individual disease maps.

    Methodology:
      1. For each disease, compute the state's average incidence rate.
      2. Min-max normalise each disease to [0, 1].
      3. Apply optional weights (default: equal weights).
      4. Sum weighted normalised scores → DBI in [0, 1].

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: state, disease, incidence_per_100k.
    diseases : list[str], optional
        Diseases to include. Default: all diseases in the data.
    year : int, optional
        Filter to a specific year.
    weights : dict[str, float], optional
        Per-disease weights. Default: equal weights (1.0 each).
        Example: {'Cholera': 2.0, 'Meningitis': 1.5}

    Returns
    -------
    pd.DataFrame
        Columns: state, burden_index, rank, and one column per disease.
        Sorted by burden_index descending.
    """
    data = df.copy()

    # Guard against a completely empty DataFrame before column access
    if data.empty or "disease" not in data.columns:
        logger.warning("No disease data available for burden index")
        return pd.DataFrame()

    if year and "year" in data.columns:
        data = data[data["year"] == year]

    if diseases:
        data = data[data["disease"].isin(diseases)]

    available_diseases = data["disease"].unique().tolist()
    if not available_diseases:
        logger.warning("No disease data available for burden index")
        return pd.DataFrame()

    if weights is None:
        weights = {d: 1.0 for d in available_diseases}

    # Pivot: one row per state, one column per disease
    pivot = (
        data.groupby(["state", "disease"])["incidence_per_100k"]
        .mean()
        .unstack(fill_value=0)
        .reset_index()
    )

    # Min-max normalise each disease column
    dbi_scores = pd.Series(0.0, index=pivot.index)

    for disease_name in available_diseases:
        if disease_name not in pivot.columns:
            continue

        col     = pivot[disease_name].astype(float)
        col_min = col.min()
        col_max = col.max()

        if col_max > col_min:
            normalised = (col - col_min) / (col_max - col_min)
        else:
            normalised = pd.Series(0.0, index=col.index)

        weight       = weights.get(disease_name, 1.0)
        dbi_scores  += normalised * weight

    # Normalise total DBI to [0, 1]
    total_weight = sum(weights.get(d, 1.0) for d in available_diseases)
    pivot["burden_index"] = (dbi_scores / total_weight).round(4)
    pivot["rank"] = pivot["burden_index"].rank(ascending=False).astype(int)

    result = pivot[["state", "burden_index", "rank"] + list(available_diseases)]
    result = result.sort_values("burden_index", ascending=False).reset_index(drop=True)

    logger.info(
        "Burden index computed: %d states, %d diseases",
        len(result), len(available_diseases),
    )
    return result


# ── Internal helpers ─────────────────────────────────────────────

def _find_geo_name_column(gdf: "gpd.GeoDataFrame") -> Optional[str]:
    """
    Find the column in a GeoDataFrame that holds state names.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame

    Returns
    -------
    str | None
    """
    candidates = ["state_name", "statename", "name", "adm1name",
                  "NAME_1", "admin1Name", "State"]
    lower_map  = {col.lower(): col for col in gdf.columns}

    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None
