"""
dashboard/_pages/admin.py
────────────────────────────────────────────────────────────────
Admin page — password-protected PDF upload portal.

Non-technical users can drop a new NCDC situation report PDF here.
The API extracts, cleans, and loads the data into the database
automatically. No command-line access required.

Streamlit Cloud secrets required:
  ADMIN_PASSWORD — password shown to the person uploading
  API_KEY        — forwarded to the FastAPI /admin/upload endpoint
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os

import requests
import streamlit as st


_DISEASES = [
    "Cholera",
    "Lassa Fever",
    "Meningitis",
    "Mpox",
    "Yellow Fever",
]


def render() -> None:
    st.title("⚙️ Admin — Upload PDF Report")
    st.caption(
        "Upload a new NCDC situation report PDF. "
        "The system will extract, clean, and load the data automatically."
    )

    st.divider()

    # ── Password gate ────────────────────────────────────────────
    password = st.text_input(
        "Admin password",
        type="password",
        placeholder="Enter password to continue",
    )

    if not password:
        st.info("Enter the admin password to access the upload form.")
        st.stop()

    expected = st.secrets.get("ADMIN_PASSWORD", "")
    if password != expected:
        st.error("Incorrect password. Please try again.")
        st.stop()

    # ── Authenticated ────────────────────────────────────────────
    st.success("✅ Authenticated")
    st.divider()

    st.markdown("### Upload a new NCDC PDF report")

    disease = st.selectbox(
        "Disease type",
        options=_DISEASES,
        help="Select the disease this PDF report covers.",
    )

    uploaded_file = st.file_uploader(
        "Select PDF file",
        type=["pdf"],
        help="NCDC weekly situation report in PDF format.",
    )

    if uploaded_file is None:
        st.stop()

    st.caption(f"File ready: **{uploaded_file.name}** ({len(uploaded_file.getvalue()):,} bytes)")

    if not st.button("Upload & Process", type="primary"):
        st.stop()

    # ── Call the API ─────────────────────────────────────────────
    api_base = os.environ.get("API_BASE_URL", "http://localhost:8000")
    api_key  = st.secrets.get("API_KEY", "")

    with st.spinner(f"Processing {uploaded_file.name} — this may take up to 60 seconds…"):
        try:
            response = requests.post(
                f"{api_base}/admin/upload",
                headers={"X-API-Key": api_key},
                data={"disease": disease},
                files={
                    "file": (
                        uploaded_file.name,
                        uploaded_file.getvalue(),
                        "application/pdf",
                    )
                },
                timeout=120,
            )
        except requests.exceptions.Timeout:
            st.error("Request timed out. The PDF may be large — try again.")
            return
        except requests.exceptions.ConnectionError:
            st.error("Could not reach the API. Check that the API is running.")
            return

    # ── Show result ──────────────────────────────────────────────
    try:
        result = response.json()
    except Exception:
        st.error(f"Unexpected response from API (HTTP {response.status_code}).")
        return

    if response.status_code == 200 and result.get("status") == "success":
        st.success(f"✅ {result['message']}")
        col1, col2, col3 = st.columns(3)
        col1.metric("Rows extracted",  result.get("rows_extracted", "—"))
        col2.metric("Rows loaded",     result.get("rows_loaded",    "—"))
        col3.metric("Duplicates skipped", result.get("rows_skipped", "—"))

    elif response.status_code == 200 and result.get("status") == "no_data":
        st.warning(f"⚠️ {result['message']}")

    else:
        st.error(
            result.get("detail")
            or result.get("message")
            or f"Upload failed (HTTP {response.status_code})."
        )
