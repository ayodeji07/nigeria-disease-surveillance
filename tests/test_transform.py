"""
tests/test_transform.py
────────────────────────────────────────────────────────────────
Unit tests for src/etl/transform.py.

These tests run against pure Python / pandas logic — no database,
no network, no file I/O required. They are the fastest tests in
the suite and should always pass in any environment.

Run with:
    pytest tests/test_transform.py -v
────────────────────────────────────────────────────────────────
"""

import numpy as np
import pandas as pd
import pytest

from src.etl.transform import (
    _standardise_column_names,
    _parse_numeric_column,
    _epiweek_to_date,
    clean_disease_dataframe,
    clean_population_data,
    clean_rainfall_data,
    merge_all_diseases,
    add_incidence_rate,
)
from src.utils.config import Diseases
from src.utils.state_maps import CANONICAL_STATE_SET


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def raw_cholera_df() -> pd.DataFrame:
    """
    Simulate messy raw DataFrame from NCDC PDF extraction.
    Includes: inconsistent column names, comma-formatted numbers,
    dash-as-zero, variant state names, national totals row, unknowns.
    """
    return pd.DataFrame([
        {
            "State":            "Lagos",
            "Suspected Cases":  "1,234",
            "Confirmed Cases":  "456",
            "Deaths":           "12",
            "Epi Week":         "10",
            "Year":             "2023",
            "_source_file":     "ncdc_cholera_w10_2023.pdf",
        },
        {
            "State":            "FCT-Abuja",
            "Suspected Cases":  "200",
            "Confirmed Cases":  "45",
            "Deaths":           "-",
            "Epi Week":         "10",
            "Year":             "2023",
            "_source_file":     "ncdc_cholera_w10_2023.pdf",
        },
        {
            "State":            "CROSS RIVER",
            "Suspected Cases":  "80",
            "Confirmed Cases":  "30",
            "Deaths":           "1",
            "Epi Week":         "10",
            "Year":             "2023",
            "_source_file":     "ncdc_cholera_w10_2023.pdf",
        },
        {
            "State":            "Grand Total",      # must be dropped
            "Suspected Cases":  "5,000",
            "Confirmed Cases":  "2,000",
            "Deaths":           "50",
            "Epi Week":         "10",
            "Year":             "2023",
            "_source_file":     "ncdc_cholera_w10_2023.pdf",
        },
        {
            "State":            "XYZ_Unknown",      # unrecognised — must be dropped
            "Suspected Cases":  "10",
            "Confirmed Cases":  "5",
            "Deaths":           "0",
            "Epi Week":         "10",
            "Year":             "2023",
            "_source_file":     "ncdc_cholera_w10_2023.pdf",
        },
    ])


@pytest.fixture
def raw_population_df() -> pd.DataFrame:
    """Minimal population DataFrame with valid and invalid entries."""
    return pd.DataFrame([
        {"State": "Lagos",  "Population 2022": "15,000,000"},
        {"State": "Kano",   "Population 2022": "12,000,000"},
        {"State": "FCT",    "Population 2022": "3,500,000"},
        {"State": "fct",    "Population 2022": "3,500,000"},   # duplicate variant
        {"State": "XYZ",    "Population 2022": "500"},         # invalid state
    ])


# ── Column standardisation ────────────────────────────────────────

class TestStandardiseColumnNames:

    def test_renames_state_column(self, raw_cholera_df):
        result = _standardise_column_names(raw_cholera_df)
        assert "state" in result.columns

    def test_renames_confirmed_cases(self, raw_cholera_df):
        result = _standardise_column_names(raw_cholera_df)
        assert "confirmed_cases" in result.columns

    def test_renames_suspected_cases(self, raw_cholera_df):
        result = _standardise_column_names(raw_cholera_df)
        assert "suspected_cases" in result.columns

    def test_renames_deaths(self, raw_cholera_df):
        result = _standardise_column_names(raw_cholera_df)
        assert "deaths" in result.columns

    def test_renames_epi_week(self, raw_cholera_df):
        result = _standardise_column_names(raw_cholera_df)
        assert "epi_week" in result.columns

    def test_preserves_source_file_column(self, raw_cholera_df):
        result = _standardise_column_names(raw_cholera_df)
        assert "_source_file" in result.columns

    def test_row_count_unchanged(self, raw_cholera_df):
        result = _standardise_column_names(raw_cholera_df)
        assert len(result) == len(raw_cholera_df)


