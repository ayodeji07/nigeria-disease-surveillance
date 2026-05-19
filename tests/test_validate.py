"""
tests/test_validate.py
────────────────────────────────────────────────────────────────
Unit tests for src/etl/validate.py.

Tests cover every individual check function and the report
aggregation logic. No database or network access needed.

Run with:
    pytest tests/test_validate.py -v
────────────────────────────────────────────────────────────────
"""

import numpy as np
import pandas as pd
import pytest

from src.etl.validate import (
    ValidationResult,
    ValidationReport,
    check_no_null_states,
    check_states_in_canonical_list,
    check_no_null_dates,
    check_case_counts_non_negative,
    check_confirmed_not_exceed_suspected,
    check_cfr_in_range,
    check_date_range,
    check_disease_values,
    check_state_coverage,
    check_no_duplicate_rows,
    check_incidence_rate_plausibility,
    run_all_checks,
    run_population_checks,
    run_rainfall_checks,
)
from src.utils.config import Diseases
from src.utils.state_maps import CANONICAL_STATES


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def good_df() -> pd.DataFrame:
    """Well-formed DataFrame — all 11 checks should pass."""
    return pd.DataFrame({
        "state":               CANONICAL_STATES,
        "disease":             [Diseases.CHOLERA] * 37,
        "report_date":         pd.date_range("2023-01-02", periods=37, freq="W"),
        "suspected_cases":     [100] * 37,
        "confirmed_cases":     [80]  * 37,
        "deaths":              [2]   * 37,
        "cfr_pct":             [2.5] * 37,
        "incidence_per_100k":  [10.0] * 37,
        "data_quality_flag":   ["CLEAN"] * 37,
    })


@pytest.fixture
def minimal_bad_df() -> pd.DataFrame:
    """DataFrame with deliberate errors across multiple check categories."""
    return pd.DataFrame({
        "state":              ["Lagos", None, "UNKNOWN:XYZ", "Kano"],
        "disease":            [Diseases.CHOLERA, Diseases.CHOLERA,
                               "FakeDisease", Diseases.CHOLERA],
        "report_date":        [
            pd.Timestamp("2023-01-02"), pd.NaT,
            pd.Timestamp("1985-01-01"),    # out of valid range
            pd.Timestamp("2023-01-02"),
        ],
        "suspected_cases":    [100, 50, 10, 5],
        "confirmed_cases":    [-5,  60,  8, 3],   # -5 invalid; 60 > 50 suspicious
        "deaths":             [2,   1,   0, 0],
        "cfr_pct":            [110.0, 1.67, 0, 0],  # 110% impossible
        "incidence_per_100k": [10.0, 5.0, 2.0, 1.0],
    })


# ── ValidationResult ──────────────────────────────────────────────

class TestValidationResult:

    def test_pass_rate_perfect(self):
        r = ValidationResult(
            check_name="test", passed=True,
            failed_row_count=0, total_row_count=100,
        )
        assert r.pass_rate == 1.0

    def test_pass_rate_zero(self):
        r = ValidationResult(
            check_name="test", passed=False,
            failed_row_count=100, total_row_count=100,
        )
        assert r.pass_rate == 0.0

    def test_pass_rate_partial(self):
        r = ValidationResult(
            check_name="test", passed=False,
            failed_row_count=3, total_row_count=10,
        )
        assert abs(r.pass_rate - 0.7) < 1e-9

    def test_pass_rate_zero_total_rows(self):
        r = ValidationResult(
            check_name="test", passed=True,
            failed_row_count=0, total_row_count=0,
        )
        assert r.pass_rate == 1.0

    def test_str_contains_check_name(self):
        r = ValidationResult(check_name="my_check", passed=True, message="OK")
        assert "my_check" in str(r)

    def test_str_shows_pass(self):
        r = ValidationResult(check_name="c", passed=True)
        assert "PASS" in str(r)

    def test_str_shows_fail_with_severity(self):
        r = ValidationResult(check_name="c", passed=False, severity="ERROR")
        assert "FAIL" in str(r)
        assert "ERROR" in str(r)


# ── ValidationReport ──────────────────────────────────────────────

