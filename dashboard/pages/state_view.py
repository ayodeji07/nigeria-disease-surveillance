"""
dashboard/pages/state_view.py
────────────────────────────────────────────────────────────────
State Deep-Dive page.

Lets users drill into a single state's full disease history:
  • State-level KPI cards
  • Multi-disease trend for the selected state
  • CFR benchmarking vs. national mean
  • Disease burden ranking (where does this state sit nationally?)
  • Data table with download option
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from dashboard.api_client import (
    get_surveillance,
    get_trend,
    get_hotspots,
    get_cfr_benchmark,
    get_states,
)


def render(selected_year: int | None, selected_disease: str) -> None:
    """
    Render the State Deep-Dive page.

    Parameters
    ----------
    selected_year : int | None
    selected_disease : str
    """
    st.header("🔍 State Deep-Dive")
    st.caption("Drill into a single state's full surveillance history.")

    # ── State selector ────────────────────────────────────────────
    states = get_states() or [
        "Abia", "Adamawa", "Akwa Ibom", "Lagos", "Kano", "Rivers"
    ]
    selected_state = st.selectbox(
        "Select a state",
        options = states,
        index   = states.index("Lagos") if "Lagos" in states else 0,
    )

    st.divider()

    # ── Load state data ───────────────────────────────────────────
    disease_filter = None if selected_disease == "All diseases" else selected_disease

    with st.spinner(f"Loading data for {selected_state}..."):
        state_df = get_surveillance(
            state   = selected_state,
            disease = disease_filter,
            year    = selected_year,
            limit   = 50_000,
        )

    if state_df.empty:
        st.warning(
            f"No data found for **{selected_state}**. "
            "This may mean the ETL pipeline has not yet loaded data for this state."
        )
        return

    # ── State KPIs ────────────────────────────────────────────────
    _render_state_kpis(state_df, selected_state)

    st.divider()

    col_left, col_right = st.columns([3, 2])

    with col_left:
        _render_state_trend(selected_state, disease_filter, selected_year)

    with col_right:
        _render_disease_split(state_df, selected_state)

    st.divider()

    # ── CFR benchmarking ──────────────────────────────────────────
    _render_cfr_benchmark(selected_state, disease_filter, selected_year)

    st.divider()

    # ── Data table with download ──────────────────────────────────
    _render_data_table(state_df, selected_state)


# ── State KPIs ────────────────────────────────────────────────────

def _render_state_kpis(df: pd.DataFrame, state: str) -> None:
    """Four KPI cards for the selected state."""
    st.subheader(f"📌 {state} — Summary")

    total_cases  = int(df["confirmed_cases"].sum())  if "confirmed_cases"  in df.columns else 0
    total_deaths = int(df["deaths"].sum())           if "deaths"           in df.columns else 0
    avg_cfr      = df["cfr_pct"].mean()              if "cfr_pct"          in df.columns else None
    avg_incidence = df["incidence_per_100k"].mean()  if "incidence_per_100k" in df.columns else None

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Confirmed Cases",    f"{total_cases:,}")
    with col2:
        st.metric("Deaths",             f"{total_deaths:,}")
    with col3:
        st.metric("Avg CFR",
                  f"{avg_cfr:.2f}%" if avg_cfr is not None else "N/A")
    with col4:
        st.metric("Avg Incidence /100k",
                  f"{avg_incidence:.1f}" if avg_incidence is not None else "N/A")


# ── State trend chart ─────────────────────────────────────────────

def _render_state_trend(
    state: str,
    disease: str | None,
    year: int | None,
) -> None:
    """Monthly trend for the selected state and disease."""
    st.subheader("📈 Case Trend")

    query_disease = disease or "Cholera"

    with st.spinner("Loading trend..."):
        trend_df = get_trend(
            disease = query_disease,
            state   = state,
            freq    = "monthly",
        )

    if trend_df.empty:
        st.info("No trend data available for this state.")
        return

    if year and "period" in trend_df.columns:
        trend_df = trend_df[trend_df["period"].str.startswith(str(year))]

    fig = px.area(
        trend_df,
        x        = "period",
        y        = "confirmed_cases",
        title    = f"{query_disease} — {state}",
        labels   = {"period": "Month", "confirmed_cases": "Cases"},
        template = "plotly_white",
        color_discrete_sequence = ["#1D9E75"],
    )
    fig.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=300)
    st.plotly_chart(fig, use_container_width=True)


# ── Disease split ─────────────────────────────────────────────────

def _render_disease_split(df: pd.DataFrame, state: str) -> None:
    """Pie chart showing which disease contributes most in this state."""
    st.subheader("🥧 Burden by Disease")

    if "disease" not in df.columns or "confirmed_cases" not in df.columns:
        st.info("Disease breakdown not available.")
        return

    breakdown = (
        df.groupby("disease")["confirmed_cases"]
        .sum()
        .reset_index()
        .sort_values("confirmed_cases", ascending=False)
    )

    if breakdown.empty:
        st.info("No breakdown data.")
        return

    fig = px.pie(
        breakdown,
        values   = "confirmed_cases",
        names    = "disease",
        title    = f"Disease split — {state}",
        template = "plotly_white",
        color_discrete_sequence = px.colors.qualitative.Set2,
    )
    fig.update_traces(textposition="inside", textinfo="percent+label")
    fig.update_layout(
        margin     = dict(l=0, r=0, t=40, b=0),
        height     = 300,
        showlegend = False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── CFR benchmark ─────────────────────────────────────────────────

def _render_cfr_benchmark(
    state: str,
    disease: str | None,
    year: int | None,
) -> None:
    """Show the state's CFR vs. the national mean."""
    st.subheader("📊 CFR vs. National Mean")

    query_disease = disease or "Cholera"

    with st.spinner("Loading CFR benchmarks..."):
        cfr_df = get_cfr_benchmark(disease=query_disease, year=year)

    if cfr_df.empty:
        st.info("CFR benchmarking data not available.")
        return

    national_mean = cfr_df["national_mean_cfr"].iloc[0] if "national_mean_cfr" in cfr_df.columns else None

    # Highlight the selected state
    cfr_df["is_selected"] = cfr_df["state"] == state

    fig = px.bar(
        cfr_df.sort_values("avg_cfr", ascending=False),
        x      = "state",
        y      = "avg_cfr",
        color  = "flag",
        color_discrete_map = {
            "HIGH":   "#E24B4A",
            "NORMAL": "#1D9E75",
            "LOW":    "#85B7EB",
        },
        title    = f"CFR by state — {query_disease}",
        labels   = {"avg_cfr": "Avg CFR (%)", "state": "State"},
        template = "plotly_white",
    )
    if national_mean is not None:
        fig.add_hline(
            y               = national_mean,
            line_dash       = "dash",
            line_color      = "grey",
            annotation_text = f"National mean: {national_mean:.2f}%",
        )
    fig.update_layout(
        margin    = dict(l=0, r=0, t=40, b=0),
        height    = 320,
        xaxis_tickangle = -45,
        showlegend = True,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Data table ────────────────────────────────────────────────────

def _render_data_table(df: pd.DataFrame, state: str) -> None:
    """Paginated data table with CSV download button."""
    st.subheader("📋 Raw Data")

    display_cols = [
        c for c in [
            "disease", "report_date", "epi_week", "year",
            "suspected_cases", "confirmed_cases", "deaths",
            "cfr_pct", "incidence_per_100k", "data_quality_flag",
        ]
        if c in df.columns
    ]

    display_df = df[display_cols].sort_values(
        ["disease", "report_date"], ascending=[True, False]
    )

    st.dataframe(
        display_df,
        use_container_width = True,
        hide_index          = True,
        height              = 350,
    )

    # CSV download button
    csv_bytes = display_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label     = "⬇️ Download as CSV",
        data      = csv_bytes,
        file_name = f"surveillance_{state.lower().replace(' ', '_')}.csv",
        mime      = "text/csv",
    )