# ── Numeric parsing ───────────────────────────────────────────────

class TestParseNumericColumn:

    def test_parses_plain_integer_string(self):
        assert list(_parse_numeric_column(pd.Series(["100"]))) == [100]

    def test_removes_commas(self):
        assert _parse_numeric_column(pd.Series(["1,234"])).iloc[0] == 1234

    def test_dash_becomes_zero(self):
        assert _parse_numeric_column(pd.Series(["-"])).iloc[0] == 0

    def test_nil_becomes_zero(self):
        assert _parse_numeric_column(pd.Series(["nil"])).iloc[0] == 0

    def test_na_string_becomes_zero(self):
        assert _parse_numeric_column(pd.Series(["N/A"])).iloc[0] == 0

    def test_float_string_rounds_down(self):
        assert _parse_numeric_column(pd.Series(["12.0"])).iloc[0] == 12

    def test_returns_integer_dtype(self):
        result = _parse_numeric_column(pd.Series(["10", "20"]))
        assert result.dtype in (int, "int64", np.int64)

    def test_all_zero_series(self):
        result = _parse_numeric_column(pd.Series(["0", "0"]))
        assert (result == 0).all()


# ── Epi-week to date ──────────────────────────────────────────────

class TestEpiweekToDate:

    def test_valid_week_returns_monday(self):
        dt = _epiweek_to_date(2023, 1)
        assert dt is not pd.NaT
        assert dt.day_of_week == 0

    def test_invalid_week_returns_nat(self):
        assert _epiweek_to_date(2023, 99) is pd.NaT

    def test_invalid_year_returns_nat(self):
        assert _epiweek_to_date("bad", 1) is pd.NaT

    def test_float_inputs_work(self):
        result = _epiweek_to_date(2023.0, 10.0)
        assert result is not pd.NaT

    def test_week_52_valid(self):
        assert _epiweek_to_date(2022, 52) is not pd.NaT


# ── Disease DataFrame cleaning ────────────────────────────────────

