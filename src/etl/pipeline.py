"""
src/etl/pipeline.py
────────────────────────────────────────────────────────────────
ETL pipeline orchestrator.

This is the single entry point for running the full pipeline.
It calls extract → transform → validate → load in sequence,
handles errors gracefully, and logs a structured run summary
to both the application logger and the pipeline_runs DB table.

Running the pipeline:
    python -m src.etl.pipeline              # full run
    python -m src.etl.pipeline --dry-run    # validate only, no DB writes

The pipeline is designed to be:
  • Idempotent  — safe to re-run; upserts prevent duplicates.
  • Resilient   — one failing disease does not stop others.
  • Observable  — every step logs timing and record counts.
  • Auditable   — run metadata is persisted to the DB.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from src.etl import extract, transform, validate
from src.etl.load import load_all
from src.etl.notify import send_pipeline_notification
from src.db.connection import get_db_session, verify_connection
from src.db.repository import log_pipeline_run, log_quality_check_results
from src.utils.config import Diseases, Paths, settings
from src.utils.logger import get_logger, set_log_level

logger = get_logger(__name__)


# ── Pipeline run summary ─────────────────────────────────────────

@dataclass
class PipelineResult:
    """
    Holds the complete outcome of one pipeline execution.

    Passed between pipeline stages and ultimately persisted
    to the pipeline_runs table.
    """
    pipeline_name:     str     = "nigeria_disease_surveillance_etl"
    status:            str     = "SUCCESS"     # SUCCESS | FAILED | PARTIAL
    records_extracted: int     = 0
    records_loaded:    int     = 0
    records_failed:    int     = 0
    duration_seconds:  float   = 0.0
    error_message:     Optional[str] = None
    stage_timings:     dict    = field(default_factory=dict)
    warnings:          list    = field(default_factory=list)

    def mark_failed(self, error: str) -> None:
        """Mark the pipeline as failed with an error message."""
        self.status        = "FAILED"
        self.error_message = error

    def mark_partial(self, reason: str) -> None:
        """Mark as partial when some but not all data loaded."""
        self.status = "PARTIAL"
        self.warnings.append(reason)

    def log_summary(self) -> None:
        """Write a structured summary to the application logger."""
        logger.info("─" * 55)
        logger.info("Pipeline run complete")
        logger.info("  Status    : %s", self.status)
        logger.info("  Extracted : %d records", self.records_extracted)
        logger.info("  Loaded    : %d records", self.records_loaded)
        logger.info("  Failed    : %d records", self.records_failed)
        logger.info("  Duration  : %.1fs", self.duration_seconds)
        if self.warnings:
            for w in self.warnings:
                logger.warning("  Warning   : %s", w)
        if self.error_message:
            logger.error("  Error     : %s", self.error_message)
        logger.info("  Stage timings:")
        for stage, secs in self.stage_timings.items():
            logger.info("    %-20s %.1fs", stage, secs)
        logger.info("─" * 55)


# ── Stage runners ────────────────────────────────────────────────
# Each stage is a separate function so errors in one stage are
# caught independently. The orchestrator decides whether to
# continue or abort based on the failure type.

def _run_extract(result: PipelineResult) -> dict[str, pd.DataFrame]:
    """
    Extract all raw data from all sources.

    Returns a dict of named DataFrames. Missing sources return
    empty DataFrames — the transform stage handles those gracefully.
    """
    t0 = time.perf_counter()
    logger.info("── Stage 1: EXTRACT ──────────────────────────────")

    raw: dict[str, pd.DataFrame] = {}

    # NCDC PDFs — one per disease
    disease_folder_map = Diseases.pdf_folder_map
    for disease_name, folder_key in disease_folder_map.items():
        folder = Paths.raw / "ncdc_pdfs" / folder_key
        df = extract.extract_ncdc_pdfs(folder, disease_name)
        raw[f"ncdc_{folder_key}"] = df
        logger.info(
            "  NCDC %-15s %d rows", disease_name, len(df)
        )

    # WHO AFRO
    raw["who"]         = extract.extract_who_data()
    raw["rainfall"]    = extract.extract_nasa_rainfall()
    raw["facilities"]  = extract.extract_health_facilities()
    raw["population"]  = extract.extract_population()
    raw["shapefiles"]  = pd.DataFrame()  # handled separately below

    # Shapefiles return a dict, not a DataFrame
    raw["_shapefiles_dict"] = extract.extract_shapefiles()

    total_extracted = sum(
        len(df) for key, df in raw.items()
        if isinstance(df, pd.DataFrame) and key != "_shapefiles_dict"
    )
    result.records_extracted      = total_extracted
    result.stage_timings["extract"] = round(time.perf_counter() - t0, 2)

    logger.info("  Total extracted: %d rows in %.1fs",
                total_extracted, result.stage_timings["extract"])
    return raw


def _run_transform(
    raw: dict[str, pd.DataFrame],
    result: PipelineResult,
) -> dict[str, pd.DataFrame]:
    """
    Clean and enrich all extracted DataFrames.

    Returns a dict of cleaned DataFrames keyed by logical name.
    """
    t0 = time.perf_counter()
    logger.info("── Stage 2: TRANSFORM ────────────────────────────")

    clean: dict[str, pd.DataFrame] = {}

    # Clean each disease independently
    disease_folder_map = Diseases.pdf_folder_map
    cleaned_diseases: dict[str, pd.DataFrame] = {}

    for disease_name, folder_key in disease_folder_map.items():
        raw_key = f"ncdc_{folder_key}"
        raw_df  = raw.get(raw_key, pd.DataFrame())

        if raw_df.empty:
            result.mark_partial(
                f"No raw data for {disease_name} — skipped in transform"
            )
            continue

        cleaned = transform.clean_disease_dataframe(raw_df, disease_name)
        if not cleaned.empty:
            cleaned_diseases[disease_name] = cleaned
            logger.info(
                "  Cleaned %-15s %d rows", disease_name, len(cleaned)
            )
        else:
            result.mark_partial(
                f"{disease_name} produced no clean rows after transform"
            )

    # Population — needed for incidence rates
    raw_pop  = raw.get("population", pd.DataFrame())
    clean_pop = (
        transform.clean_population_data(raw_pop)
        if not raw_pop.empty
        else pd.DataFrame(columns=["state", "population"])
    )
    clean["population"] = clean_pop

    # Merge all diseases into one master DataFrame
    if cleaned_diseases:
        master = transform.merge_all_diseases(cleaned_diseases)
        master = transform.add_incidence_rate(master, clean_pop)
        clean["master"] = master
        logger.info("  Master DataFrame: %d rows", len(master))
    else:
        clean["master"] = pd.DataFrame()
        result.mark_failed("All disease transforms produced empty results")

    # Rainfall
    raw_rain = raw.get("rainfall", pd.DataFrame())
    clean["rainfall"] = (
        transform.clean_rainfall_data(raw_rain)
        if not raw_rain.empty
        else pd.DataFrame()
    )

    # Facilities — passed through as-is (transform is minimal)
    clean["facilities"] = raw.get("facilities", pd.DataFrame())

    # Shapefiles dict
    clean["shapefiles"] = raw.get("_shapefiles_dict", {})

    result.stage_timings["transform"] = round(time.perf_counter() - t0, 2)
    logger.info("  Transform complete in %.1fs",
                result.stage_timings["transform"])
    return clean


def _run_validate(
    clean: dict[str, pd.DataFrame],
    result: PipelineResult,
    abort_on_error: bool = True,
) -> bool:
    """
    Run data quality checks on the cleaned master DataFrame.

    Parameters
    ----------
    clean : dict[str, pd.DataFrame]
        Output of _run_transform().
    result : PipelineResult
        Updated in place with any validation warnings.
    abort_on_error : bool
        If True and any ERROR-severity check fails, mark the
        pipeline as FAILED and return False.

    Returns
    -------
    bool
        True if the pipeline should continue, False if it should stop.
    """
    t0 = time.perf_counter()
    logger.info("── Stage 3: VALIDATE ─────────────────────────────")

    master_df = clean.get("master", pd.DataFrame())

    if master_df.empty:
        logger.warning("  Skipping validation — master DataFrame is empty")
        result.stage_timings["validate"] = 0.0
        return True

    # Run main surveillance validation
    surveillance_report = validate.run_all_checks(
        master_df, "fact_disease_surveillance"
    )
    clean["_validation_report"] = surveillance_report

    # Run supplementary checks
    if not clean.get("population", pd.DataFrame()).empty:
        validate.run_population_checks(clean["population"])

    if not clean.get("rainfall", pd.DataFrame()).empty:
        validate.run_rainfall_checks(clean["rainfall"])

    result.stage_timings["validate"] = round(time.perf_counter() - t0, 2)

    # Collect warnings from the report
    for vr in surveillance_report.results:
        if not vr.passed:
            result.warnings.append(
                f"Validation [{vr.severity}] {vr.check_name}: {vr.message}"
            )

    if surveillance_report.has_errors and abort_on_error:
        result.mark_failed(
            f"Validation failed with {surveillance_report.error_count} error(s). "
            "Check data_quality_log for details."
        )
        logger.error(
            "  Pipeline aborted — %d validation error(s)",
            surveillance_report.error_count,
        )
        return False

    if surveillance_report.has_warnings:
        logger.warning(
            "  %d validation warning(s) — pipeline continuing",
            surveillance_report.warning_count,
        )

    logger.info(
        "  Validation complete in %.1fs — %s",
        result.stage_timings["validate"],
        surveillance_report.summary(),
    )
    return True


def _run_load(
    clean: dict[str, pd.DataFrame],
    result: PipelineResult,
    dry_run: bool = False,
) -> None:
    """
    Load all cleaned data into the database.

    Parameters
    ----------
    clean : dict[str, pd.DataFrame]
        Output of _run_transform().
    result : PipelineResult
        Updated in place with load counts.
    dry_run : bool
        If True, skip the actual DB writes. Useful for testing
        the pipeline end-to-end without touching the database.
    """
    t0 = time.perf_counter()
    logger.info("── Stage 4: LOAD ─────────────────────────────────")

    if dry_run:
        logger.info(
            "  DRY RUN — skipping DB writes. "
            "Would have loaded %d rows.", len(clean.get("master", []))
        )
        result.stage_timings["load"] = 0.0
        return

    master_df    = clean.get("master",     pd.DataFrame())
    population_df = clean.get("population", pd.DataFrame())
    facilities_df = clean.get("facilities", pd.DataFrame())
    rainfall_df   = clean.get("rainfall",   pd.DataFrame())
    states_gdf    = clean.get("shapefiles", {}).get("states")

    load_summary = load_all(
        master_df     = master_df,
        population_df = population_df,
        facilities_df = facilities_df,
        rainfall_df   = rainfall_df,
        states_gdf    = states_gdf,
    )

    result.records_loaded  = load_summary.get("fact_loaded",  0)
    result.records_failed  = load_summary.get("fact_skipped", 0)
    result.stage_timings["load"] = round(time.perf_counter() - t0, 2)

    if load_summary["status"] == "FAILED":
        result.mark_failed(
            load_summary.get("error", "Unknown load error")
        )

    # Persist validation report to DB if available
    validation_report = clean.get("_validation_report")
    if validation_report is not None:
        try:
            with get_db_session() as session:
                log_quality_check_results(
                    session, validation_report.to_dataframe()
                )
        except Exception as exc:
            logger.warning("Could not persist validation report: %s", exc)

    logger.info(
        "  Load complete in %.1fs — %d loaded, %d skipped",
        result.stage_timings["load"],
        result.records_loaded,
        result.records_failed,
    )


def _persist_run_metadata(result: PipelineResult) -> None:
    """
    Write the pipeline run outcome to the pipeline_runs DB table.

    This is best-effort — if it fails we log the error but do not
    change the overall pipeline status.
    """
    try:
        with get_db_session() as session:
            log_pipeline_run(
                session           = session,
                pipeline_name     = result.pipeline_name,
                status            = result.status,
                records_extracted = result.records_extracted,
                records_loaded    = result.records_loaded,
                records_failed    = result.records_failed,
                duration_seconds  = result.duration_seconds,
                error_message     = result.error_message,
            )
    except Exception as exc:
        logger.warning("Could not persist run metadata: %s", exc)


# ── Main orchestrator ────────────────────────────────────────────

def run_pipeline(
    dry_run: bool = False,
    abort_on_validation_error: bool = True,
    skip_notification: bool = False,
) -> PipelineResult:
    """
    Execute the full ETL pipeline: Extract → Transform → Validate → Load.

    Parameters
    ----------
    dry_run : bool
        When True, runs all stages but skips DB writes.
        Useful for testing the pipeline logic end-to-end.
    abort_on_validation_error : bool
        When True (default), stop the pipeline if any validation
        check raises an ERROR. Set to False to force a load even
        with known quality issues (use with caution).
    skip_notification : bool
        When True, do not send email notification on completion.
        Useful for local development runs.

    Returns
    -------
    PipelineResult
        Full outcome summary of the run.
    """
    set_log_level(settings.log_level)
    wall_start = time.perf_counter()

    result = PipelineResult()

    logger.info("═" * 55)
    logger.info("Nigeria Disease Surveillance ETL pipeline")
    logger.info("Started at : %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Dry run    : %s", dry_run)
    logger.info("Environment: %s", settings.app_env)
    logger.info("═" * 55)

    # ── Pre-flight: verify DB is reachable ───────────────────────
    if not dry_run:
        if not verify_connection():
            result.mark_failed(
                "Database is unreachable. Check DATABASE_URL in .env"
            )
            result.duration_seconds = time.perf_counter() - wall_start
            result.log_summary()
            return result
    else:
        logger.info("Skipping DB connectivity check (dry run)")

    # ── Ensure data directories exist ────────────────────────────
    Paths.ensure_all()

    try:
        # ── Stage 1: Extract ─────────────────────────────────────
        raw = _run_extract(result)

        # ── Stage 2: Transform ───────────────────────────────────
        clean = _run_transform(raw, result)

        # Abort early if transform produced nothing usable
        if result.status == "FAILED":
            raise RuntimeError(result.error_message)

        # ── Stage 3: Validate ────────────────────────────────────
        should_continue = _run_validate(
            clean, result, abort_on_error=abort_on_validation_error
        )
        if not should_continue:
            raise RuntimeError(result.error_message)

        # ── Stage 4: Load ────────────────────────────────────────
        _run_load(clean, result, dry_run=dry_run)

    except RuntimeError:
        # Already marked as FAILED above — just fall through
        pass
    except Exception as exc:
        result.mark_failed(f"Unexpected error: {exc}")
        logger.error("Unexpected pipeline error: %s", exc, exc_info=True)

    # ── Wrap up ──────────────────────────────────────────────────
    result.duration_seconds = round(time.perf_counter() - wall_start, 2)
    result.log_summary()

    if not dry_run:
        _persist_run_metadata(result)

    if not skip_notification and settings.notifications_enabled:
        try:
            send_pipeline_notification(result)
        except Exception as exc:
            logger.warning("Notification failed (non-fatal): %s", exc)

    return result


# ── CLI entry point ──────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the pipeline runner."""
    parser = argparse.ArgumentParser(
        description="Nigeria Disease Surveillance ETL pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.etl.pipeline                   # Full run
  python -m src.etl.pipeline --dry-run         # Validate only
  python -m src.etl.pipeline --force           # Ignore validation errors
  python -m src.etl.pipeline --log-level DEBUG # Verbose output
        """,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run all stages but skip database writes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Continue loading even if validation errors are found.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override the log level for this run.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        default=False,
        help="Skip email notification on completion.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.log_level:
        set_log_level(args.log_level)

    pipeline_result = run_pipeline(
        dry_run                    = args.dry_run,
        abort_on_validation_error  = not args.force,
        skip_notification          = args.no_notify,
    )

    # Exit with a non-zero code on failure so GitHub Actions
    # and other CI systems correctly mark the run as failed.
    sys.exit(0 if pipeline_result.status in ("SUCCESS", "PARTIAL") else 1)
