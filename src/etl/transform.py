"""
src/etl/transform.py
────────────────────────────────────────────────────────────────
Data transformation — the "T" in ETL.

This module takes raw DataFrames (as returned by extract.py) and
produces clean, analysis-ready DataFrames suitable for loading
into the database.

Design principles:
  • Every function is pure: same input always produces same output.
  • Raw DataFrames are never modified in-place — we always work
    on copies.
  • Every cleaning decision is documented in the function that
    implements it, not just in notebook comments.
  • Data quality issues are flagged, not silently dropped.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
import warnings
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.config import (
    INCIDENCE_PER_N,
    SHORT_GAP_WEEKS,
    Diseases,
)
from src.utils.logger import get_logger
from src.utils.state_maps import (
    CANONICAL_STATES,
    CANONICAL_STATE_SET,
    NATIONAL_SENTINEL,
    normalise_state_name,
)

logger = get_logger(__name__)


# ── Column name normalisation ────────────────────────────────────

# Map the many different column names NCDC uses across years to
# a single standard set. Keys are lowercase substrings — if any
# key appears in a column name, the column is renamed to the value.
_COLUMN_KEYWORD_MAP: dict[str, str] = {
    "suspect":    "suspected_cases",
    "confirm":    "confirmed_cases",
    "death":      "deaths",
    "fatal":      "deaths",
    "cfr":        "cfr_raw",
    "case fatality": "cfr_raw",
    "epi week":   "epi_week",
    "epiweek":    "epi_week",
    "week":       "epi_week",
    "year":       "year",
    "state":      "state",
    "lga":        "state",    # Some reports use LGA instead of state
}


def _standardise_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rename columns to the project's standard names.

    This handles the inconsistency in NCDC PDF column headers
    across years (e.g. "Confirmed Cases", "Confirmed", "No. confirmed").

    Parameters
    ----------
    df : pd.DataFrame
        A DataFrame with raw column names from PDF extraction.

    Returns
    -------
    pd.DataFrame
        The same data with standardised column names.
    """
    rename_map: dict[str, str] = {}

    for original_col in df.columns:
        if not isinstance(original_col, str):
            continue
        col_lower = original_col.lower().strip()

        for keyword, standard_name in _COLUMN_KEYWORD_MAP.items():
            if keyword in col_lower:
                # Only map if we haven't already mapped a column to this name
                if standard_name not in rename_map.values():
                    rename_map[original_col] = standard_name
                break

    return df.rename(columns=rename_map)


# ── Numeric cleaning ─────────────────────────────────────────────

def _parse_numeric_column(series: pd.Series) -> pd.Series:
    """
    Convert a messy string column to integers.

    NCDC PDFs often contain:
      - Numbers with commas: "1,234"
      - Dashes for zero: "-"
      - "N/A", "nil", empty strings
      - Floating point strings: "12.0"

    We convert all of the above to integers, with NaN → 0.

    Parameters
    ----------
    series : pd.Series
        Raw string series from a PDF table.

    Returns
    -------
    pd.Series
        Integer series with non-parseable values set to 0.
    """
    cleaned = (
        series.astype(str)
              .str.strip()
              .str.replace(",", "", regex=False)   # 1,234 → 1234
              .str.replace("-", "0", regex=False)   # dash → 0
              .str.replace("n/a", "0", case=False, regex=False)
              .str.replace("nil", "0", case=False, regex=False)
              .str.replace("none", "0", case=False, regex=False)
    )

    numeric = pd.to_numeric(cleaned, errors="coerce")

    # Fill NaN with 0 for count columns — absence of a report
    # does not mean absence of cases, but 0 is our best signal.
    # We flag these rows separately in the quality column.
    return numeric.fillna(0).round(0).astype(int)


# ── Epi-week to date conversion ──────────────────────────────────

def _epiweek_to_date(year: int | float, week: int | float) -> pd.Timestamp:
    """
    Convert an epidemiological year + week number to a calendar date.

    We use ISO week dates (Monday as day 1) which aligns with how
    NCDC reports their epi-weeks.

    Parameters
    ----------
    year : int | float
        The year (e.g. 2022).
    week : int | float
        ISO week number 1–53.

    Returns
    -------
    pd.Timestamp
        The Monday that starts that ISO week, or NaT on failure.
    """
    try:
        return pd.Timestamp.fromisocalendar(int(year), int(week), 1)
    except (ValueError, TypeError):
        return pd.NaT


# ── Disease data cleaning ────────────────────────────────────────