class TestCleanDiseaseDataframe:

    def test_drops_national_total_rows(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert "NATIONAL" not in result.get("state", pd.Series()).values

    def test_drops_unknown_state_rows(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        unknown = result[result["state"].str.startswith("UNKNOWN:", na=False)]
        assert len(unknown) == 0

    def test_normalises_fct_variant(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert "FCT" in result["state"].values

    def test_normalises_cross_river(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert "Cross River" in result["state"].values

    def test_all_states_canonical(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        for state in result["state"].unique():
            assert state in CANONICAL_STATE_SET, f"Non-canonical: {state}"

    def test_confirmed_cases_are_integers(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert result["confirmed_cases"].dtype in (int, "int64", np.int64)

    def test_comma_formatted_number_parsed(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        lagos  = result[result["state"] == "Lagos"]
        assert not lagos.empty
        assert lagos["confirmed_cases"].iloc[0] == 456

    def test_dash_deaths_become_zero(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        fct    = result[result["state"] == "FCT"]
        assert not fct.empty
        assert fct["deaths"].iloc[0] == 0

    def test_cfr_column_present(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert "cfr_pct" in result.columns

    def test_cfr_non_negative(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert (result["cfr_pct"] >= 0).all()

    def test_disease_column_set(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert (result["disease"] == Diseases.CHOLERA).all()

    def test_quality_flag_present(self, raw_cholera_df):
        result = clean_disease_dataframe(raw_cholera_df, Diseases.CHOLERA)
        assert "data_quality_flag" in result.columns

    def test_empty_input_returns_empty(self):
        result = clean_disease_dataframe(pd.DataFrame(), Diseases.CHOLERA)
        assert result.empty


# ── Population cleaning ───────────────────────────────────────────

class TestCleanPopulationData:

    def test_has_state_and_population_columns(self, raw_population_df):
        result = clean_population_data(raw_population_df)
        assert {"state", "population"}.issubset(result.columns)

    def test_population_is_integer(self, raw_population_df):
        result = clean_population_data(raw_population_df)
        assert result["population"].dtype in (int, "int64", np.int64)

    def test_comma_formatted_population_parsed(self, raw_population_df):
        result = clean_population_data(raw_population_df)
        lagos  = result[result["state"] == "Lagos"]
        assert not lagos.empty
        assert lagos["population"].iloc[0] == 15_000_000

    def test_all_states_canonical(self, raw_population_df):
        result = clean_population_data(raw_population_df)
        for state in result["state"].unique():
            assert state in CANONICAL_STATE_SET

    def test_empty_input_returns_empty(self):
        result = clean_population_data(pd.DataFrame())
        assert result.empty


# ── Rainfall cleaning ─────────────────────────────────────────────

class TestCleanRainfallData:

    @pytest.fixture
    def raw_rainfall(self):
        return pd.DataFrame([
            {"state": "Lagos",   "year": 2023, "month": 1, "rainfall_mm": 12.5},
            {"state": "Kano",    "year": 2023, "month": 1, "rainfall_mm": -999.0},
            {"state": "FCT",     "year": 2023, "month": 1, "rainfall_mm": -5.0},
            {"state": "XYZ_Bad", "year": 2023, "month": 1, "rainfall_mm": 20.0},
            {"state": "Rivers",  "year": 2023, "month": 1, "rainfall_mm": 80.0},
        ])

    def test_nasa_fill_value_becomes_nan(self, raw_rainfall):
        result = clean_rainfall_data(raw_rainfall)
        kano   = result[result["state"] == "Kano"]
        if not kano.empty:
            assert pd.isna(kano["rainfall_mm"].iloc[0])

    def test_negative_rainfall_becomes_nan(self, raw_rainfall):
        result = clean_rainfall_data(raw_rainfall)
        fct    = result[result["state"] == "FCT"]
        if not fct.empty:
            assert pd.isna(fct["rainfall_mm"].iloc[0])

    def test_unknown_states_dropped(self, raw_rainfall):
        result = clean_rainfall_data(raw_rainfall)
        assert "XYZ_Bad" not in result["state"].values

    def test_valid_values_preserved(self, raw_rainfall):
        result = clean_rainfall_data(raw_rainfall)
        lagos  = result[result["state"] == "Lagos"]
        assert not lagos.empty
        assert lagos["rainfall_mm"].iloc[0] == 12.5

    def test_empty_input_returns_empty(self):
        assert clean_rainfall_data(pd.DataFrame()).empty


# ── Merge and incidence ───────────────────────────────────────────

class TestMergeAndIncidence:

    @pytest.fixture
    def clean_disease_map(self):
        dates = pd.date_range("2023-01-02", periods=4, freq="W")
        def _make(disease):
            return pd.DataFrame([
                {
                    "state": s, "disease": disease,
                    "report_date": d, "confirmed_cases": 10,
                    "suspected_cases": 15, "deaths": 1,
                    "cfr_pct": 10.0, "data_quality_flag": "CLEAN",
                }
                for s in ["Lagos", "Kano"] for d in dates
            ])
        return {
            Diseases.CHOLERA:    _make(Diseases.CHOLERA),
            Diseases.MENINGITIS: _make(Diseases.MENINGITIS),
        }

    @pytest.fixture
    def pop_df(self):
        return pd.DataFrame([
            {"state": "Lagos", "population": 15_000_000},
            {"state": "Kano",  "population": 12_000_000},
        ])

    def test_merge_all_diseases_combines_correctly(self, clean_disease_map):
        result = merge_all_diseases(clean_disease_map)
        assert set(result["disease"].unique()) == {
            Diseases.CHOLERA, Diseases.MENINGITIS
        }

    def test_merge_preserves_row_count(self, clean_disease_map):
        result   = merge_all_diseases(clean_disease_map)
        expected = sum(len(df) for df in clean_disease_map.values())
        assert len(result) == expected

    def test_merge_empty_map_returns_empty(self):
        assert merge_all_diseases({}).empty

    def test_incidence_column_present(self, clean_disease_map, pop_df):
        merged = merge_all_diseases(clean_disease_map)
        result = add_incidence_rate(merged, pop_df)
        assert "incidence_per_100k" in result.columns

    def test_incidence_formula(self, clean_disease_map, pop_df):
        """incidence = confirmed_cases / population * 100,000"""
        merged   = merge_all_diseases(clean_disease_map)
        result   = add_incidence_rate(merged, pop_df)
        row      = result[result["state"] == "Lagos"].iloc[0]
        expected = 10 / 15_000_000 * 100_000
        assert abs(row["incidence_per_100k"] - expected) < 0.001

    def test_incidence_null_without_population(self, clean_disease_map):
        merged = merge_all_diseases(clean_disease_map)
        result = add_incidence_rate(
            merged, pd.DataFrame(columns=["state", "population"])
        )
        assert result["incidence_per_100k"].isna().all()
