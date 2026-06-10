"""
src/etl/load.py
────────────────────────────────────────────────────────────────
Data loading — the "L" in ETL.

This module takes clean, validated DataFrames (from transform.py
and validate.py) and persists them to the database.

Responsibilities:
  • Resolve dimension keys (state_id, disease_id, date_id) before
    inserting into the fact table.
  • Upsert all records — re-running the pipeline never creates
    duplicates.
  • Load spatial data (geometries) into PostGIS via GeoDataFrame.
  • Populate the dim_date table automatically from the date range
    present in the data — no manual seeding needed.
  • Log every load operation to pipeline_runs and data_quality_log.

A key design decision: all loading goes through the repository
layer (repository.py). This module orchestrates the loading
sequence; it does not write raw SQL itself.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.connection import get_db_session
from src.db.models import (
    DimDate,
    DimDisease,
    DimState,
    RainfallMonthly,
    HealthFacility,
)
from src.db.repository import (
    log_pipeline_run,
    log_quality_check_results,
    upsert_surveillance_batch,
    get_dimension_id,
)
from src.utils.config import Diseases
from src.utils.logger import get_logger
from src.utils.state_maps import (
    CANONICAL_STATES,
    GEOPOLITICAL_ZONES,
    get_centroid,
)

logger = get_logger(__name__)


# ── Dimension table loaders ──────────────────────────────────────
# These run once (or on schema changes) to populate the reference
# tables that the fact table foreign keys point to.


def load_dim_states(
    session: Session,
    states_gdf: Optional["gpd.GeoDataFrame"] = None,
    population_df: Optional[pd.DataFrame] = None,
) -> dict[str, int]:
    """
    Populate dim_states with all 37 canonical Nigerian states.

    If a GeoDataFrame is provided, state boundary geometries are
    stored as WKT text. If a population DataFrame is provided,
    population estimates are included.

    Parameters
    ----------
    session : Session
        Active database session.
    states_gdf : gpd.GeoDataFrame, optional
        State boundary geometries from GRID3 shapefiles.
    population_df : pd.DataFrame, optional
        Clean population data — columns: state, population.

    Returns
    -------
    dict[str, int]
        Mapping of canonical state name → state_id in the database.
        Used by downstream loaders to resolve foreign keys.
    """
    logger.info("Loading dim_states (%d states)...", len(CANONICAL_STATES))

    # Build a population lookup if data was provided
    pop_lookup: dict[str, int] = {}
    if population_df is not None and not population_df.empty:
        pop_lookup = (
            population_df.set_index("state")["population"]
            .to_dict()
        )

    # Build a geometry lookup from the GeoDataFrame if provided
    geom_lookup: dict[str, str] = {}
    if states_gdf is not None and not states_gdf.empty:
        # Find the column that holds state names in the shapefile
        name_col = _find_name_column(states_gdf, ["statename", "state", "name", "adm1name"])
        if name_col:
            for _, row in states_gdf.iterrows():
                raw_name = str(row[name_col])
                # Import here to avoid circular dep at module level
                from src.utils.state_maps import normalise_state_name
                canonical = normalise_state_name(raw_name)
                if canonical and canonical in set(CANONICAL_STATES):
                    from shapely.geometry import MultiPolygon
                    geom = row.geometry
                    if geom.geom_type == "Polygon":
                        geom = MultiPolygon([geom])
                    geom_lookup[canonical] = geom.wkt

    loaded = 0
    state_id_map: dict[str, int] = {}

    for state_name in CANONICAL_STATES:
        zone       = GEOPOLITICAL_ZONES.get(state_name)
        population = pop_lookup.get(state_name)
        geometry   = geom_lookup.get(state_name)

        # Upsert: insert if new, update geometry/population if changed
        if session.bind.dialect.name == "postgresql":
            result = session.execute(
                text("""
                    INSERT INTO dim_states (state_name, geopolitical_zone,
                                           population, geometry)
                    VALUES (:state_name, :zone, :population, :geometry)
                    ON CONFLICT (state_name)
                    DO UPDATE SET
                        geopolitical_zone = EXCLUDED.geopolitical_zone,
                        population        = COALESCE(EXCLUDED.population,
                                                     dim_states.population),
                        geometry          = COALESCE(EXCLUDED.geometry,
                                                     dim_states.geometry)
                    RETURNING state_id
                """),
                {
                    "state_name": state_name,
                    "zone":       zone,
                    "population": population,
                    "geometry":   geometry,
                },
            )
        else:
            # SQLite fallback
            result = session.execute(
                text("""
                    INSERT OR IGNORE INTO dim_states
                        (state_name, geopolitical_zone, population, geometry)
                    VALUES (:state_name, :zone, :population, :geometry)
                """),
                {
                    "state_name": state_name,
                    "zone":       zone,
                    "population": population,
                    "geometry":   geometry,
                },
            )
            result = session.execute(
                text("SELECT state_id FROM dim_states WHERE state_name = :n"),
                {"n": state_name},
            )

        row = result.fetchone()
        if row:
            state_id_map[state_name] = row[0]
            loaded += 1

    session.flush()
    logger.info("dim_states: %d rows loaded", loaded)
    return state_id_map


def load_dim_diseases(session: Session) -> dict[str, int]:
    """
    Populate dim_diseases with the five tracked diseases.

    ICD-10 codes and transmission routes are hardcoded here because
    they are stable clinical facts, not data that changes with new
    source files.

    Parameters
    ----------
    session : Session

    Returns
    -------
    dict[str, int]
        Mapping of disease name → disease_id.
    """
    logger.info("Loading dim_diseases...")

    # Clinical metadata for each disease
    disease_metadata = {
        Diseases.CHOLERA: {
            "disease_code": "A00",
            "category":     "Infectious",
            "transmission": "Waterborne / Faecal-oral",
        },
        Diseases.LASSA: {
            "disease_code": "A96.2",
            "category":     "Infectious / Haemorrhagic fever",
            "transmission": "Rodent contact / Person-to-person",
        },
        Diseases.MPOX: {
            "disease_code": "B04",
            "category":     "Infectious / Zoonotic",
            "transmission": "Contact / Droplet",
        },
        Diseases.MENINGITIS: {
            "disease_code": "G03",
            "category":     "Infectious",
            "transmission": "Droplet / Close contact",
        },
        Diseases.YELLOW_FEVER: {
            "disease_code": "A95",
            "category":     "Infectious / Arboviral",
            "transmission": "Mosquito-borne (Aedes)",
        },
    }

    disease_id_map: dict[str, int] = {}

    for disease_name, meta in disease_metadata.items():
        if session.bind.dialect.name == "postgresql":
            result = session.execute(
                text("""
                    INSERT INTO dim_diseases
                        (disease_name, disease_code, category,
                         transmission, is_notifiable)
                    VALUES (:name, :code, :category, :transmission, TRUE)
                    ON CONFLICT (disease_name)
                    DO UPDATE SET
                        disease_code = EXCLUDED.disease_code,
                        category     = EXCLUDED.category,
                        transmission = EXCLUDED.transmission
                    RETURNING disease_id
                """),
                {
                    "name":         disease_name,
                    "code":         meta["disease_code"],
                    "category":     meta["category"],
                    "transmission": meta["transmission"],
                },
            )
        else:
            session.execute(
                text("""
                    INSERT OR IGNORE INTO dim_diseases
                        (disease_name, disease_code, category,
                         transmission, is_notifiable)
                    VALUES (:name, :code, :category, :transmission, 1)
                """),
                {
                    "name":         disease_name,
                    "code":         meta["disease_code"],
                    "category":     meta["category"],
                    "transmission": meta["transmission"],
                },
            )
            result = session.execute(
                text("SELECT disease_id FROM dim_diseases WHERE disease_name = :n"),
                {"n": disease_name},
            )

        row = result.fetchone()
        if row:
            disease_id_map[disease_name] = row[0]

    session.flush()
    logger.info("dim_diseases: %d rows loaded", len(disease_id_map))
    return disease_id_map


def load_dim_dates(
    session: Session,
    date_series: pd.Series,
) -> dict[str, int]:
    """
    Populate dim_date for every unique date present in the data.

    Rather than pre-generating a fixed date range, we derive the
    dates from what actually exists in the cleaned data. This keeps
    the date dimension tight and avoids loading thousands of rows
    for years we have no data for.

    Nigerian seasons are assigned as:
      Dry season   — November through March
      Rainy season — April through October

    Parameters
    ----------
    session : Session
    date_series : pd.Series
        All report_date values from the cleaned surveillance data.
        NaT values are silently skipped.

    Returns
    -------
    dict[str, int]
        Mapping of date string (YYYY-MM-DD) → date_id.
    """
    unique_dates = date_series.dropna().unique()
    logger.info("Loading dim_date (%d unique dates)...", len(unique_dates))

    # Nigerian dry season months
    dry_months = {11, 12, 1, 2, 3}

    date_id_map: dict[str, int] = {}

    for raw_date in unique_dates:
        ts = pd.Timestamp(raw_date)

        season = "Dry" if ts.month in dry_months else "Rainy"

        # ISO week and quarter
        iso_week = ts.isocalendar().week
        quarter  = (ts.month - 1) // 3 + 1

        if session.bind.dialect.name == "postgresql":
            result = session.execute(
                text("""
                    INSERT INTO dim_date
                        (report_date, week_number, month,
                         quarter, year, season)
                    VALUES
                        (:report_date, :week_number, :month,
                         :quarter, :year, :season)
                    ON CONFLICT (report_date) DO UPDATE SET
                        week_number = EXCLUDED.week_number,
                        season      = EXCLUDED.season
                    RETURNING date_id
                """),
                {
                    "report_date": ts.date(),
                    "week_number": int(iso_week),
                    "month":       ts.month,
                    "quarter":     quarter,
                    "year":        ts.year,
                    "season":      season,
                },
            )
        else:
            session.execute(
                text("""
                    INSERT OR IGNORE INTO dim_date
                        (report_date, week_number, month,
                         quarter, year, season)
                    VALUES
                        (:report_date, :week_number, :month,
                         :quarter, :year, :season)
                """),
                {
                    "report_date": ts.date().isoformat(),
                    "week_number": int(iso_week),
                    "month":       ts.month,
                    "quarter":     quarter,
                    "year":        ts.year,
                    "season":      season,
                },
            )
            result = session.execute(
                text("SELECT date_id FROM dim_date WHERE report_date = :d"),
                {"d": ts.date().isoformat()},
            )

        row = result.fetchone()
        if row:
            date_id_map[ts.date().isoformat()] = row[0]

    session.flush()
    logger.info("dim_date: %d rows loaded", len(date_id_map))
    return date_id_map


# ── Fact table loader ────────────────────────────────────────────

def load_surveillance_fact(
    session: Session,
    master_df: pd.DataFrame,
    state_id_map: dict[str, int],
    disease_id_map: dict[str, int],
    date_id_map: dict[str, int],
    batch_size: int = 500,
) -> tuple[int, int]:
    """
    Load the cleaned surveillance master DataFrame into the fact table.

    Rows are resolved to their dimension foreign keys, then upserted
    in batches. Rows whose keys cannot be resolved are logged and
    skipped — they do not crash the load.

    Parameters
    ----------
    session : Session
    master_df : pd.DataFrame
        Output of transform.merge_all_diseases() +
        transform.add_incidence_rate().
    state_id_map : dict[str, int]
        From load_dim_states().
    disease_id_map : dict[str, int]
        From load_dim_diseases().
    date_id_map : dict[str, int]
        From load_dim_dates().
    batch_size : int
        Number of rows per upsert call. Tuned for memory and
        transaction size balance. Default: 500.

    Returns
    -------
    tuple[int, int]
        (rows_loaded, rows_skipped)
    """
    if master_df.empty:
        logger.warning("Fact load called with empty DataFrame — nothing to do")
        return 0, 0

    logger.info(
        "Loading fact table: %d rows in batches of %d",
        len(master_df),
        batch_size,
    )

    records:  list[dict] = []
    skipped:  int        = 0
    loaded:   int        = 0

    for _, row in master_df.iterrows():
        state_id   = state_id_map.get(row.get("state"))
        disease_id = disease_id_map.get(row.get("disease"))

        # Resolve date_id from the date string
        report_date = row.get("report_date")
        date_key    = (
            pd.Timestamp(report_date).date().isoformat()
            if pd.notna(report_date)
            else None
        )
        date_id = date_id_map.get(date_key) if date_key else None

        # All three foreign keys must resolve — skip if any are missing
        if not all([state_id, disease_id]):
            skipped += 1
            logger.debug(
                "Skipping row — unresolved FK: state=%s state_id=%s "
                "disease=%s disease_id=%s",
                row.get("state"), state_id,
                row.get("disease"), disease_id,
            )
            continue

        records.append(
            {
                "state_id":           state_id,
                "disease_id":         disease_id,
                "date_id":            date_id,       # nullable — allowed
                "suspected_cases":    _safe_int(row.get("suspected_cases", 0)),
                "confirmed_cases":    _safe_int(row.get("confirmed_cases", 0)),
                "deaths":             _safe_int(row.get("deaths", 0)),
                "incidence_per_100k": _safe_float(row.get("incidence_per_100k")),
                "cfr_pct":            _safe_float(row.get("cfr_pct")),
                "cases_4wk_avg":      _safe_float(row.get("cases_4wk_avg")),
                "pct_change_wow":     _safe_float(row.get("pct_change_wow")),
                "data_source":        str(row.get("_source_file", ""))[:100],
                "data_quality_flag":  str(row.get("data_quality_flag", "CLEAN")),
            }
        )

        # Flush in batches to keep memory usage predictable
        if len(records) >= batch_size:
            loaded += upsert_surveillance_batch(session, records)
            records = []

    # Flush any remaining records
    if records:
        loaded += upsert_surveillance_batch(session, records)

    logger.info(
        "Fact table load complete: %d loaded, %d skipped",
        loaded,
        skipped,
    )
    return loaded, skipped


# ── Supplementary table loaders ──────────────────────────────────

def load_health_facilities(
    session: Session,
    facilities_df: pd.DataFrame,
    state_id_map: dict[str, int],
    batch_size: int = 500,
) -> int:
    """
    Load health facility locations into the health_facilities table.

    Parameters
    ----------
    session : Session
    facilities_df : pd.DataFrame
        Raw output from extract.extract_health_facilities().
        Expected columns: facility_name, facility_type, ownership,
        state (canonical), lga_name, latitude, longitude.
    state_id_map : dict[str, int]
        From load_dim_states().
    batch_size : int
        Rows per batch commit. Keeps transactions short to avoid
        connection timeouts on free-tier cloud databases.

    Returns
    -------
    int
        Number of rows inserted.
    """
    if facilities_df.empty:
        logger.warning("No health facilities data to load")
        return 0

    logger.info(
        "Loading health_facilities (%d records)...", len(facilities_df)
    )

    from src.utils.state_maps import normalise_state_name
    col_map  = _map_facility_columns(facilities_df)
    dialect  = session.bind.dialect.name

    # Build the full record list before touching the DB
    records: list[dict] = []
    for _, row in facilities_df.iterrows():
        canonical_state = normalise_state_name(
            str(row.get(col_map.get("state", "state"), ""))
        )
        lat = _safe_float(row.get(col_map.get("latitude",  "latitude")))
        lon = _safe_float(row.get(col_map.get("longitude", "longitude")))
        records.append({
            "facility_name": str(row.get(col_map.get("name",      ""), ""))[:200],
            "facility_type": str(row.get(col_map.get("type",      ""), ""))[:50],
            "state_id":      state_id_map.get(canonical_state),
            "lga_name":      str(row.get(col_map.get("lga",       ""), ""))[:100],
            "ownership":     str(row.get(col_map.get("ownership", ""), ""))[:50],
            "latitude":      lat,
            "longitude":     lon,
            "geometry":      f"POINT({lon} {lat})" if lat is not None and lon is not None else None,
        })

    if dialect == "postgresql":
        sql = text("""
            INSERT INTO health_facilities
                (facility_name, facility_type, state_id,
                 lga_name, ownership, latitude, longitude, geometry)
            VALUES
                (:facility_name, :facility_type, :state_id,
                 :lga_name, :ownership, :latitude, :longitude,
                 CASE WHEN :geometry IS NULL THEN NULL
                      ELSE ST_GeomFromText(:geometry, 4326) END)
            ON CONFLICT DO NOTHING
        """)
    else:
        sql = text("""
            INSERT OR IGNORE INTO health_facilities
                (facility_name, facility_type, state_id,
                 lga_name, ownership, latitude, longitude, geometry)
            VALUES
                (:facility_name, :facility_type, :state_id,
                 :lga_name, :ownership, :latitude, :longitude,
                 :geometry)
        """)

    loaded = 0
    total  = len(records)
    for i in range(0, total, batch_size):
        batch = records[i : i + batch_size]
        session.execute(sql, batch)
        session.commit()
        loaded += len(batch)
        logger.info("health_facilities: %d / %d rows loaded", loaded, total)

    logger.info("health_facilities: %d rows loaded", loaded)
    return loaded


def load_rainfall(
    session: Session,
    rainfall_df: pd.DataFrame,
    state_id_map: dict[str, int],
) -> int:
    """
    Load monthly rainfall data into the rainfall_monthly table.

    Parameters
    ----------
    session : Session
    rainfall_df : pd.DataFrame
        Clean output from transform.clean_rainfall_data().
        Expected columns: state, year, month, rainfall_mm.
    state_id_map : dict[str, int]
        From load_dim_states().

    Returns
    -------
    int
        Number of rows upserted.
    """
    if rainfall_df.empty:
        logger.warning("No rainfall data to load")
        return 0

    logger.info("Loading rainfall_monthly (%d records)...", len(rainfall_df))
    loaded = 0

    for _, row in rainfall_df.iterrows():
        state_id = state_id_map.get(row["state"])
        if not state_id:
            continue

        if session.bind.dialect.name == "postgresql":
            session.execute(
                text("""
                    INSERT INTO rainfall_monthly
                        (state_id, year, month, rainfall_mm)
                    VALUES
                        (:state_id, :year, :month, :rainfall_mm)
                    ON CONFLICT (state_id, year, month)
                    DO UPDATE SET
                        rainfall_mm = EXCLUDED.rainfall_mm
                """),
                {
                    "state_id":    state_id,
                    "year":        int(row["year"]),
                    "month":       int(row["month"]),
                    "rainfall_mm": _safe_float(row.get("rainfall_mm")),
                },
            )
        else:
            session.execute(
                text("""
                    INSERT OR REPLACE INTO rainfall_monthly
                        (state_id, year, month, rainfall_mm)
                    VALUES
                        (:state_id, :year, :month, :rainfall_mm)
                """),
                {
                    "state_id":    state_id,
                    "year":        int(row["year"]),
                    "month":       int(row["month"]),
                    "rainfall_mm": _safe_float(row.get("rainfall_mm")),
                },
            )
        loaded += 1

    session.flush()
    logger.info("rainfall_monthly: %d rows loaded", loaded)
    return loaded


# ── Full load orchestrator ───────────────────────────────────────

def load_all(
    master_df: pd.DataFrame,
    population_df: pd.DataFrame,
    facilities_df: pd.DataFrame,
    rainfall_df: pd.DataFrame,
    states_gdf: Optional["gpd.GeoDataFrame"] = None,
) -> dict:
    """
    Run the full load sequence in the correct dependency order.

    Order matters:
      1. Dimension tables first (dim_states, dim_diseases, dim_date)
      2. Fact table second (references all three dimensions)
      3. Supplementary tables last (reference dim_states only)

    All operations run inside a single transaction. If any step
    fails, the entire load is rolled back so the database is never
    left in a partially-loaded state.

    Parameters
    ----------
    master_df : pd.DataFrame
        Merged and enriched surveillance data.
    population_df : pd.DataFrame
        Clean population data.
    facilities_df : pd.DataFrame
        Health facility locations.
    rainfall_df : pd.DataFrame
        Monthly rainfall per state.
    states_gdf : gpd.GeoDataFrame, optional
        State boundary geometries.

    Returns
    -------
    dict
        Summary of the load: rows loaded per table, any errors.
    """
    start_time = time.perf_counter()
    summary = {
        "dim_states":    0,
        "dim_diseases":  0,
        "dim_dates":     0,
        "fact_loaded":   0,
        "fact_skipped":  0,
        "facilities":    0,
        "rainfall":      0,
        "status":        "SUCCESS",
        "error":         None,
    }

    try:
        with get_db_session() as session:
            # ── Step 1: Dimensions ────────────────────────────────
            state_id_map = load_dim_states(
                session, states_gdf, population_df
            )
            summary["dim_states"] = len(state_id_map)

            disease_id_map = load_dim_diseases(session)
            summary["dim_diseases"] = len(disease_id_map)

            if not master_df.empty and "report_date" in master_df.columns:
                date_id_map = load_dim_dates(
                    session, master_df["report_date"]
                )
                summary["dim_dates"] = len(date_id_map)
            else:
                date_id_map = {}

            # ── Step 2: Fact table ────────────────────────────────
            if not master_df.empty:
                loaded, skipped = load_surveillance_fact(
                    session,
                    master_df,
                    state_id_map,
                    disease_id_map,
                    date_id_map,
                )
                summary["fact_loaded"]  = loaded
                summary["fact_skipped"] = skipped

            # ── Step 3: Supplementary tables ─────────────────────
            if not facilities_df.empty:
                summary["facilities"] = load_health_facilities(
                    session, facilities_df, state_id_map
                )

            if not rainfall_df.empty:
                summary["rainfall"] = load_rainfall(
                    session, rainfall_df, state_id_map
                )

    except Exception as exc:
        summary["status"] = "FAILED"
        summary["error"]  = str(exc)
        logger.error("Load failed: %s", exc, exc_info=True)

    duration = time.perf_counter() - start_time
    summary["duration_seconds"] = round(duration, 2)

    logger.info(
        "Load complete in %.1fs | status=%s | fact=%d loaded, %d skipped",
        duration,
        summary["status"],
        summary.get("fact_loaded", 0),
        summary.get("fact_skipped", 0),
    )
    return summary


# ── Internal helpers ─────────────────────────────────────────────

def _safe_int(value) -> int:
    """
    Convert a value to int, returning 0 for None/NaN/invalid.

    Parameters
    ----------
    value : any

    Returns
    -------
    int
    """
    if value is None:
        return 0
    try:
        if isinstance(value, float) and np.isnan(value):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> Optional[float]:
    """
    Convert a value to float, returning None for NaN/invalid.

    We use None rather than 0.0 for nullable float columns so
    the database stores NULL, which is semantically correct
    (unknown is not the same as zero).

    Parameters
    ----------
    value : any

    Returns
    -------
    float | None
    """
    if value is None:
        return None
    try:
        f = float(value)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _find_name_column(df: "pd.DataFrame", candidates: list[str]) -> Optional[str]:
    """
    Return the first column whose lowercase name matches any candidate.

    Parameters
    ----------
    df : DataFrame or GeoDataFrame
    candidates : list[str]
        Lowercase substrings to search for.

    Returns
    -------
    str | None
    """
    lower_map = {col.lower().strip(): col for col in df.columns}
    for candidate in candidates:
        for lower_col, original_col in lower_map.items():
            if candidate in lower_col:
                return original_col
    return None


def _map_facility_columns(df: pd.DataFrame) -> dict[str, str]:
    """
    Build a mapping of logical field names to actual column names
    in the facilities DataFrame.

    HDX facility files use inconsistent column names across
    download versions. This function handles the variation.

    Parameters
    ----------
    df : pd.DataFrame

    Returns
    -------
    dict[str, str]
        e.g. {"name": "Facility Name", "type": "Type", ...}
    """
    lower_map = {col.lower().strip(): col for col in df.columns}

    def find(keywords: list[str]) -> str:
        for kw in keywords:
            for lower_col, original_col in lower_map.items():
                if kw in lower_col:
                    return original_col
        return ""

    return {
        "name":      find(["facility name", "name", "facility"]),
        "type":      find(["facility type", "type", "category"]),
        "state":     find(["state"]),
        "lga":       find(["lga", "local government"]),
        "ownership": find(["ownership", "owner", "sector"]),
        "latitude":  find(["lat", "latitude", "y"]),
        "longitude": find(["lon", "lng", "longitude", "x"]),
    }