def clean_disease_dataframe(
    df: pd.DataFrame,
    disease_name: str,
) -> pd.DataFrame:
    """
    Full cleaning pipeline for one disease's raw extracted DataFrame.

    Steps applied in order:
      1. Drop completely empty rows
      2. Standardise column names
      3. Standardise state names → canonical form
      4. Drop NATIONAL aggregate rows
      5. Parse numeric columns to integers
      6. Add disease name column
      7. Parse epi-week + year into a date column
      8. Calculate CFR from raw counts
      9. Assign data quality flags

    Parameters
    ----------
    df : pd.DataFrame
        Raw output from extract_ncdc_pdfs().
    disease_name : str
        Canonical disease name (from Diseases constants).

    Returns
    -------
    pd.DataFrame
        Cleaned DataFrame ready for population join and loading.
    """
    if df.empty:
        logger.warning("Received empty DataFrame for %s — skipping", disease_name)
        return pd.DataFrame()

    data = df.copy()
    initial_rows = len(data)

    # ── Step 1: Drop rows that are entirely None/NaN ──────────────
    data.dropna(how="all", inplace=True)

    # ── Step 2: Standardise column names ─────────────────────────
    data = _standardise_column_names(data)

    # ── Step 3: Standardise state names ──────────────────────────
    if "state" not in data.columns:
        logger.warning(
            "No 'state' column found in %s data after renaming. "
            "Columns present: %s",
            disease_name,
            list(data.columns),
        )
        return pd.DataFrame()

    data["state"] = data["state"].apply(normalise_state_name)

    # Collect unknowns before dropping so we can log them
    unknowns = data[
        data["state"].str.startswith("UNKNOWN:", na=False)
    ]["state"].unique()

    if len(unknowns) > 0:
        logger.warning(
            "%s: %d unknown state names found — investigate: %s",
            disease_name,
            len(unknowns),
            ", ".join(unknowns[:10]),  # cap at 10 to avoid log spam
        )

    # ── Step 4: Drop NATIONAL rows and unknowns ───────────────────
    # We keep only rows with a valid canonical state name.
    # National totals distort per-state analysis.
    data = data[data["state"].isin(CANONICAL_STATE_SET)].copy()

    if data.empty:
        logger.warning(
            "%s: no valid state rows remain after filtering", disease_name
        )
        return pd.DataFrame()

    # ── Step 5: Parse numeric columns ────────────────────────────
    numeric_cols = ["suspected_cases", "confirmed_cases", "deaths"]
    for col in numeric_cols:
        if col in data.columns:
            data[col] = _parse_numeric_column(data[col])
        else:
            # Fill missing count columns with 0 so downstream
            # arithmetic never encounters NaN unexpectedly
            data[col] = 0

    # ── Step 6: Add disease name ──────────────────────────────────
    data["disease"] = disease_name

    # ── Step 7: Parse dates ───────────────────────────────────────
    data = _add_date_column(data, disease_name)

    # ── Step 8: Calculate CFR ────────────────────────────────────
    # CFR = deaths / confirmed_cases * 100
    # Guard against division by zero — a state reporting 0 confirmed
    # cases with 1 death is a data quality issue, not 100% CFR.
    data["cfr_pct"] = np.where(
        data["confirmed_cases"] > 0,
        (data["deaths"] / data["confirmed_cases"] * 100).round(4),
        0.0,
    )

    # ── Step 9: Data quality flags ────────────────────────────────
    data["data_quality_flag"] = "CLEAN"

    # Flag rows where confirmed > suspected (data entry error)
    if "suspected_cases" in data.columns:
        data.loc[
            data["confirmed_cases"] > data["suspected_cases"],
            "data_quality_flag",
        ] = "CONFIRMED_EXCEEDS_SUSPECTED"

    # Flag rows with suspiciously high single-week counts
    # (99th percentile × 3 as a heuristic threshold)
    high_threshold = data["confirmed_cases"].quantile(0.99) * 3
    if high_threshold > 0:
        data.loc[
            data["confirmed_cases"] > high_threshold,
            "data_quality_flag",
        ] = "SUSPECT_HIGH_COUNT"

    # Flag missing dates
    if "report_date" in data.columns:
        data.loc[
            data["report_date"].isna(),
            "data_quality_flag",
        ] = "MISSING_DATE"

    logger.info(
        "Cleaned %s: %d → %d rows | flags: %s",
        disease_name,
        initial_rows,
        len(data),
        data["data_quality_flag"].value_counts().to_dict(),
    )

    return data.reset_index(drop=True)


