"""
src/utils/state_maps.py
────────────────────────────────────────────────────────────────
Ground truth reference data for Nigerian states.

This module owns three things:
  1. The canonical list of 37 administrative units (36 states + FCT)
  2. A mapping from every known name variant to the canonical form
  3. Geopolitical zone assignments for each state
  4. Approximate centroid coordinates used for the NASA rainfall API

Why a dedicated module?
  State name mismatches are one of the most common data quality
  issues in Nigerian public health datasets. NCDC, WHO, NBS, and
  HDX all spell state names differently. Centralising the mapping
  here means we fix a typo once and every pipeline benefits.

────────────────────────────────────────────────────────────────
"""

from __future__ import annotations


# ── Canonical state list ─────────────────────────────────────────
# This is the single source of truth. Every other list, mapping,
# and validation check derives from this.

CANONICAL_STATES: list[str] = [
    "Abia",
    "Adamawa",
    "Akwa Ibom",
    "Anambra",
    "Bauchi",
    "Bayelsa",
    "Benue",
    "Borno",
    "Cross River",
    "Delta",
    "Ebonyi",
    "Edo",
    "Ekiti",
    "Enugu",
    "FCT",           # Federal Capital Territory (Abuja)
    "Gombe",
    "Imo",
    "Jigawa",
    "Kaduna",
    "Kano",
    "Katsina",
    "Kebbi",
    "Kogi",
    "Kwara",
    "Lagos",
    "Nasarawa",
    "Niger",
    "Ogun",
    "Ondo",
    "Osun",
    "Oyo",
    "Plateau",
    "Rivers",
    "Sokoto",
    "Taraba",
    "Yobe",
    "Zamfara",
]

# Quick membership check — O(1) instead of O(n) list scan
CANONICAL_STATE_SET: frozenset[str] = frozenset(CANONICAL_STATES)


# ── Name variant → canonical mapping ─────────────────────────────
# Keys are lowercase, stripped, and have common suffixes removed.
# The transform module normalises input before looking it up here.
#
# Ordering: more specific variants first, generic fallbacks last.

STATE_NAME_VARIANTS: dict[str, str] = {
    # FCT / Abuja
    "fct":                       "FCT",
    "f.c.t":                     "FCT",
    "f.c.t.":                    "FCT",
    "fct-abuja":                 "FCT",
    "fct (abuja)":               "FCT",
    "abuja":                     "FCT",
    "federal capital territory": "FCT",
    "federal capital terr":      "FCT",

    # Akwa Ibom
    "akwa ibom":   "Akwa Ibom",
    "akwaibom":    "Akwa Ibom",
    "akwa-ibom":   "Akwa Ibom",
    "a/ibom":      "Akwa Ibom",

    # Cross River
    "cross river":   "Cross River",
    "crossriver":    "Cross River",
    "cross-river":   "Cross River",
    "c/river":       "Cross River",

    # Niger — must not be confused with the country Nigeria
    "niger":  "Niger",

    # Nasarawa — often misspelled
    "nassarawa": "Nasarawa",
    "nasarawa":  "Nasarawa",

    # All other states map directly (lowercase → title case)
    "abia":      "Abia",
    "adamawa":   "Adamawa",
    "anambra":   "Anambra",
    "bauchi":    "Bauchi",
    "bayelsa":   "Bayelsa",
    "benue":     "Benue",
    "borno":     "Borno",
    "delta":     "Delta",
    "ebonyi":    "Ebonyi",
    "edo":       "Edo",
    "ekiti":     "Ekiti",
    "enugu":     "Enugu",
    "gombe":     "Gombe",
    "imo":       "Imo",
    "jigawa":    "Jigawa",
    "kaduna":    "Kaduna",
    "kano":      "Kano",
    "katsina":   "Katsina",
    "kebbi":     "Kebbi",
    "kogi":      "Kogi",
    "kwara":     "Kwara",
    "lagos":     "Lagos",
    "ogun":      "Ogun",
    "ondo":      "Ondo",
    "osun":      "Osun",
    "oyo":       "Oyo",
    "plateau":   "Plateau",
    "rivers":    "Rivers",
    "sokoto":    "Sokoto",
    "taraba":    "Taraba",
    "yobe":      "Yobe",
    "zamfara":   "Zamfara",

    # ── Rows that should be dropped (national totals) ─────────
    "nigeria":     "NATIONAL",
    "national":    "NATIONAL",
    "total":       "NATIONAL",
    "grand total": "NATIONAL",
    "subtotal":    "NATIONAL",
    "all states":  "NATIONAL",
}

# Sentinel value used to mark rows that represent national aggregates
NATIONAL_SENTINEL = "NATIONAL"


# ── Geopolitical zones ───────────────────────────────────────────
# Nigeria's six geopolitical zones are widely used in health
# reporting and allow regional aggregation.

