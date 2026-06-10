"""
src/api/routes/admin.py
────────────────────────────────────────────────────────────────
Admin endpoints — protected by X-API-Key header.

Currently provides:
  POST /admin/upload — accept a single NCDC PDF, run the ETL
                       pipeline for that file, load into Supabase.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from sqlalchemy import text

from src.api.auth import require_api_key
from src.db.connection import get_db_session
from src.etl.extract import _extract_single_pdf
from src.etl.transform import clean_disease_dataframe
from src.etl.load import (
    load_dim_states,
    load_dim_diseases,
    load_dim_dates,
    load_surveillance_fact,
)
from src.utils.config import Diseases
from src.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

_VALID_DISEASES = list(Diseases.pdf_folder_map.keys())


@router.post(
    "/upload",
    summary="Upload a single NCDC PDF and run ETL",
    description=(
        "Upload one NCDC situation report PDF. The API extracts, cleans, "
        "and loads the data into the database. Duplicate rows are skipped "
        "automatically. Requires a valid X-API-Key header."
    ),
    dependencies=[Depends(require_api_key)],
)
async def upload_pdf(
    disease: str = Form(
        ...,
        description=f"Disease name. One of: {', '.join(_VALID_DISEASES)}",
    ),
    file: UploadFile = File(
        ...,
        description="NCDC situation report PDF file.",
    ),
) -> JSONResponse:
    # ── Validate inputs ──────────────────────────────────────────
    if disease not in _VALID_DISEASES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown disease '{disease}'. "
                f"Must be one of: {_VALID_DISEASES}"
            ),
        )

    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file must be a PDF.",
        )

    # ── Save to a temp file so pdfplumber can open it ────────────
    contents = await file.read()
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)

        logger.info(
            "Admin upload: %s / %s (%d bytes)",
            disease, file.filename, len(contents),
        )

        # ── Extract ──────────────────────────────────────────────
        raw_df = _extract_single_pdf(tmp_path, disease)

        if raw_df.empty:
            # Check whether this disease already has data in the database
            # to distinguish "already processed" from "unsupported format"
            with get_db_session() as session:
                row = session.execute(text("""
                    SELECT COUNT(*) FROM fact_disease_surveillance f
                    JOIN dim_diseases d ON f.disease_id = d.disease_id
                    WHERE d.disease_name = :disease
                """), {"disease": disease}).scalar()
            already_loaded = (row or 0) > 0

            if already_loaded:
                message = (
                    "This PDF appears to have been processed already — "
                    "the data is already in the database."
                )
                status_val = "already_loaded"
            else:
                message = (
                    "PDF was parsed but no rows were extracted. "
                    "The report may use an unsupported table format."
                )
                status_val = "no_data"

            return JSONResponse(
                status_code=200,
                content={
                    "status": status_val,
                    "disease": disease,
                    "filename": file.filename,
                    "message": message,
                    "rows_loaded": 0,
                    "rows_skipped": 0,
                },
            )

        # ── Clean ────────────────────────────────────────────────
        clean_df = clean_disease_dataframe(raw_df, disease)

        if clean_df.empty:
            return JSONResponse(
                status_code=200,
                content={
                    "status": "no_data",
                    "disease": disease,
                    "filename": file.filename,
                    "message": "Rows were extracted but none survived cleaning "
                               "(likely all national-level or malformed rows).",
                    "rows_loaded": 0,
                    "rows_skipped": 0,
                },
            )

        # ── Load ─────────────────────────────────────────────────
        with get_db_session() as session:
            state_id_map   = load_dim_states(session)
            disease_id_map = load_dim_diseases(session)
            date_id_map    = load_dim_dates(session, clean_df["report_date"])
            rows_loaded, rows_skipped = load_surveillance_fact(
                session, clean_df, state_id_map, disease_id_map, date_id_map,
            )

        logger.info(
            "Admin upload complete: %s — %d loaded, %d skipped",
            file.filename, rows_loaded, rows_skipped,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "disease": disease,
                "filename": file.filename,
                "rows_extracted": len(raw_df),
                "rows_cleaned": len(clean_df),
                "rows_loaded": rows_loaded,
                "rows_skipped": rows_skipped,
                "message": (
                    f"{rows_loaded} new rows loaded into the database. "
                    f"{rows_skipped} duplicate rows skipped."
                ),
            },
        )

    except Exception as exc:
        logger.error(
            "Admin upload failed for %s: %s",
            file.filename, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ETL pipeline failed: {exc}",
        )

    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