class TestValidationReport:

    def test_has_errors_true_when_error_fails(self):
        report = ValidationReport(table_name="t")
        report.results.append(
            ValidationResult("c", passed=False, severity="ERROR")
        )
        assert report.has_errors is True

    def test_has_errors_false_when_only_warnings(self):
        report = ValidationReport(table_name="t")
        report.results.append(
            ValidationResult("c", passed=False, severity="WARNING")
        )
        assert report.has_errors is False

    def test_has_warnings_true(self):
        report = ValidationReport(table_name="t")
        report.results.append(
            ValidationResult("c", passed=False, severity="WARNING")
        )
        assert report.has_warnings is True

    def test_error_count(self):
        report = ValidationReport(table_name="t")
        report.results = [
            ValidationResult("a", passed=False, severity="ERROR"),
            ValidationResult("b", passed=False, severity="ERROR"),
            ValidationResult("c", passed=False, severity="WARNING"),
            ValidationResult("d", passed=True),
        ]
        assert report.error_count == 2
        assert report.warning_count == 1

    def test_to_dataframe_has_required_columns(self):
        report = ValidationReport(table_name="my_table")
        report.results.append(
            ValidationResult("check1", passed=True,
                             failed_row_count=0, total_row_count=10,
                             message="All good")
        )
        df = report.to_dataframe()
        for col in {"table_name", "check_name", "status",
                    "records_affected", "total_records", "pass_rate", "message"}:
            assert col in df.columns, f"Missing column: {col}"

    def test_to_dataframe_one_row_per_result(self):
        report = ValidationReport(table_name="t")
        for i in range(5):
            report.results.append(ValidationResult(f"c{i}", passed=True))
        assert len(report.to_dataframe()) == 5

    def test_summary_contains_table_name(self):
        report = ValidationReport(table_name="surveillance_table")
        assert "surveillance_table" in report.summary()


# ── check_no_null_states ──────────────────────────────────────────

class TestCheckNoNullStates:

    def test_passes_with_all_canonical_states(self, good_df):
        assert check_no_null_states(good_df).passed is True

    def test_fails_with_none_state(self):
        df = pd.DataFrame({"state": [None, "Lagos"]})
        r  = check_no_null_states(df)
        assert r.passed is False
        assert r.failed_row_count >= 1

    def test_fails_with_unknown_prefix(self):
        df = pd.DataFrame({"state": ["UNKNOWN:XYZ", "Lagos"]})
        assert check_no_null_states(df).passed is False

    def test_severity_is_error(self, good_df):
        assert check_no_null_states(good_df).severity == "ERROR"

    def test_failed_examples_populated(self):
        df = pd.DataFrame({"state": ["UNKNOWN:ABC"]})
        r  = check_no_null_states(df)
        assert len(r.failed_examples) > 0


# ── check_states_in_canonical_list ───────────────────────────────

class TestCheckStatesInCanonicalList:

    def test_passes_with_37_canonical_states(self, good_df):
        assert check_states_in_canonical_list(good_df).passed is True

    def test_fails_with_invalid_state(self):
        df = pd.DataFrame({"state": ["Lagos", "FakeState"]})
        r  = check_states_in_canonical_list(df)
        assert r.passed is False
        assert "FakeState" in r.failed_examples

    def test_severity_is_error(self):
        df = pd.DataFrame({"state": ["BadState"]})
        assert check_states_in_canonical_list(df).severity == "ERROR"


# ── check_no_null_dates ───────────────────────────────────────────

class TestCheckNoNullDates:

    def test_passes_with_all_dates(self, good_df):
        assert check_no_null_dates(good_df).passed is True

    def test_fails_with_nat(self):
        df = pd.DataFrame({
            "report_date": [pd.Timestamp("2023-01-01"), pd.NaT]
        })
        r = check_no_null_dates(df)
        assert r.passed is False
        assert r.failed_row_count == 1

    def test_severity_is_warning(self):
        df = pd.DataFrame({"report_date": [pd.NaT]})
        assert check_no_null_dates(df).severity == "WARNING"


# ── check_case_counts_non_negative ───────────────────────────────

class TestCheckCaseCountsNonNegative:

    def test_passes_with_zero_and_positive(self, good_df):
        assert check_case_counts_non_negative(good_df).passed is True

    def test_fails_with_negative_confirmed(self):
        df = pd.DataFrame({
            "confirmed_cases": [-1, 10],
            "suspected_cases": [5,  20],
            "deaths":          [0,   1],
        })
        assert check_case_counts_non_negative(df).passed is False

    def test_fails_with_negative_deaths(self):
        df = pd.DataFrame({
            "confirmed_cases": [10],
            "deaths":          [-1],
        })
        assert check_case_counts_non_negative(df).passed is False

    def test_severity_is_error(self):
        df = pd.DataFrame({"confirmed_cases": [-5]})
        assert check_case_counts_non_negative(df).severity == "ERROR"

    def test_zero_is_valid(self):
        df = pd.DataFrame({
            "confirmed_cases": [0],
            "suspected_cases": [0],
            "deaths":          [0],
        })
        assert check_case_counts_non_negative(df).passed is True


# ── check_confirmed_not_exceed_suspected ─────────────────────────

