"""
dashboard/_pages/admin.py
────────────────────────────────────────────────────────────────
Admin page — password-protected PDF upload portal.

Non-technical users can drop a new NCDC situation report PDF here.
The API extracts, cleans, and loads the data into the database
automatically. No command-line access required.

Security:
  - Password gate: upload form is hidden until correct password entered
  - Session expires after 30 minutes of inactivity
  - Password field is hidden once authenticated

Streamlit Cloud secrets required:
  ADMIN_PASSWORD — password for the upload portal
  API_KEY        — forwarded to the FastAPI /admin/upload endpoint
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import time

import requests
import streamlit as st

_DISEASES = [
    "Cholera",
    "Lassa Fever",
    "Meningitis",
    "Mpox",
    "Yellow Fever",
]

_SESSION_TIMEOUT = 30 * 60  # 30 minutes in seconds


def _is_session_valid() -> bool:
    """Return True if the admin session is authenticated and not yet expired."""
    if not st.session_state.get("admin_authenticated"):
        return False
    elapsed = time.time() - st.session_state.get("admin_login_time", 0)
    return elapsed < _SESSION_TIMEOUT


def _login(password: str) -> bool:
    """Validate password and start a session. Returns True on success."""
    expected = st.secrets.get("ADMIN_PASSWORD", "")
    if password == expected:
        st.session_state["admin_authenticated"] = True
        st.session_state["admin_login_time"] = time.time()
        return True
    return False


def _logout() -> None:
    st.session_state["admin_authenticated"] = False
    st.session_state["admin_login_time"] = 0


def render() -> None:
    st.title("⚙️ Admin — Upload PDF Report")
    st.caption(
        "Upload a new NCDC situation report PDF. "
        "The system will extract, clean, and load the data automatically."
    )
    st.divider()

    # ── Session expired notice ────────────────────────────────────
    if st.session_state.get("admin_authenticated") and not _is_session_valid():
        _logout()
        st.warning("Your session has expired after 30 minutes. Please log in again.")

    # ── Password gate ─────────────────────────────────────────────
    if not _is_session_valid():
        password = st.text_input(
            "Admin password",
            type="password",
            placeholder="Enter password to continue",
        )

        if not password:
            st.info("Enter the admin password to access the upload form.")
            st.stop()

        if not _login(password):
            st.error("Incorrect password. Please try again.")
            st.stop()

        st.rerun()

    # ── Authenticated ─────────────────────────────────────────────
    elapsed = time.time() - st.session_state.get("admin_login_time", 0)
    remaining = int((_SESSION_TIMEOUT - elapsed) / 60)

    col_status, col_logout = st.columns([4, 1])
    col_status.success(f"✅ Authenticated — session expires in {remaining} min")
    if col_logout.button("Log out"):
        _logout()
        st.rerun()

    st.divider()
    st.markdown("### Upload NCDC PDF reports")

    disease = st.selectbox(
        "Disease type",
        options=_DISEASES,
        help="Select the disease these PDFs cover. All files in one batch must be the same disease.",
    )

    # Keyed uploader — incrementing the key resets it after processing
    if "uploader_key" not in st.session_state:
        st.session_state["uploader_key"] = 0

    uploaded_files = st.file_uploader(
        "Select PDF file(s)",
        type=["pdf"],
        accept_multiple_files=True,
        help="You can select multiple PDFs at once. All must be for the same disease.",
        key=f"uploader_{st.session_state['uploader_key']}",
    )

    if not uploaded_files:
        st.stop()

    st.caption(f"{len(uploaded_files)} file(s) selected")

    if not st.button("Upload & Process", type="primary"):
        st.stop()

    # ── Process each file ─────────────────────────────────────────
    api_base = os.environ.get("API_BASE_URL", "http://localhost:8000")
    api_key  = st.secrets.get("API_KEY", "")

    for uploaded_file in uploaded_files:
        with st.spinner(f"Processing {uploaded_file.name}…"):
            try:
                response = requests.post(
                    f"{api_base}/api/v1/admin/upload",
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
                st.error(f"{uploaded_file.name} — timed out. Try again.")
                continue
            except requests.exceptions.ConnectionError:
                st.error("Could not reach the API. Check that the API is running.")
                break

        try:
            result = response.json()
        except Exception:
            st.error(f"{uploaded_file.name} — unexpected response (HTTP {response.status_code}).")
            continue

        if response.status_code == 200 and result.get("status") == "success":
            st.success(f"✅ **{uploaded_file.name}** — {result['message']}")
            col1, col2, col3 = st.columns(3)
            col1.metric("Rows extracted",     result.get("rows_extracted", "—"))
            col2.metric("Rows loaded",        result.get("rows_loaded",    "—"))
            col3.metric("Duplicates skipped", result.get("rows_skipped",   "—"))

        elif response.status_code == 200 and result.get("status") == "already_loaded":
            st.info(f"ℹ️ **{uploaded_file.name}** — {result['message']}")

        elif response.status_code == 200 and result.get("status") == "no_data":
            st.warning(f"⚠️ **{uploaded_file.name}** — {result['message']}")

        else:
            st.error(
                f"**{uploaded_file.name}** — "
                + (result.get("detail") or result.get("message") or f"Upload failed (HTTP {response.status_code}).")
            )

    # Clear the uploader by incrementing its key
    st.session_state["uploader_key"] += 1
    st.rerun()