def _add_date_column(df: pd.DataFrame, disease_name: str) -> pd.DataFrame:
    """
    Derive a `report_date` column from epi_week + year columns.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain 'year' column. May contain 'epi_week'.
    disease_name : str
        Used in log messages only.

    Returns
    -------
    pd.DataFrame
        Input DataFrame with a 'report_date' column added.
    """
    data = df.copy()

    if "epi_week" in data.columns and "year" in data.columns:
        data["report_date"] = data.apply(
            lambda row: _epiweek_to_date(row["year"], row["epi_week"]),
            axis=1,
        )
    elif "year" in data.columns:
        # No week information — approximate to January 1 of the year.
        # We flag these rows so analysts know the date is approximate.
        logger.debug(
            "%s: no epi_week column — dates approximated to Jan 1", disease_name
        )
        data["report_date"] = pd.to_datetime(
            data["year"].astype(str).str.strip() + "-01-01",
            errors="coerce",
        )
        # Flag all rows that had their date approximated
        data.loc[
            data["report_date"].notna(),
            "data_quality_flag",
        ] = "DATE_APPROXIMATED"
    else:
        logger.warning(
            "%s: no year or epi_week columns found — report_date will be null",
            disease_name,
        )
        data["report_date"] = pd.NaT

    return data


# ── Gap filling ──────────────────────────────────────────────────

def fill_temporal_gaps(
    df: pd.DataFrame,
    disease: str,
    state: str,
) -> pd.DataFrame:
    """
    Ensure a complete weekly time series for one disease + state.

    Surveillance data often has gaps (holiday weeks, reporting delays).
    We fill them so the time series is regular — required for
    statistical tests and the Prophet forecasting model.

    Strategy:
      - Gaps of 1–SHORT_GAP_WEEKS consecutive weeks: forward-fill.
      - Longer gaps: linear interpolation.
      - All filled values are flagged as "IMPUTED".

    Parameters
    ----------
    df : pd.DataFrame
        Must contain 'report_date' and 'confirmed_cases'.
    disease : str
        Used to restore the disease column after reindexing.
    state : str
        Used to restore the state column after reindexing.

    Returns
    -------
    pd.DataFrame
        A complete weekly series with no missing dates.
    """
    if df.empty or "report_date" not in df.columns:
        return df

    data = df.copy()
    data = data.set_index("report_date").sort_index()

    # Build a complete Monday-frequency date range
    full_range = pd.date_range(
        start=data.index.min(),
        end=data.index.max(),
        freq="W-MON",
    )

    data = data.reindex(full_range)

    # Track which rows were originally missing
    was_missing = data["confirmed_cases"].isna()

    # Count consecutive missing values (for gap-length decisions)
    gap_groups = was_missing.ne(was_missing.shift()).cumsum()
    gap_lengths = was_missing.groupby(gap_groups).cumsum()

    # Forward-fill short gaps
    short_gap_mask = was_missing & (gap_lengths <= SHORT_GAP_WEEKS)
    data.loc[short_gap_mask, "confirmed_cases"] = (
        data["confirmed_cases"].ffill()
    )

    # Interpolate longer gaps
    data["confirmed_cases"] = (
        data["confirmed_cases"]
        .interpolate(method="linear", limit_direction="forward")
        .clip(lower=0)          # Case counts cannot be negative
        .round(0)
    )

    # Convert back to Int64 (nullable integer — tolerates NaN better
    # than numpy int64)
    data["confirmed_cases"] = data["confirmed_cases"].astype("Int64")

    # Restore metadata columns that were lost during reindex
    data["disease"]            = disease
    data["state"]              = state
    data["data_quality_flag"]  = data["data_quality_flag"].fillna("IMPUTED")
    data.loc[was_missing, "data_quality_flag"] = "IMPUTED"

    return data.reset_index().rename(columns={"index": "report_date"})


# ── Population cleaning ──────────────────────────────────────────