class TestCheckConfirmedNotExceedSuspected:

    def test_passes_when_confirmed_lte_suspected(self, good_df):
        assert check_confirmed_not_exceed_suspected(good_df).passed is True

    def test_fails_when_confirmed_gt_suspected(self):
        df = pd.DataFrame({
            "confirmed_cases": [60],
            "suspected_cases": [50],
        })
        assert check_confirmed_not_exceed_suspected(df).passed is False

    def test_passes_when_suspected_is_zero(self):
        # zero suspected + some confirmed = lab-only reporting (valid)
        df = pd.DataFrame({
            "confirmed_cases": [10],
            "suspected_cases": [0],
        })
        assert check_confirmed_not_exceed_suspected(df).passed is True

    def test_severity_is_warning(self):
        df = pd.DataFrame({
            "confirmed_cases": [60],
            "suspected_cases": [50],
        })
        assert check_confirmed_not_exceed_suspected(df).severity == "WARNING"

    def test_skips_when_columns_missing(self):
        df = pd.DataFrame({"confirmed_cases": [10]})
        r  = check_confirmed_not_exceed_suspected(df)
        # Should not crash — returns INFO-level skip
        assert r.severity == "INFO"


# ── check_cfr_in_range ────────────────────────────────────────────

class TestCheckCfrInRange:

    def test_passes_with_valid_cfr(self, good_df):
        assert check_cfr_in_range(good_df).passed is True

    def test_fails_with_cfr_above_100(self):
        df = pd.DataFrame({"cfr_pct": [110.0, 2.5]})
        assert check_cfr_in_range(df).passed is False

    def test_fails_with_negative_cfr(self):
        df = pd.DataFrame({"cfr_pct": [-1.0]})
        assert check_cfr_in_range(df).passed is False

    def test_skips_when_column_absent(self):
        df = pd.DataFrame({"confirmed_cases": [10]})
        r  = check_cfr_in_range(df)
        assert r.passed is True
        assert r.severity == "INFO"

    def test_passes_with_zero_cfr(self):
        df = pd.DataFrame({"cfr_pct": [0.0, 0.0]})
        assert check_cfr_in_range(df).passed is True


# ── check_date_range ──────────────────────────────────────────────

class TestCheckDateRange:

    def test_passes_with_valid_dates(self, good_df):
        assert check_date_range(good_df).passed is True

    def test_fails_with_very_old_date(self):
        df = pd.DataFrame({
            "report_date": [
                pd.Timestamp("1985-01-01"),
                pd.Timestamp("2023-06-01"),
            ]
        })
        assert check_date_range(df).passed is False

    def test_fails_with_far_future_date(self):
        df = pd.DataFrame({
            "report_date": [pd.Timestamp("2099-01-01")]
        })
        assert check_date_range(df, max_year=2050).passed is False

    def test_ignores_nat_values(self):
        df = pd.DataFrame({
            "report_date": [pd.NaT, pd.Timestamp("2023-06-01")]
        })
        # NaT rows are skipped — only valid dates are range-checked
        result = check_date_range(df)
        assert result.total_row_count == 1


# ── check_disease_values ──────────────────────────────────────────

class TestCheckDiseaseValues:

    def test_passes_with_known_diseases(self, good_df):
        assert check_disease_values(good_df).passed is True

    def test_fails_with_unknown_disease(self):
        df = pd.DataFrame({"disease": ["Cholera", "FakeDisease"]})
        r  = check_disease_values(df)
        assert r.passed is False
        assert "FakeDisease" in r.failed_examples

    def test_fails_when_disease_column_missing(self):
        df = pd.DataFrame({"state": ["Lagos"]})
        assert check_disease_values(df).passed is False

    def test_severity_is_error(self):
        df = pd.DataFrame({"disease": ["Unknown"]})
        assert check_disease_values(df).severity == "ERROR"


# ── check_state_coverage ──────────────────────────────────────────

class TestCheckStateCoverage:

    def test_passes_with_all_37_states(self, good_df):
        assert check_state_coverage(good_df).passed is True

    def test_fails_with_only_two_states(self):
        df = pd.DataFrame({"state": ["Lagos", "Kano"]})
        assert check_state_coverage(df).passed is False

    def test_missing_states_in_failed_examples(self):
        df = pd.DataFrame({"state": ["Lagos", "Kano"]})
        r  = check_state_coverage(df)
        # Some of the 35 missing states should appear in failed_examples
        assert len(r.failed_examples) > 0

    def test_severity_is_warning(self):
        df = pd.DataFrame({"state": ["Lagos"]})
        assert check_state_coverage(df).severity == "WARNING"


# ── check_no_duplicate_rows ───────────────────────────────────────

