"""
dashboard/pages/geo_atlas.py
────────────────────────────────────────────────────────────────
Geospatial Atlas page.

Interactive choropleth maps showing disease burden across
Nigeria's 37 administrative units, with:
  • Choropleth map — disease incidence by state
  • Health facility overlay layer
  • Disease burden index map
  • Accessibility gap analysis
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
from typing import Optional

import folium
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_folium import st_folium

from dashboard.api_client import (
    get_choropleth,
    get_facilities,
    get_burden_index,
    get_accessibility,
)

# Nigeria's geographic centre — used as the default map centre
_NIGERIA_LAT = 9.082
_NIGERIA_LON = 8.675
_DEFAULT_ZOOM = 6


def render(selected_year: int, selected_disease: str) -> None:
    """
    Render the Geospatial Atlas page.

    Parameters
    ----------
    selected_year : int
        Year to visualise. Required for choropleth (no 'All' option here).
    selected_disease : str
        Disease to map.
    """
    st.header("🗺️ Geospatial Disease Atlas")
    st.caption(
        "Interactive maps of disease burden across Nigeria's 36 states + FCT."
    )

    # ── Map selector tabs ─────────────────────────────────────────
    tab1, tab2, tab3 = st.tabs([
        "🔵 Disease Choropleth",
        "📍 Facility Accessibility",
        "📊 Burden Index",
    ])

    with tab1:
        _render_choropleth_tab(selected_disease, selected_year)

    with tab2:
        _render_accessibility_tab(selected_disease, selected_year)

    with tab3:
        _render_burden_index_tab(selected_year)


# ── Choropleth tab ────────────────────────────────────────────────

def _render_choropleth_tab(disease: str, year: int) -> None:
    """Folium choropleth map of disease incidence by state."""
    query_disease = disease if disease != "All diseases" else "Cholera"

    col_settings, col_info = st.columns([3, 1])
    with col_settings:
        metric = st.radio(
            "Colour by",
            ["Incidence per 100k", "Total cases", "Deaths"],
            horizontal = True,
        )
    with col_info:
        st.caption(
            f"Showing: **{query_disease}** | Year: **{year}**"
        )

    metric_col = {
        "Incidence per 100k": "avg_incidence_per_100k",
        "Total cases":        "total_cases",
        "Deaths":             "total_deaths",
    }[metric]

    with st.spinner(f"Loading {query_disease} map for {year}..."):
        geojson_data = get_choropleth(disease=query_disease, year=year)

    if not geojson_data or not geojson_data.get("features"):
        st.warning(
            f"No geographic data available for {query_disease} in {year}. "
            "Ensure the ETL pipeline has been run with PostGIS enabled."
        )
        return

    # Build Folium map
    m = folium.Map(
        location    = [_NIGERIA_LAT, _NIGERIA_LON],
        zoom_start  = _DEFAULT_ZOOM,
        tiles       = "CartoDB positron",
    )

    # Choropleth layer
    folium.Choropleth(
        geo_data        = geojson_data,
        data            = _extract_state_values(geojson_data, metric_col),
        columns         = ["state", metric_col],
        key_on          = "feature.properties.state",
        fill_color      = "YlOrRd",
        fill_opacity    = 0.75,
        line_opacity    = 0.4,
        line_color      = "white",
        legend_name     = f"{metric} — {query_disease} ({year})",
        nan_fill_color  = "#E8E8E8",
    ).add_to(m)

    # Tooltip on hover
    folium.GeoJson(
        geojson_data,
        tooltip = folium.GeoJsonTooltip(
            fields    = ["state", "total_cases", "avg_incidence_per_100k"],
            aliases   = ["State", "Total Cases", "Incidence /100k"],
            localize  = True,
        ),
        style_function = lambda x: {
            "fillColor":   "transparent",
            "color":       "transparent",
            "weight":      0,
        },
    ).add_to(m)

    # Health facilities overlay (optional)
    show_facilities = st.checkbox("Show health facilities", value=False)
    if show_facilities:
        _add_facility_layer(m)

    st_folium(m, width="100%", height=520)


def _add_facility_layer(m: folium.Map) -> None:
    """Add health facility markers as a separate layer."""
    with st.spinner("Loading facility locations..."):
        facilities_df = get_facilities()

    if facilities_df.empty:
        return

    facility_group = folium.FeatureGroup(name="Health Facilities")

    for _, row in facilities_df.dropna(subset=["latitude", "longitude"]).iterrows():
        folium.CircleMarker(
            location  = [float(row["latitude"]), float(row["longitude"])],
            radius    = 3,
            color     = "#185FA5",
            fill      = True,
            fill_color = "#185FA5",
            fill_opacity = 0.6,
            tooltip   = str(row.get("facility_name", "Facility")),
        ).add_to(facility_group)

    facility_group.add_to(m)
    folium.LayerControl().add_to(m)


# ── Accessibility tab ─────────────────────────────────────────────

def _render_accessibility_tab(disease: str, year: int | None) -> None:
    """Bar chart and table of facility accessibility analysis."""
    st.subheader("🏥 Facility Accessibility Analysis")
    st.caption(
        "States with high disease burden and few facilities per capita "
        "have the largest access gaps."
    )

    query_disease = disease if disease != "All diseases" else "Cholera"

    with st.spinner("Running accessibility analysis..."):
        access_df = get_accessibility(disease=query_disease, year=year)

    if access_df.empty:
        st.info("Accessibility data not available.")
        return

    # Flag colour map
    flag_colours = {
        "CRITICAL": "#E24B4A",
        "POOR":     "#EF9F27",
        "ADEQUATE": "#1D9E75",
        "GOOD":     "#185FA5",
    }

    # Summary bar chart
    fig = px.bar(
        access_df.sort_values("access_gap_score", ascending=False),
        x        = "state",
        y        = "access_gap_score",
        color    = "flag",
        color_discrete_map = flag_colours,
        title    = f"Facility Access Gap Score — {query_disease}",
        labels   = {
            "access_gap_score": "Access Gap Score",
            "state":            "State",
            "flag":             "Access Level",
        },
        template = "plotly_white",
    )
    fig.update_layout(
        xaxis_tickangle = -45,
        height          = 380,
        margin          = dict(l=0, r=0, t=40, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Critical states callout
    critical = access_df[access_df["flag"] == "CRITICAL"]
    if not critical.empty:
        st.error(
            f"⚠️ **{len(critical)} state(s) flagged CRITICAL**: "
            + ", ".join(critical["state"].tolist())
        )

    # Data table
    with st.expander("View full accessibility data"):
        st.dataframe(
            access_df[[
                "state", "total_facilities", "facilities_per_100k",
                "disease_burden", "access_gap_score", "flag",
            ]].sort_values("access_gap_score", ascending=False),
            use_container_width = True,
            hide_index          = True,
        )


# ── Burden index tab ──────────────────────────────────────────────

def _render_burden_index_tab(year: int | None) -> None:
    """Map and chart of the composite Disease Burden Index."""
    st.subheader("📊 Composite Disease Burden Index")
    st.caption(
        "Combines normalised incidence rates across all diseases "
        "into a single 0–1 score per state. Higher = more burden."
    )

    with st.spinner("Computing burden index..."):
        dbi_df = get_burden_index(year=year)

    if dbi_df.empty:
        st.info("Burden index data not available.")
        return

    # Horizontal bar chart — top 15 states
    top_states = dbi_df.head(15)

    fig = px.bar(
        top_states.sort_values("burden_index"),
        x           = "burden_index",
        y           = "state",
        orientation = "h",
        color       = "burden_index",
        color_continuous_scale = "Reds",
        title       = f"Top 15 States by Disease Burden Index{f' ({year})' if year else ''}",
        labels      = {"burden_index": "Burden Index (0–1)", "state": ""},
        template    = "plotly_white",
    )
    fig.update_layout(
        height             = 420,
        margin             = dict(l=0, r=0, t=40, b=0),
        coloraxis_showscale = False,
    )
    st.plotly_chart(fig, use_container_width=True)

    # Full table
    with st.expander("View all states"):
        display_cols = ["rank", "state", "burden_index"] + [
            c for c in dbi_df.columns
            if c not in ("rank", "state", "burden_index")
        ]
        st.dataframe(
            dbi_df[[c for c in display_cols if c in dbi_df.columns]],
            use_container_width = True,
            hide_index          = True,
        )


# ── Internal helpers ─────────────────────────────────────────────

def _extract_state_values(
    geojson: dict,
    value_col: str,
) -> pd.DataFrame:
    """
    Extract a (state, value) DataFrame from a GeoJSON FeatureCollection.

    Used to feed Folium's Choropleth layer which needs a separate
    DataFrame in addition to the GeoJSON geometry.

    Parameters
    ----------
    geojson : dict
        GeoJSON FeatureCollection.
    value_col : str
        Property key to extract.

    Returns
    -------
    pd.DataFrame
        Columns: state, <value_col>.
    """
    rows = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        rows.append({
            "state":   props.get("state", ""),
            value_col: props.get(value_col),
        })
    return pd.DataFrame(rows)
