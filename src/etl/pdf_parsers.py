"""
src/etl/pdf_parsers.py
────────────────────────────────────────────────────────────────
Disease-specific PDF table parsers for NCDC Situation Reports.

Based on analysis of real NCDC PDFs (2022–2023):
  • Cholera   — Monthly Epidemiological Report (Table 6 = state breakdown)
  • Lassa Fever — Weekly Situation Report (Table 3 = state breakdown)
  • Meningitis  — Monthly Situation Report (Table 3 = state breakdown)
  • Mpox        — Weekly Update (Table 2 = cumulative by year/state)
  • Yellow Fever — Monthly Sitrep (Table 1 = state lab summary)

Each parser:
  1. Receives a list of raw pdfplumber table objects from one PDF
  2. Identifies which table is the state-level breakdown
  3. Returns a clean DataFrame with standardised columns:
     state | suspected_cases | confirmed_cases | deaths |
     epi_week | year | cfr_pct | _source_file | _disease

Usage:
    from src.etl.pdf_parsers import parse_pdf_by_disease
    df = parse_pdf_by_disease(raw_tables, disease_name, pdf_path)
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.utils.logger import get_logger
from src.utils.state_maps import CANONICAL_STATE_SET, normalise_state_name

logger = get_logger(__name__)


# ── Column name normalisation helpers ────────────────────────────

def _norm(text: Optional[str]) -> str:
    """Lowercase, strip whitespace and newlines from a header string."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text).lower().strip())


def _contains(text: Optional[str], *keywords: str) -> bool:
    """Return True if the normalised text contains any keyword."""
    n = _norm(text)
    return any(kw.lower() in n for kw in keywords)


def _safe_int(value) -> int:
    """Convert a messy string value to int — 0 if unparseable."""
    if value is None:
        return 0
    cleaned = re.sub(r"[,\s%]", "", str(value).strip())
    cleaned = re.sub(r"[–\-]$", "0", cleaned)       # dash → 0
    cleaned = re.sub(r"nil|n/a|none", "0", cleaned, flags=re.I)
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return 0


def _safe_float(value) -> Optional[float]:
    """Convert to float — None if unparseable."""
    if value is None:
        return None
    cleaned = re.sub(r"[,\s%]", "", str(value).strip())
    try:
        return round(float(cleaned), 4)
    except (ValueError, TypeError):
        return None


# ── Epi week / year extraction from PDF header text ──────────────

def _extract_epi_week_year(pdf_path: Path, page_text: str) -> tuple[Optional[int], Optional[int]]:
    """
    Extract epi week and year from PDF filename or page header text.

    Years are validated to be in 2000-2030 range so we don't
    accidentally pick up case counts like '2733' as a year.

    Returns (epi_week, year) — either can be None.
    """
    def _valid_year(y: int) -> bool:
        return 2000 <= y <= 2030

    # Try filename first — most reliable
    name = pdf_path.stem.lower()
    year_match = re.search(r"(20[0-2]\d)", name)
    week_match = re.search(r"w(?:ee)?k?_?(\d{1,2})", name)

    year = int(year_match.group(1)) if year_match else None
    week = int(week_match.group(1)) if week_match else None

    if year and week:
        return week, year

    # Pattern: "Epi Week 47 2022" / "Epi Week 27-29 2023"
    wk_match = re.search(
        r"epi[\s\-]*week[\s:]*(\d{1,2})(?:\s*[-\u2013]\s*\d{1,2})?[,\s]+(20[0-2]\d)",
        page_text, re.I
    )
    if wk_match:
        return int(wk_match.group(1)), int(wk_match.group(2))

    # Year from first 500 chars (header area is more reliable)
    header_text = page_text[:500]
    yr_match = re.search(r"\b(20[0-2]\d)\b", header_text)
    if yr_match and not year:
        candidate = int(yr_match.group(1))
        if _valid_year(candidate):
            year = candidate

    # Week from anywhere in text
    wk_only = re.search(r"epi[\s\-]*week[\s:]*(\d{1,2})", page_text, re.I)
    if wk_only and not week:
        week = int(wk_only.group(1))

    return week, (year if year and _valid_year(year) else None)


# ── Table identification helpers ─────────────────────────────────

def _find_state_table(
    raw_tables: list[list[list]],
    min_rows: int = 5,
    required_keywords: list[str] | None = None,
) -> Optional[list[list]]:
    """
    Find the table that contains the state-level breakdown.

    Scans all tables for one whose header contains 'state' and
    has at least min_rows data rows. Returns the raw table or None.

    Parameters
    ----------
    raw_tables : list of raw pdfplumber tables
    min_rows : minimum data rows (excl. header)
    required_keywords : additional keywords that must appear in header

    Returns
    -------
    list[list] | None
        The matching raw table, or None if not found.
    """
    if required_keywords is None:
        required_keywords = []

    for table in raw_tables:
        if not table or len(table) < min_rows + 1:
            continue
        header_row = table[0]
        header_str = " ".join(_norm(c) for c in header_row if c)

        # Must contain 'state'
        if "state" not in header_str:
            continue

        # Must contain all required keywords
        if not all(kw.lower() in header_str for kw in required_keywords):
            continue

        return table

    return None


def _raw_table_to_df(raw_table: list[list]) -> pd.DataFrame:
    """
    Convert a raw pdfplumber table (list of lists) to a DataFrame.

    Handles:
      - Multi-line headers (joined with space)
      - Rows with fewer columns than header (padded with None)
      - Rows with more columns than header (truncated)
    """
    if not raw_table or len(raw_table) < 2:
        return pd.DataFrame()

    # Some headers span two rows — detect by checking if second row
    # also looks like a header (non-numeric values in most cells)
    header_row = raw_table[0]
    second_row = raw_table[1] if len(raw_table) > 1 else []

    # Check if second row looks like a continuation of the header
    non_numeric = sum(
        1 for cell in second_row
        if cell and not re.match(r"^\d[\d,.\s%]*$", str(cell).strip())
    )
    if second_row and non_numeric >= len(second_row) * 0.6:
        # Second row is also a header — merge with first
        merged = []
        for h1, h2 in zip(header_row, second_row):
            h1 = str(h1 or "").strip()
            h2 = str(h2 or "").strip()
            merged.append(f"{h1} {h2}".strip() if h2 else h1)
        header_row = merged
        data_rows = raw_table[2:]
    else:
        data_rows = raw_table[1:]

    n_cols = len(header_row)
    padded = []
    for row in data_rows:
        if len(row) < n_cols:
            row = list(row) + [None] * (n_cols - len(row))
        elif len(row) > n_cols:
            row = row[:n_cols]
        padded.append(row)

    return pd.DataFrame(padded, columns=header_row)


# ── CHOLERA parser ────────────────────────────────────────────────

_CHOLERA_SUMMARY_RE = re.compile(
    r"summary\s+table\s+for\s+weekly\s+[&]\s+cumulative\s+number\s+of\s+(?:suspected\s+)?cholera\s+cases"
    r"|weekly\s+[&]\s+cumulative\s+number\s+of\s+(?:suspected\s+)?cholera\s+cases",
    re.I,
)

# Outbreak status labels that appear in the state column but are NOT part of the state name
_CHOLERA_STATUS_WORDS = {"Active", "Sporadic", "Closed", "active", "sporadic", "closed"}


