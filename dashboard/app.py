"""
dashboard/app.py
────────────────────────────────────────────────────────────────
Streamlit dashboard — entry point.

Run locally:
    streamlit run dashboard/app.py

Deploy to Streamlit Community Cloud:
    Connect this GitHub repo and set API_BASE_URL in secrets.

Architecture:
    This file owns the sidebar (filters + navigation) and routes
    to the correct page module. All data fetching happens inside
    the page modules via api_client.py — never directly here.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path so 'dashboard' is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

from dashboard._pages import overview, state_view, geo_atlas, forecasting, admin
from dashboard.api_client import get_diseases, get_health

# ── Page configuration ────────────────────────────────────────────
# Must be the first Streamlit call in the script.

st.set_page_config(
    page_title     = "Nigeria Disease Surveillance",
    page_icon      = "🏥",
    layout         = "wide",
    initial_sidebar_state = "expanded",
    menu_items     = {
        "Get Help":    "https://github.com/ayodeji/nigeria-disease-surveillance",
        "Report a bug":"https://github.com/ayodeji/nigeria-disease-surveillance/issues",
        "About":       "Nigeria Disease Surveillance Dashboard — built by Ayodeji.",
    },
)




# ── API health check ──────────────────────────────────────────────

@st.cache_data(ttl=60)
def _check_api_health() -> bool:
    """Return True if the API is reachable. Cached for 60 seconds."""
    import os, requests
    base_url = os.environ.get("API_BASE_URL", "http://localhost:8000")
    try:
        r = requests.get(f"{base_url}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ── Available diseases ────────────────────────────────────────────

@st.cache_data(ttl=3600)
def _get_disease_options() -> list[str]:
    """
    Fetch disease list from the API.

    Falls back to a hardcoded list if the API is unreachable so
    the sidebar still renders while the API is starting up.
    """
    diseases = get_diseases()
    return diseases if diseases else [
        "Cholera", "Lassa Fever", "Mpox",
        "Meningitis", "Yellow Fever",
    ]


# ── Sidebar ───────────────────────────────────────────────────────

def _render_sidebar() -> tuple[str, int | None, str]:
    """
    Render the sidebar navigation and global filters.

    Returns
    -------
    tuple[str, int | None, str]
        (selected_page, selected_year, selected_disease)
    """
    with st.sidebar:

        # ── Branding ──────────────────────────────────────────────
        st.markdown("### 🏥 Nigeria Disease Surveillance")
        st.caption("5 diseases · 37 states · 2015–present")
        st.divider()

        # ── API status indicator ──────────────────────────────────
        api_ok = _check_api_health()
        if api_ok:
            st.success("API connected", icon="✅")
        else:
            st.error("API unreachable — check API_BASE_URL", icon="❌")

        st.divider()

        # ── Navigation ────────────────────────────────────────────
        st.markdown("**Navigation**")
        page = st.radio(
            label            = "Go to",
            options          = [
                "🏠 National Overview",
                "🔍 State Deep-Dive",
                "🗺️ Geospatial Atlas",
                "🔮 Forecasting",
                "⚙️ Admin",
            ],
            label_visibility = "collapsed",
        )

        st.divider()

        # ── Global filters ────────────────────────────────────────
        st.markdown("**Filters**")

        year_options = ["All years"] + list(range(2024, 2014, -1))
        selected_year_label = st.selectbox("Year", options=year_options, index=0)
        selected_year = (
            None if selected_year_label == "All years"
            else int(selected_year_label)
        )

        disease_options  = ["All diseases"] + _get_disease_options()
        selected_disease = st.selectbox("Disease", options=disease_options, index=0)

        st.divider()

        # ── Data sources ──────────────────────────────────────────
        with st.expander("ℹ️ Data sources"):
            st.markdown(
                """
                - **NCDC** Nigeria — weekly sitreps
                - **WHO AFRO** — cross-validation
                - **NASA POWER** — rainfall data
                - **HDX** — health facilities
                - **NBS / WorldPop** — state populations
                """
            )

        # ── Footer ────────────────────────────────────────────────
        st.caption(
            "Built by **Ayodeji** · HealthTech Data Scientist  \n"
            "[GitHub](https://github.com/ayodeji/nigeria-disease-surveillance)"
        )

    return page, selected_year, selected_disease


# ── Page router ───────────────────────────────────────────────────

def _route(page: str, year: int | None, disease: str) -> None:
    """
    Dispatch to the correct page module.

    Parameters
    ----------
    page : str
        Label from the sidebar radio button.
    year : int | None
    disease : str
    """
    if page == "🏠 National Overview":
        overview.render(
            selected_year    = year,
            selected_disease = disease,
        )

    elif page == "🔍 State Deep-Dive":
        state_view.render(
            selected_year    = year,
            selected_disease = disease,
        )

    elif page == "🗺️ Geospatial Atlas":
        geo_atlas.render(
            selected_year    = year,
            selected_disease = disease,
        )

    elif page == "🔮 Forecasting":
        forecasting.render(
            selected_year    = year,
            selected_disease = disease,
        )

    elif page == "⚙️ Admin":
        admin.render()

    else:
        st.error(f"Unknown page: {page!r}")


# ── Main ──────────────────────────────────────────────────────────

def main() -> None:
    """Application entry point called by Streamlit."""
    page, year, disease = _render_sidebar()
    # When the active page changes, force a full script re-run so all widgets
    # from the previous page are cleared before the new page renders.
    if st.session_state.get("_active_page") != page:
        st.session_state["_active_page"] = page
        st.rerun()
    _route(page, year, disease)


if __name__ == "__main__":
    main()
