"""
src/db/repository.py
────────────────────────────────────────────────────────────────
Data access layer — all SQL queries live here.

The Repository pattern means that no other module (API routes,
analysis scripts, ETL loaders) ever writes raw SQL directly.
They call a function here instead.

Benefits:
  • SQL is tested and versioned in one place.
  • Switching the underlying DB (or mocking it in tests) only
    requires changing this file.
  • API routes stay clean — they call repository functions and
    get back DataFrames or dicts, not cursor results.

Query design:
  • Simple lookups use SQLAlchemy ORM expressions.
  • Complex analytical queries use parameterised raw SQL via
    sqlalchemy.text() — it is clearer and easier to optimise
    than ORM-generated SQL for multi-join aggregations.
  • All queries return pandas DataFrames so callers don't need
    to know whether the result came from an ORM query or raw SQL.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.utils.logger import get_logger

logger = get_logger(__name__)


def _round_expr(col: str, decimals: int, dialect: str) -> str:
    """Return a ROUND() SQL fragment compatible with the active dialect."""
    if dialect == "postgresql":
        return f"ROUND({col}::numeric, {decimals})"
    return f"ROUND({col}, {decimals})"


# ── Surveillance queries ─────────────────────────────────────────

def get_surveillance_records(
    session: Session,
    disease: Optional[str] = None,
    state: Optional[str] = None,
    year: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    quality_flags: Optional[list[str]] = None,
    limit: int = 10_000,
) -> pd.DataFrame:
    """
    Fetch surveillance records with optional filtering.

    This is the primary query used by the API's /surveillance
    endpoint and by the dashboard's chart components.

    Parameters
    ----------
    session : Session
        An active database session.
    disease : str, optional
        Filter to a single disease (exact match).
    state : str, optional
        Filter to a single state (exact match).
    year : int, optional
        Filter to a single year.
    start_date : date, optional
        Filter to records on or after this date.
    end_date : date, optional
        Filter to records on or before this date.
    quality_flags : list[str], optional
        If provided, only return rows matching these flags.
        Default: all quality flags.
    limit : int
        Maximum rows to return. Capped at 50,000 to protect the API.

    Returns
    -------
    pd.DataFrame
        One row per (state, disease, report_date) combination.
    """
    limit = min(limit, 50_000)

    # Build the query dynamically — only add WHERE clauses for
    # parameters that were actually provided.
    where_clauses = ["1=1"]
    params: dict = {"limit": limit}

    if disease:
        where_clauses.append("d.disease_name = :disease")
        params["disease"] = disease

    if state:
        where_clauses.append("s.state_name = :state")
        params["state"] = state

    if year:
        where_clauses.append("dt.year = :year")
        params["year"] = year

    if start_date:
        where_clauses.append("dt.report_date >= :start_date")
        params["start_date"] = start_date

    if end_date:
        where_clauses.append("dt.report_date <= :end_date")
        params["end_date"] = end_date

    if quality_flags:
        # Use ANY() so callers can pass multiple flags
        where_clauses.append("f.data_quality_flag = ANY(:flags)")
        params["flags"] = quality_flags

    where_sql = " AND ".join(where_clauses)

    query = text(f"""
        SELECT
            s.state_name              AS state,
            s.geopolitical_zone       AS zone,
            d.disease_name            AS disease,
            dt.report_date,
            dt.week_number            AS epi_week,
            dt.year,
            dt.season,
            f.suspected_cases,
            f.confirmed_cases,
            f.deaths,
            f.cfr_pct,
            f.incidence_per_100k,
            f.cases_4wk_avg,
            f.pct_change_wow,
            f.data_quality_flag,
            f.data_source
        FROM  fact_disease_surveillance f
        JOIN  dim_states   s  ON f.state_id   = s.state_id
        JOIN  dim_diseases d  ON f.disease_id = d.disease_id
        JOIN  dim_date     dt ON f.date_id    = dt.date_id
        WHERE {where_sql}
        ORDER BY dt.report_date DESC, s.state_name
        LIMIT :limit
    """)

    df = pd.read_sql(query, session.bind, params=params)
    logger.debug(
        "get_surveillance_records: %d rows (disease=%s, state=%s, year=%s)",
        len(df),
        disease,
        state,
        year,
    )
    return df


def get_national_summary(
    session: Session,
    year: Optional[int] = None,
) -> pd.DataFrame:
    """
    Return high-level KPI aggregates per disease.

    Used by the dashboard's National Overview page and the API's
    /stats/summary endpoint.

    Parameters
    ----------
    session : Session
    year : int, optional
        If provided, limit to this year. Default: all years.

    Returns
    -------
    pd.DataFrame
        Columns: disease, total_cases, total_deaths, avg_cfr,
                 peak_week_cases, highest_burden_state.
    """
    year_clause = "AND dt.year = :year" if year else ""
    params: dict = {}
    if year:
        params["year"] = year

    dialect = session.bind.dialect.name
    cfr_expr = _round_expr("AVG(f.cfr_pct)", 2, dialect)

    query = text(f"""
        SELECT
            d.disease_name                          AS disease,
            SUM(f.confirmed_cases)                  AS total_cases,
            SUM(f.deaths)                           AS total_deaths,
            {cfr_expr}                              AS avg_cfr_pct,
            MAX(f.confirmed_cases)                  AS peak_week_cases,
            COUNT(DISTINCT f.state_id)              AS states_affected
        FROM  fact_disease_surveillance f
        JOIN  dim_diseases d  ON f.disease_id = d.disease_id
        JOIN  dim_date     dt ON f.date_id    = dt.date_id
        WHERE 1=1 {year_clause}
        GROUP BY d.disease_name
        ORDER BY total_cases DESC
    """)

    return pd.read_sql(query, session.bind, params=params)


def get_disease_trend(
    session: Session,
    disease: str,
    state: Optional[str] = None,
    freq: str = "weekly",
) -> pd.DataFrame:
    """
    Return a time series of case counts for one disease.

    Parameters
    ----------
    session : Session
    disease : str
        The disease to trend.
    state : str, optional
        If provided, return state-level trend. Default: national.
    freq : str
        "weekly" or "monthly" aggregation.

    Returns
    -------
    pd.DataFrame
        Columns: period, confirmed_cases, deaths, cfr_pct.
    """
    dialect = session.bind.dialect.name  # "postgresql" or "sqlite"

    if freq == "monthly":
        if dialect == "postgresql":
            period_expr = "TO_CHAR(dt.report_date, 'YYYY-MM')"
        else:
            period_expr = "strftime('%Y-%m', dt.report_date)"
        order_expr = period_expr
    else:
        if dialect == "postgresql":
            period_expr = "dt.report_date::text"
        else:
            period_expr = "CAST(dt.report_date AS TEXT)"
        order_expr = "dt.report_date"

    if dialect == "postgresql":
        cfr_expr = "ROUND(AVG(f.cfr_pct)::numeric, 3)"
    else:
        cfr_expr = "ROUND(AVG(f.cfr_pct), 3)"

    state_clause = "AND s.state_name = :state" if state else ""
    params: dict = {"disease": disease}
    if state:
        params["state"] = state

    query = text(f"""
        SELECT
            {period_expr}                        AS period,
            SUM(f.confirmed_cases)               AS confirmed_cases,
            SUM(f.deaths)                        AS deaths,
            {cfr_expr}                           AS cfr_pct,
            AVG(f.incidence_per_100k)            AS avg_incidence
        FROM  fact_disease_surveillance f
        JOIN  dim_diseases d  ON f.disease_id = d.disease_id
        JOIN  dim_states   s  ON f.state_id   = s.state_id
        JOIN  dim_date     dt ON f.date_id    = dt.date_id
        WHERE d.disease_name = :disease
          {state_clause}
        GROUP BY {period_expr}
        ORDER BY {order_expr}
    """)

    return pd.read_sql(query, session.bind, params=params)


def get_state_burden(
    session: Session,
    disease: str,
    year: Optional[int] = None,
) -> pd.DataFrame:
    """
    Return total confirmed cases and incidence by state for a disease.

    Used to populate choropleth maps and hotspot rankings.

    Parameters
    ----------
    session : Session
    disease : str
    year : int, optional

    Returns
    -------
    pd.DataFrame
        Columns: state, zone, total_cases, total_deaths,
                 avg_incidence_per_100k, cfr_pct.
    """
    year_clause = "AND dt.year = :year" if year else ""
    params: dict = {"disease": disease}
    if year:
        params["year"] = year

    dialect = session.bind.dialect.name
    inc_expr = _round_expr("AVG(f.incidence_per_100k)", 2, dialect)
    cfr_expr = _round_expr("AVG(f.cfr_pct)", 3, dialect)

    query = text(f"""
        SELECT
            s.state_name                             AS state,
            s.geopolitical_zone                      AS zone,
            SUM(f.confirmed_cases)                   AS total_cases,
            SUM(f.deaths)                            AS total_deaths,
            {inc_expr}                               AS avg_incidence_per_100k,
            {cfr_expr}                               AS cfr_pct
        FROM  fact_disease_surveillance f
        JOIN  dim_states   s  ON f.state_id   = s.state_id
        JOIN  dim_diseases d  ON f.disease_id = d.disease_id
        JOIN  dim_date     dt ON f.date_id    = dt.date_id
        WHERE d.disease_name = :disease
          {year_clause}
        GROUP BY s.state_name, s.geopolitical_zone
        ORDER BY total_cases DESC
    """)

    return pd.read_sql(query, session.bind, params=params)


def get_hotspots(
    session: Session,
    disease: str,
    year: Optional[int] = None,
    top_n: int = 5,
) -> pd.DataFrame:
    """
    Return the top N states by confirmed case count for a disease.

    Parameters
    ----------
    session : Session
    disease : str
    year : int, optional
    top_n : int
        Number of states to return. Default: 5.

    Returns
    -------
    pd.DataFrame
    """
    burden = get_state_burden(session, disease, year)
    return burden.head(top_n)


def get_choropleth_data(
    session: Session,
    disease: str,
    year: int,
) -> pd.DataFrame:
    """
    Return state burden + geometry for choropleth map rendering.

    The `geometry` column contains the state boundary as GeoJSON
    (via PostGIS ST_AsGeoJSON). Callers combine this into a
    GeoJSON FeatureCollection for the map.

    Parameters
    ----------
    session : Session
    disease : str
    year : int

    Returns
    -------
    pd.DataFrame
        Columns: state, total_cases, avg_incidence_per_100k, geometry_json.
        Returns empty DataFrame on non-PostgreSQL backends (no PostGIS).
    """
    # PostGIS-specific query — falls back gracefully on SQLite
    if session.bind.dialect.name != "postgresql":
        logger.warning(
            "get_choropleth_data requires PostgreSQL + PostGIS. "
            "Current backend: %s. Returning empty DataFrame.",
            session.bind.dialect.name,
        )
        return pd.DataFrame()

    query = text("""
        SELECT
            s.state_name                                    AS state,
            s.geopolitical_zone                             AS zone,
            SUM(f.confirmed_cases)                          AS total_cases,
            SUM(f.deaths)                                   AS total_deaths,
            ROUND(AVG(f.incidence_per_100k)::numeric, 2)    AS avg_incidence_per_100k,
            ST_AsGeoJSON(s.geometry)                        AS geometry_json
        FROM  fact_disease_surveillance f
        JOIN  dim_states   s  ON f.state_id   = s.state_id
        JOIN  dim_diseases d  ON f.disease_id = d.disease_id
        JOIN  dim_date     dt ON f.date_id    = dt.date_id
        WHERE d.disease_name = :disease
          AND dt.year = :year
          AND s.geometry IS NOT NULL
        GROUP BY s.state_name, s.geopolitical_zone, s.geometry
        ORDER BY total_cases DESC
    """)

    return pd.read_sql(
        query, session.bind, params={"disease": disease, "year": year}
    )


def get_health_facilities(
    session: Session,
    state: Optional[str] = None,
    facility_type: Optional[str] = None,
) -> pd.DataFrame:
    """
    Return health facility locations, optionally filtered by state.

    Parameters
    ----------
    session : Session
    state : str, optional
    facility_type : str, optional
        e.g. "Hospital", "PHC", "Clinic"

    Returns
    -------
    pd.DataFrame
        Columns: facility_name, facility_type, ownership,
                 state, lga_name, latitude, longitude.
    """
    where_clauses = ["1=1"]
    params: dict = {}

    if state:
        where_clauses.append("s.state_name = :state")
        params["state"] = state

    if facility_type:
        where_clauses.append("hf.facility_type = :facility_type")
        params["facility_type"] = facility_type

    query = text(f"""
        SELECT
            hf.facility_name,
            hf.facility_type,
            hf.ownership,
            s.state_name  AS state,
            hf.lga_name,
            hf.latitude,
            hf.longitude
        FROM  health_facilities hf
        LEFT JOIN dim_states s ON hf.state_id = s.state_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY s.state_name, hf.facility_name
    """)

    return pd.read_sql(query, session.bind, params=params)


def get_rainfall_correlation_data(
    session: Session,
    disease: str,
) -> pd.DataFrame:
    """
    Return monthly disease cases alongside rainfall for correlation analysis.

    Joins disease counts (aggregated to monthly) with rainfall data
    so the analysis module can compute Spearman correlation.

    Parameters
    ----------
    session : Session
    disease : str

    Returns
    -------
    pd.DataFrame
        Columns: state, year, month, confirmed_cases, rainfall_mm.
    """
    query = text("""
        SELECT
            s.state_name          AS state,
            dt.year,
            dt.month,
            SUM(f.confirmed_cases) AS confirmed_cases,
            AVG(r.rainfall_mm)     AS rainfall_mm
        FROM  fact_disease_surveillance f
        JOIN  dim_diseases d  ON f.disease_id = d.disease_id
        JOIN  dim_states   s  ON f.state_id   = s.state_id
        JOIN  dim_date     dt ON f.date_id    = dt.date_id
        LEFT JOIN rainfall_monthly r
               ON r.state_id = f.state_id
              AND r.year      = dt.year
              AND r.month     = dt.month
        WHERE d.disease_name = :disease
        GROUP BY s.state_name, dt.year, dt.month
        ORDER BY s.state_name, dt.year, dt.month
    """)

    return pd.read_sql(query, session.bind, params={"disease": disease})


# ── ETL / audit queries ──────────────────────────────────────────

def upsert_surveillance_batch(
    session: Session,
    records: list[dict],
) -> int:
    """
    Insert or update a batch of surveillance records.

    Uses PostgreSQL's INSERT ... ON CONFLICT DO UPDATE (upsert)
    so the pipeline is fully idempotent — re-running it with the
    same data does not create duplicates.

    Falls back to a simple INSERT for SQLite (used in testing).

    Parameters
    ----------
    session : Session
    records : list[dict]
        Each dict must contain keys matching the fact table columns.

    Returns
    -------
    int
        Number of rows affected.
    """
    if not records:
        return 0

    dialect = session.bind.dialect.name

    if dialect == "postgresql":
        query = text("""
            INSERT INTO fact_disease_surveillance (
                state_id, disease_id, date_id,
                suspected_cases, confirmed_cases, deaths,
                incidence_per_100k, cfr_pct,
                cases_4wk_avg, pct_change_wow,
                data_source, data_quality_flag
            )
            VALUES (
                :state_id, :disease_id, :date_id,
                :suspected_cases, :confirmed_cases, :deaths,
                :incidence_per_100k, :cfr_pct,
                :cases_4wk_avg, :pct_change_wow,
                :data_source, :data_quality_flag
            )
            ON CONFLICT (state_id, disease_id, date_id)
            DO UPDATE SET
                suspected_cases   = EXCLUDED.suspected_cases,
                confirmed_cases   = EXCLUDED.confirmed_cases,
                deaths            = EXCLUDED.deaths,
                incidence_per_100k = EXCLUDED.incidence_per_100k,
                cfr_pct           = EXCLUDED.cfr_pct,
                cases_4wk_avg     = EXCLUDED.cases_4wk_avg,
                pct_change_wow    = EXCLUDED.pct_change_wow,
                data_source       = EXCLUDED.data_source,
                data_quality_flag = EXCLUDED.data_quality_flag,
                updated_at        = NOW()
        """)
    else:
        # SQLite fallback — used in unit tests
        query = text("""
            INSERT OR REPLACE INTO fact_disease_surveillance (
                state_id, disease_id, date_id,
                suspected_cases, confirmed_cases, deaths,
                incidence_per_100k, cfr_pct,
                data_source, data_quality_flag
            )
            VALUES (
                :state_id, :disease_id, :date_id,
                :suspected_cases, :confirmed_cases, :deaths,
                :incidence_per_100k, :cfr_pct,
                :data_source, :data_quality_flag
            )
        """)

    result = session.execute(query, records)
    session.flush()

    rows_affected = result.rowcount
    logger.debug("Upserted %d surveillance records", rows_affected)
    return rows_affected


def log_pipeline_run(
    session: Session,
    pipeline_name: str,
    status: str,
    records_extracted: int = 0,
    records_loaded: int = 0,
    records_failed: int = 0,
    duration_seconds: float = 0.0,
    error_message: Optional[str] = None,
) -> int:
    """
    Persist the outcome of an ETL pipeline run.

    Parameters
    ----------
    session : Session
    pipeline_name : str
    status : str
        "SUCCESS", "FAILED", or "PARTIAL"
    records_extracted : int
    records_loaded : int
    records_failed : int
    duration_seconds : float
    error_message : str, optional

    Returns
    -------
    int
        The run_id of the new record.
    """
    query = text("""
        INSERT INTO pipeline_runs (
            pipeline_name, status,
            records_extracted, records_loaded, records_failed,
            duration_seconds, error_message
        )
        VALUES (
            :pipeline_name, :status,
            :records_extracted, :records_loaded, :records_failed,
            :duration_seconds, :error_message
        )
        RETURNING run_id
    """)

    result = session.execute(
        query,
        {
            "pipeline_name":     pipeline_name,
            "status":            status,
            "records_extracted": records_extracted,
            "records_loaded":    records_loaded,
            "records_failed":    records_failed,
            "duration_seconds":  duration_seconds,
            "error_message":     error_message,
        },
    )
    session.flush()
    row = result.fetchone()
    run_id = row[0] if row else -1
    logger.info(
        "Pipeline run logged: id=%d name=%s status=%s loaded=%d",
        run_id, pipeline_name, status, records_loaded,
    )
    return run_id


def log_quality_check_results(
    session: Session,
    report_df: pd.DataFrame,
) -> None:
    """
    Persist validation check results to the data_quality_log table.

    Parameters
    ----------
    session : Session
    report_df : pd.DataFrame
        Output of ValidationReport.to_dataframe().
    """
    if report_df.empty:
        return

    report_df.to_sql(
        "data_quality_log",
        session.bind,
        if_exists="append",
        index=False,
        method="multi",
    )
    logger.debug(
        "Logged %d quality check results to data_quality_log", len(report_df)
    )


def get_dimension_id(
    session: Session,
    table: str,
    name_column: str,
    name_value: str,
) -> Optional[int]:
    """
    Return the integer primary key for a dimension table lookup.

    Used by the load layer to resolve dimension keys before inserting
    into the fact table.

    Parameters
    ----------
    session : Session
    table : str
        Dimension table name, e.g. "dim_states".
    name_column : str
        The name column, e.g. "state_name".
    name_value : str
        The value to look up, e.g. "Lagos".

    Returns
    -------
    int | None
        The integer ID, or None if not found.
    """
    # Table and column names cannot be parameterised in SQL —
    # they are validated against a whitelist here to prevent
    # SQL injection through this function.
    allowed_tables  = {"dim_states", "dim_diseases", "dim_date"}
    allowed_columns = {"state_name", "disease_name", "report_date"}

    if table not in allowed_tables:
        raise ValueError(f"Table not allowed: {table!r}")
    if name_column not in allowed_columns:
        raise ValueError(f"Column not allowed: {name_column!r}")

    # Safe to interpolate after whitelist validation
    id_col = {
        "dim_states":   "state_id",
        "dim_diseases": "disease_id",
        "dim_date":     "date_id",
    }[table]

    query = text(f"""
        SELECT {id_col}
        FROM   {table}
        WHERE  {name_column} = :value
        LIMIT  1
    """)

    result = session.execute(query, {"value": name_value}).fetchone()
    return result[0] if result else None