def _parse_cholera_weekly_table(
    page,
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """
    Word-position extractor for the cholera 'Summary table for Weekly &
    Cumulative number of Cholera Cases' table.

    Locates the 'Current week' section header to identify the Cases and Deaths
    column x-coordinates, then reads each state row.
    State rows have rank concatenated with state name: '1Bayelsa', '2Cross River'.
    """
    words = page.extract_words(x_tolerance=5, y_tolerance=3) or []
    if not words:
        return pd.DataFrame()

    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))

    # Group words into y-rows (within 8 pt)
    rows_by_y: list[list] = []
    cur_row = [words_sorted[0]]
    for w in words_sorted[1:]:
        if abs(w["top"] - cur_row[-1]["top"]) <= 8:
            cur_row.append(w)
        else:
            rows_by_y.append(sorted(cur_row, key=lambda w: w["x0"]))
            cur_row = [w]
    rows_by_y.append(sorted(cur_row, key=lambda w: w["x0"]))

    # Find CW Cases and CW Deaths column x-positions from the header section
    cw_cases_x: Optional[float] = None
    cw_deaths_x: Optional[float] = None
    in_cw = False
    table_start_y: Optional[float] = None

    for row_words in rows_by_y:
        row_text = " ".join(w["text"] for w in row_words).lower()
        if "current" in row_text and ("week" in row_text or ":" in row_text):
            in_cw = True
            table_start_y = row_words[0]["top"]
        if not in_cw:
            continue
        for w in row_words:
            wt = w["text"].lower()
            # CW Cases: first "cases" header in the left half of the table (x < 230)
            if wt == "cases" and cw_cases_x is None and 110 < w["x0"] < 230:
                cw_cases_x = w["x0"]
            # CW Deaths: first "deaths" after Cases but before cumulative region (x < 300)
            if "death" in wt and cw_deaths_x is None and cw_cases_x is not None:
                if cw_cases_x + 20 < w["x0"] < 300:
                    cw_deaths_x = w["x0"]
        if cw_cases_x and cw_deaths_x:
            break

    if cw_cases_x is None:
        return pd.DataFrame()

    col_tol = 30.0   # pt tolerance for column matching
    cum_x_start = 320.0  # cumulative columns start here; stay left of this

    records = []
    for row_words in rows_by_y:
        if not row_words:
            continue
        # Only process rows below the table header
        if table_start_y is not None and row_words[0]["top"] < table_start_y + 8:
            continue

        first = row_words[0]
        if first["x0"] > 90:
            continue

        # State rows have either:
        #  - a rank prefix: "1Bayelsa", "2Cross River" (standard 2022+ format)
        #  - no rank prefix: "Ebonyi", "Cross River" (year-end report format)
        # Both are accepted; header words (States, Reporting, …) are filtered
        # later by the canonical-state normalisation check.
        if re.match(r"^\d", first["text"]):
            state_start = re.sub(r"^\d+", "", first["text"]).strip()
        elif len(first["text"]) >= 3:
            state_start = first["text"]
        else:
            continue

        # Collect remaining state-name words: between first word and CW Cases column.
        # Only alphabetic tokens — numeric values at the column boundary (e.g. CW Cases
        # at x≈163 when cw_cases_x=175) must not be included in the state name.
        extra_words = [
            w["text"] for w in row_words
            if w["x0"] > first["x0"] + 5
            and w["x0"] < cw_cases_x - 10
            and w["text"] not in _CHOLERA_STATUS_WORDS
            and re.match(r"^[A-Za-z]", w["text"])
        ]
        state_text = (state_start + " " + " ".join(extra_words)).strip()

        if not state_text:
            continue
        state_val = _clean_state_name(state_text)
        if not state_val or _is_skip_row(state_val):
            continue
        canonical = normalise_state_name(state_val)
        if canonical and not canonical.startswith("UNKNOWN:"):
            state_val = canonical
        else:
            continue  # skip unrecognised names (National total, etc.)

        def _get_col(target_x: float, tol: float = col_tol) -> Optional[str]:
            candidates = [
                w for w in row_words
                if abs(w["x0"] - target_x) <= tol and w["x0"] < cum_x_start
            ]
            if not candidates:
                return None
            return min(candidates, key=lambda w: abs(w["x0"] - target_x))["text"]

        susp = _safe_int(_get_col(cw_cases_x)) or 0
        deaths = (_safe_int(_get_col(cw_deaths_x)) or 0) if cw_deaths_x else 0

        records.append({
            "state":            state_val,
            "disease":          "Cholera",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  susp,
            "confirmed_cases":  0,
            "deaths":           deaths,
            "cfr_pct":          None,
            "_source_file":     pdf_path.name,
            "_data_type":       "current_week",
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    total_susp = df["suspected_cases"].sum()
    total_deaths = df["deaths"].sum()

    # Likely a different table format (e.g. 2021) where rank-prefix rows don't
    # represent actual state data — fall back to the cumulative table.
    if total_susp == 0 and len(df) < 5:
        return pd.DataFrame()

    # Sanity check: CFR > 30% means the deaths column was misidentified
    if total_susp > 0 and total_deaths / total_susp > 0.30:
        df["deaths"] = 0

    return df


def parse_cholera(
    raw_tables: list[list[list]],
    pdf_path: Path,
    page_texts: list[str],
) -> pd.DataFrame:
    """
    Parse NCDC Cholera Monthly Epidemiological Report.

    Strategy 1: Word-position extraction from the 'Summary table for Weekly &
      Cumulative number of Cholera Cases' — returns current_week rows.
    Strategy 2: Best state table from raw_tables — returns top10_cumulative.
    """
    full_text = " ".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    # Strategy 1 — word-position from the weekly summary table
    try:
        import pdfplumber as _pdfplumber
        with _pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                page_txt = page.extract_text() or ""
                if not _CHOLERA_SUMMARY_RE.search(page_txt):
                    continue
                result = _parse_cholera_weekly_table(page, epi_week, year, pdf_path)
                if not result.empty:
                    logger.info(
                        "Cholera: word-position extracted %d rows from page %d in %s",
                        len(result), page_idx, pdf_path.name,
                    )
                    return result
    except Exception as exc:
        logger.warning("Cholera word-pos failed for %s: %s", pdf_path.name, exc)

    # Strategy 2 — highlights text (older PDFs without the summary table)
    # Only accept current_week_highlights; ignore top10_cumulative from Source B.
    try:
        text_result = parse_cholera_from_text(page_texts, pdf_path)
        if not text_result.empty and (text_result["_data_type"] == "current_week_highlights").all():
            return text_result
    except Exception as exc:
        logger.warning("Cholera text parser failed for %s: %s", pdf_path.name, exc)

    # Strategy 3 — raw table fallback (returns top10_cumulative)
    # Find the table with a States column AND the most rows
    best_table = None
    best_count = 0

    for table in raw_tables:
        if not table or len(table) < 5:
            continue
        header_str = " ".join(_norm(c) for c in (table[0] or []) if c)
        if "state" not in header_str:
            continue
        # Count rows that look like Nigerian state names (not response text)
        data_rows = table[1:]
        state_like = 0
        for row in data_rows:
            if not row:
                continue
            first_val = _clean_state_name(str(row[0] or ""))
            if 2 < len(first_val) < 25 and not _is_skip_row(first_val):
                state_like += 1

        if state_like > best_count:
            best_count = state_like
            best_table = table

    # If full table not found, try top-10 table as fallback
    if best_table is None:
        best_table = _find_state_table(raw_tables, min_rows=5)

    if best_table is None:
        logger.warning("Cholera: no state table found in %s", pdf_path.name)
        table_result = pd.DataFrame()
    else:
        df = _raw_table_to_df(best_table)
        table_result = pd.DataFrame() if df.empty else _map_cholera_columns(df, epi_week, year, pdf_path)

    # Strategy 4 — full text fallback (Source B: rank-state pattern from page text)
    # Used when the raw table extracted 0 suspected cases or nothing at all.
    if table_result.empty or table_result["suspected_cases"].sum() == 0:
        try:
            text_result = parse_cholera_from_text(page_texts, pdf_path)
            if not text_result.empty:
                return text_result
        except Exception as exc:
            logger.warning("Cholera text fallback failed for %s: %s", pdf_path.name, exc)

    return table_result


def _map_cholera_columns(
    df: pd.DataFrame,
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """
    Map raw Cholera table columns to standardised output columns.
    """
    rows = []
    state_col = _find_col(df, ["state", "states"])
    if state_col is None:
        return pd.DataFrame()

    # Find cumulative cases and deaths columns
    cum_cases_col  = _find_col(df, ["cumulative case", "cumul.*case", "cases.*cumul"])
    cum_deaths_col = _find_col(df, ["cumulative death", "cumul.*death", "deaths.*cumul"])
    cfr_col        = _find_col(df, ["cfr", "case fatality"])
    suspected_col  = _find_col(df, ["suspected"])

    for _, row in df.iterrows():
        state_val = _clean_state_name(str(row[state_col] or ""))

        # Skip header repetitions, totals, empty rows
        if not state_val or _is_skip_row(state_val):
            continue

        suspected = _safe_int(row.get(suspected_col)) if suspected_col else 0
        confirmed  = _safe_int(row.get(cum_cases_col)) if cum_cases_col else suspected
        deaths     = _safe_int(row.get(cum_deaths_col)) if cum_deaths_col else 0
        cfr        = _safe_float(row.get(cfr_col)) if cfr_col else None

        rows.append({
            "state":            state_val,
            "disease":          "Cholera",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  suspected,
            "confirmed_cases":  confirmed,
            "deaths":           deaths,
            "cfr_pct":          cfr,
            "_source_file":     pdf_path.name,
            "_data_type":       "cumulative",
        })

    return pd.DataFrame(rows)


# ── LASSA FEVER parser ────────────────────────────────────────────

_LASSA_TABLE_TITLE_RE = re.compile(
    r"weekly\s+and\s+cumulative\s+number\s+of\s+suspected\s+and\s+confirmed",
    re.I,
)

# Column x-ranges for the NCDC Lassa state breakdown table.
# Derived from analysis of multiple 2022 PDFs. Values are (x_min, x_max).
_LASSA_X_COLS = {
    "state":         (55,  118),
    "cw_suspected":  (108, 165),
    "cw_confirmed":  (163, 202),   # Trend symbol '▼/▲' sits at x≈206 — exclude it
    "cw_deaths":     (270, 348),
    "cum_suspected": (348, 400),
    "cum_confirmed": (398, 450),
    "cum_deaths":    (488, 548),
}


def _word_groups_to_df(
    words: list[dict],
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
    page_width_pts: float = 594.0,
    row_gap_pts: float = 6.0,
) -> pd.DataFrame:
    """
    Shared core: turn a word list (each dict has 'text', 'x0', 'top' in
    PDF points) into a current-week state DataFrame using _LASSA_X_COLS.

    Called by both the pdfplumber path and the OCR path.
    page_width_pts is used to normalise OCR pixel coords when needed.
    """
    if not words:
        return pd.DataFrame()

    # Locate the y-range of the table body.
    # title_top: first occurrence of "weekly" — but only if it's in the top
    # half of the page. Some PDFs place the caption below the table image,
    # which would push title_top past the midpoint and collapse y_min/y_max.
    title_top: Optional[float] = None
    key_top:   Optional[float] = None
    for w in words:
        t = w["text"].lower()
        if "weekly" in t and title_top is None:
            candidate = w["top"]
            if candidate < page_width_pts * 0.75:   # reject if in lower half
                title_top = candidate
        if t == "key" and w["x0"] < page_width_pts * 0.30:
            key_top = w["top"]

    y_min = (title_top or 40)  + 15
    y_max = (key_top   or 580) - 5

    table_words = [w for w in words if y_min <= w["top"] <= y_max]
    if not table_words:
        return pd.DataFrame()

    # Sort by (top, x0) and group nearby rows (within row_gap_pts)
    table_words.sort(key=lambda w: (w["top"], w["x0"]))
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in table_words:
        if not cur or abs(w["top"] - cur[-1]["top"]) <= row_gap_pts:
            cur.append(w)
        else:
            groups.append(cur)
            cur = [w]
    if cur:
        groups.append(cur)

    def _col_for(x0: float) -> Optional[str]:
        for col, (lo, hi) in _LASSA_X_COLS.items():
            if lo <= x0 < hi:
                return col
        return None

    records = []
    for group in groups:
        group.sort(key=lambda w: w["x0"])

        state_parts = [
            w["text"] for w in group
            if _LASSA_X_COLS["state"][0] <= w["x0"] < _LASSA_X_COLS["state"][1]
        ]
        state_raw = " ".join(state_parts).strip()
        state_val = _clean_state_name(state_raw)

        if not state_val or _is_skip_row(state_val):
            continue
        if re.match(r"^\d+$", state_val):
            continue
        if len(state_val) > 20:
            continue
        state_lower = state_val.lower()
        if any(kw in state_lower for kw in (
            "suspected", "confirmed", "probable", "cumulative", "current",
            "cases", "deaths", "states", "rank",
        )):
            continue

        # Canonical name mapping — also strips OCR symbol artifacts
        # e.g. '} Abia' → 'Abia', 'Fet' skipped (unrecognisable misread)
        canonical = normalise_state_name(state_val)
        if canonical and canonical.startswith("UNKNOWN:"):
            # Strip leading non-alpha characters and retry
            cleaned = re.sub(r"^[^A-Za-z]+", "", state_val).strip()
            if cleaned:
                canonical = normalise_state_name(cleaned)
        if not canonical or canonical.startswith("UNKNOWN:"):
            continue   # unrecognisable OCR noise — skip
        state_val = canonical

        cell: dict[str, int] = {"cw_suspected": 0, "cw_confirmed": 0, "cw_deaths": 0}
        for w in group:
            col = _col_for(w["x0"])
            if col and col in cell:
                cell[col] = _safe_int(w["text"])

        records.append({
            "state":            state_val,
            "disease":          "Lassa Fever",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  cell["cw_suspected"],
            "confirmed_cases":  cell["cw_confirmed"],
            "deaths":           cell["cw_deaths"],
            "cfr_pct":          None,
            "_source_file":     pdf_path.name,
            "_data_type":       "current_week",
        })

    return pd.DataFrame(records)


def _parse_lassa_from_word_positions(
    page,
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """
    Reconstruct the Lassa state table from pdfplumber word positions.

    Uses x_tolerance=12 to join individually-spaced glyphs back into
    readable tokens, then delegates to _word_groups_to_df.
    """
    words = page.extract_words(x_tolerance=12, y_tolerance=3)
    return _word_groups_to_df(words, epi_week, year, pdf_path)


def _parse_lassa_from_ocr(
    pdf_path: Path,
    page_idx: int,
    page_width_pts: float,
    epi_week: Optional[int],
    year: Optional[int],
) -> pd.DataFrame:
    """
    OCR the Lassa state table and extract current-week data.

    Renders the target page at 300 DPI with pdf2image, then uses
    pytesseract image_to_data to get word positions. Coordinates are
    scaled from pixels to PDF points so _word_groups_to_df can use
    the same _LASSA_X_COLS column boundaries.

    Dependencies: pdf2image, pytesseract, Pillow, poppler, tesseract.
    Returns empty DataFrame if any dependency is missing.
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
        from PIL import ImageOps
    except ImportError as exc:
        logger.warning("OCR dependencies missing: %s", exc)
        return pd.DataFrame()

    DPI = 300
    try:
        images = convert_from_path(
            str(pdf_path),
            dpi=DPI,
            first_page=page_idx + 1,
            last_page=page_idx + 1,
        )
    except Exception as exc:
        logger.warning("pdf2image failed for %s: %s", pdf_path.name, exc)
        return pd.DataFrame()

    if not images:
        return pd.DataFrame()

    img = images[0].convert("L")           # grayscale
    img = ImageOps.autocontrast(img)        # boost contrast for OCR

    img_w, _ = img.size
    scale = page_width_pts / img_w         # pixels → PDF points

    try:
        data = pytesseract.image_to_data(
            img,
            config="--psm 6 --oem 3",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:
        logger.warning("pytesseract failed for %s: %s", pdf_path.name, exc)
        return pd.DataFrame()

    words = []
    for i, text in enumerate(data["text"]):
        text = str(text).strip()
        if not text or data["conf"][i] < 20:
            continue
        x0_pt  = data["left"][i]  * scale
        top_pt = data["top"][i]   * scale
        words.append({"text": text, "x0": x0_pt, "top": top_pt})

    if not words:
        return pd.DataFrame()

    return _word_groups_to_df(
        words, epi_week, year, pdf_path,
        page_width_pts=page_width_pts,
        row_gap_pts=8.0,    # OCR coords are slightly less precise than PDF
    )


def _resolve_lassa_column_map(
    table: list[list],
) -> tuple[dict[tuple[str, str], int], Optional[int]]:
    """
    Parse the 3-row Lassa table header and return column positions.

    The NCDC Lassa table has this header structure:
      Row 0: section spans — '' | 'Current week: (Week N)' | 'Cumulative (Week 1-N)'
      Row 1: '' | 'States' | 'Cases' | ... | 'Deaths' | '' | 'Cases' | ... | 'Deaths'
      Row 2: '' | '' | 'Suspected' | 'Confirmed' | 'Trend' | 'Probable' | 'HCW*' | '' | ...

    Returns
    -------
    col_map : dict  (section, metric) → column_index
              section is 'cw' or 'cum'; metric is one of
              'suspected', 'confirmed', 'deaths', 'probable', 'hcw', 'trend'
    state_col : int | None  — column index for state names
    """
    def _pad(row, n):
        return list(row or []) + [None] * max(0, n - len(row or []))

    n = max(len(table[0] or []), len(table[1] or []), len(table[2] or []))
    row0 = _pad(table[0], n)
    row1 = _pad(table[1], n)
    row2 = _pad(table[2], n)

    # Forward-fill section labels from row 0
    sections: list[Optional[str]] = [None] * n
    cur_sec: Optional[str] = None
    for i, cell in enumerate(row0):
        s = str(cell or "").lower()
        if "current" in s:
            cur_sec = "cw"
        elif "cumul" in s:
            cur_sec = "cum"
        sections[i] = cur_sec

    col_map: dict[tuple[str, str], int] = {}
    state_col: Optional[int] = None

    for i in range(n):
        r1 = _norm(row1[i])
        r2 = _norm(row2[i])
        sec = sections[i]

        if "state" in r1:
            state_col = i
        elif "suspected" in r2:
            if sec:
                col_map[(sec, "suspected")] = i
        elif "confirmed" in r2:
            if sec:
                col_map[(sec, "confirmed")] = i
        elif "death" in r2 or ("death" in r1 and not r2):
            if sec:
                col_map[(sec, "deaths")] = i
        elif "probable" in r2:
            if sec:
                col_map[(sec, "probable")] = i
        elif "hcw" in r2:
            if sec:
                col_map[(sec, "hcw")] = i
        elif "trend" in r2:
            if sec:
                col_map[(sec, "trend")] = i

    return col_map, state_col


def _parse_lassa_multirow_table(
    table: list[list],
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """
    Extract current-week state data from the Lassa 3-row-header table.

    Handles two row patterns pdfplumber produces:
      Normal  : ['1', 'Ondo', '20', '3', '▼', '', '', '1', '', '1361', ...]
      Crammed : ['2', 'Edo 31 2620 238 3 29', None, None, ...]

    In crammed rows pdfplumber squeezes multiple columns into one cell.
    We extract the state name and the first number as CW_Suspected.
    """
    if len(table) < 4:
        return pd.DataFrame()

    col_map, state_idx = _resolve_lassa_column_map(table)
    if state_idx is None:
        return pd.DataFrame()

    cw_susp_idx  = col_map.get(("cw", "suspected"))
    cw_conf_idx  = col_map.get(("cw", "confirmed"))
    cw_death_idx = col_map.get(("cw", "deaths"))

    def _cell(row, idx):
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    rows = []
    for row in table[3:]:  # skip the 3-row header
        if not row:
            continue

        raw_state = str(_cell(row, state_idx) or "").strip()

        # Crammed row: pdfplumber merged state + numbers into one cell
        # e.g. 'Edo 31 2620 238 3 29'  (all other cells are None)
        next_cell = _cell(row, state_idx + 1) if state_idx + 1 < len(row) else None
        if next_cell is None and re.search(r"\d", raw_state):
            m = re.match(r"([A-Za-z][A-Za-z\s]{1,20}?)\s+([\d\s▼▲\-]+)$", raw_state)
            if m:
                raw_state = m.group(1).strip()
                nums = [int(x) for x in re.findall(r"\d+", m.group(2))]
                # If first number < second, the sequence starts with CW_suspected
                # followed by cumulative values. If first >= second (or only one
                # number), all values are cumulative — no CW data for this state.
                if len(nums) >= 2 and nums[0] < nums[1]:
                    cw_susp = nums[0]
                else:
                    cw_susp = 0
                state_val = _clean_state_name(raw_state)
                if state_val and not _is_skip_row(state_val):
                    rows.append({
                        "state":            state_val,
                        "disease":          "Lassa Fever",
                        "epi_week":         epi_week,
                        "year":             year,
                        "suspected_cases":  cw_susp,
                        "confirmed_cases":  0,
                        "deaths":           0,
                        "cfr_pct":          None,
                        "_source_file":     pdf_path.name,
                        "_data_type":       "current_week",
                    })
            continue

        state_val = _clean_state_name(raw_state)
        if not state_val or _is_skip_row(state_val) or re.match(r"^\d+$", state_val):
            continue

        rows.append({
            "state":            state_val,
            "disease":          "Lassa Fever",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  _safe_int(_cell(row, cw_susp_idx)),
            "confirmed_cases":  _safe_int(_cell(row, cw_conf_idx)),
            "deaths":           _safe_int(_cell(row, cw_death_idx)),
            "cfr_pct":          None,
            "_source_file":     pdf_path.name,
            "_data_type":       "current_week",
        })

    return pd.DataFrame(rows)


def _is_lassa_state_table(table: list[list]) -> bool:
    """
    Return True if this table looks like the Lassa state breakdown.

    The real table has 'states' somewhere in rows 0-2 and 'current'
    or 'cumul' in row 0 (section span header).
    """
    if not table or len(table) < 10:
        return False
    row0_str = " ".join(_norm(c) for c in (table[0] or []) if c)
    if "current" not in row0_str and "cumul" not in row0_str:
        return False
    for row in table[:3]:
        if row and any("state" in _norm(c) for c in row if c):
            return True
    return False


def parse_lassa(
    raw_tables: list[list[list]],
    pdf_path: Path,
    page_texts: list[str],
) -> pd.DataFrame:
    """
    Parse NCDC Lassa Fever Weekly Situation Report.

    Target: the table titled 'Weekly and Cumulative number of suspected
    and confirmed cases for [year]' (Table 3 in most reports).

    The table has a 3-row merged header:
      Row 0: section spans ('Current week' / 'Cumulative')
      Row 1: 'States' and sub-group labels ('Cases', 'Deaths')
      Row 2: metric names ('Suspected', 'Confirmed', 'Trend', ...)

    Strategy (in order):
      1.  Title-targeted pdfplumber table extraction (text-layer PDFs).
      1b. Word-position reconstruction for spaced-glyph text PDFs.
      1c. OCR via pdf2image + pytesseract for image-embedded tables.
      2.  Scan pre-extracted raw_tables fallback.
      3.  National totals from readable summary text (last resort).
    """
    full_text = " ".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    # ── Strategy 1 + 1b: pdfplumber (table then word-position) ───
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                page_text = page_texts[page_idx] if page_idx < len(page_texts) else ""
                if not _LASSA_TABLE_TITLE_RE.search(page_text):
                    continue

                # 1: structured table extraction
                for tbl_settings in [
                    None,
                    {"vertical_strategy": "text", "horizontal_strategy": "text",
                     "snap_tolerance": 5, "join_tolerance": 5},
                ]:
                    tables = (
                        page.extract_tables(tbl_settings)
                        if tbl_settings else page.extract_tables()
                    )
                    for table in (tables or []):
                        if not _is_lassa_state_table(table):
                            continue
                        result = _parse_lassa_multirow_table(
                            table, epi_week, year, pdf_path
                        )
                        if not result.empty:
                            logger.debug(
                                "Lassa: %d rows via table page %d in %s",
                                len(result), page_idx + 1, pdf_path.name,
                            )
                            return result

                # 1b: word-position reconstruction
                result = _parse_lassa_from_word_positions(
                    page, epi_week, year, pdf_path
                )
                if not result.empty:
                    logger.debug(
                        "Lassa: %d rows via word-pos page %d in %s",
                        len(result), page_idx + 1, pdf_path.name,
                    )
                    return result
    except Exception as exc:
        logger.warning("Lassa pdfplumber strategies failed: %s", exc)

    # ── Strategy 1c: OCR (title-guided) ──────────────────────────
    # For PDFs where the table data is a raster image but the title text
    # is still in the text layer — we find the right page via title match.
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                page_text = page_texts[page_idx] if page_idx < len(page_texts) else ""
                if not _LASSA_TABLE_TITLE_RE.search(page_text):
                    continue
                page_width_pts = float(page.width)
                result = _parse_lassa_from_ocr(
                    pdf_path, page_idx, page_width_pts, epi_week, year
                )
                if not result.empty:
                    logger.info(
                        "Lassa: %d rows via OCR page %d in %s",
                        len(result), page_idx + 1, pdf_path.name,
                    )
                    return result
    except Exception as exc:
        logger.warning("Lassa OCR (title-guided) failed: %s", exc)

    # ── Strategy 1d: OCR (blind page scan) ───────────────────────
    # For PDFs where even the title is image-embedded — the title regex
    # never matches, so strategy 1c never fires. We scan pages 3–5
    # (the table is almost always on page 4 in NCDC Lassa reports) and
    # OCR any page that has a large embedded image and very few words.
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            n_pages = len(pdf.pages)
            for page_idx in range(2, min(6, n_pages)):   # pages 3–6 (0-indexed)
                page = pdf.pages[page_idx]
                page_text = page_texts[page_idx] if page_idx < len(page_texts) else ""
                # Skip pages already tried (title matched) or with lots of text
                if _LASSA_TABLE_TITLE_RE.search(page_text):
                    continue
                if len(page_text.split()) > 60:   # real text page, not table image
                    continue
                big_images = [
                    img for img in page.images
                    if img.get("width", 0) > 300 and img.get("height", 0) > 200
                ]
                if not big_images:
                    continue
                page_width_pts = float(page.width)
                result = _parse_lassa_from_ocr(
                    pdf_path, page_idx, page_width_pts, epi_week, year
                )
                if not result.empty:
                    logger.info(
                        "Lassa: %d rows via blind OCR page %d in %s",
                        len(result), page_idx + 1, pdf_path.name,
                    )
                    return result
    except Exception as exc:
        logger.warning("Lassa OCR (blind scan) failed: %s", exc)

    # ── Strategy 2: scan pre-extracted tables ─────────────────────
    for table in raw_tables:
        if not _is_lassa_state_table(table):
            continue
        result = _parse_lassa_multirow_table(table, epi_week, year, pdf_path)
        if not result.empty:
            return result

    # ── Strategy 3: national totals from readable summary text ────
    text_result = parse_lassa_from_text(page_texts, pdf_path)
    if not text_result.empty:
        logger.info(
            "Lassa: fell back to national-summary text for %s", pdf_path.name
        )
        return text_result

    logger.warning("Lassa: no state table found in %s", pdf_path.name)
    return pd.DataFrame()


def _map_lassa_columns(
    df: pd.DataFrame,
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """Map raw Lassa Fever table to standardised output."""
    rows = []

    state_col = _find_col(df, ["state", "states"])
    if state_col is None:
        return pd.DataFrame()

    # Lassa tracks confirmed cases (not suspected) as primary metric
    # Table 3 has current week + cumulative side by side
    # Cumulative columns appear after the current-week columns

    all_cols = list(df.columns)
    # Find all 'confirmed' columns — take the last one (cumulative)
    confirmed_cols = [
        c for c in all_cols
        if _contains(c, "confirmed")
    ]
    suspected_cols = [
        c for c in all_cols
        if _contains(c, "suspected")
    ]
    deaths_cols = [
        c for c in all_cols
        if _contains(c, "death", "deaths")
    ]

    # Use cumulative (last occurrence) for main values
    cum_confirmed = confirmed_cols[-1] if confirmed_cols else None
    cum_suspected = suspected_cols[-1] if suspected_cols else None
    cum_deaths    = deaths_cols[-1]    if deaths_cols    else None

    for _, row in df.iterrows():
        state_val = _clean_state_name(str(row[state_col] or ""))

        # Skip row numbers, totals, blank rows
        if not state_val or _is_skip_row(state_val):
            continue
        # Lassa table has row numbers in first col — skip numeric-only state names
        if re.match(r"^\d+$", state_val):
            continue

        rows.append({
            "state":            state_val,
            "disease":          "Lassa Fever",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  _safe_int(row.get(cum_suspected)) if cum_suspected else 0,
            "confirmed_cases":  _safe_int(row.get(cum_confirmed)) if cum_confirmed else 0,
            "deaths":           _safe_int(row.get(cum_deaths))    if cum_deaths    else 0,
            "cfr_pct":          None,   # Calculated in transform.py
            "_source_file":     pdf_path.name,
            "_data_type":       "cumulative",
        })

    return pd.DataFrame(rows)


# ── MENINGITIS parser ─────────────────────────────────────────────

def parse_meningitis(
    raw_tables: list[list[list]],
    pdf_path: Path,
    page_texts: list[str],
) -> pd.DataFrame:
    """
    Parse NCDC Cerebrospinal Meningitis (CSM) Situation Report.

    Target: Table 3 — 'Reporting Status for Weekly & Cumulative
    number of CSM Cases'.

    Real PDF observation:
      - The large state table is on page 6 (32 rows × 4 cols in
        pdfplumber due to merged cells)
      - Response activity tables have very long text in cells
      - We identify the state data table by finding the one whose
        rows mostly contain short strings (state names + numbers)

    Strategy: Pick the table with the highest ratio of
    numeric/short-string cells (data table) vs. long text cells
    (response table).
    """
    full_text = " ".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    # ── Strategy 1: title-row table scan ─────────────────────────
    # The CSM state table has its title in row 0 of the table itself
    # (not in the surrounding page text), so we scan raw_tables directly.
    for table in raw_tables:
        if not table or len(table) < 10:
            continue
        row0_str = " ".join(str(c or "") for c in table[0]).lower()
        if "weekly" in row0_str and "cumulative" in row0_str:
            result = _parse_csm_multirow_table(table, epi_week, year, pdf_path)
            if not result.empty:
                logger.debug(
                    "Meningitis: %d rows via title-table in %s",
                    len(result), pdf_path.name,
                )
                return result

    # ── Strategy 2: page-by-page re-extraction ────────────────────
    # Some PDFs need looser extraction settings to find the table.
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for settings in [None,
                                  {"vertical_strategy": "text",
                                   "horizontal_strategy": "text",
                                   "snap_tolerance": 5}]:
                    tables = (page.extract_tables(settings)
                              if settings else page.extract_tables())
                    for table in (tables or []):
                        if not table or len(table) < 10:
                            continue
                        row0_str = " ".join(str(c or "") for c in table[0]).lower()
                        if "weekly" in row0_str and "cumulative" in row0_str:
                            result = _parse_csm_multirow_table(
                                table, epi_week, year, pdf_path
                            )
                            if not result.empty:
                                return result
    except Exception as exc:
        logger.warning("Meningitis page-re-extraction failed: %s", exc)

    # ── Strategy 2.5: word-position + OCR ────────────────────────
    # Handles two failure modes:
    #   a) extract_words() returns words but table extraction misses structure
    #   b) Table is image-embedded or CID-encoded (chars unreadable by extract_words)
    _CSM_PAGE_RE = re.compile(
        r"(weekly\s*[&]\s*cumulative|reporting\s+status.*csm|reporting\s+status.*weekly"
        r"|summary\s+table.*week)",
        re.I,
    )
    try:
        import pdfplumber as _pdfplumber
        with _pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                page_txt = page.extract_text() or ""
                if not _CSM_PAGE_RE.search(page_txt):
                    continue
                # Try word-position reconstruction first (fast, no OCR)
                result = _parse_csm_from_word_positions(page, epi_week, year, pdf_path)
                if not result.empty:
                    logger.info(
                        "Meningitis: word-position extracted %d rows from page %d in %s",
                        len(result), page_idx, pdf_path.name,
                    )
                    return result
                # Fall back to OCR for this page
                result = _parse_csm_from_ocr(pdf_path, page_idx, epi_week, year)
                if not result.empty:
                    logger.info(
                        "Meningitis: OCR extracted %d rows from page %d in %s",
                        len(result), page_idx, pdf_path.name,
                    )
                    return result
    except Exception as exc:
        logger.warning("Meningitis word-pos/OCR failed for %s: %s", pdf_path.name, exc)

    # ── Strategy 3: text-based current-week highlights ────────────
    text_result = parse_meningitis_from_text(page_texts, pdf_path)
    if not text_result.empty:
        logger.info(
            "Meningitis: fell back to highlights text for %s", pdf_path.name
        )
        return text_result

    logger.warning("Meningitis: no state table found in %s", pdf_path.name)
    return pd.DataFrame()


def _parse_csm_from_word_positions(
    page,
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """
    Word-position based CSM table extractor.

    Works when extract_words() succeeds but extract_tables() misses the
    table structure (e.g. embedded image with text layer, or table that
    spans a complex page layout).
    """
    words = page.extract_words(x_tolerance=5, y_tolerance=3) or []
    if not words:
        return pd.DataFrame()

    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))

    # Group into y-rows (within 8pt)
    rows_by_y: list[list] = []
    cur_row = [words_sorted[0]]
    for w in words_sorted[1:]:
        if abs(w["top"] - cur_row[-1]["top"]) <= 8:
            cur_row.append(w)
        else:
            rows_by_y.append(sorted(cur_row, key=lambda w: w["x0"]))
            cur_row = [w]
    rows_by_y.append(sorted(cur_row, key=lambda w: w["x0"]))

    # Locate CW_Suspected and CW_Deaths column x-positions from headers
    cw_susp_x: Optional[float] = None
    cw_death_x: Optional[float] = None
    in_cw = False
    for row_words in rows_by_y:
        row_text = " ".join(w["text"] for w in row_words).lower()
        if "current" in row_text and ("week" in row_text or ":" in row_text):
            in_cw = True
        if not in_cw:
            continue
        for w in row_words:
            wt = w["text"].lower()
            if "suspected" in wt and cw_susp_x is None:
                cw_susp_x = w["x0"]
            if "deaths" in wt and cw_death_x is None and cw_susp_x is not None:
                cw_death_x = w["x0"]
        if cw_susp_x and cw_death_x:
            break

    if cw_susp_x is None:
        return pd.DataFrame()

    col_tol = 25.0  # pt tolerance for column matching

    records = []
    for row_words in rows_by_y:
        if not row_words:
            continue
        first = row_words[0]
        if first["x0"] > 80:
            continue
        try:
            rank = int(first["text"])
            if not (1 <= rank <= 50):
                continue
        except ValueError:
            continue

        # State name: words between rank and the CW_suspected column
        state_words = [
            w for w in row_words
            if w["x0"] > first["x0"] + 5 and w["x0"] < cw_susp_x - 5
        ]
        if not state_words:
            continue
        state_text = " ".join(w["text"] for w in state_words)
        state_val = _clean_state_name(state_text)
        if not state_val or _is_skip_row(state_val):
            continue
        canonical = normalise_state_name(state_val)
        if canonical and not canonical.startswith("UNKNOWN:"):
            state_val = canonical

        def _get_col(target_x: float, tol: float = col_tol) -> Optional[str]:
            candidates = [w for w in row_words if abs(w["x0"] - target_x) <= tol]
            if not candidates:
                return None
            return min(candidates, key=lambda w: abs(w["x0"] - target_x))["text"]

        susp = _safe_int(_get_col(cw_susp_x))
        deaths = _safe_int(_get_col(cw_death_x)) if cw_death_x else 0

        records.append({
            "state":           state_val,
            "disease":         "Meningitis",
            "epi_week":        epi_week,
            "year":            year,
            "suspected_cases": susp,
            "confirmed_cases": 0,
            "deaths":          deaths,
            "cfr_pct":         None,
            "_source_file":    pdf_path.name,
            "_data_type":      "current_week",
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # Sanity check: if aggregate CFR > 30%, the Deaths column was
    # misidentified (e.g. mapped to Confirmed Cases). Reset to 0.
    total_susp = df["suspected_cases"].sum()
    total_deaths = df["deaths"].sum()
    if total_susp > 0 and total_deaths / total_susp > 0.30:
        df["deaths"] = 0

    return df


def _parse_csm_from_ocr(
    pdf_path: Path,
    page_idx: int,
    epi_week: Optional[int],
    year: Optional[int],
) -> pd.DataFrame:
    """
    OCR-based CSM table extractor for image-embedded or CID-encoded pages.

    Renders the page at 300 DPI, runs pytesseract, then finds data rows
    by rank number and maps CW_Suspected / CW_Deaths by x-position.
    """
    try:
        from pdf2image import convert_from_path
        from PIL import ImageOps
        import pytesseract
    except ImportError:
        return pd.DataFrame()

    images = convert_from_path(
        str(pdf_path), dpi=300,
        first_page=page_idx + 1, last_page=page_idx + 1,
    )
    if not images:
        return pd.DataFrame()

    img = ImageOps.autocontrast(images[0].convert("L"))
    img_w, _ = img.size

    data = pytesseract.image_to_data(
        img, config="--psm 6 --oem 3",
        output_type=pytesseract.Output.DICT,
    )

    words = [
        {"text": t, "x0": float(data["left"][i]), "top": float(data["top"][i])}
        for i, t in enumerate(data["text"])
        if str(t).strip() and data["conf"][i] >= 20
    ]
    if not words:
        return pd.DataFrame()

    # Group into y-rows (within 10 pixels)
    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows_by_y: list[list] = []
    cur_row = [words_sorted[0]]
    for w in words_sorted[1:]:
        if abs(w["top"] - cur_row[-1]["top"]) <= 10:
            cur_row.append(w)
        else:
            rows_by_y.append(sorted(cur_row, key=lambda w: w["x0"]))
            cur_row = [w]
    rows_by_y.append(sorted(cur_row, key=lambda w: w["x0"]))

    # Find CW_Suspected and CW_Deaths x-positions
    cw_susp_x: Optional[float] = None
    cw_death_x: Optional[float] = None
    in_cw = False
    for row_words in rows_by_y:
        row_text = " ".join(w["text"] for w in row_words).lower()
        if "current" in row_text and "week" in row_text:
            in_cw = True
        if not in_cw:
            continue
        for w in row_words:
            wt = w["text"].lower()
            if "suspected" in wt and cw_susp_x is None:
                cw_susp_x = w["x0"]
            if "death" in wt and cw_death_x is None and cw_susp_x is not None:
                cw_death_x = w["x0"]
        if cw_susp_x and cw_death_x:
            break

    if cw_susp_x is None:
        return pd.DataFrame()

    col_tol = 60.0  # pixel tolerance (300 DPI image coords)
    records = []
    for row_words in rows_by_y:
        if not row_words:
            continue
        first = row_words[0]
        if first["x0"] > 200:
            continue
        try:
            rank = int(first["text"])
            if not (1 <= rank <= 50):
                continue
        except ValueError:
            continue

        state_words = [
            w for w in row_words
            if w["x0"] > first["x0"] + 5 and w["x0"] < cw_susp_x - 10
        ]
        if not state_words:
            continue
        state_text = " ".join(w["text"] for w in state_words)
        state_val = _clean_state_name(state_text)
        if not state_val or _is_skip_row(state_val):
            continue
        canonical = normalise_state_name(state_val)
        if canonical and not canonical.startswith("UNKNOWN:"):
            state_val = canonical

        def _get_col(target_x: float, tol: float = col_tol) -> Optional[str]:
            candidates = [w for w in row_words if abs(w["x0"] - target_x) <= tol]
            if not candidates:
                return None
            return min(candidates, key=lambda w: abs(w["x0"] - target_x))["text"]

        susp = _safe_int(_get_col(cw_susp_x))
        deaths = _safe_int(_get_col(cw_death_x)) if cw_death_x else 0

        records.append({
            "state":           state_val,
            "disease":         "Meningitis",
            "epi_week":        epi_week,
            "year":            year,
            "suspected_cases": susp,
            "confirmed_cases": 0,
            "deaths":          deaths,
            "cfr_pct":         None,
            "_source_file":    pdf_path.name,
            "_data_type":      "current_week",
        })

    return pd.DataFrame(records)


def _parse_csm_multirow_table(
    table: list[list],
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """
    Parse the CSM 'Weekly and Cumulative' state breakdown table.

    The table has a deep header (4–6 rows) structured as:
      Row 0: full title string
      Row 1 or 2: section spans — 'Current week: (Week N, Year)' | 'Cumulative ...'
      Row 2 or 3: 'States' label (merged over rank+state columns)
      Row 3 or 4: metric names — 'Suspected' | 'Deaths' | 'CFR%' | ...
      Row 4 or 5: sub-headers for serotypes or PCR

    Strategy:
    - Forward-fill 'current week' / 'cumulative' section from any header row.
    - Find the first 'Suspected' column inside the CW section → CW_Suspected.
    - Find the first 'Death' column inside the CW section → CW_Deaths.
    - Data rows: col 0 = rank (small integer), col 1 = state name.
    """
    if len(table) < 8:
        return pd.DataFrame()

    # Determine where header ends and data begins
    header_end = len(table)
    for i, row in enumerate(table):
        if not row:
            continue
        cell0 = str(row[0] or "").strip()
        if re.match(r"^\d{1,2}$", cell0) and 1 <= int(cell0) <= 40:
            header_end = i
            break

    if header_end < 3:
        return pd.DataFrame()

    header_rows = table[:header_end]
    n_cols = max((len(row) for row in header_rows if row), default=0)
    if n_cols == 0:
        return pd.DataFrame()

    # Forward-fill section labels across header rows.
    # Skip row 0 — it's the full table title ("Weekly and Cumulative number…")
    # whose "Cumulative" word would incorrectly mark every column as CUM.
    sections: list[Optional[str]] = [None] * n_cols
    for row in header_rows[1:]:
        cur_sec: Optional[str] = None
        for i, cell in enumerate(row or []):
            s = str(cell or "").lower()
            if "current" in s:
                cur_sec = "cw"
            elif "cumul" in s:
                cur_sec = "cum"
            if cur_sec and sections[i] is None:
                sections[i] = cur_sec

    # Propagate: fill None gaps between known sections
    last_sec: Optional[str] = None
    for i in range(n_cols):
        if sections[i] is not None:
            last_sec = sections[i]
        elif last_sec is not None:
            sections[i] = last_sec

    # Find CW_Suspected and CW_Deaths column indices
    cw_susp_idx:  Optional[int] = None
    cw_death_idx: Optional[int] = None

    for row in header_rows[1:]:
        for i, cell in enumerate(row or []):
            s = str(cell or "").lower().strip()
            if "suspected" in s and sections[i] == "cw" and cw_susp_idx is None:
                cw_susp_idx = i
            if "death" in s and sections[i] == "cw" and cw_death_idx is None:
                cw_death_idx = i

    if cw_susp_idx is None:
        return pd.DataFrame()

    records = []
    for row in table[header_end:]:
        if not row or len(row) < 2:
            continue
        cell0 = str(row[0] or "").strip()
        if not re.match(r"^\d{1,2}$", cell0):
            continue

        raw_state = str(row[1] or "").strip()
        state_val = _clean_state_name(raw_state)
        if not state_val or _is_skip_row(state_val) or re.match(r"^\d+$", state_val):
            continue
        canonical = normalise_state_name(state_val)
        if canonical and not canonical.startswith("UNKNOWN:"):
            state_val = canonical

        def _get(idx: Optional[int]) -> Optional[str]:
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        records.append({
            "state":            state_val,
            "disease":          "Meningitis",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  _safe_int(_get(cw_susp_idx)),
            "confirmed_cases":  0,
            "deaths":           _safe_int(_get(cw_death_idx)),
            "cfr_pct":          None,
            "_source_file":     pdf_path.name,
            "_data_type":       "current_week",
        })

    return pd.DataFrame(records)


# ── MPOX parser ───────────────────────────────────────────────────

_MPOX_BULLET_CHAR = "•"   # • (bullet)
_MPOX_ENDASH_CHAR = "–"   # – (en-dash, separates intro from state list)

_MPOX_KEY_IND_RE = re.compile(r"key\s+indicators?", re.I)

# Bullet paragraphs that are cumulative/all-time — skip entirely
_MPOX_SKIP_RE = re.compile(
    r"from\s+(?:1st\s+)?january"
    r"|from\s+september\s+2017"
    r"|overall[,\s]+since"
    r"|of\s+the\s+reported\s+cases"
    r"|a\s+total\s+of\s+\d+\s+(?:suspected|confirmed)\s+cases\s+has\s+been\s+reported\s+between"
    r"|protecting\s+the\s+health"
    r"|technical\s+working\s+group"
    r"|continues\s+(?:to\s+)?coord",
    re.I,
)

# A bullet is "current period" if it mentions a specific week/month
_MPOX_CURRENT_RE = re.compile(
    r"epi\s+week\s+\d+[,\s]+\d{4}"
    r"|in\s+week\s+\d+"
    r"|new\s+(?:suspected|confirmed|positive)"
    r"|in\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+20\d{2}",
    re.I,
)

# State (count) — optional opening paren handles typos like "Kebbi 1)"
_MPOX_STATE_COUNT_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+)?)\s*\(?(\d+)\)",
)


def _extract_mpox_state_counts(text: str) -> dict[str, int]:
    """Extract {canonical_state: count} from text segment 'Lagos (21), Bayelsa (4), ...'"""
    # Navigate past the en-dash separator to reach the state list
    endash_pos = text.find(_MPOX_ENDASH_CHAR)
    if endash_pos >= 0:
        text = text[endash_pos + 1:]
    # For "The reporting states are: –" pattern
    colon_m = re.search(r"are\s*:\s*", text, re.I)
    if colon_m:
        text = text[colon_m.end():]
        if text.startswith(_MPOX_ENDASH_CHAR):
            text = text[1:]

    # Fix missing open-paren: "Kebbi 1)" → "Kebbi (1)"
    text = re.sub(r"\b([A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+)?)\s+(\d+)\)", r"\1 (\2)", text)

    counts: dict[str, int] = {}
    for m in _MPOX_STATE_COUNT_RE.finditer(text):
        raw = m.group(1).strip()
        count = int(m.group(2))
        canonical = normalise_state_name(raw)
        if canonical and canonical in CANONICAL_STATE_SET:
            counts[canonical] = counts.get(canonical, 0) + count
    return counts


def _parse_mpox_bullet(bullet: str) -> tuple[dict, dict]:
    """Return (confirmed_states, suspected_states) dicts from one bullet paragraph."""
    has_confirmed = bool(re.search(r"new\s+confirmed|new\s+positive", bullet, re.I))
    has_suspected = bool(re.search(r"new\s+suspected", bullet, re.I))
    reporting_m = re.search(r"the\s+reporting\s+states\s+are\s*:", bullet, re.I)

    if has_confirmed and reporting_m:
        # Combined bullet: confirmed state list then suspected list after marker
        conf_part = bullet[: reporting_m.start()]
        susp_part = bullet[reporting_m.start():]
        return _extract_mpox_state_counts(conf_part), _extract_mpox_state_counts(susp_part)
    elif has_confirmed:
        return _extract_mpox_state_counts(bullet), {}
    elif has_suspected:
        return {}, _extract_mpox_state_counts(bullet)
    else:
        return {}, {}


def _parse_mpox_from_key_indicators(page_texts: list[str], pdf_path: Path) -> pd.DataFrame:
    """
    Extract current-week state counts from Key Indicators / Highlights bullet paragraphs.

    Three report formats are handled:
    • Mid-2022: separate suspected + confirmed bullets under EPIDEMIOLOGICAL SUMMARY
    • Late-2022: combined suspected+confirmed in one bullet ("The reporting states are:")
    • 2023: Highlights section; suspected by state in first bullet, confirmed national-only
    """
    epi_week, year = _extract_epi_week_year(pdf_path, " ".join(page_texts))

    confirmed_states: dict[str, int] = {}
    suspected_states: dict[str, int] = {}

    for page_text in page_texts:
        if not _MPOX_KEY_IND_RE.search(page_text):
            continue

        bullets = page_text.split(_MPOX_BULLET_CHAR)
        for bullet in bullets[1:]:   # skip pre-bullet table text
            bullet = bullet.strip()
            if not bullet:
                continue
            if _MPOX_SKIP_RE.search(bullet):
                continue
            if not _MPOX_CURRENT_RE.search(bullet):
                continue

            conf, susp = _parse_mpox_bullet(bullet)
            for state, cnt in conf.items():
                confirmed_states[state] = confirmed_states.get(state, 0) + cnt
            for state, cnt in susp.items():
                suspected_states[state] = suspected_states.get(state, 0) + cnt

    all_states = set(confirmed_states) | set(suspected_states)
    if not all_states:
        return pd.DataFrame()

    records = []
    for state in sorted(all_states):
        records.append({
            "state":            state,
            "disease":          "Mpox",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  suspected_states.get(state, 0),
            "confirmed_cases":  confirmed_states.get(state, 0),
            "deaths":           0,
            "cfr_pct":          None,
            "_source_file":     pdf_path.name,
            "_data_type":       "current_week",
        })

    logger.info(
        "Mpox: Key Indicators extracted %d states (conf=%d, susp=%d) from %s",
        len(records),
        sum(confirmed_states.values()),
        sum(suspected_states.values()),
        pdf_path.name,
    )
    return pd.DataFrame(records)


def parse_mpox(
    raw_tables: list[list[list]],
    pdf_path: Path,
    page_texts: list[str],
) -> pd.DataFrame:
    """
    Parse NCDC Mpox (Monkeypox) Weekly Update.

    Strategy 1 — Key Indicators paragraph bullets → current_week rows per state.
    Strategy 2 — Table 2 (annual confirmed by state) → annual_cumulative rows.
    """
    # Strategy 1: paragraph-based Key Indicators extractor
    try:
        result = _parse_mpox_from_key_indicators(page_texts, pdf_path)
        if not result.empty:
            return result
    except Exception as exc:
        logger.warning("Mpox: Key Indicators extraction failed for %s: %s", pdf_path.name, exc)

    # Strategy 2: fall back to Table 2 (annual cumulative)
    full_text = " ".join(page_texts)
    _, report_year = _extract_epi_week_year(pdf_path, full_text)

    year_table = None
    for table in raw_tables:
        if not table or len(table) < 5:
            continue
        header = " ".join(str(c or "").strip() for c in table[0])
        if re.search(r"201[789]|202[0-9]", header) and "state" in header.lower():
            year_table = table
            break

    if year_table is None:
        logger.warning("Mpox: no Key Indicators data or year-table in %s", pdf_path.name)
        return pd.DataFrame()

    df = _raw_table_to_df(year_table)
    if df.empty:
        return df

    return _map_mpox_columns(df, pdf_path)


def _map_mpox_columns(
    df: pd.DataFrame,
    pdf_path: Path,
) -> pd.DataFrame:
    """
    Expand Mpox year-columns into one row per (state, year).
    """
    rows = []

    state_col = _find_col(df, ["state", "states"])
    if state_col is None:
        return pd.DataFrame()

    # Find year columns
    year_cols = {}
    for col in df.columns:
        match = re.search(r"(20\d{2})", str(col))
        if match:
            year_cols[int(match.group(1))] = col

    if not year_cols:
        return pd.DataFrame()

    for _, row in df.iterrows():
        state_val = _clean_state_name(str(row[state_col] or ""))
        if not state_val or _is_skip_row(state_val):
            continue
        if re.match(r"^\d+$", state_val):
            continue

        for year, col in year_cols.items():
            confirmed = _safe_int(row.get(col))
            if confirmed == 0:
                continue    # Skip zero-case years to keep data sparse
            rows.append({
                "state":            state_val,
                "disease":          "Mpox",
                "epi_week":         None,    # Annual data — no week available
                "year":             year,
                "suspected_cases":  confirmed,   # Only confirmed data available
                "confirmed_cases":  confirmed,
                "deaths":           0,
                "cfr_pct":          None,
                "_source_file":     pdf_path.name,
                "_data_type":       "annual_cumulative",
            })

    return pd.DataFrame(rows)


# ── YELLOW FEVER parser ───────────────────────────────────────────

_YF_BULLET_RE      = re.compile(r"[▪]")
_YF_HIGHLIGHTS_RE  = re.compile(r"\bHIGHLIGHTS\b", re.I)
_YF_CUMULATIVE_RE  = re.compile(r"\bCUM{1,2}ULATIVE\s*FOR", re.I)

_YF_SKIP_BULLET_RE = re.compile(
    r"continues?\s+to\s+monitor"
    r"|continuestomonitor"           # CID-encoded variant (no spaces)
    r"|presumptive"
    r"|inconclusive"
    r"|confirmed"                    # no \b — CID text has "confirmedfrom"
    r"|\bdeath\b"
    r"|male.{0,10}female"
    r"|predominantly\s+aged"
    r"|received\s+at\s+least"
    r"|coord(?:inating|ination)"
    r"|technical\s+working\s+group"
    r"|reporting\s+period",
    re.I,
)

# State (n) — negative lookahead (?!\s*\[) excludes "State (n) [LGA breakdown]"
_YF_STATE_COUNT_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:[, \-][A-Z][a-zA-Z]+)*)\s*\((\d+)\)(?!\s*\[)"
)

# Deaths: "from State state." — single state, count=1 (no parenthesised number)
_YF_DEATH_SINGLE_RE = re.compile(r"from\s+([A-Z][a-z]{1,20})\s+[Ss]tate\b")

# Confirmed: "State -N [LGA]" format (Institut Pasteur / IP Dakar style)
_YF_CONF_DASH_RE = re.compile(r"\b([A-Z][a-z]{1,20})\s*-\s*(\d+)\s*\[")

# Confirmed: "State (N)" without negative lookahead — used only after "reported from"
_YF_CONF_PAREN_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:[, \-][A-Z][a-zA-Z]+)*)\s*\((\d+)\)"
)


def _parse_yf_from_highlights(page_texts: list[str], pdf_path: Path) -> pd.DataFrame:
    """Extract current-period state suspected/confirmed/deaths from the HIGHLIGHTS section."""
    full_text = "\n".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    hi_m = _YF_HIGHLIGHTS_RE.search(full_text)
    if not hi_m:
        return pd.DataFrame()

    text_after = full_text[hi_m.end():]

    # Stop before the cumulative section (separate page or clearly demarcated)
    cum_m = _YF_CUMULATIVE_RE.search(text_after)
    highlights_text = text_after[: cum_m.start()] if cum_m else text_after

    parts = _YF_BULLET_RE.split(highlights_text)

    state_counts:    dict[str, int] = {}
    state_confirmed: dict[str, int] = {}
    state_deaths:    dict[str, int] = {}

    for part in parts:
        # Collapse internal newlines to spaces (handles "Cross\nRiver (2)")
        part = re.sub(r"\n+", " ", part).strip()
        if not part:
            continue

        is_death     = bool(re.search(r"\bdeath", part, re.I)) and not bool(re.search(r"\bno\s+death", part, re.I))
        is_confirmed = bool(re.search(r"confirm", part, re.I))

        if is_death:
            # CID preprocessing
            part_d = re.sub(r"\band([A-Z])", r"and \1", part)
            found = False
            for raw_name, count_str in _YF_STATE_COUNT_RE.findall(part_d):
                raw_name = re.sub(r",\s*", " ", raw_name).strip()
                canonical = normalise_state_name(raw_name)
                if canonical and canonical in CANONICAL_STATE_SET:
                    state_deaths[canonical] = state_deaths.get(canonical, 0) + int(count_str)
                    found = True
            if not found:
                # "from State state." — single state, deaths=1
                m = _YF_DEATH_SINGLE_RE.search(part_d)
                if m:
                    canonical = normalise_state_name(m.group(1))
                    if canonical and canonical in CANONICAL_STATE_SET:
                        state_deaths[canonical] = state_deaths.get(canonical, 0) + 1
            continue

        if is_confirmed:
            part_c = re.sub(r"\band([A-Z])", r"and \1", part)
            has_dash = False
            for m in _YF_CONF_DASH_RE.finditer(part_c):
                canonical = normalise_state_name(m.group(1))
                if canonical and canonical in CANONICAL_STATE_SET:
                    state_confirmed[canonical] = state_confirmed.get(canonical, 0) + int(m.group(2))
                    has_dash = True
            if not has_dash:
                # Restrict paren search to after "reported from" to avoid lab-city names
                from_m = re.search(r"reported\s+from\s*[:\s]|from:\s*", part_c, re.I)
                search_text = part_c[from_m.end():] if from_m else ""
                for raw_name, count_str in _YF_CONF_PAREN_RE.findall(search_text):
                    raw_name = re.sub(r",\s*", " ", raw_name).strip()
                    canonical = normalise_state_name(raw_name)
                    if canonical and canonical in CANONICAL_STATE_SET:
                        state_confirmed[canonical] = state_confirmed.get(canonical, 0) + int(count_str)
            continue

        if _YF_SKIP_BULLET_RE.search(part):
            continue

        # CID text: "andZamfara(4)" → "and Zamfara(4)"
        part = re.sub(r"\band([A-Z])", r"and \1", part)

        for raw_name, count_str in _YF_STATE_COUNT_RE.findall(part):
            # "FCT, Abuja" → "FCT Abuja" → normalise → "FCT"
            raw_name = re.sub(r",\s*", " ", raw_name).strip()
            canonical = normalise_state_name(raw_name)
            if canonical and canonical in CANONICAL_STATE_SET:
                state_counts[canonical] = state_counts.get(canonical, 0) + int(count_str)

    if not state_counts and not state_confirmed and not state_deaths:
        return pd.DataFrame()

    all_states = sorted(set(state_counts) | set(state_confirmed) | set(state_deaths))
    records = [
        {
            "state":            state,
            "disease":          "Yellow Fever",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  state_counts.get(state, 0),
            "confirmed_cases":  state_confirmed.get(state, 0),
            "deaths":           state_deaths.get(state, 0),
            "cfr_pct":          None,
            "_source_file":     pdf_path.name,
            "_data_type":       "current_highlights",
        }
        for state in all_states
    ]

    logger.info(
        "Yellow Fever: HIGHLIGHTS extracted %d states (susp=%d, conf=%d, deaths=%d) from %s",
        len(records),
        sum(state_counts.values()),
        sum(state_confirmed.values()),
        sum(state_deaths.values()),
        pdf_path.name,
    )
    return pd.DataFrame(records)


def parse_yellow_fever(
    raw_tables: list[list[list]],
    pdf_path: Path,
    page_texts: list[str],
) -> pd.DataFrame:
    """
    Parse NCDC Yellow Fever Monthly Situation Report.

    Strategy 1: HIGHLIGHTS section paragraph → current_highlights rows
    Strategy 2: Table 1 (cumulative summary) → monthly_cumulative rows
    """
    # Strategy 1 — HIGHLIGHTS paragraph
    df_hi = _parse_yf_from_highlights(page_texts, pdf_path)
    if not df_hi.empty:
        return df_hi

    full_text = " ".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    # Strategy 2 — YF table 1 (wide table with confirmed + suspected columns)
    state_table = _find_state_table(
        raw_tables,
        min_rows=10,
        required_keywords=["suspected", "confirmed"],
    )

    if state_table is None:
        state_table = _find_state_table(raw_tables, min_rows=5)

    if state_table is None:
        logger.warning("Yellow Fever: no state table found in %s", pdf_path.name)
        return pd.DataFrame()

    df = _raw_table_to_df(state_table)
    if df.empty:
        return df

    return _map_yellow_fever_columns(df, epi_week, year, pdf_path)


def _map_yellow_fever_columns(
    df: pd.DataFrame,
    epi_week: Optional[int],
    year: Optional[int],
    pdf_path: Path,
) -> pd.DataFrame:
    """Map raw Yellow Fever table to standardised output."""
    rows = []

    state_col     = _find_col(df, ["state", "states"])
    suspected_col = _find_col(df, ["suspected"])
    # YF has multiple confirmed columns — prefer 'Total Confirmed'
    total_conf_col = _find_col(df, ["total confirmed"])
    if total_conf_col is None:
        confirmed_cols = [c for c in df.columns if _contains(c, "confirmed")]
        total_conf_col = confirmed_cols[-1] if confirmed_cols else None

    deaths_col = _find_col(df, ["death", "deaths"])
    cfr_col    = _find_col(df, ["cfr", "case fatality"])

    if state_col is None:
        return pd.DataFrame()

    for _, row in df.iterrows():
        state_val = _clean_state_name(str(row[state_col] or ""))
        if not state_val or _is_skip_row(state_val):
            continue

        rows.append({
            "state":            state_val,
            "disease":          "Yellow Fever",
            "epi_week":         epi_week,
            "year":             year,
            "suspected_cases":  _safe_int(row.get(suspected_col))   if suspected_col  else 0,
            "confirmed_cases":  _safe_int(row.get(total_conf_col))  if total_conf_col else 0,
            "deaths":           _safe_int(row.get(deaths_col))      if deaths_col     else 0,
            "cfr_pct":          _safe_float(row.get(cfr_col))       if cfr_col        else None,
            "_source_file":     pdf_path.name,
            "_data_type":       "monthly_cumulative",
        })

    return pd.DataFrame(rows)




# ── Text-based fallback parsers ───────────────────────────────────
# Many NCDC PDFs use CID-encoded fonts for the main state-breakdown
# table, making it unreadable by pdfplumber. These functions parse
# whatever IS readable: the Top-10 table (Cholera) and the summary
# row + highlighted states (Lassa, Meningitis).

def parse_cholera_from_text(page_texts: list[str], pdf_path: Path) -> pd.DataFrame:
    """
    Extract Cholera current-week and cumulative data from readable page text.

    Source A — Current-week active states (PRIORITY):
      Page 2 Highlights contains the sentence:
      "In the reporting week, Borno(24) Gombe (14), Bauchi(13), Kano (5),
       Katsina (1) andAdamawa (1) reported 58 suspected cases"
      Regex captures State(N) patterns from within this sentence.
      Space before ( is optional — NCDC formatting is inconsistent.

    Source B — Top-10 cumulative fallback:
      Table 4 pattern: "1 Borno 12,459 53% 53%"
      Used only when Source A yields no results.
    """
    full_text = " ".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    # ── Source A: current-week from "In the reporting week" sentence ──
    week_m = re.search(
        r"In the reporting week[,.]?\s*(.*?)\s*reported\s*\d+\s*suspected",
        full_text, re.I | re.S
    )

    # Extract national weekly death total (handles CID: "therewas2deaths" and normal text)
    _death_m = re.search(r"there\s*(?:was|were)\s*(\d+)\s*deaths?", full_text, re.I)
    _total_deaths = int(_death_m.group(1)) if _death_m else 0

    _cfr_m = re.search(
        r"weekly\s*case\s*fatality\s*(?:ratio\s*)?\(CFR\)\s*of\s*([\d.]+)\s*%", full_text, re.I
    )
    _weekly_cfr = float(_cfr_m.group(1)) if _cfr_m else 0.0

    if week_m:
        section   = week_m.group(1)
        state_re  = re.compile(r"([A-Za-z][a-zA-Z\s]{1,20}?)\s*\((\d+)\)")
        rows      = []
        for m in state_re.finditer(section):
            # Strip leading "and" that sometimes concatenates with state name
            state_raw = re.sub(r"^and\s*", "", m.group(1), flags=re.I).strip()
            state_raw = state_raw.rstrip(",").strip()
            count     = int(m.group(2))
            canonical = normalise_state_name(state_raw)
            if canonical and canonical in CANONICAL_STATE_SET and count > 0:
                rows.append({
                    "state":           canonical,
                    "disease":         "Cholera",
                    "epi_week":        epi_week,
                    "year":            year,
                    "suspected_cases": count,
                    "confirmed_cases": 0,
                    "deaths":          0,
                    "cfr_pct":         0.0,
                    "_source_file":    pdf_path.name,
                    "_data_type":      "current_week_highlights",
                })
        if rows:
            # Distribute national death total proportionally across states
            if _total_deaths > 0:
                total_susp = sum(r["suspected_cases"] for r in rows)
                if total_susp > 0:
                    # Proportional allocation with rounding; remainder goes to largest state
                    order = sorted(range(len(rows)), key=lambda i: -rows[i]["suspected_cases"])
                    allocated = 0
                    for idx in order:
                        share = round(rows[idx]["suspected_cases"] / total_susp * _total_deaths)
                        rows[idx]["deaths"] = share
                        allocated += share
                    rows[order[0]]["deaths"] += _total_deaths - allocated  # fix rounding

            # Set CFR on all rows from the national weekly CFR
            if _weekly_cfr:
                for r in rows:
                    r["cfr_pct"] = _weekly_cfr

            df = pd.DataFrame(rows).drop_duplicates(subset=["state"])
            logger.info(
                "Cholera: %d current-week states (deaths=%d, CFR=%.1f%%) from highlights in %s",
                len(df), _total_deaths, _weekly_cfr, pdf_path.name
            )
            return df

    # ── Source B: top-10 cumulative states ────────────────────────
    rows    = []
    pattern = re.compile(
        r"\b(\d{1,2})\s+([A-Z][a-zA-Z\s]+?)\s+([\d,]+)\s+(\d+)%",
        re.MULTILINE
    )
    for m in pattern.finditer(full_text):
        rank      = int(m.group(1))
        state_raw = m.group(2).strip()
        cases_str = m.group(3).replace(",", "")
        if rank > 15:
            continue
        try:
            cases = int(cases_str)
        except ValueError:
            continue
        if cases < 1:
            continue
        canonical = normalise_state_name(state_raw)
        if canonical not in CANONICAL_STATE_SET:
            continue
        rows.append({
            "state":           canonical,
            "disease":         "Cholera",
            "epi_week":        epi_week,
            "year":            year,
            "suspected_cases": cases,
            "confirmed_cases": cases,
            "deaths":          0,
            "cfr_pct":         None,
            "_source_file":    pdf_path.name,
            "_data_type":      "top10_cumulative",
        })

    if rows:
        return pd.DataFrame(rows).drop_duplicates(subset=["state"])
    return pd.DataFrame()


def parse_lassa_from_text(page_texts: list[str], pdf_path: Path) -> pd.DataFrame:
    """
    Extract Lassa Fever data from page 1 summary text.

    What IS readable:
      - Table 1: national totals (suspected, confirmed, deaths, CFR)
        for current week, 2023 cumulative, 2022 cumulative

    Returns 1 row with national totals (state = 'National').
    Individual state breakdown requires the CID-font table.
    """
    full_text = " ".join(page_texts[:2])  # summary on page 1
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    # Pattern for cumulative row: large numbers near "17.3%" or similar CFR
    # "6714 1021 9 177 17.3%"
    cum_match = re.search(
        r"(\d{3,5})\s+(\d{3,4})\s+\d+\s+(\d{1,3})\s+([\d.]+)%",
        full_text
    )
    if cum_match:
        suspected  = int(cum_match.group(1))
        confirmed  = int(cum_match.group(2))
        deaths     = int(cum_match.group(3))
        cfr        = float(cum_match.group(4))

        return pd.DataFrame([{
            "state":           "National",
            "disease":         "Lassa Fever",
            "epi_week":        epi_week,
            "year":            year,
            "suspected_cases": suspected,
            "confirmed_cases": confirmed,
            "deaths":          deaths,
            "cfr_pct":         cfr,
            "_source_file":    pdf_path.name,
            "_data_type":      "national_cumulative",
            "_note":           "State table uses CID font — unreadable by pdfplumber",
        }])

    return pd.DataFrame()


def parse_meningitis_from_text(page_texts: list[str], pdf_path: Path) -> pd.DataFrame:
    """
    Extract Meningitis current-week data from readable page text.

    Source A — Current-week states (PRIORITY):
      Page 2 contains the section:
      "Reportingweek29(5) (5)suspectedCSMcases werereportedintwo(2)states,
       Jigawa(3casesand1confirmed)andGombe(2cases)"
      Pattern: StateName(N cases...) — state immediately followed by (digit cases)

    Source B — National cumulative totals (fallback):
      Page 1 Table 1: "2733 303 187 6.9%"
    """
    full_text = " ".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    # ── Source A: current-week states ─────────────────────────────
    # Scope to the "Reporting week N" section only, stopping before
    # "No death", "Cumulative", or "NationalEpi" — this prevents
    # the cumulative summary sentence ("Jigawa(1512cases)...") from
    # being mistakenly parsed as current-week data.
    section_m = re.search(
        r"[Rr]eporting\s*week\s*\d+.*?(?:No\s*death|Cumulative|NationalEpi|\Z)",
        full_text, re.S | re.I
    )
    rows = []
    if section_m:
        section = section_m.group(0)

        # "Jigawa (6 cases)", "Bauchi (1 case)" — match both singular and plural
        men_re = re.compile(r"([A-Z][a-zA-Z]{2,20})\s*\((\d+)\s*case")
        state_deaths: dict[str, int] = {}

        # Extract per-state deaths: "Yobe (17) and jigawa (6)" after
        # "deaths recorded from" — handles no-space CID-encoded text and
        # lowercase state names (e.g. "jigawa" from garbled font rendering).
        death_section_m = re.search(
            r"death\w*\s*recorded\s*from\s*(.*?)(?:No\s*LGA|threshold|\Z)",
            section, re.I | re.S
        )
        if death_section_m:
            # Split "andjigawa" → "and jigawa" so the state name is a separate token
            death_text = re.sub(r"and([A-Za-z])", r"and \1", death_section_m.group(1))
            death_re = re.compile(r"([A-Za-z][a-zA-Z]{2,20})\s*\((\d+)\)")
            for dm in death_re.finditer(death_text):
                cname = normalise_state_name(dm.group(1).strip())
                if cname and cname in CANONICAL_STATE_SET:
                    state_deaths[cname] = int(dm.group(2))

        for m in men_re.finditer(section):
            state_raw = re.sub(r"^and", "", m.group(1), flags=re.I).strip()
            count     = int(m.group(2))
            canonical = normalise_state_name(state_raw)
            if canonical and canonical in CANONICAL_STATE_SET and count > 0:
                rows.append({
                    "state":           canonical,
                    "disease":         "Meningitis",
                    "epi_week":        epi_week,
                    "year":            year,
                    "suspected_cases": count,
                    "confirmed_cases": 0,
                    "deaths":          state_deaths.get(canonical, 0),
                    "cfr_pct":         0.0,
                    "_source_file":    pdf_path.name,
                    "_data_type":      "current_week_highlights",
                })

    if rows:
        df = pd.DataFrame(rows).drop_duplicates(subset=["state"])
        logger.info(
            "Meningitis: %d current-week states from highlights in %s",
            len(df), pdf_path.name
        )
        return df

    # ── Source B: national cumulative totals ──────────────────────
    cum_match = re.search(
        r"(\d{3,5})\s+(\d{2,4})\s+(\d{1,4})\s+([\d.]+)%",
        full_text
    )
    if cum_match:
        return pd.DataFrame([{
            "state":           "National",
            "disease":         "Meningitis",
            "epi_week":        epi_week,
            "year":            year,
            "suspected_cases": int(cum_match.group(1)),
            "confirmed_cases": int(cum_match.group(2)),
            "deaths":          int(cum_match.group(3)),
            "cfr_pct":         float(cum_match.group(4)),
            "_source_file":    pdf_path.name,
            "_data_type":      "national_cumulative",
            "_note":           "State table uses CID font — unreadable by pdfplumber",
        }])

    return pd.DataFrame()


# ── Main dispatch function ────────────────────────────────────────

def parse_pdf_by_disease(
    raw_tables: list[list[list]],
    disease_name: str,
    pdf_path: Path,
    page_texts: list[str],
) -> pd.DataFrame:
    """
    Dispatch to the correct disease-specific parser.

    Parameters
    ----------
    raw_tables : list of raw pdfplumber table extractions
    disease_name : str — must match Diseases.* constants
    pdf_path : Path — used for provenance and date extraction
    page_texts : list[str] — full text of each PDF page

    Returns
    -------
    pd.DataFrame
        Standardised output with columns:
        state | disease | epi_week | year |
        suspected_cases | confirmed_cases | deaths |
        cfr_pct | _source_file | _data_type
    """
    disease_lower = disease_name.lower().replace(" ", "_")

    # Primary parsers -- table-based extraction
    table_parsers = {
        "cholera":      parse_cholera,
        "lassa_fever":  parse_lassa,
        "lassa":        parse_lassa,
        "mpox":         parse_mpox,
        "monkeypox":    parse_mpox,
        "meningitis":   parse_meningitis,
        "yellow_fever": parse_yellow_fever,
        "yellow":       parse_yellow_fever,
    }

    # Text-based parsers extract current-week data from readable highlights text.
    # Cholera, Lassa and Meningitis are NOT here — their table parsers handle
    # text fallback internally, so the table parser must run first.
    text_parsers: dict = {}

    parser_fn = table_parsers.get(disease_lower)
    if parser_fn is None:
        logger.warning(
            "No parser defined for disease '%s' -- using generic extractor",
            disease_name,
        )
        return _generic_parse(raw_tables, disease_name, pdf_path, page_texts)

    # For diseases with text parsers, try text first to get current-week data
    if disease_lower in text_parsers:
        try:
            text_result = text_parsers[disease_lower](page_texts, pdf_path)
            if not text_result.empty:
                return text_result
        except Exception as exc:
            logger.warning("Text parser failed for %s: %s", disease_name, exc)

    # Fall back to table-based parser
    try:
        result = parser_fn(raw_tables, pdf_path, page_texts)
    except Exception as exc:
        logger.error(
            "Parser failed for %s (%s): %s",
            disease_name, pdf_path.name, exc,
        )
        result = pd.DataFrame()

    return result



def _generic_parse(
    raw_tables: list[list[list]],
    disease_name: str,
    pdf_path: Path,
    page_texts: list[str],
) -> pd.DataFrame:
    """
    Fallback parser for diseases without a specific parser.
    Attempts to find any state-breakdown table and extract it.
    """
    full_text = " ".join(page_texts)
    epi_week, year = _extract_epi_week_year(pdf_path, full_text)

    state_table = _find_state_table(raw_tables, min_rows=5)
    if state_table is None:
        return pd.DataFrame()

    df = _raw_table_to_df(state_table)
    if df.empty:
        return df

    state_col = _find_col(df, ["state", "states"])
    if state_col is None:
        return pd.DataFrame()

    rows = []
    for _, row in df.iterrows():
        state_val = str(row.get(state_col) or "").strip()
        if not state_val or _is_skip_row(state_val):
            continue
        rows.append({
            "state":           state_val,
            "disease":         disease_name,
            "epi_week":        epi_week,
            "year":            year,
            "suspected_cases": 0,
            "confirmed_cases": 0,
            "deaths":          0,
            "cfr_pct":         None,
            "_source_file":    pdf_path.name,
            "_data_type":      "generic",
        })

    return pd.DataFrame(rows)


# ── Shared column-finding helper ─────────────────────────────────

def _find_col(
    df: pd.DataFrame,
    keywords: list[str],
) -> Optional[str]:
    """
    Find the first DataFrame column whose normalised name contains
    any of the given keywords. Supports simple regex patterns.

    Returns the actual column name, or None if not found.
    """
    for col in df.columns:
        col_norm = _norm(col)
        for kw in keywords:
            if re.search(kw.lower(), col_norm):
                return col
    return None


def _is_skip_row(value: str) -> bool:
    """
    Return True if this value looks like a row to skip
    (totals, headers, blank lines, response text).
    """
    skip_patterns = [
        r"^total",
        r"^national",
        r"^grand total",
        r"^states$",
        r"^s/n",
        r"^#$",
        r"^\s*$",
        r"^no\s",
        # Meningitis response activity rows
        r"^coordination",
        r"^surveillance",
        r"^laboratory",
        r"^vaccination",
        r"^logistics",
        r"^case management",
        r"^risk comm",
        r"^state response",
        r"^continue",
        r"^\u25cf",   # bullet point
        r"^\uf0b7",   # encoded bullet
        r"^pillar",
        r"^activities",
    ]
    val_lower = value.lower().strip()

    # Also skip any value longer than 80 chars — must be free text not a state name
    if len(value.strip()) > 80:
        return True

    return any(re.match(p, val_lower) for p in skip_patterns)


def _clean_state_name(value: str) -> str:
    """
    Clean a raw state name extracted from a PDF cell or OCR line.
    Handles:
      - Newlines embedded in cell text (e.g. 'Akwa\\nIbom' → 'Akwa Ibom')
      - Leading row numbers (e.g. '1Abia', '3)Bauchi', '13|Gombe')
      - Pipe, slash, bracket, parenthesis delimiters after rank numbers
      - Extra whitespace
    """
    # Replace newlines and tabs with space
    cleaned = re.sub(r"[\n\r\t]+", " ", str(value)).strip()
    # Remove leading rank number + any delimiter character
    # Handles: "1 Abia", "32Abia", "3)Bauchi", "13|Gombe", "3/Akwa"
    cleaned = re.sub(r"^\d+[|/\[\]()\.\s]*", "", cleaned).strip()
    # Collapse multiple spaces
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned
