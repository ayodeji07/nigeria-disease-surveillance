"""
src/etl/extract.py
────────────────────────────────────────────────────────────────
Data extraction — the "E" in ETL.

This module fetches raw data from all external sources and saves
it to data/raw/ as CSV files. No cleaning happens here.

Sources:
  1. NCDC Nigeria PDF Situation Reports  — 5 diseases
  2. WHO AFRO CSV/Excel files            — cross-validation
  3. NASA POWER REST API                 — monthly rainfall
  4. HDX Nigeria                        — health facilities CSV
  5. NBS / WorldPop                     — state populations
  6. GRID3 Nigeria                      — state shapefiles

Design:
  • Every extractor is idempotent — calling it twice produces
    the same result.
  • Results are cached to data/raw/ so re-runs are fast.
  • Pass force_download=True to bypass the cache.
  • All extractors return a DataFrame (never None, never raise).
  • Every row has a _source_file column for provenance.

PDF parsing notes:
  Many NCDC PDFs use CID-encoded fonts for the state breakdown
  table, which pdfplumber cannot decode. Where this occurs the
  parser falls back to extracting whatever text IS readable
  (national totals from the summary table, top-N state lists).
  See src/etl/pdf_parsers.py for full details.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from src.utils.config import Paths, Diseases, settings
from src.utils.logger import get_logger
from src.utils.state_maps import CANONICAL_STATES, STATE_CENTROIDS

logger = get_logger(__name__)


# ────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ────────────────────────────────────────────────────────────────

def _save_raw(df: pd.DataFrame, filename: str) -> Path:
    """
    Save a DataFrame to data/raw/<filename> as CSV.

    Parameters
    ----------
    df : pd.DataFrame
    filename : str  e.g. 'ncdc_cholera_raw.csv'

    Returns
    -------
    Path  — absolute path of the saved file
    """
    out_path = Paths.raw / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    logger.debug("Saved %d rows → %s", len(df), out_path.name)
    return out_path


def _load_cached(filename: str) -> Optional[pd.DataFrame]:
    """
    Load a cached CSV from data/raw/<filename>.

    Returns
    -------
    pd.DataFrame if the file exists, else None.
    """
    path = Paths.raw / filename
    if path.exists():
        logger.debug("Cache hit: %s", filename)
        return pd.read_csv(path)
    return None


# ────────────────────────────────────────────────────────────────
# 1. NCDC PDF EXTRACTION
# ────────────────────────────────────────────────────────────────

def extract_ncdc_pdfs(
    folder: Path,
    disease_name: str,
    force_download: bool = False,
) -> pd.DataFrame:
    """
    Extract state-level surveillance data from all NCDC PDFs
    in a disease folder.

    The results are cached as data/raw/ncdc_<disease>_raw.csv.
    Pass force_download=True to re-extract even if cache exists.

    Parameters
    ----------
    folder : Path
        Directory containing NCDC PDF sitreps for one disease.
        e.g. data/raw/ncdc_pdfs/cholera/
    disease_name : str
        Disease name — passed to the parser for correct table logic.
    force_download : bool

    Returns
    -------
    pd.DataFrame
        All rows extracted across all PDFs, with provenance columns.
        Empty DataFrame if no PDFs found or all fail.
    """
    cache_key = f"ncdc_{disease_name.lower().replace(' ', '_')}_raw.csv"

    if not force_download:
        cached = _load_cached(cache_key)
        if cached is not None:
            logger.info(
                "NCDC %s: loaded %d rows from cache",
                disease_name, len(cached),
            )
            return cached

    if not folder.exists():
        logger.warning(
            "PDF folder not found: %s  "
            "(Create it and place NCDC sitreps there)",
            folder,
        )
        return pd.DataFrame()

    pdf_files = sorted(folder.glob("*.pdf"))
    if not pdf_files:
        logger.warning(
            "No PDF files in %s  "
            "(Download from ncdc.gov.ng/reports)",
            folder,
        )
        return pd.DataFrame()

    logger.info(
        "NCDC %s: extracting %d PDFs from %s",
        disease_name, len(pdf_files), folder,
    )

    all_frames: list[pd.DataFrame] = []
    for pdf_path in pdf_files:
        df = _extract_single_pdf(pdf_path, disease_name)
        if not df.empty:
            all_frames.append(df)

    if not all_frames:
        logger.warning(
            "NCDC %s: no data extracted from %d PDFs",
            disease_name, len(pdf_files),
        )
        return pd.DataFrame()

    combined = pd.concat(all_frames, ignore_index=True)
    _save_raw(combined, cache_key)
    logger.info(
        "NCDC %s: extracted %d rows from %d PDFs",
        disease_name, len(combined), len(pdf_files),
    )
    return combined


def _extract_single_pdf(pdf_path: Path, disease_name: str) -> pd.DataFrame:
    """
    Extract state-level surveillance data from one NCDC PDF.

    Uses disease-specific parsers (src/etl/pdf_parsers.py) that
    understand the exact table structure of each NCDC report type.

    Many NCDC PDFs use CID-encoded fonts for the main state
    breakdown table. Where this occurs, the parser falls back to
    extracting data from whatever text IS readable.

    Parameters
    ----------
    pdf_path : Path
    disease_name : str

    Returns
    -------
    pd.DataFrame
        state | disease | epi_week | year | suspected_cases |
        confirmed_cases | deaths | cfr_pct | _source_file | _data_type
    """
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber not installed — run: pip install pdfplumber")
        return pd.DataFrame()

    from src.etl.pdf_parsers import parse_pdf_by_disease

    raw_tables: list[list[list]] = []
    page_texts: list[str]        = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            logger.debug(
                "  Parsing %s (%d pages) [%s]",
                pdf_path.name, len(pdf.pages), disease_name,
            )
            for page in pdf.pages:
                tbls = page.extract_tables()
                if tbls:
                    raw_tables.extend(tbls)
                # Use word-level extraction for better CID font handling
                words = page.extract_words()
                page_texts.append(
                    " ".join(w["text"] for w in words) if words else ""
                )

    except Exception as exc:
        logger.error("Failed to open %s: %s", pdf_path.name, exc)
        return pd.DataFrame()

    result = parse_pdf_by_disease(
        raw_tables   = raw_tables,
        disease_name = disease_name,
        pdf_path     = pdf_path,
        page_texts   = page_texts,
    )

    if result.empty:
        logger.warning(
            "No rows extracted from %s (%s)",
            pdf_path.name, disease_name,
        )
    else:
        logger.debug(
            "  %d rows from %s", len(result), pdf_path.name,
        )

    return result


# ────────────────────────────────────────────────────────────────
# 2. WHO GHO DATA
# ────────────────────────────────────────────────────────────────

# WHO Global Health Observatory OData API — no auth required
_WHO_GHO_BASE    = "https://ghoapi.azureedge.net/api"
_WHO_GHO_COUNTRY = "NGA"

# Indicator codes for annual national totals (both sexes)
_WHO_GHO_INDICATORS: dict[str, dict[str, str]] = {
    "Cholera": {
        "cases":  "CHOLERA_0000000001",
        "deaths": "CHOLERA_0000000002",
    },
}


def _fetch_who_gho_indicator(code: str) -> list[dict]:
    """
    Fetch one WHO GHO indicator for Nigeria from the OData API.

    Returns the raw list of value dicts, or [] on any error.
    """
    url = (
        f"{_WHO_GHO_BASE}/{code}"
        f"?$filter=SpatialDim eq '{_WHO_GHO_COUNTRY}'"
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])
    except requests.exceptions.Timeout:
        logger.warning("WHO GHO timeout for indicator %s", code)
        return []
    except Exception as exc:
        logger.warning("WHO GHO fetch failed for %s: %s", code, exc)
        return []


def extract_who_data(force_download: bool = False) -> pd.DataFrame:
    """
    Fetch WHO annual surveillance data for Nigeria from the WHO GHO API.

    Queries the WHO Global Health Observatory OData API — no login or
    manual download required. Results are cached as who_raw.csv.

    If local files exist in data/raw/who/ they are loaded instead of
    calling the API (useful for air-gapped environments).

    Diseases covered: Cholera (cases and deaths).

    Parameters
    ----------
    force_download : bool
        If False (default), return cached who_raw.csv if it exists.

    Returns
    -------
    pd.DataFrame
        Columns: year | disease | reported_cases | reported_deaths |
                 _source_file
        One row per year per disease. Empty DataFrame if API is
        unreachable and no cache or local files exist.
    """
    cache_key = "who_raw.csv"

    if not force_download:
        cached = _load_cached(cache_key)
        if cached is not None:
            logger.info("WHO: loaded %d rows from cache", len(cached))
            return cached

    # ── Prefer local files if the user has placed any ──────────────
    who_dir = Paths.raw / "who"
    if who_dir.exists():
        local_files = (
            list(who_dir.glob("*.csv")) + list(who_dir.glob("*.xlsx"))
        )
        if local_files:
            frames: list[pd.DataFrame] = []
            for fpath in local_files:
                try:
                    df = (
                        pd.read_csv(fpath)
                        if fpath.suffix == ".csv"
                        else pd.read_excel(fpath)
                    )
                    df["_source_file"] = fpath.name
                    frames.append(df)
                    logger.info(
                        "WHO: loaded %d rows from local file %s",
                        len(df), fpath.name,
                    )
                except Exception as exc:
                    logger.warning(
                        "WHO: could not read %s: %s", fpath.name, exc
                    )
            if frames:
                combined = pd.concat(frames, ignore_index=True)
                _save_raw(combined, cache_key)
                return combined

    # ── Fall back to WHO GHO API ───────────────────────────────────
    logger.info(
        "WHO: fetching data from WHO GHO API for Nigeria "
        "(no local files found)..."
    )

    # Collect cases and deaths per year for each disease
    yearly: dict[tuple[str, int], dict] = {}

    for disease, indicators in _WHO_GHO_INDICATORS.items():
        for metric, code in indicators.items():
            values = _fetch_who_gho_indicator(code)
            logger.info(
                "WHO GHO %s %s (%s): %d records returned",
                disease, metric, code, len(values),
            )
            for record in values:
                # Keep both-sex aggregate (Dim1='BTSX') or null-sex rows
                dim1 = record.get("Dim1") or "BTSX"
                if dim1 not in ("BTSX", ""):
                    continue
                year = record.get("TimeDim")
                val  = record.get("NumericValue")
                if year is None or val is None:
                    continue
                key = (disease, int(year))
                if key not in yearly:
                    yearly[key] = {
                        "year":             int(year),
                        "disease":          disease,
                        "reported_cases":   0,
                        "reported_deaths":  0,
                        "_source_file":     "WHO_GHO_API",
                    }
                if metric == "cases":
                    yearly[key]["reported_cases"]  = int(val)
                else:
                    yearly[key]["reported_deaths"] = int(val)

    if not yearly:
        logger.warning(
            "WHO GHO: no data returned. "
            "Check network connectivity or place files in data/raw/who/."
        )
        return pd.DataFrame()

    result = (
        pd.DataFrame(list(yearly.values()))
        .sort_values(["disease", "year"])
        .reset_index(drop=True)
    )
    _save_raw(result, cache_key)
    logger.info(
        "WHO GHO: %d rows fetched and cached as %s",
        len(result), cache_key,
    )
    return result


# ────────────────────────────────────────────────────────────────
# 3. NASA POWER RAINFALL API
# ────────────────────────────────────────────────────────────────

def extract_nasa_rainfall(
    start_year:     int  = 2015,
    end_year:       int  = 2024,
    force_download: bool = False,
) -> pd.DataFrame:
    """
    Fetch monthly precipitation per Nigerian state from NASA POWER.

    Queries the POWER API for each state centroid. Adds a 2-second
    delay between requests to respect the rate limit (~30 req/min).

    No API key required.

    Parameters
    ----------
    start_year : int
    end_year : int
    force_download : bool

    Returns
    -------
    pd.DataFrame
        Columns: state | year | month | rainfall_mm | latitude | longitude
    """
    cache_key = "rainfall_raw.csv"

    if not force_download:
        cached = _load_cached(cache_key)
        if cached is not None:
            logger.info(
                "Rainfall: loaded %d rows from cache", len(cached)
            )
            return cached

    logger.info(
        "Rainfall: fetching %d states × %d years from NASA POWER...",
        len(STATE_CENTROIDS),
        end_year - start_year + 1,
    )

    frames: list[pd.DataFrame] = []
    for i, (state_name, (lat, lon)) in enumerate(STATE_CENTROIDS.items()):
        df = _fetch_one_state_rainfall(
            state      = state_name,
            lat        = lat,
            lon        = lon,
            start_year = start_year,
            end_year   = end_year,
        )
        if not df.empty:
            frames.append(df)
            logger.debug(
                "  Rainfall %s: %d rows", state_name, len(df)
            )

        # Progress indicator every 10 states
        if (i + 1) % 10 == 0:
            logger.info(
                "  Rainfall: %d/%d states fetched",
                i + 1, len(STATE_CENTROIDS),
            )

        # Rate limit: 2 second delay between requests
        if i < len(STATE_CENTROIDS) - 1:
            time.sleep(2.2)

    if not frames:
        logger.warning("Rainfall: no data retrieved from NASA POWER")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    _save_raw(combined, cache_key)
    logger.info(
        "Rainfall: %d rows for %d states saved",
        len(combined), combined["state"].nunique(),
    )
    return combined


def _fetch_one_state_rainfall(
    state:      str,
    lat:        float,
    lon:        float,
    start_year: int,
    end_year:   int,
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    Fetch monthly rainfall for one state centroid from NASA POWER.

    Uses the PRECTOTCORR parameter (precipitation corrected, mm/month).
    The NASA fill value is -999 — replaced with NaN in transform.py.

    Retries up to max_retries times on timeout or connection errors,
    with exponential backoff (5s, 10s, 20s).

    Parameters
    ----------
    state : str
    lat, lon : float  — state centroid coordinates (WGS84)
    start_year, end_year : int
    max_retries : int

    Returns
    -------
    pd.DataFrame
        Columns: state | year | month | rainfall_mm | latitude | longitude
        Empty DataFrame if all attempts fail.
    """
    url = (
        "https://power.larc.nasa.gov/api/temporal/monthly/point"
        f"?parameters=PRECTOTCORR"
        f"&community=AG"
        f"&longitude={lon}"
        f"&latitude={lat}"
        f"&start={start_year}"
        f"&end={end_year}"
        f"&format=JSON"
    )

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, timeout=45)
            if not response.ok:
                logger.warning(
                    "NASA POWER %s HTTP %s — body: %s",
                    state, response.status_code,
                    response.text[:500],
                )
                return pd.DataFrame()

            data = response.json()
            rainfall_data = (
                data
                .get("properties", {})
                .get("parameter", {})
                .get("PRECTOTCORR", {})
            )

            if not rainfall_data:
                logger.warning(
                    "NASA POWER: no PRECTOTCORR data for %s", state
                )
                return pd.DataFrame()

            rows = []
            for yyyymm, value in rainfall_data.items():
                try:
                    year  = int(yyyymm[:4])
                    month = int(yyyymm[4:])
                    if not 1 <= month <= 12:
                        continue
                    rows.append({
                        "state":        state,
                        "year":         year,
                        "month":        month,
                        "rainfall_mm":  float(value),
                        "latitude":     lat,
                        "longitude":    lon,
                    })
                except (ValueError, IndexError):
                    continue

            return pd.DataFrame(rows)

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as exc:
            wait = 5 * (2 ** (attempt - 1))   # 5s, 10s, 20s
            if attempt < max_retries:
                logger.warning(
                    "NASA POWER %s attempt %d/%d failed (%s) — "
                    "retrying in %ds",
                    state, attempt, max_retries, exc.__class__.__name__, wait,
                )
                time.sleep(wait)
            else:
                logger.warning(
                    "NASA POWER %s failed after %d attempts: %s",
                    state, max_retries, exc,
                )
                return pd.DataFrame()

        except requests.exceptions.RequestException as exc:
            logger.warning(
                "NASA POWER request failed for %s: %s", state, exc
            )
            return pd.DataFrame()

        except Exception as exc:
            logger.warning(
                "Unexpected error fetching rainfall for %s: %s", state, exc
            )
            return pd.DataFrame()

    return pd.DataFrame()


# ────────────────────────────────────────────────────────────────
# 4. HEALTH FACILITIES (HDX)
# ────────────────────────────────────────────────────────────────

def extract_health_facilities(force_download: bool = False) -> pd.DataFrame:
    """
    Load health facility locations from data/raw/health_facilities.csv.

    The file is manually downloaded from HDX Nigeria:
    https://data.humdata.org  (search: 'Nigeria health facilities')

    Parameters
    ----------
    force_download : bool
        If False, return cached data if it exists.

    Returns
    -------
    pd.DataFrame
        Columns vary by source file but always include _source_file.
        Empty DataFrame if file not found.
    """
    cache_key = "health_facilities_raw.csv"

    if not force_download:
        cached = _load_cached(cache_key)
        if cached is not None:
            return cached

    # Try several possible filenames
    candidates = [
        Paths.raw / "health_facilities.csv",
        Paths.raw / "nigeria_health_facilities.csv",
        Paths.raw / "NGA_facilities.csv",
    ]

    for fpath in candidates:
        if fpath.exists():
            try:
                df = pd.read_csv(fpath, low_memory=False)
                df["_source_file"] = fpath.name
                _save_raw(df, cache_key)
                logger.info(
                    "Facilities: loaded %d rows from %s",
                    len(df), fpath.name,
                )
                return df
            except Exception as exc:
                logger.warning("Could not read %s: %s", fpath.name, exc)

    logger.warning(
        "Health facilities file not found. "
        "Download from data.humdata.org and save as "
        "data/raw/health_facilities.csv"
    )
    return pd.DataFrame()