class TestCheckNoDuplicateRows:

    def test_passes_with_unique_rows(self, good_df):
        assert check_no_duplicate_rows(good_df).passed is True

    def test_fails_with_exact_duplicate(self):
        df = pd.DataFrame({
            "state":       ["Lagos",                 "Lagos"],
            "disease":     ["Cholera",               "Cholera"],
            "report_date": [pd.Timestamp("2023-01-02")] * 2,
        })
        r = check_no_duplicate_rows(df)
        assert r.passed is False
        assert r.failed_row_count == 2

    def test_passes_same_state_different_disease(self):
        df = pd.DataFrame({
            "state":       ["Lagos",                 "Lagos"],
            "disease":     ["Cholera",               "Mpox"],
            "report_date": [pd.Timestamp("2023-01-02")] * 2,
        })
        assert check_no_duplicate_rows(df).passed is True


# ── check_incidence_rate_plausibility ────────────────────────────

class TestCheckIncidencePlausibility:

    def test_passes_with_reasonable_values(self, good_df):
        assert check_incidence_rate_plausibility(good_df).passed is True

    def test_fails_with_implausible_value(self):
        df = pd.DataFrame({"incidence_per_100k": [99_999.0, 10.0]})
        assert check_incidence_rate_plausibility(df).passed is False

    def test_skips_when_column_absent(self):
        df = pd.DataFrame({"confirmed_cases": [10]})
        r  = check_incidence_rate_plausibility(df)
        assert r.passed is True
        assert r.severity == "INFO"

    def test_threshold_is_5000(self):
        # 4999 should pass; 5001 should fail
        df_ok   = pd.DataFrame({"incidence_per_100k": [4999.0]})
        df_fail = pd.DataFrame({"incidence_per_100k": [5001.0]})
        assert check_incidence_rate_plausibility(df_ok).passed is True
        assert check_incidence_rate_plausibility(df_fail).passed is False


# ── run_all_checks ────────────────────────────────────────────────

class TestRunAllChecks:

    def test_good_data_passes_all_checks(self, good_df):
        report = run_all_checks(good_df, "test_good")
        assert report.has_errors   is False
        assert report.has_warnings is False

    def test_bad_data_has_errors(self, minimal_bad_df):
        report = run_all_checks(minimal_bad_df, "test_bad")
        assert report.has_errors is True

    def test_exactly_11_checks_run(self, good_df):
        report = run_all_checks(good_df, "test")
        assert len(report.results) == 11

    def test_summary_contains_table_name(self, good_df):
        report = run_all_checks(good_df, "fact_surveillance")
        assert "fact_surveillance" in report.summary()

    def test_to_dataframe_length_matches_checks(self, good_df):
        report = run_all_checks(good_df, "test")
        assert len(report.to_dataframe()) == 11

    def test_crashed_check_does_not_raise(self):
        # Pass a completely empty DataFrame — some checks will handle
        # it gracefully, others may encounter edge cases.
        # The important thing is no unhandled exception.
        try:
            report = run_all_checks(pd.DataFrame(), "empty_test")
            assert isinstance(report, ValidationReport)
        except Exception as exc:
            pytest.fail(f"run_all_checks raised unexpectedly: {exc}")


# ── Supplementary checks ──────────────────────────────────────────

class TestPopulationChecks:

    def test_passes_with_valid_population(self):
        df = pd.DataFrame({
            "state":      CANONICAL_STATES,
            "population": [1_000_000] * 37,
        })
        report = run_population_checks(df)
        assert not report.has_errors

    def test_fails_with_missing_columns(self):
        report = run_population_checks(pd.DataFrame())
        assert report.has_errors

    def test_warns_on_zero_population(self):
        df = pd.DataFrame({
            "state":      ["Lagos", "Kano"],
            "population": [0, 1_000_000],
        })
        report = run_population_checks(df)
        # Zero population should trigger a warning
        assert report.has_warnings or report.has_errors


class TestRainfallChecks:

    def test_passes_with_valid_rainfall(self):
        df = pd.DataFrame({
            "state":       CANONICAL_STATES[:5],
            "year":        [2023] * 5,
            "month":       [1] * 5,
            "rainfall_mm": [25.0] * 5,
        })
        report = run_rainfall_checks(df)
        assert not report.has_errors

    def test_warns_or_errors_on_empty(self):
        report = run_rainfall_checks(pd.DataFrame())
        assert report.has_warnings or report.has_errors

    def test_warns_on_residual_negative(self):
        df = pd.DataFrame({
            "state":       ["Lagos"],
            "year":        [2023],
            "month":       [1],
            "rainfall_mm": [-5.0],
        })
        report = run_rainfall_checks(df)
        assert report.has_warnings or report.has_errors
