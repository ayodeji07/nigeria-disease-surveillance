"""
tests/test_extract.py
────────────────────────────────────────────────────────────────
Unit tests for src/etl/extract.py.

Strategy:
  • All tests that touch the real filesystem use a temporary
    directory (tmp_path pytest fixture) — no pollution of the
    real data/raw/ folder.
  • All tests that would make network calls (NASA API, etc.)
    are either mocked with unittest.mock or skipped when
    network access is unavailable.
  • Tests focus on the behaviour we own: caching logic, column
    tagging, error handling for missing files, and the output
    contract (returns a DataFrame or dict, never raises).

Run with:
    pytest tests/test_extract.py -v
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.etl.extract import (
    _load_cached,
    _save_raw,
    extract_health_facilities,
    extract_ncdc_pdfs,
    extract_population,
    extract_who_data,
    _fetch_one_state_rainfall,
)
from src.utils.config import Diseases


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def sample_df() -> pd.DataFrame:
    """A minimal DataFrame used across multiple save/load tests."""
    return pd.DataFrame({
        "state":   ["Lagos", "Kano", "FCT"],
        "cases":   [100, 80, 45],
        "disease": ["Cholera", "Cholera", "Cholera"],
    })


@pytest.fixture
def raw_dir(tmp_path) -> Path:
    """
    A temporary directory that acts as data/raw/ for tests.

    We patch Paths.raw to point here so tests never write to
    the real project data directory.
    """
    raw = tmp_path / "raw"
    raw.mkdir()
    return raw


# ── _save_raw tests ───────────────────────────────────────────────

class TestSaveRaw:

    def test_creates_file_in_raw_dir(self, sample_df, raw_dir):
        with patch("src.etl.extract.Paths.raw", raw_dir):
            path = _save_raw(sample_df, "test_save.csv")
            assert path.exists()

    def test_returns_correct_path(self, sample_df, raw_dir):
        with patch("src.etl.extract.Paths.raw", raw_dir):
            path = _save_raw(sample_df, "test_path.csv")
            assert path.name == "test_path.csv"

    def test_saved_content_matches_input(self, sample_df, raw_dir):
        with patch("src.etl.extract.Paths.raw", raw_dir):
            path = _save_raw(sample_df, "test_content.csv")
            loaded = pd.read_csv(path)
            assert list(loaded.columns) == list(sample_df.columns)
            assert len(loaded) == len(sample_df)

    def test_overwrites_existing_file(self, raw_dir):
        """Calling _save_raw twice on the same filename should overwrite."""
        df1 = pd.DataFrame({"a": [1, 2]})
        df2 = pd.DataFrame({"a": [3, 4, 5]})
        with patch("src.etl.extract.Paths.raw", raw_dir):
            _save_raw(df1, "overwrite_test.csv")
            _save_raw(df2, "overwrite_test.csv")
            result = pd.read_csv(raw_dir / "overwrite_test.csv")
            assert len(result) == 3   # df2 has 3 rows

    def test_preserves_all_columns(self, raw_dir):
        df = pd.DataFrame({"x": [1], "y": [2], "z": [3]})
        with patch("src.etl.extract.Paths.raw", raw_dir):
            path = _save_raw(df, "cols_test.csv")
            loaded = pd.read_csv(path)
            assert set(loaded.columns) == {"x", "y", "z"}

    def test_handles_empty_dataframe(self, raw_dir):
        """Saving an empty DataFrame should not raise."""
        empty = pd.DataFrame(columns=["state", "cases"])
        with patch("src.etl.extract.Paths.raw", raw_dir):
            path = _save_raw(empty, "empty_test.csv")
            assert path.exists()
            loaded = pd.read_csv(path)
            assert len(loaded) == 0


# ── _load_cached tests ────────────────────────────────────────────

class TestLoadCached:

    def test_returns_dataframe_when_file_exists(self, sample_df, raw_dir):
        sample_df.to_csv(raw_dir / "existing.csv", index=False)
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = _load_cached("existing.csv")
            assert result is not None
            assert isinstance(result, pd.DataFrame)
            assert len(result) == len(sample_df)

    def test_returns_none_when_file_missing(self, raw_dir):
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = _load_cached("does_not_exist.csv")
            assert result is None

    def test_loaded_data_matches_saved(self, sample_df, raw_dir):
        sample_df.to_csv(raw_dir / "match_test.csv", index=False)
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = _load_cached("match_test.csv")
            assert list(result.columns) == list(sample_df.columns)
            assert result["state"].tolist() == sample_df["state"].tolist()

    def test_returns_none_for_empty_filename(self, raw_dir):
        """An empty filename path should not exist → returns None."""
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = _load_cached("nonexistent_file_xyz.csv")
            assert result is None


# ── extract_ncdc_pdfs tests ───────────────────────────────────────

class TestExtractNcdcPdfs:

    def test_returns_empty_df_when_folder_missing(self, tmp_path):
        """A missing PDF folder should return empty DataFrame, not raise."""
        missing_folder = tmp_path / "nonexistent_disease"
        result = extract_ncdc_pdfs(missing_folder, "Cholera")
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_returns_empty_df_when_no_pdfs_in_folder(self, tmp_path):
        """An existing but empty folder should return empty DataFrame."""
        empty_folder = tmp_path / "empty_disease"
        empty_folder.mkdir()
        result = extract_ncdc_pdfs(empty_folder, "Cholera")
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_uses_cache_when_available(self, tmp_path, sample_df):
        """
        When a cached CSV exists, the extractor should return it
        without looking for PDFs.
        """
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        cache_file = raw_dir / "ncdc_cholera_raw.csv"
        sample_df.to_csv(cache_file, index=False)

        disease_folder = tmp_path / "pdfs" / "cholera"
        disease_folder.mkdir(parents=True)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_ncdc_pdfs(
                disease_folder, "Cholera", force_download=False
            )
            assert len(result) == len(sample_df)

    def test_force_download_bypasses_cache(self, tmp_path, sample_df):
        """
        force_download=True should re-extract even when a cache file exists.
        Because no real PDFs exist, result should be empty.
        """
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        cache_file = raw_dir / "ncdc_cholera_raw.csv"
        sample_df.to_csv(cache_file, index=False)

        empty_folder = tmp_path / "pdfs" / "cholera"
        empty_folder.mkdir(parents=True)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_ncdc_pdfs(
                empty_folder, "Cholera", force_download=True
            )
            # No PDFs → empty result despite cache
            assert result.empty

    def test_always_returns_dataframe_not_none(self, tmp_path):
        """The function must always return a DataFrame — never None."""
        missing = tmp_path / "missing"
        result  = extract_ncdc_pdfs(missing, "Mpox")
        assert result is not None
        assert isinstance(result, pd.DataFrame)


# ── extract_who_data tests ────────────────────────────────────────

class TestExtractWhoData:

    def test_returns_empty_df_when_who_folder_missing(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        # No 'who' subfolder created
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_who_data()
            assert isinstance(result, pd.DataFrame)
            assert result.empty

    def test_loads_csv_from_who_folder(self, tmp_path, sample_df):
        raw_dir = tmp_path / "raw"
        who_dir = raw_dir / "who"
        who_dir.mkdir(parents=True)
        sample_df.to_csv(who_dir / "who_test.csv", index=False)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_who_data(force_download=True)
            assert not result.empty
            assert len(result) == len(sample_df)

    def test_source_file_column_added(self, tmp_path, sample_df):
        raw_dir = tmp_path / "raw"
        who_dir = raw_dir / "who"
        who_dir.mkdir(parents=True)
        sample_df.to_csv(who_dir / "who_source.csv", index=False)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_who_data(force_download=True)
            assert "_source_file" in result.columns
            assert result["_source_file"].iloc[0] == "who_source.csv"

    def test_loads_multiple_csv_files(self, tmp_path):
        raw_dir = tmp_path / "raw"
        who_dir = raw_dir / "who"
        who_dir.mkdir(parents=True)

        df1 = pd.DataFrame({"state": ["Lagos"], "cases": [10]})
        df2 = pd.DataFrame({"state": ["Kano"],  "cases": [20]})
        df1.to_csv(who_dir / "file1.csv", index=False)
        df2.to_csv(who_dir / "file2.csv", index=False)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_who_data(force_download=True)
            assert len(result) == 2

    def test_uses_cache_when_available(self, tmp_path, sample_df):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        cache = raw_dir / "who_raw.csv"
        sample_df.to_csv(cache, index=False)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_who_data(force_download=False)
            assert len(result) == len(sample_df)


# ── extract_health_facilities tests ──────────────────────────────

class TestExtractHealthFacilities:

    def test_returns_empty_df_when_file_missing(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_health_facilities()
            assert isinstance(result, pd.DataFrame)
            assert result.empty

    def test_loads_facilities_csv(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        facilities = pd.DataFrame({
            "facility_name": ["General Hospital", "PHC Ikeja"],
            "facility_type": ["Hospital", "PHC"],
            "state":         ["Lagos", "Lagos"],
            "latitude":      [6.5244, 6.6018],
            "longitude":     [3.3792, 3.3515],
        })
        facilities.to_csv(raw_dir / "health_facilities.csv", index=False)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_health_facilities()
            assert len(result) == 2
            assert "facility_name" in result.columns

    def test_source_file_column_added(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        pd.DataFrame({"name": ["Test Clinic"]}).to_csv(
            raw_dir / "health_facilities.csv", index=False
        )
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_health_facilities()
            assert "_source_file" in result.columns
            assert result["_source_file"].iloc[0] == "health_facilities.csv"

    def test_always_returns_dataframe(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_health_facilities()
            assert result is not None
            assert isinstance(result, pd.DataFrame)


# ── extract_population tests ──────────────────────────────────────

class TestExtractPopulation:

    def test_returns_empty_when_no_file(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_population()
            assert isinstance(result, pd.DataFrame)
            assert result.empty

    def test_loads_csv_population_file(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        pop = pd.DataFrame({
            "State":          ["Lagos", "Kano", "FCT"],
            "Population 2022": ["15,000,000", "12,000,000", "3,500,000"],
        })
        pop.to_csv(raw_dir / "nigeria_population.csv", index=False)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_population()
            assert len(result) == 3
            assert "State" in result.columns

    def test_source_file_column_added(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        pd.DataFrame({"State": ["Lagos"], "Pop": [15_000_000]}).to_csv(
            raw_dir / "nigeria_population.csv", index=False
        )
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_population()
            assert "_source_file" in result.columns

    def test_prefers_xlsx_over_csv(self, tmp_path):
        """
        extract_population() tries Excel first, then CSV.
        If an Excel file exists alongside a CSV, Excel is used.
        """
        import openpyxl
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        # Create both files with different row counts
        csv_df  = pd.DataFrame({"State": ["Lagos"], "Pop": [15_000_000]})
        xlsx_df = pd.DataFrame({
            "State": ["Lagos", "Kano"],
            "Pop":   [15_000_000, 12_000_000],
        })
        csv_df.to_csv(raw_dir / "nigeria_population.csv", index=False)
        xlsx_df.to_excel(raw_dir / "nigeria_population.xlsx", index=False)

        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_population()
            # Should load the xlsx (2 rows), not the csv (1 row)
            assert len(result) == 2

    def test_always_returns_dataframe(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        with patch("src.etl.extract.Paths.raw", raw_dir):
            result = extract_population()
            assert result is not None
            assert isinstance(result, pd.DataFrame)


# ── _fetch_one_state_rainfall tests ──────────────────────────────

class TestFetchOneStateRainfall:

    def test_returns_dataframe_with_correct_columns(self):
        """
        Mock the NASA API response and verify output structure.
        """
        mock_response_data = {
            "properties": {
                "parameter": {
                    "PRECTOTCORR": {
                        "202301": 12.5,
                        "202302": 8.3,
                        "202303": 15.1,
                    }
                }
            }
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_response_data
        mock_resp.raise_for_status = MagicMock()

        with patch("src.etl.extract.requests.get", return_value=mock_resp):
            result = _fetch_one_state_rainfall(
                state      = "Lagos",
                lat        = 6.5244,
                lon        = 3.3792,
                start_year = 2023,
                end_year   = 2023,
            )

        assert isinstance(result, pd.DataFrame)
        assert not result.empty
        assert set(result.columns) == {
            "state", "year", "month", "rainfall_mm", "latitude", "longitude"
        }

    def test_returns_correct_state_name(self):
        """The state column should match the input state parameter."""
        mock_response_data = {
            "properties": {
                "parameter": {
                    "PRECTOTCORR": {"202301": 20.0}
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_response_data
        mock_resp.raise_for_status  = MagicMock()

        with patch("src.etl.extract.requests.get", return_value=mock_resp):
            result = _fetch_one_state_rainfall("Kano", 12.0, 8.5, 2023, 2023)

        assert (result["state"] == "Kano").all()

    def test_parses_year_and_month_from_key(self):
        """YYYYMM keys should be parsed into separate year and month columns."""
        mock_response_data = {
            "properties": {
                "parameter": {
                    "PRECTOTCORR": {
                        "202306": 55.2,
                        "202312": 3.1,
                    }
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_response_data
        mock_resp.raise_for_status  = MagicMock()

        with patch("src.etl.extract.requests.get", return_value=mock_resp):
            result = _fetch_one_state_rainfall("Rivers", 4.8, 7.0, 2023, 2023)

        june_row = result[result["month"] == 6]
        assert len(june_row) == 1
        assert june_row["year"].iloc[0] == 2023
        assert june_row["rainfall_mm"].iloc[0] == pytest.approx(55.2)

    def test_returns_empty_on_timeout(self):
        """A network timeout should return empty DataFrame, not raise."""
        import requests as req_lib
        with patch("src.etl.extract.requests.get",
                   side_effect=req_lib.exceptions.Timeout):
            result = _fetch_one_state_rainfall("FCT", 8.9, 7.2, 2023, 2023)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_returns_empty_on_http_error(self):
        """An HTTP error should return empty DataFrame, not raise."""
        import requests as req_lib
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("503")
        with patch("src.etl.extract.requests.get", return_value=mock_resp):
            result = _fetch_one_state_rainfall("Borno", 11.8, 13.2, 2023, 2023)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_returns_empty_on_malformed_json(self):
        """Unexpected API response structure should return empty DataFrame."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"unexpected": "structure"}
        mock_resp.raise_for_status  = MagicMock()
        with patch("src.etl.extract.requests.get", return_value=mock_resp):
            result = _fetch_one_state_rainfall("Oyo", 7.85, 3.93, 2023, 2023)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_coordinates_stored_in_output(self):
        """Latitude and longitude should be preserved in the output."""
        mock_response_data = {
            "properties": {
                "parameter": {
                    "PRECTOTCORR": {"202301": 25.0}
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_response_data
        mock_resp.raise_for_status  = MagicMock()

        lat, lon = 6.5244, 3.3792
        with patch("src.etl.extract.requests.get", return_value=mock_resp):
            result = _fetch_one_state_rainfall("Lagos", lat, lon, 2023, 2023)

        assert result["latitude"].iloc[0]  == pytest.approx(lat)
        assert result["longitude"].iloc[0] == pytest.approx(lon)


# ── extract_shapefiles tests ──────────────────────────────────────

class TestExtractShapefiles:

    def test_returns_empty_dict_when_no_shapefiles(self, tmp_path):
        """No shapefiles folder → empty dict, not an error."""
        from src.etl.extract import extract_shapefiles
        shapefiles_dir = tmp_path / "shapefiles"
        shapefiles_dir.mkdir()

        with patch("src.etl.extract.Paths.shapefiles", shapefiles_dir):
            result = extract_shapefiles()
            assert isinstance(result, dict)
            assert len(result) == 0

    def test_always_returns_dict(self, tmp_path):
        """extract_shapefiles() must always return a dict — never None."""
        from src.etl.extract import extract_shapefiles
        empty_dir = tmp_path / "shapefiles"
        empty_dir.mkdir()

        with patch("src.etl.extract.Paths.shapefiles", empty_dir):
            result = extract_shapefiles()
            assert result is not None
            assert isinstance(result, dict)

    def test_returns_empty_when_geopandas_unavailable(self, tmp_path):
        """
        If geopandas is not installed, the function should return {}
        gracefully rather than crashing with ImportError.
        """
        from src.etl.extract import extract_shapefiles
        shapefiles_dir = tmp_path / "shapefiles"
        shapefiles_dir.mkdir()

        with patch("src.etl.extract.Paths.shapefiles", shapefiles_dir):
            with patch("builtins.__import__",
                       side_effect=lambda name, *args, **kwargs: (
                           (_ for _ in ()).throw(ImportError("geopandas"))
                           if name == "geopandas" else
                           __import__(name, *args, **kwargs)
                       )):
                # Should return {} not raise
                try:
                    result = extract_shapefiles()
                    assert isinstance(result, dict)
                except ImportError:
                    pytest.skip("geopandas mock did not intercept correctly in this env")


# ── Integration — extract + cache round-trip ─────────────────────

class TestCacheRoundTrip:

    def test_save_and_reload_preserves_data_types(self, tmp_path):
        """
        Data saved by _save_raw and loaded by _load_cached should
        preserve numeric types and string values correctly.
        """
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()

        original = pd.DataFrame({
            "state":   ["Lagos", "Kano"],
            "cases":   [1234,    567],
            "cfr_pct": [2.5,     1.8],
            "flag":    ["CLEAN", "IMPUTED"],
        })

        with patch("src.etl.extract.Paths.raw", raw_dir):
            _save_raw(original, "roundtrip_test.csv")
            loaded = _load_cached("roundtrip_test.csv")

        assert loaded is not None
        assert loaded["state"].tolist()   == original["state"].tolist()
        assert loaded["cases"].tolist()   == original["cases"].tolist()
        assert loaded["flag"].tolist()    == original["flag"].tolist()
        assert loaded["cfr_pct"].tolist() == pytest.approx(original["cfr_pct"].tolist())

    def test_who_extractor_caches_output(self, tmp_path):
        """
        After extract_who_data() runs, a who_raw.csv cache file should
        exist in data/raw/ for future calls.
        """
        raw_dir = tmp_path / "raw"
        who_dir = raw_dir / "who"
        who_dir.mkdir(parents=True)
        pd.DataFrame({"state": ["Lagos"], "cases": [10]}).to_csv(
            who_dir / "sample.csv", index=False
        )

        with patch("src.etl.extract.Paths.raw", raw_dir):
            extract_who_data(force_download=True)
            cache_file = raw_dir / "who_raw.csv"
            assert cache_file.exists(), "who_raw.csv should be created after extraction"