# ────────────────────────────────────────────────────────────────
# 5. POPULATION DATA (NBS / WorldPop)
# ────────────────────────────────────────────────────────────────

def extract_population(force_download: bool = False) -> pd.DataFrame:
    """
    Load Nigerian state population estimates.

    Tries Excel first (preferred), then CSV.
    The file is manually downloaded from:
      - NBS: nigerianstat.gov.ng
      - WorldPop: worldpop.org

    Parameters
    ----------
    force_download : bool

    Returns
    -------
    pd.DataFrame
        Raw population table with _source_file column.
        Empty DataFrame if no file found.
    """
    cache_key = "population_raw.csv"

    if not force_download:
        cached = _load_cached(cache_key)
        if cached is not None:
            return cached

    # Try Excel first, then CSV
    candidates = [
        (Paths.raw / "nigeria_population.xlsx",  "excel"),
        (Paths.raw / "nigeria_population.xls",   "excel"),
        (Paths.raw / "nigeria_population.csv",   "csv"),
        (Paths.raw / "NGA_population.xlsx",      "excel"),
        (Paths.raw / "NGA_population.csv",       "csv"),
    ]

    for fpath, ftype in candidates:
        if fpath.exists():
            try:
                df = pd.read_excel(fpath) if ftype == "excel" \
                     else pd.read_csv(fpath)
                df["_source_file"] = fpath.name
                _save_raw(df, cache_key)
                logger.info(
                    "Population: loaded %d rows from %s",
                    len(df), fpath.name,
                )
                return df
            except Exception as exc:
                logger.warning(
                    "Could not read %s: %s", fpath.name, exc
                )

    logger.warning(
        "Population file not found. "
        "Download from nigerianstat.gov.ng and save as "
        "data/raw/nigeria_population.xlsx"
    )
    return pd.DataFrame()