def clean_population_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract a clean state → population lookup from raw NBS/WorldPop data.

    The raw file may have many columns. We identify the state and
    population columns heuristically and return a slim two-column
    DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Raw output from extract_population().

    Returns
    -------
    pd.DataFrame
        Columns: state (canonical), population (int).
        37 rows maximum.
    """
    if df.empty:
        return pd.DataFrame(columns=["state", "population"])

    data = df.copy()

    # Identify the state column
    state_col = _find_column(data, ["state", "states", "name", "lga"])
    if state_col is None:
        logger.error(
            "Cannot identify state column in population data. "
            "Columns: %s",
            list(data.columns),
        )
        return pd.DataFrame(columns=["state", "population"])

    # Identify the population column — prefer more recent years
    pop_col = _find_column(
        data,
        ["2023", "2022", "2021", "2020", "population", "pop", "total"],
    )
    if pop_col is None:
        logger.error(
            "Cannot identify population column. Columns: %s",
            list(data.columns),
        )
        return pd.DataFrame(columns=["state", "population"])

    result = data[[state_col, pop_col]].copy()
    result.columns = ["state", "population"]

    # Standardise state names
    result["state"] = result["state"].apply(normalise_state_name)
    result = result[result["state"].isin(CANONICAL_STATE_SET)].copy()

    # Parse population to integer
    result["population"] = (
        result["population"]
        .astype(str)
        .str.replace(",", "", regex=False)
        .pipe(lambda s: pd.to_numeric(s, errors="coerce"))
        .fillna(0)
        .astype(int)
    )

    # Sanity check: Nigerian state populations range from ~600k to ~15M
    suspicious = result[
        (result["population"] < 500_000) | (result["population"] > 20_000_000)
    ]
    if not suspicious.empty:
        logger.warning(
            "Suspicious population values: %s",
            suspicious.set_index("state")["population"].to_dict(),
        )

    logger.info(
        "Population data cleaned: %d states", len(result)
    )
    return result.reset_index(drop=True)


def _find_column(df: pd.DataFrame, keywords: list[str]) -> Optional[str]:
    """
    Return the first column name whose lowercase form contains any keyword.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to search.
    keywords : list[str]
        Substrings to look for in column names (order matters).

    Returns
    -------
    str | None
        The matching column name, or None if not found.
    """
    lower_cols = {col.lower().strip(): col for col in df.columns}
    for keyword in keywords:
        for lower_col, original_col in lower_cols.items():
            if keyword in lower_col:
                return original_col
    return None


# ── Master merge ─────────────────────────────────────────────────

def merge_all_diseases(
    cleaned_disease_map: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """
    Concatenate all cleaned disease DataFrames into one master table.

    Parameters
    ----------
    cleaned_disease_map : dict[str, pd.DataFrame]
        Keys are disease names, values are cleaned DataFrames.

    Returns
    -------
    pd.DataFrame
        A single DataFrame with all diseases combined and
        columns ordered consistently.
    """
    frames = [df for df in cleaned_disease_map.values() if not df.empty]

    if not frames:
        logger.error("No cleaned disease data to merge")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    # Ensure a consistent column order regardless of source
    ordered_columns = [
        "state",
        "disease",
        "report_date",
        "epi_week",
        "year",
        "suspected_cases",
        "confirmed_cases",
        "deaths",
        "cfr_pct",
        "data_quality_flag",
        "_source_file",
    ]

    existing_ordered = [c for c in ordered_columns if c in combined.columns]
    extra_cols = [c for c in combined.columns if c not in existing_ordered]
    combined = combined[existing_ordered + extra_cols]

    combined = combined.sort_values(
        ["disease", "state", "report_date"]
    ).reset_index(drop=True)

    logger.info(
        "Master table: %d rows | %d diseases | %d states",
        len(combined),
        combined["disease"].nunique(),
        combined["state"].nunique(),
    )
    return combined


def add_incidence_rate(
    df: pd.DataFrame,
    population_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge population data and compute incidence rate per 100,000.

    Parameters
    ----------
    df : pd.DataFrame
        Master disease DataFrame with a 'state' column.
    population_df : pd.DataFrame
        Clean population data — columns: state, population.

    Returns
    -------
    pd.DataFrame
        Input DataFrame with 'population' and
        'incidence_per_100k' columns added.
    """
    if population_df.empty:
        logger.warning(
            "No population data available — incidence rates will be null"
        )
        df["population"]        = np.nan
        df["incidence_per_100k"] = np.nan
        return df

    merged = df.merge(
        population_df[["state", "population"]],
        on="state",
        how="left",
    )

    # States with no population match get NaN incidence — honest
    # rather than silently wrong.
    merged["incidence_per_100k"] = np.where(
        (merged["population"] > 0) & (merged["confirmed_cases"].notna()),
        (merged["confirmed_cases"] / merged["population"] * INCIDENCE_PER_N).round(4),
        np.nan,
    )

    missing_pop = merged[merged["population"].isna()]["state"].unique()
    if len(missing_pop) > 0:
        logger.warning(
            "No population data for %d states: %s",
            len(missing_pop),
            ", ".join(missing_pop),
        )

    return merged


def clean_rainfall_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validate and clean the NASA rainfall DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Raw output from extract_nasa_rainfall().

    Returns
    -------
    pd.DataFrame
        Validated rainfall data with -999 fill values replaced by NaN.
    """
    if df.empty:
        return df

    data = df.copy()

    # NASA POWER uses -999 as a fill value for missing data
    data["rainfall_mm"] = data["rainfall_mm"].replace(-999.0, np.nan)

    # Ensure state names are canonical
    data["state"] = data["state"].apply(normalise_state_name)
    data = data[data["state"].isin(CANONICAL_STATE_SET)].copy()

    # Rainfall cannot be negative (barring the -999 fill values above)
    negative_count = (data["rainfall_mm"] < 0).sum()
    if negative_count > 0:
        logger.warning(
            "%d negative rainfall values found — setting to NaN",
            negative_count,
        )
        data.loc[data["rainfall_mm"] < 0, "rainfall_mm"] = np.nan

    logger.info(
        "Rainfall cleaned: %d records, %d states, %d–%d",
        len(data),
        data["state"].nunique(),
        data["year"].min(),
        data["year"].max(),
    )
    return data.reset_index(drop=True)