GEOPOLITICAL_ZONES: dict[str, str] = {
    "Borno":       "North-East",
    "Adamawa":     "North-East",
    "Yobe":        "North-East",
    "Bauchi":      "North-East",
    "Gombe":       "North-East",
    "Taraba":      "North-East",

    "Kano":        "North-West",
    "Kaduna":      "North-West",
    "Katsina":     "North-West",
    "Jigawa":      "North-West",
    "Sokoto":      "North-West",
    "Kebbi":       "North-West",
    "Zamfara":     "North-West",

    "Niger":       "North-Central",
    "Kogi":        "North-Central",
    "Kwara":       "North-Central",
    "Benue":       "North-Central",
    "Plateau":     "North-Central",
    "Nasarawa":    "North-Central",
    "FCT":         "North-Central",

    "Lagos":       "South-West",
    "Oyo":         "South-West",
    "Ogun":        "South-West",
    "Osun":        "South-West",
    "Ondo":        "South-West",
    "Ekiti":       "South-West",

    "Delta":       "South-South",
    "Rivers":      "South-South",
    "Bayelsa":     "South-South",
    "Akwa Ibom":   "South-South",
    "Cross River": "South-South",
    "Edo":         "South-South",

    "Anambra":     "South-East",
    "Enugu":       "South-East",
    "Imo":         "South-East",
    "Abia":        "South-East",
    "Ebonyi":      "South-East",
}


# ── State centroids (latitude, longitude) ────────────────────────
# Approximate geographic centres of each state.
# Used to query the NASA POWER rainfall API — one request per state.
# Coordinates are in WGS84 decimal degrees.

STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "Abia":        (5.4527,  7.5248),
    "Adamawa":     (9.3265,  12.3984),
    "Akwa Ibom":   (5.0074,  7.8497),
    "Anambra":     (6.2209,  6.9370),
    "Bauchi":      (10.3158, 9.8442),
    "Bayelsa":     (4.7719,  6.0698),
    "Benue":       (7.1906,  8.1275),
    "Borno":       (11.8333, 13.1510),
    "Cross River": (5.8702,  8.5988),
    "Delta":       (5.5320,  5.8987),
    "Ebonyi":      (6.2649,  8.0137),
    "Edo":         (6.5244,  5.8987),
    "Ekiti":       (7.6190,  5.2200),
    "Enugu":       (6.4584,  7.5464),
    "FCT":         (8.8965,  7.1858),
    "Gombe":       (10.2791, 11.1670),
    "Imo":         (5.5720,  7.0588),
    "Jigawa":      (12.1833, 9.3500),
    "Kaduna":      (10.5105, 7.4165),
    "Kano":        (11.9964, 8.5167),
    "Katsina":     (12.9816, 7.6162),
    "Kebbi":       (11.4942, 4.2333),
    "Kogi":        (7.7337,  6.6906),
    "Kwara":       (8.9669,  4.3874),
    "Lagos":       (6.5244,  3.3792),
    "Nasarawa":    (8.4966,  8.1984),
    "Niger":       (9.9309,  5.5983),
    "Ogun":        (6.9980,  3.4737),
    "Ondo":        (6.9100,  5.1478),
    "Osun":        (7.5629,  4.5200),
    "Oyo":         (7.8500,  3.9300),
    "Plateau":     (9.2182,  9.5179),
    "Rivers":      (4.8156,  7.0498),
    "Sokoto":      (13.0059, 5.2476),
    "Taraba":      (7.9994,  10.7739),
    "Yobe":        (12.2939, 11.7467),
    "Zamfara":     (12.1702, 6.6572),
}


# ── Helper functions ─────────────────────────────────────────────

def normalise_state_name(raw_name: str | None) -> str | None:
    """
    Map any raw state name string to its canonical form.

    The function is deliberately forgiving — it strips whitespace,
    lowercases, and removes common trailing words like "state" or
    "province" before looking up the variant map.

    Parameters
    ----------
    raw_name : str | None
        The state name as it appears in a raw data source.

    Returns
    -------
    str | None
        The canonical state name, "NATIONAL" for aggregate rows,
        or None if the input was None/NaN.

    Examples
    --------
    >>> normalise_state_name("FCT-Abuja")
    'FCT'
    >>> normalise_state_name("CROSS RIVER STATE")
    'Cross River'
    >>> normalise_state_name("Grand Total")
    'NATIONAL'
    >>> normalise_state_name(None)
    None
    """
    import re

    if raw_name is None:
        return None

    # pandas NaN arrives as float; guard against that
    try:
        if raw_name != raw_name:  # NaN check
            return None
    except TypeError:
        return None

    cleaned = str(raw_name).strip().lower()

    # Strip trailing "state", "province", "lga" suffixes
    cleaned = re.sub(r"\s+(state|province|lga)\s*$", "", cleaned)

    # Collapse multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if cleaned in STATE_NAME_VARIANTS:
        return STATE_NAME_VARIANTS[cleaned]

    # Last resort: try title-casing and direct membership check
    title_version = cleaned.title()
    if title_version in CANONICAL_STATE_SET:
        return title_version

    # Return a flagged unknown so callers can log and investigate
    return f"UNKNOWN:{raw_name}"


def is_valid_state(name: str | None) -> bool:
    """
    Return True if the name is a known canonical state.

    Parameters
    ----------
    name : str | None
        A state name — should already be normalised.

    Returns
    -------
    bool
    """
    return name in CANONICAL_STATE_SET


def get_zone(state: str) -> str:
    """
    Return the geopolitical zone for a canonical state name.

    Parameters
    ----------
    state : str
        A canonical state name.

    Returns
    -------
    str
        Zone name, or "Unknown" if not found.
    """
    return GEOPOLITICAL_ZONES.get(state, "Unknown")


def get_centroid(state: str) -> tuple[float, float] | None:
    """
    Return (latitude, longitude) centroid for a state.

    Parameters
    ----------
    state : str
        A canonical state name.

    Returns
    -------
    tuple[float, float] | None
        (lat, lon) pair or None if state not found.
    """
    return STATE_CENTROIDS.get(state)
