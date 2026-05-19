"""
src/etl/validate.py
────────────────────────────────────────────────────────────────
Data validation — the quality gate between Transform and Load.

This module sits between transform.py and load.py. Before any
data is written to the database, it passes through here first.

Why a dedicated validation layer?
  Cleaning (transform.py) fixes what we *know* is wrong.
  Validation (this module) catches what we *don't expect* —
  things that should be true but might not be given the
  messiness of real public health data.

Design:
  - Each check returns a ValidationResult (passed, failed rows,
    a human-readable message).
  - The run_all_checks() function assembles a full report.
  - The pipeline decides whether to abort or continue based on
    the severity of failures — warnings are logged, errors halt.
  - All results are persisted to the data_quality_log table so
    there is a permanent audit trail of every pipeline run.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.config import INCIDENCE_PER_N, Diseases
from src.utils.logger import get_logger
from src.utils.state_maps import CANONICAL_STATE_SET

logger = get_logger(__name__)


# ── Result dataclass ─────────────────────────────────────────────

@dataclass
class ValidationResult:
    """
    The outcome of a single validation check.

    Attributes
    ----------
    check_name : str
        A short, readable identifier for this check.
    passed : bool
        True if the check found no violations.
    severity : str
        "ERROR"   — pipeline should stop, data is fundamentally broken.
        "WARNING" — pipeline can continue, but analyst should review.
        "INFO"    — informational, always logged regardless of outcome.
    failed_row_count : int
        Number of rows that violated the check.
    total_row_count : int
        Total rows the check was applied to.
    message : str
        Human-readable description of what was checked and what failed.
    failed_examples : list
        A sample of the actual failing values (capped at 10).
    """
    check_name:       str
    passed:           bool
    severity:         str          = "WARNING"
    failed_row_count: int          = 0
    total_row_count:  int          = 0
    message:          str          = ""
    failed_examples:  list         = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        """Fraction of rows that passed (0.0 – 1.0)."""
        if self.total_row_count == 0:
            return 1.0
        return (self.total_row_count - self.failed_row_count) / self.total_row_count

    def __str__(self) -> str:
        status = "PASS" if self.passed else f"FAIL [{self.severity}]"
        return (
            f"[{status}] {self.check_name}: {self.message} "
            f"({self.failed_row_count}/{self.total_row_count} rows affected)"
        )


@dataclass
class ValidationReport:
    """
    Aggregated results from running all checks on a DataFrame.

    Attributes
    ----------
    table_name : str
        Name of the logical table being validated (e.g. "disease_surveillance").
    results : list[ValidationResult]
        Individual check results.
    """
    table_name: str
    results: list[ValidationResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """True if any ERROR-severity check failed."""
        return any(
            not r.passed and r.severity == "ERROR"
            for r in self.results
        )

    @property
    def has_warnings(self) -> bool:
        """True if any WARNING-severity check failed."""
        return any(
            not r.passed and r.severity == "WARNING"
            for r in self.results
        )

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if not r.passed and r.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for r in self.results if not r.passed and r.severity == "WARNING")

    def summary(self) -> str:
        """Return a one-line summary of the validation run."""
        total   = len(self.results)
        passed  = sum(1 for r in self.results if r.passed)
        return (
            f"{self.table_name}: {passed}/{total} checks passed | "
            f"{self.error_count} errors, {self.warning_count} warnings"
        )

    def to_dataframe(self) -> pd.DataFrame:
        """
        Convert results to a DataFrame suitable for DB logging.

        Returns
        -------
        pd.DataFrame
            One row per check — matches the data_quality_log schema.
        """
        rows = []
        for r in self.results:
            rows.append(
                {
                    "table_name":        self.table_name,
                    "check_name":        r.check_name,
                    "status":            "PASS" if r.passed else f"FAIL_{r.severity}",
                    "records_affected":  r.failed_row_count,
                    "total_records":     r.total_row_count,
                    "pass_rate":         round(r.pass_rate, 4),
                    "message":           r.message,
                    "failed_examples":   str(r.failed_examples[:5]),
                }
            )
        return pd.DataFrame(rows)

    def log_all(self) -> None:
        """Write every result to the application logger."""
        logger.info("─── Validation report: %s ───", self.table_name)
        for result in self.results:
            if result.passed:
                logger.info("  %s", result)
            elif result.severity == "ERROR":
                logger.error("  %s", result)
            else:
                logger.warning("  %s", result)
        logger.info("  Summary: %s", self.summary())


# ── Individual check functions ───────────────────────────────────
# Each function takes a DataFrame and returns a ValidationResult.
# They are pure — no side effects, no DB access.

def check_no_null_states(df: pd.DataFrame) -> ValidationResult:
    """
    Verify every row has a non-null, canonical state name.

    This is an ERROR because rows with null states cannot be
    joined to the dim_states dimension table.
    """
    null_mask   = df["state"].isna()
    unknown_mask = df["state"].str.startswith("UNKNOWN:", na=False)
    failed_mask = null_mask | unknown_mask

    failed_count = int(failed_mask.sum())
    examples = df.loc[failed_mask, "state"].unique().tolist()[:10]

    return ValidationResult(
        check_name="no_null_states",
        passed=failed_count == 0,
        severity="ERROR",
        failed_row_count=failed_count,
        total_row_count=len(df),
        message=(
            "All state values must be canonical and non-null."
            if failed_count == 0
            else f"Found {failed_count} rows with null or unrecognised states."
        ),
        failed_examples=examples,
    )


def check_states_in_canonical_list(df: pd.DataFrame) -> ValidationResult:
    """
    Verify all state values are members of the canonical 37-state list.

    This is an ERROR — an unrecognised state cannot be stored in
    the dim_states dimension table.
    """
    invalid_mask  = ~df["state"].isin(CANONICAL_STATE_SET)
    failed_count  = int(invalid_mask.sum())
    examples      = df.loc[invalid_mask, "state"].unique().tolist()[:10]

    return ValidationResult(
        check_name="states_in_canonical_list",
        passed=failed_count == 0,
        severity="ERROR",
        failed_row_count=failed_count,
        total_row_count=len(df),
        message=(
            "All states are in the canonical list."
            if failed_count == 0
            else f"{failed_count} rows have states not in the canonical list."
        ),
        failed_examples=examples,
    )


def check_no_null_dates(df: pd.DataFrame) -> ValidationResult:
    """
    Verify every row has a non-null report_date.

    This is a WARNING rather than ERROR — rows with null dates
    can still be stored (as NULL in the DB), but they cannot
    participate in time-series analysis.
    """
    failed_mask  = df["report_date"].isna()
    failed_count = int(failed_mask.sum())

    return ValidationResult(
        check_name="no_null_dates",
        passed=failed_count == 0,
        severity="WARNING",
        failed_row_count=failed_count,
        total_row_count=len(df),
        message=(
            "All rows have a report_date."
            if failed_count == 0
            else f"{failed_count} rows are missing a report_date."
        ),
    )


def check_case_counts_non_negative(df: pd.DataFrame) -> ValidationResult:
    """
    Verify confirmed_cases and deaths are >= 0.

    Negative case counts are physically impossible — they indicate
    a data entry error or a failed numeric conversion.
    """
    cols_to_check = [
        c for c in ["suspected_cases", "confirmed_cases", "deaths"]
        if c in df.columns
    ]

    violations: list[str] = []
    total_failed = 0

    for col in cols_to_check:
        negative_mask = df[col] < 0
        n_negative    = int(negative_mask.sum())
        if n_negative > 0:
            violations.append(f"{col}: {n_negative} negative values")
            total_failed += n_negative

    return ValidationResult(
        check_name="case_counts_non_negative",
        passed=total_failed == 0,
        severity="ERROR",
        failed_row_count=total_failed,
        total_row_count=len(df) * len(cols_to_check),
        message=(
            "All case counts are non-negative."
            if total_failed == 0
            else f"Negative count values found: {'; '.join(violations)}"
        ),
        failed_examples=violations,
    )


def check_confirmed_not_exceed_suspected(df: pd.DataFrame) -> ValidationResult:
    """
    Verify confirmed cases do not exceed suspected cases.

    Epidemiologically, confirmed ≤ suspected should always hold.
    Violations suggest a data entry error or inconsistent reporting.
    This is a WARNING — the violation may reflect a reporting
    period mismatch rather than a genuine error.
    """
    if "suspected_cases" not in df.columns or "confirmed_cases" not in df.columns:
        return ValidationResult(
            check_name="confirmed_not_exceed_suspected",
            passed=True,
            severity="INFO",
            message="Skipped — one or both count columns not present.",
        )

    # Only flag rows where suspected > 0 (0 suspected with some confirmed
    # is common when only lab-confirmed cases are reported)
    relevant = df[df["suspected_cases"] > 0]
    failed_mask  = relevant["confirmed_cases"] > relevant["suspected_cases"]
    failed_count = int(failed_mask.sum())

    return ValidationResult(
        check_name="confirmed_not_exceed_suspected",
        passed=failed_count == 0,
        severity="WARNING",
        failed_row_count=failed_count,
        total_row_count=len(relevant),
        message=(
            "Confirmed cases ≤ suspected cases for all rows."
            if failed_count == 0
            else f"{failed_count} rows where confirmed > suspected."
        ),
    )


def check_cfr_in_range(df: pd.DataFrame) -> ValidationResult:
    """
    Verify the Case Fatality Rate is between 0% and 100%.

    CFR > 100% is mathematically impossible. CFR > 50% for a
    large cohort is extremely unusual and warrants investigation.
    """
    if "cfr_pct" not in df.columns:
        return ValidationResult(
            check_name="cfr_in_range",
            passed=True,
            severity="INFO",
            message="Skipped — cfr_pct column not present.",
        )

    impossible_mask  = (df["cfr_pct"] < 0) | (df["cfr_pct"] > 100)
    suspicious_mask  = df["cfr_pct"] > 50

    impossible_count = int(impossible_mask.sum())
    suspicious_count = int(suspicious_mask.sum())

    passed  = impossible_count == 0
    message_parts = []
    if impossible_count > 0:
        message_parts.append(f"{impossible_count} rows with CFR outside 0–100%")
    if suspicious_count > 0:
        message_parts.append(f"{suspicious_count} rows with CFR > 50% (investigate)")

    return ValidationResult(
        check_name="cfr_in_range",
        passed=passed,
        severity="WARNING",
        failed_row_count=impossible_count + suspicious_count,
        total_row_count=len(df),
        message=(
            "All CFR values in 0–100%."
            if not message_parts
            else " | ".join(message_parts)
        ),
    )


def check_date_range(
    df: pd.DataFrame,
    min_year: int = 2010,
    max_year: int = 2030,
) -> ValidationResult:
    """
    Verify report_date falls within the expected historical range.

    Dates outside this window are almost certainly parse errors.
    The bounds are intentionally generous to accommodate future data.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain a 'report_date' column.
    min_year : int
        Earliest acceptable year.
    max_year : int
        Latest acceptable year.
    """
    if "report_date" not in df.columns:
        return ValidationResult(
            check_name="date_range",
            passed=True,
            severity="INFO",
            message="Skipped — report_date column not present.",
        )

    valid_dates = df["report_date"].dropna()
    out_of_range = valid_dates[
        (valid_dates.dt.year < min_year) | (valid_dates.dt.year > max_year)
    ]
    failed_count = len(out_of_range)

    return ValidationResult(
        check_name="date_range",
        passed=failed_count == 0,
        severity="WARNING",
        failed_row_count=failed_count,
        total_row_count=len(valid_dates),
        message=(
            f"All dates in {min_year}–{max_year}."
            if failed_count == 0
            else f"{failed_count} dates fall outside {min_year}–{max_year}."
        ),
        failed_examples=out_of_range.dt.year.unique().tolist()[:5],
    )


def check_disease_values(df: pd.DataFrame) -> ValidationResult:
    """
    Verify the disease column contains only known disease names.

    This is an ERROR — an unknown disease cannot be joined to
    the dim_diseases dimension table.
    """
    if "disease" not in df.columns:
        return ValidationResult(
            check_name="disease_values",
            passed=False,
            severity="ERROR",
            message="'disease' column is missing entirely.",
        )

    known_diseases = set(Diseases.all)
    unknown_mask   = ~df["disease"].isin(known_diseases)
    failed_count   = int(unknown_mask.sum())
    examples       = df.loc[unknown_mask, "disease"].unique().tolist()[:10]

    return ValidationResult(
        check_name="disease_values",
        passed=failed_count == 0,
        severity="ERROR",
        failed_row_count=failed_count,
        total_row_count=len(df),
        message=(
            "All disease values are recognised."
            if failed_count == 0
            else f"{failed_count} rows have unrecognised disease names: {examples}"
        ),
        failed_examples=examples,
    )


def check_state_coverage(df: pd.DataFrame) -> ValidationResult:
    """
    Verify the dataset covers a reasonable number of states.

    We expect all 37 administrative units for a full national dataset.
    Fewer than 30 suggests a significant data gap.

    This is a WARNING — a partial dataset is still useful.
    """
    if "state" not in df.columns:
        return ValidationResult(
            check_name="state_coverage",
            passed=False,
            severity="WARNING",
            message="'state' column is missing.",
        )

    states_present = df["state"].nunique()
    missing_states = sorted(
        CANONICAL_STATE_SET - set(df["state"].unique())
    )
    passed = states_present >= 30

    return ValidationResult(
        check_name="state_coverage",
        passed=passed,
        severity="WARNING",
        failed_row_count=37 - states_present,
        total_row_count=37,
        message=(
            f"All 37 states/FCT represented."
            if states_present == 37
            else f"Only {states_present}/37 states present. "
                 f"Missing: {', '.join(missing_states[:10])}"
        ),
        failed_examples=missing_states[:10],
    )


def check_no_duplicate_rows(df: pd.DataFrame) -> ValidationResult:
    """
    Verify there are no duplicate (state, disease, report_date) combinations.

    Duplicates cause double-counting in aggregations and violate the
    UNIQUE constraint on the fact table.
    """
    key_cols = [
        c for c in ["state", "disease", "report_date"]
        if c in df.columns
    ]

    if len(key_cols) < 2:
        return ValidationResult(
            check_name="no_duplicate_rows",
            passed=True,
            severity="INFO",
            message="Skipped — insufficient key columns present.",
        )

    duplicate_mask = df.duplicated(subset=key_cols, keep=False)
    failed_count   = int(duplicate_mask.sum())

    return ValidationResult(
        check_name="no_duplicate_rows",
        passed=failed_count == 0,
        severity="WARNING",
        failed_row_count=failed_count,
        total_row_count=len(df),
        message=(
            "No duplicate (state, disease, date) combinations."
            if failed_count == 0
            else f"{failed_count} rows are duplicates on keys: {key_cols}"
        ),
    )


def check_incidence_rate_plausibility(df: pd.DataFrame) -> ValidationResult:
    """
    Flag incidence rates that are unrealistically high.

    A weekly incidence rate above 5,000 per 100,000 (5% of the
    entire state population in one week) is almost certainly a
    data quality issue — not a genuine outbreak.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain 'incidence_per_100k' column.
    """
    if "incidence_per_100k" not in df.columns:
        return ValidationResult(
            check_name="incidence_rate_plausibility",
            passed=True,
            severity="INFO",
            message="Skipped — incidence_per_100k column not present.",
        )

    # Cap at 5% of population in one reporting period
    threshold     = 5_000.0
    failed_mask   = df["incidence_per_100k"] > threshold
    failed_count  = int(failed_mask.sum())

    return ValidationResult(
        check_name="incidence_rate_plausibility",
        passed=failed_count == 0,
        severity="WARNING",
        failed_row_count=failed_count,
        total_row_count=len(df[df["incidence_per_100k"].notna()]),
        message=(
            f"All incidence rates ≤ {threshold:,.0f}/100k."
            if failed_count == 0
            else f"{failed_count} rows have incidence > {threshold:,.0f}/100k."
        ),
    )


# ── Orchestrator ─────────────────────────────────────────────────

def run_all_checks(
    df: pd.DataFrame,
    table_name: str = "disease_surveillance",
) -> ValidationReport:
    """
    Run the full validation suite against a cleaned DataFrame.

    This is the main entry point called by the ETL pipeline.
    It runs every check and assembles a ValidationReport which
    the pipeline then logs and (optionally) persists to the DB.

    Parameters
    ----------
    df : pd.DataFrame
        A cleaned DataFrame — output of transform.py functions.
    table_name : str
        Label used in the report for logging and DB storage.

    Returns
    -------
    ValidationReport
        Full report with one result per check.
    """
    report = ValidationReport(table_name=table_name)

    checks = [
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
    ]

    for check_fn in checks:
        try:
            result = check_fn(df)
            report.results.append(result)
        except Exception as exc:
            # A crashed check should not bring down the pipeline.
            # Log it and add a failed result so it appears in the report.
            logger.error(
                "Check %s raised an exception: %s",
                check_fn.__name__,
                exc,
                exc_info=True,
            )
            report.results.append(
                ValidationResult(
                    check_name=check_fn.__name__,
                    passed=False,
                    severity="ERROR",
                    message=f"Check raised exception: {exc}",
                )
            )

    report.log_all()
    return report


def run_population_checks(df: pd.DataFrame) -> ValidationReport:
    """
    Run basic checks on the cleaned population DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Output of transform.clean_population_data().

    Returns
    -------
    ValidationReport
    """
    report = ValidationReport(table_name="population")

    # Must have both required columns
    if "state" not in df.columns or "population" not in df.columns:
        report.results.append(
            ValidationResult(
                check_name="required_columns",
                passed=False,
                severity="ERROR",
                message="Population DataFrame is missing 'state' or 'population' column.",
            )
        )
        report.log_all()
        return report

    # State coverage
    report.results.append(check_state_coverage(df))

    # Population values must be positive integers
    invalid_pop = df[df["population"] <= 0]
    report.results.append(
        ValidationResult(
            check_name="population_positive",
            passed=len(invalid_pop) == 0,
            severity="WARNING",
            failed_row_count=len(invalid_pop),
            total_row_count=len(df),
            message=(
                "All population values are positive."
                if len(invalid_pop) == 0
                else f"{len(invalid_pop)} states have zero or negative population."
            ),
            failed_examples=invalid_pop["state"].tolist(),
        )
    )

    report.log_all()
    return report


def run_rainfall_checks(df: pd.DataFrame) -> ValidationReport:
    """
    Run basic checks on the cleaned rainfall DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Output of transform.clean_rainfall_data().

    Returns
    -------
    ValidationReport
    """
    report = ValidationReport(table_name="rainfall")

    if df.empty:
        report.results.append(
            ValidationResult(
                check_name="not_empty",
                passed=False,
                severity="WARNING",
                message="Rainfall DataFrame is empty.",
            )
        )
        report.log_all()
        return report

    # State coverage
    report.results.append(check_state_coverage(df))

    # Rainfall mm should be >= 0 (NaN is acceptable for missing months)
    negative_rain = df[df["rainfall_mm"] < 0]
    report.results.append(
        ValidationResult(
            check_name="rainfall_non_negative",
            passed=len(negative_rain) == 0,
            severity="WARNING",
            failed_row_count=len(negative_rain),
            total_row_count=len(df),
            message=(
                "All rainfall values are non-negative."
                if len(negative_rain) == 0
                else f"{len(negative_rain)} negative rainfall values remain."
            ),
        )
    )

    report.log_all()
    return report