# ────────────────────────────────────────────────────────────────
# 6. SHAPEFILES (GRID3 Nigeria)
# ────────────────────────────────────────────────────────────────

def extract_shapefiles() -> dict[str, "gpd.GeoDataFrame"]:
    """
    Load Nigerian state boundary shapefiles into GeoDataFrames.

    Automatically reprojects to WGS84 (EPSG:4326) if needed.

    Download from: grid3.org or gadm.org/country/NGA
    Save to: data/shapefiles/nigeria_states.shp
             (+ .shx, .dbf, .prj files in the same folder)

    Returns
    -------
    dict[str, gpd.GeoDataFrame]
        Keys: 'states' (and 'lgas' if LGA file found)
        Empty dict if geopandas not installed or no files found.
    """
    try:
        import geopandas as gpd
    except ImportError:
        logger.warning(
            "geopandas not installed — shapefiles not loaded. "
            "Run: pip install geopandas"
        )
        return {}

    result: dict[str, gpd.GeoDataFrame] = {}

    shp_dir = Paths.shapefiles
    if not shp_dir.exists():
        logger.warning(
            "Shapefiles directory not found: %s  "
            "(Create it and place GRID3 shapefiles there)",
            shp_dir,
        )
        return {}

    def _load_shp(
        candidates: list[Path],
        label: str,
        exclude: Path | None = None,
    ) -> "tuple[gpd.GeoDataFrame | None, Path | None]":
        """
        Try each candidate path; if none match, fall back to any .shp in
        the folder (excluding `exclude`). Returns (GeoDataFrame, path) or
        (None, None).
        """
        search_paths = list(dict.fromkeys(candidates + sorted(shp_dir.glob("*.shp"))))
        for shp_path in search_paths:
            if not shp_path.exists() or shp_path == exclude:
                continue
            try:
                gdf = gpd.read_file(shp_path)
                if gdf.crs and gdf.crs.to_epsg() != 4326:
                    gdf = gdf.to_crs(epsg=4326)
                logger.info(
                    "Shapefiles: loaded %d %s features from %s (CRS=%s)",
                    len(gdf), label, shp_path.name, gdf.crs,
                )
                return gdf, shp_path
            except Exception as exc:
                logger.warning(
                    "Could not read shapefile %s: %s", shp_path.name, exc
                )
        return None, None

    # State-level shapefile
    state_candidates = [
        shp_dir / "nigeria_states.shp",
        shp_dir / "NGA_adm1.shp",
        shp_dir / "gadm41_NGA_1.shp",
    ]
    gdf_states, states_path = _load_shp(state_candidates, "state")
    if gdf_states is not None:
        result["states"] = gdf_states

    # LGA-level shapefile (optional — only if a second distinct .shp exists)
    lga_candidates = [
        shp_dir / "nigeria_lgas.shp",
        shp_dir / "NGA_adm2.shp",
        shp_dir / "gadm41_NGA_2.shp",
    ]
    gdf_lgas, _ = _load_shp(lga_candidates, "LGA", exclude=states_path)
    if gdf_lgas is not None:
        result["lgas"] = gdf_lgas

    if not result:
        logger.warning(
            "No shapefiles found in %s. "
            "Download from grid3.org and place .shp files there.",
            shp_dir,
        )

    return result
