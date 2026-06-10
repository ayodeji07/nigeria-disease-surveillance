"""
dashboard/pages/forecasting.py
────────────────────────────────────────────────────────────────
Forecasting & Statistical Analysis page.

Shows:
  • Prophet 52-week ahead forecast with confidence bands
  • Mann-Kendall trend test result
  • Seasonality analysis (Dry vs. Rainy season comparison)
  • K-means state clustering
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard.api_client import (
    get_forecast,
    get_trend_test,
    get_clusters,
    get_surveillance,
)


_ALL_DISEASES = ["Cholera", "Lassa Fever", "Meningitis", "Mpox", "Yellow Fever"]


def render(selected_year: Optional[int], selected_disease: str) -> None:
    """
    Render the Forecasting & Statistical Analysis page.

    Parameters
    ----------
    selected_year : int | None
    selected_disease : str
    """
    st.header("🔮 Forecasting & Statistical Analysis")
    st.caption(
        "Prophet time-series forecasts, trend tests, and "
        "cluster analysis for Nigerian disease surveillance data."
    )

    # Local disease picker — forecasting runs per-disease so needs a specific choice
    disease_default = selected_disease if selected_disease in _ALL_DISEASES else "Cholera"
    query_disease = st.selectbox(
        "Disease (forecasting)",
        options = _ALL_DISEASES,
        index   = _ALL_DISEASES.index(disease_default),
        key     = "forecast_disease",
    )

    tab1, tab2, tab3 = st.tabs([
        "📈 52-Week Forecast",
        "📉 Trend & Seasonality",
        "🗂️ State Clustering",
    ])

    with tab1:
        _render_forecast_tab(query_disease)

    with tab2:
        _render_trend_tab(query_disease, selected_year)

    with tab3:
        _render_cluster_tab(query_disease, selected_year)


# ── Forecast tab ──────────────────────────────────────────────────

def _render_forecast_tab(disease: str) -> None:
    """Prophet forecast chart with confidence bands."""
    st.subheader(f"📈 52-Week Forecast — {disease}")
    st.caption(
        "Fitted using Facebook Prophet. Shaded region shows the 95% "
        "confidence interval."
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        state_option = st.text_input(
            "State (leave blank for national forecast)",
            value       = "",
            placeholder = "e.g. Lagos",
        )
    with col2:
        horizon = st.slider(
            "Forecast horizon (weeks)",
            min_value = 4,
            max_value = 104,
            value     = 52,
            step      = 4,
        )

    state = state_option.strip() or None

    if st.button("▶  Run Forecast", type="primary"):
        with st.spinner("Fitting Prophet model — this takes 10–30 seconds..."):
            forecast_df = get_forecast(
                disease       = disease,
                state         = state,
                horizon_weeks = horizon,
            )

        if forecast_df.empty:
            st.warning(
                "Forecast returned no data. "
                "Ensure the API is running and the database has sufficient history."
            )
            return

        _plot_forecast(forecast_df, disease, state, horizon)
    else:
        st.info("Set your options and click **Run Forecast** to generate predictions.")


def _plot_forecast(
    df: pd.DataFrame,
    disease: str,
    state: Optional[str],
    horizon: int,
) -> None:
    """
    Plot a Prophet forecast using Plotly with confidence bands.

    Parameters
    ----------
    df : pd.DataFrame
        Columns: forecast_date, y, yhat, yhat_lower, yhat_upper, is_forecast.
    disease : str
    state : str | None
    horizon : int
    """
    if df.empty:
        return

    df = df.copy()
    df["forecast_date"] = pd.to_datetime(df["forecast_date"])

    history = df[~df["is_forecast"].astype(bool)]
    future  = df[ df["is_forecast"].astype(bool)]

    fig = go.Figure()

    # Confidence band (future only)
    if not future.empty:
        fig.add_trace(go.Scatter(
            x    = pd.concat([future["forecast_date"], future["forecast_date"][::-1]]),
            y    = pd.concat([future["yhat_upper"], future["yhat_lower"][::-1]]),
            fill = "toself",
            fillcolor = "rgba(29, 158, 117, 0.15)",
            line      = dict(color="rgba(255,255,255,0)"),
            name      = "95% CI",
            showlegend = True,
        ))

    # Historical actuals
    if not history.empty:
        fig.add_trace(go.Scatter(
            x          = history["forecast_date"],
            y          = history["y"],
            mode       = "lines",
            name       = "Actual",
            line       = dict(color="#333333", width=1.5),
        ))

    # Fitted line (history) + forecast line (future)
    fig.add_trace(go.Scatter(
        x    = df["forecast_date"],
        y    = df["yhat"],
        mode = "lines",
        name = "Forecast",
        line = dict(color="#1D9E75", width=2.5, dash="dot" if future.empty else "solid"),
    ))

    # Vertical line at forecast start
    if not future.empty:
        cutoff = future["forecast_date"].min()
        fig.add_vline(
            x             = cutoff.timestamp() * 1000,
            line_dash     = "dash",
            line_color    = "grey",
            annotation_text = "Forecast start",
        )

    location_label = state if state else "Nigeria (national)"
    fig.update_layout(
        title    = f"{disease} forecast — {location_label} ({horizon}w ahead)",
        xaxis_title = "Date",
        yaxis_title = "Confirmed Cases",
        template    = "plotly_white",
        height      = 420,
        margin      = dict(l=0, r=0, t=50, b=0),
        hovermode   = "x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Model quality metrics
    mae  = df["mae"].iloc[0]  if "mae"  in df.columns else None
    rmse = df["rmse"].iloc[0] if "rmse" in df.columns else None
    if mae is not None or rmse is not None:
        m1, m2, _ = st.columns(3)
        with m1:
            st.metric("In-sample MAE",  f"{mae:.1f}"  if mae  else "N/A")
        with m2:
            st.metric("In-sample RMSE", f"{rmse:.1f}" if rmse else "N/A")


# ── Trend & seasonality tab ───────────────────────────────────────

def _render_trend_tab(disease: str, year: Optional[int]) -> None:
    """Mann-Kendall result and dry vs. rainy season comparison."""
    st.subheader("📉 Trend Test (Mann-Kendall)")

    state_input = st.text_input(
        "State for trend test (leave blank for national)",
        value       = "",
        placeholder = "e.g. Borno",
        key         = "trend_state",
    )
    state = state_input.strip() or None

    with st.spinner("Running Mann-Kendall test..."):
        result = get_trend_test(disease=disease, state=state)

    if not result:
        st.warning("Trend test returned no data.")
        return

    # Result card
    trend     = result.get("trend", "unknown")
    sig       = result.get("significant", False)
    tau       = result.get("tau", 0)
    p_val     = result.get("p_value", 1.0)
    interp    = result.get("interpretation", "")

    colour = (
        "#E24B4A" if trend == "increasing" and sig
        else "#1D9E75" if trend == "decreasing" and sig
        else "#5F5E5A"
    )

    sig_label = "✓ Significant" if sig else "(not significant)"
    st.metric(
        label = f"Trend — {sig_label}",
        value = trend.title(),
    )
    st.write(f"τ = {tau:.3f} | p = {p_val:.4f}")
    st.caption(interp)

    st.divider()

    # ── Seasonal comparison ───────────────────────────────────────
    st.subheader("🌦️ Dry vs. Rainy Season Comparison")

    with st.spinner("Loading seasonal data..."):
        surv_df = get_surveillance(
            disease = disease,
            state   = state,
            year    = year,
            limit   = 10_000,
        )

    if surv_df.empty or "season" not in surv_df.columns:
        st.info("Seasonal data not available.")
        return

    seasonal = (
        surv_df.groupby("season")["confirmed_cases"]
        .agg(["mean", "median", "sum"])
        .reset_index()
        .rename(columns={"mean": "avg_cases", "median": "median_cases",
                          "sum": "total_cases"})
    )

    if seasonal.empty:
        st.info("Not enough seasonal data.")
        return

    fig = px.bar(
        seasonal,
        x        = "season",
        y        = "avg_cases",
        color    = "season",
        color_discrete_map = {"Dry": "#EF9F27", "Rainy": "#185FA5"},
        title    = f"Average weekly cases by season — {disease}",
        labels   = {"avg_cases": "Avg Weekly Cases", "season": "Season"},
        template = "plotly_white",
        text_auto = ".0f",
    )
    fig.update_layout(
        height     = 300,
        margin     = dict(l=0, r=0, t=40, b=0),
        showlegend = False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Cluster tab ───────────────────────────────────────────────────

def _render_cluster_tab(disease: str, year: Optional[int]) -> None:
    """K-means state clustering visualisation."""
    st.subheader("🗂️ K-Means State Clustering")
    st.caption(
        "Groups states by their disease burden profile "
        "(total cases, incidence rate, CFR). "
        "States in the same cluster share similar epidemiological patterns."
    )

    n_clusters = st.slider(
        "Number of clusters",
        min_value = 2,
        max_value = 6,
        value     = 4,
        key       = "n_clusters_slider",
    )

    with st.spinner("Running K-means clustering..."):
        clusters_df = get_clusters(
            disease    = disease,
            year       = year,
            n_clusters = n_clusters,
        )

    if clusters_df.empty:
        st.info("Clustering data not available.")
        return

    # Scatter plot: incidence vs CFR, coloured by cluster
    fig = px.scatter(
        clusters_df,
        x        = "avg_incidence",
        y        = "avg_cfr",
        color    = "cluster_label",
        size     = "total_cases",
        text     = "state",
        title    = f"State clusters — {disease}{f' ({year})' if year else ''}",
        labels   = {
            "avg_incidence":  "Avg Incidence /100k",
            "avg_cfr":        "Avg CFR (%)",
            "cluster_label":  "Cluster",
            "total_cases":    "Total Cases",
        },
        template = "plotly_white",
        color_discrete_sequence = px.colors.qualitative.Set2,
    )
    fig.update_traces(textposition="top center", textfont_size=9)
    fig.update_layout(
        height = 460,
        margin = dict(l=0, r=0, t=50, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Cluster membership table
    with st.expander("View cluster assignments"):
        st.dataframe(
            clusters_df[[
                "state", "cluster_label", "total_cases",
                "avg_incidence", "avg_cfr",
            ]].sort_values(["cluster_label", "total_cases"], ascending=[True, False]),
            use_container_width = True,
            hide_index          = True,
        )
