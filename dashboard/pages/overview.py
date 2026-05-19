"""
dashboard/pages/overview.py
────────────────────────────────────────────────────────────────
National Overview page — the dashboard's landing page.

Shows:
  • KPI cards: total cases, deaths, average CFR, peak week
  • Multi-disease trend chart over time
  • Disease burden comparison bar chart
  • CFR trend over time
  • Outbreak alerts table
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from dashboard.api_client import (
    get_summary,
    get_trend,
    get_hotspots,
    get_outbreak_alerts,
    get_diseases,
)


def render(selected_year: int | None, selected_disease: str) -> None:
    """
    Render the National Overview page.

    Parameters
    ----------
    selected_year : int | None
        Year filter from the sidebar. None = all years.
    selected_disease : str
        Disease filter from the sidebar.
    """
    st.header("🇳🇬 National Disease Surveillance Overview")
    st.caption(
        "Weekly confirmed case counts, deaths, and CFR across "
        "all 37 Nigerian administrative units."
    )

    # ── KPI Cards ────────────────────────────────────────────────
    _render_kpi_cards(selected_year, selected_disease)

    st.divider()

    # ── Trend chart ──────────────────────────────────────────────
    col_left, col_right = st.columns([2, 1])

    with col_left:
        _render_trend_chart(selected_year, selected_disease)

    with col_right:
        _render_burden_bar(selected_year)

    st.divider()

    # ── CFR trend ────────────────────────────────────────────────
    _render_cfr_trend(selected_disease)

    st.divider()

    # ── Outbreak alerts ──────────────────────────────────────────
    _render_outbreak_alerts(selected_disease, selected_year)


# ── KPI cards ─────────────────────────────────────────────────────

def _render_kpi_cards(year: int | None, disease: str) -> None:
    """Display four KPI metric cards at the top of the page."""
    with st.spinner("Loading summary..."):
        summary_df = get_summary(year=year)

    if summary_df.empty:
        st.warning("No summary data available. Check that the API is running.")
        return

    # Filter to selected disease if not "All"
    if disease != "All diseases":
        row = summary_df[summary_df["disease"] == disease]
    else:
        # Aggregate across all diseases
        row = summary_df.agg({
            "total_cases":   "sum",
            "total_deaths":  "sum",
            "avg_cfr_pct":   "mean",
            "peak_week_cases": "max",
        }).to_frame().T
        row["disease"] = "All Diseases"

    if row.empty:
        st.info(f"No summary data for {disease}.")
        return

    row = row.iloc[0]

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            label = "Total Confirmed Cases",
            value = f"{int(row.get('total_cases', 0)):,}",
        )
    with col2:
        st.metric(
            label = "Total Deaths",
            value = f"{int(row.get('total_deaths', 0)):,}",
        )
    with col3:
        cfr = row.get("avg_cfr_pct")
        st.metric(
            label = "Avg Case Fatality Rate",
            value = f"{cfr:.2f}%" if cfr is not None else "N/A",
        )
    with col4:
        peak = row.get("peak_week_cases")
        st.metric(
            label = "Peak Single-Week Cases",
            value = f"{int(peak):,}" if peak is not None else "N/A",
        )


# ── Trend chart ───────────────────────────────────────────────────

def _render_trend_chart(year: int | None, disease: str) -> None:
    """Line chart of confirmed cases over time."""
    st.subheader("📈 Case Count Trend")

    disease_filter = None if disease == "All diseases" else disease

    with st.spinner("Loading trend data..."):
        trend_df = get_trend(disease=disease_filter or "Cholera", freq="monthly")

    if trend_df.empty:
        st.info("No trend data available.")
        return

    if year and "period" in trend_df.columns:
        trend_df = trend_df[trend_df["period"].str.startswith(str(year))]

    fig = px.line(
        trend_df,
        x     = "period",
        y     = "confirmed_cases",
        title = f"Monthly confirmed cases — {disease_filter or 'Cholera'}",
        labels = {
            "period":          "Month",
            "confirmed_cases": "Confirmed Cases",
        },
        template = "plotly_white",
    )
    fig.update_traces(line_color="#1D9E75", line_width=2)
    fig.update_layout(
        margin = dict(l=0, r=0, t=40, b=0),
        height = 320,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Burden bar chart ──────────────────────────────────────────────

def _render_burden_bar(year: int | None) -> None:
    """Bar chart comparing total cases across diseases."""
    st.subheader("📊 Disease Burden Comparison")

    with st.spinner("Loading burden data..."):
        summary_df = get_summary(year=year)

    if summary_df.empty:
        st.info("No data available.")
        return

    fig = px.bar(
        summary_df.sort_values("total_cases", ascending=True),
        x        = "total_cases",
        y        = "disease",
        orientation = "h",
        title    = "Total cases by disease",
        labels   = {"total_cases": "Total Cases", "disease": ""},
        color    = "total_cases",
        color_continuous_scale = "Teal",
        template = "plotly_white",
    )
    fig.update_layout(
        margin             = dict(l=0, r=0, t=40, b=0),
        height             = 320,
        coloraxis_showscale = False,
        showlegend         = False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── CFR trend ─────────────────────────────────────────────────────

def _render_cfr_trend(disease: str) -> None:
    """Line chart of Case Fatality Rate over time."""
    st.subheader("💀 Case Fatality Rate (CFR) Over Time")

    disease_filter = None if disease == "All diseases" else disease
    query_disease  = disease_filter or "Cholera"

    with st.spinner("Loading CFR data..."):
        trend_df = get_trend(disease=query_disease, freq="monthly")

    if trend_df.empty or "cfr_pct" not in trend_df.columns:
        st.info("No CFR data available.")
        return

    trend_df = trend_df[trend_df["cfr_pct"].notna()]
    if trend_df.empty:
        st.info("CFR data not available for the selected filters.")
        return

    fig = px.line(
        trend_df,
        x     = "period",
        y     = "cfr_pct",
        title = f"Monthly CFR — {query_disease}",
        labels = {"period": "Month", "cfr_pct": "CFR (%)"},
        template = "plotly_white",
    )
    fig.update_traces(line_color="#993C1D", line_width=2)
    fig.add_hline(
        y          = trend_df["cfr_pct"].mean(),
        line_dash  = "dash",
        line_color = "grey",
        annotation_text = f"Mean: {trend_df['cfr_pct'].mean():.2f}%",
    )
    fig.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=280)
    st.plotly_chart(fig, use_container_width=True)


# ── Outbreak alerts table ─────────────────────────────────────────

def _render_outbreak_alerts(disease: str, year: int | None) -> None:
    """Display CUSUM outbreak alerts in a styled table."""
    st.subheader("🚨 Outbreak Alerts")
    st.caption("States where case counts exceeded the historical baseline (CUSUM detection).")

    disease_filter = None if disease == "All diseases" else disease
    query_disease  = disease_filter or "Cholera"

    with st.spinner("Running outbreak detection..."):
        alerts_df = get_outbreak_alerts(disease=query_disease, year=year)

    if alerts_df.empty:
        st.success(f"No outbreak alerts detected for {query_disease}.")
        return

    # Highlight HIGH alerts
    def _highlight_alerts(row):
        return ["background-color: #FAECE7"] * len(row)

    st.dataframe(
        alerts_df[[
            "state", "alert_date", "cases",
            "baseline_mean", "cusum_score",
        ]].rename(columns={
            "state":         "State",
            "alert_date":    "Alert Date",
            "cases":         "Cases",
            "baseline_mean": "Baseline Mean",
            "cusum_score":   "CUSUM Score",
        }),
        use_container_width = True,
        hide_index          = True,
    )
    st.caption(f"*{len(alerts_df)} alert(s) detected.*")
