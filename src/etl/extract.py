"""
src/etl/extract.py
────────────────────────────────────────────────────────────────
Data extraction — the "E" in ETL.

Each function in this module is responsible for pulling raw data
from exactly one source and returning it as a pandas DataFrame
(or GeoDataFrame for spatial data).

Rules enforced here:
  • No cleaning happens in extractors. Raw data is returned as-is.
  • Every extractor saves its output to data/raw/ so re-runs are
    idempotent — we never need to re-download if the file exists.
  • If a source is unavailable, we log a warning and return an
    empty DataFrame so the pipeline can continue gracefully.
  • All public functions accept a `force_download` flag. When
    False (default), a cached file in data/raw/ is used.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from src.utils.config import (
    DATA_END_YEAR,
    DATA_START_YEAR,
    NASA_API_DELAY_SECONDS,
    Paths,
    settings,
)
from src.utils.logger import get_logger
from src.utils.state_maps import CANONICAL_STATES, STATE_CENTROIDS

logger = get_logger(__name__)


# ── Internal helpers ─────────────────────────────────────────────

def _save_raw(df: pd.DataFrame, filename: str) -> Path:
    """
    Save a DataFrame to data/raw/ as CSV and return the path.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to persist.
    filename : str
        Target filename, e.g. "ncdc_cholera_raw.csv".

    Returns
    -------
    Path
        The full path where the file was saved.
    """
    Paths.raw.mkdir(parents=True, exist_ok=True)
    dest = Paths.raw / filename
    df.to_csv(dest, index=False)
    logger.info("Saved %d rows to %s", len(df), dest.name)
    return dest


def _load_cached(filename: str) -> Optional[pd.DataFrame]:
    """
    Return a cached CSV from data/raw/ if it exists, else None.

    Parameters
    ----------
    filename : str
        The filename to look for in data/raw/.

    Returns
    -------
    pd.DataFrame | None
    """
    path = Paths.raw / filename
    if path.exists():
        df = pd.read_csv(path)
        logger.info("Loaded cached file %s (%d rows)", filename, len(df))
        return df
    return None


# ── NCDC PDF extractor ───────────────────────────────────────────

def extract_ncdc_pdfs(
    disease_folder: Path,
    disease_name: str,
    force_download: bool = False,
) -> pd.DataFrame:
    """
    Extract all tables from NCDC Situation Report PDFs for one disease.

    NCDC publishes weekly PDF reports at ncdc.gov.ng/diseases/sitreps.
    Each PDF typically contains one or more tables with state-level
    case counts. The tables are inconsistently formatted across years,
    so we extract everything and leave clean-up to transform.py.

    Parameters
    ----------
    disease_folder : Path
        Directory containing the downloaded PDF files, e.g.
        data/raw/ncdc_pdfs/cholera/.
    disease_name : str
        Human-readable label added as a column, e.g. "Cholera".
    force_download : bool
        Unused here (PDFs are manually downloaded). Kept for API
        consistency with other extractors.

    Returns
    -------
    pd.DataFrame
        Combined raw table data from all PDFs, with provenance
        columns (_source_file, _page_number, _table_index) so
        any row can be traced back to its source.
    """
    cache_file = f"ncdc_{disease_name.lower().replace(' ', '_')}_raw.csv"

    if not force_download:
        cached = _load_cached(cache_file)
        if cached is not None:
            return cached

    try:
        import pdfplumber
    except ImportError:
        logger.error(
            "pdfplumber is not installed. Run: pip install pdfplumber"
        )
        return pd.DataFrame()

    if not disease_folder.exists():
        logger.warning(
            "PDF folder not found: %s. "
            "Download NCDC sitreps and place them there.",
            disease_folder,
        )
        return pd.DataFrame()

    pdf_files = sorted(disease_folder.glob("*.pdf"))
    if not pdf_files:
        logger.warning("No PDF files found in %s", disease_folder)
        return pd.DataFrame()

    logger.info(
        "Extracting %d PDFs for %s", len(pdf_files), disease_name
    )

    all_rows: list[pd.DataFrame] = []

    for pdf_path in pdf_files:
        rows_from_file = _extract_single_pdf(pdf_path, disease_name)
        if not rows_from_file.empty:
            all_rows.append(rows_from_file)

    if not all_rows:
        logger.warning("No tables extracted for %s", disease_name)
        return pd.DataFrame()

    combined = pd.concat(all_rows, ignore_index=True)
    _save_raw(combined, cache_file)
    return combined


def _extract_single_pdf(pdf_path: Path, disease_name: str) -> pd.DataFrame:
    """
    Extract all tables from one PDF file.

    Parameters
    ----------
    pdf_path : Path
        Absolute path to the PDF.
    disease_name : str
        Added as a column to every extracted row.

    Returns
    -------
    pd.DataFrame
        All tables from this PDF concatenated into one DataFrame.
    """
    import pdfplumber

    tables_found: list[pd.DataFrame] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            logger.debug(
                "  Reading %s (%d pages)", pdf_path.name, len(pdf.pages)
            )
            for page_num, page in enumerate(pdf.pages, start=1):
                raw_tables = page.extract_tables()
                if not raw_tables:
                    continue

                for table_idx, raw_table in enumerate(raw_tables):
                    # Skip tables that are too small to be meaningful
                    if not raw_table or len(raw_table) < 2:
                        continue

                    header = raw_table[0]
                    rows = raw_table[1:]

                    # Guard against rows with wrong column count
                    consistent_rows = [
                        r for r in rows if len(r) == len(header)
                    ]
                    if not consistent_rows:
                        continue

                    df = pd.DataFrame(consistent_rows, columns=header)
                    df["_disease"]      = disease_name
                    df["_source_file"]  = pdf_path.name
                    df["_page_number"]  = page_num
                    df["_table_index"]  = table_idx
                    tables_found.append(df)

    except Exception as exc:
        # Log but don't crash — one bad PDF shouldn't stop the pipeline
        logger.error("Failed to read %s: %s", pdf_path.name, exc)

    if not tables_found:
        return pd.DataFrame()

    return pd.concat(tables_found, ignore_index=True)


# ── WHO AFRO extractor ───────────────────────────────────────────

def extract_who_data(force_download: bool = False) -> pd.DataFrame:
    """
    Load WHO AFRO disease surveillance files from data/raw/who/.

    WHO data serves as a cross-validation source for NCDC figures.
    Supported formats: CSV, Excel (.xlsx, .xls).

    Parameters
    ----------
    force_download : bool
        When False, returns a cached version if available.

    Returns
    -------
    pd.DataFrame
        All WHO files concatenated with a _source_file column.
    """
    cache_file = "who_raw.csv"

    if not force_download:
        cached = _load_cached(cache_file)
        if cached is not None:
            return cached

    who_dir = Paths.raw / "who"
    if not who_dir.exists():
        logger.warning(
            "WHO data directory not found: %s. "
            "Download files from afro.who.int and place them there.",
            who_dir,
        )
        return pd.DataFrame()

    dfs: list[pd.DataFrame] = []

    for csv_file in sorted(who_dir.glob("*.csv")):
        try:
            df = pd.read_csv(csv_file)
            df["_source_file"] = csv_file.name
            dfs.append(df)
            logger.debug("  Loaded WHO CSV: %s (%d rows)", csv_file.name, len(df))
        except Exception as exc:
            logger.error("Could not read %s: %s", csv_file.name, exc)

    for excel_file in sorted(who_dir.glob("*.xlsx")):
        try:
            df = pd.read_excel(excel_file, engine="openpyxl")
            df["_source_file"] = excel_file.name
            dfs.append(df)
            logger.debug("  Loaded WHO Excel: %s (%d rows)", excel_file.name, len(df))
        except Exception as exc:
            logger.error("Could not read %s: %s", excel_file.name, exc)

    if not dfs:
        logger.warning("No WHO files found in %s", who_dir)
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    _save_raw(combined, cache_file)
    return combined


# ── NASA POWER rainfall extractor ────────────────────────────────

def extract_nasa_rainfall(
    start_year: int = DATA_START_YEAR,
    end_year: int = DATA_END_YEAR,
    force_download: bool = False,
) -> pd.DataFrame:
    """
    Fetch monthly precipitation data for all 37 states via NASA POWER API.

    NASA POWER (Prediction of Worldwide Energy Resources) provides free
    meteorological data. We use the monthly precipitation parameter
    "PRECTOTCORR" (precipitation corrected, mm/month) at each state's
    centroid coordinate.

    No API key is required. Rate limit: ~30 requests/minute.
    We insert a short delay between requests to be a good citizen.

    Parameters
    ----------
    start_year : int
        First year to fetch (inclusive).
    end_year : int
        Last year to fetch (inclusive).
    force_download : bool
        When False, return cached data if it exists.

    Returns
    -------
    pd.DataFrame
        Columns: state, year, month, rainfall_mm, latitude, longitude.
    """
    cache_file = "rainfall_raw.csv"

    if not force_download:
        cached = _load_cached(cache_file)
        if cached is not None:
            return cached

    logger.info(
        "Fetching NASA POWER rainfall for %d states (%d–%d)...",
        len(STATE_CENTROIDS),
        start_year,
        end_year,
    )

    all_records: list[pd.DataFrame] = []
    failed_states: list[str] = []

    for i, (state, (lat, lon)) in enumerate(STATE_CENTROIDS.items(), start=1):
        logger.info("  [%d/%d] %s", i, len(STATE_CENTROIDS), state)

        df = _fetch_one_state_rainfall(state, lat, lon, start_year, end_year)

        if df.empty:
            failed_states.append(state)
        else:
            all_records.append(df)

        # Be respectful of the API rate limit
        time.sleep(NASA_API_DELAY_SECONDS)

    if failed_states:
        logger.warning(
            "Rainfall fetch failed for %d states: %s",
            len(failed_states),
            ", ".join(failed_states),
        )

    if not all_records:
        logger.error("No rainfall data retrieved — check network access.")
        return pd.DataFrame()

    combined = pd.concat(all_records, ignore_index=True)
    _save_raw(combined, cache_file)
    logger.info(
        "Rainfall extraction complete: %d records across %d states",
        len(combined),
        combined["state"].nunique(),
    )
    return combined


def _fetch_one_state_rainfall(
    state: str,
    lat: float,
    lon: float,
    start_year: int,
    end_year: int,
) -> pd.DataFrame:
    """
    Fetch monthly rainfall for a single state from the NASA POWER API.

    Parameters
    ----------
    state : str
        Canonical state name — used as a label in the output.
    lat : float
        Centroid latitude in decimal degrees.
    lon : float
        Centroid longitude in decimal degrees.
    start_year : int
        First year.
    end_year : int
        Last year.

    Returns
    -------
    pd.DataFrame
        Columns: state, year, month, rainfall_mm, latitude, longitude.
        Returns empty DataFrame on any failure.
    """
    url = (
        f"https://power.larc.nasa.gov/api/temporal/monthly/point"
        f"?parameters=PRECTOTCORR"
        f"&community=AG"
        f"&longitude={lon}"
        f"&latitude={lat}"
        f"&start={start_year}"
        f"&end={end_year}"
        f"&format=JSON"
    )

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        payload = response.json()

        # The API nests data under properties → parameter → PRECTOTCORR
        # Keys are in "YYYYMM" string format, values are mm/month floats
        rain_dict: dict[str, float] = (
            payload["properties"]["parameter"]["PRECTOTCORR"]
        )

        records = []
        for year_month_str, rainfall_mm in rain_dict.items():
            records.append(
                {
                    "state":       state,
                    "year":        int(year_month_str[:4]),
                    "month":       int(year_month_str[4:]),
                    "rainfall_mm": rainfall_mm,
                    "latitude":    lat,
                    "longitude":   lon,
                }
            )

        return pd.DataFrame(records)

    except requests.exceptions.Timeout:
        logger.warning("NASA API timed out for %s — skipping", state)
    except requests.exceptions.HTTPError as exc:
        logger.warning("NASA API HTTP error for %s: %s", state, exc)
    except (KeyError, ValueError) as exc:
        logger.warning("NASA API unexpected response for %s: %s", state, exc)
    except Exception as exc:
        logger.error("Unexpected error fetching rainfall for %s: %s", state, exc)

    return pd.DataFrame()


# ── HDX health facilities extractor ──────────────────────────────

def extract_health_facilities(force_download: bool = False) -> pd.DataFrame:
    """
    Load Nigeria health facility locations from a local file.

    Source: Humanitarian Data Exchange (data.humdata.org)
    Search term: "Nigeria health facilities"
    Download the CSV and place it at: data/raw/health_facilities.csv

    The file contains facility names, types (hospital, PHC, clinic),
    ownership (Federal, State, Private), LGA, state, and coordinates.

    Parameters
    ----------
    force_download : bool
        Unused — facilities data is manually downloaded.

    Returns
    -------
    pd.DataFrame
        Raw facilities data with _source_file column.
    """
    cache_file = "health_facilities.csv"
    path = Paths.raw / cache_file

    if not path.exists():
        logger.warning(
            "Health facilities file not found at %s. "
            "Download from data.humdata.org (search 'Nigeria health facilities') "
            "and save as data/raw/health_facilities.csv.",
            path,
        )
        return pd.DataFrame()

    try:
        df = pd.read_csv(path)
        df["_source_file"] = cache_file
        logger.info(
            "Loaded health facilities: %d records, %d columns",
            len(df),
            df.shape[1],
        )
        return df
    except Exception as exc:
        logger.error("Could not load health facilities: %s", exc)
        return pd.DataFrame()


# ── NBS population extractor ─────────────────────────────────────

def extract_population(force_download: bool = False) -> pd.DataFrame:
    """
    Load Nigeria state population estimates.

    Source: National Bureau of Statistics (nigerianstat.gov.ng)
    or WorldPop (worldpop.org). Download an Excel or CSV file with
    at minimum two columns: state name and population estimate.

    Place the file at: data/raw/nigeria_population.xlsx (or .csv)

    Parameters
    ----------
    force_download : bool
        Unused — population data is manually downloaded.

    Returns
    -------
    pd.DataFrame
        Raw population data with _source_file column.
    """
    # Try Excel first, then CSV
    for filename in ("nigeria_population.xlsx", "nigeria_population.csv"):
        path = Paths.raw / filename
        if not path.exists():
            continue

        try:
            if filename.endswith(".xlsx"):
                df = pd.read_excel(path, engine="openpyxl")
            else:
                df = pd.read_csv(path)

            df["_source_file"] = filename
            logger.info(
                "Loaded population data: %d rows from %s", len(df), filename
            )
            return df

        except Exception as exc:
            logger.error("Could not load %s: %s", filename, exc)

    logger.warning(
        "Population file not found. Expected: data/raw/nigeria_population.xlsx "
        "Download from nigerianstat.gov.ng or worldpop.org."
    )
    return pd.DataFrame()


# ── GRID3 shapefile extractor ────────────────────────────────────

def extract_shapefiles() -> dict[str, "gpd.GeoDataFrame"]:
    """
    Load Nigeria state and LGA boundary shapefiles.

    Source: GRID3 Nigeria (grid3.org) or GADM (gadm.org/country/NGA)
    Download and extract into: data/shapefiles/

    Expected files:
      - data/shapefiles/nigeria_states.shp  (state boundaries)
      - data/shapefiles/nigeria_lgas.shp    (LGA boundaries, optional)

    All geometries are reprojected to WGS84 (EPSG:4326) which is
    what PostGIS and Folium/Leaflet maps expect.

    Returns
    -------
    dict[str, gpd.GeoDataFrame]
        Keys: "states", "lgas" (if available).
        Returns empty dict if geopandas is not installed.
    """
    try:
        import geopandas as gpd
    except ImportError:
        logger.error(
            "geopandas is not installed. Run: pip install geopandas"
        )
        return {}

    result: dict[str, gpd.GeoDataFrame] = {}

    shapefile_map = {
        "states": Paths.shapefiles / "nigeria_states.shp",
        "lgas":   Paths.shapefiles / "nigeria_lgas.shp",
    }

    for key, shp_path in shapefile_map.items():
        if not shp_path.exists():
            if key == "states":
                logger.warning(
                    "State shapefile not found: %s. "
                    "Download from grid3.org or gadm.org",
                    shp_path,
                )
            else:
                logger.debug("Optional LGA shapefile not found: %s", shp_path)
            continue

        try:
            gdf = gpd.read_file(shp_path)

            # Ensure coordinates are in WGS84 — required for PostGIS
            # and consistent with all other spatial data in this project
            if gdf.crs is None:
                logger.warning(
                    "%s has no CRS defined — assuming WGS84", shp_path.name
                )
                gdf = gdf.set_crs(epsg=4326)
            elif gdf.crs.to_epsg() != 4326:
                logger.info(
                    "Reprojecting %s from %s to WGS84",
                    shp_path.name,
                    gdf.crs.to_string(),
                )
                gdf = gdf.to_crs(epsg=4326)

            result[key] = gdf
            logger.info(
                "Loaded %s shapefile: %d features, CRS=%s",
                key,
                len(gdf),
                gdf.crs,
            )

        except Exception as exc:
            logger.error("Failed to load %s: %s", shp_path.name, exc)

    return result
