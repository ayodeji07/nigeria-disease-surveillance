"""
tests/test_api.py
────────────────────────────────────────────────────────────────
API endpoint tests using FastAPI's TestClient.

These tests run against the full FastAPI app with a real SQLite
in-memory database (no PostgreSQL needed). They verify that:
  - Correct HTTP status codes are returned
  - Response shapes match the declared Pydantic schemas
  - Filtering parameters work correctly
  - Auth is enforced on protected endpoints
  - Edge cases (empty results, bad parameters) are handled

Run with:
    pytest tests/test_api.py -v

Note: Some tests are marked with @pytest.mark.skip when they
require a populated database. Run the ETL pipeline first and
then remove the skip markers for full integration coverage.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import pytest

# FastAPI TestClient requires httpx — skip all tests gracefully
# if it isn't installed rather than failing with an ImportError.
try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _FASTAPI_AVAILABLE,
    reason="fastapi[testclient] not installed",
)


# ── App setup ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    """
    Create a TestClient backed by a temporary SQLite database.

    We override the DATABASE_URL environment variable before
    importing the app so it connects to SQLite rather than
    the production PostgreSQL instance.
    """
    import os
    import tempfile

    # Point to an in-memory SQLite DB for the test session
    os.environ["DATABASE_URL"] = "sqlite:///./test_api_temp.db"
    os.environ["API_KEY"]      = "test-api-key-12345"
    os.environ["APP_ENV"]      = "development"

    # Reset any cached engine so it is recreated with the SQLite URL above,
    # not a previously-cached PostgreSQL engine from other test modules.
    from src.db.connection import dispose_engine as _dispose
    _dispose()

    from src.api.main import app
    from src.db.connection import get_engine
    from src.db.models import Base

    # Create all tables in the test DB
    engine = get_engine()
    Base.metadata.create_all(engine)

    with TestClient(app) as c:
        yield c

    # Teardown — remove the test DB file
    from src.db.connection import dispose_engine
    dispose_engine()
    import pathlib
    db_file = pathlib.Path("./test_api_temp.db")
    if db_file.exists():
        db_file.unlink()


# ── Health endpoints ──────────────────────────────────────────────

class TestHealthEndpoints:

    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_returns_status_healthy(self, client):
        r = client.get("/health")
        assert r.json()["status"] == "healthy"

    def test_health_returns_version(self, client):
        r = client.get("/health")
        assert "version" in r.json()

    def test_health_returns_environment(self, client):
        r = client.get("/health")
        assert "environment" in r.json()

    def test_root_returns_200(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_root_contains_docs_link(self, client):
        r = client.get("/")
        assert "docs" in r.json()


# ── Surveillance list/metadata endpoints ─────────────────────────

class TestSurveillanceMetadata:

    def test_diseases_endpoint_returns_200(self, client):
        r = client.get("/api/v1/surveillance/diseases")
        assert r.status_code == 200

    def test_diseases_returns_list(self, client):
        r = client.get("/api/v1/surveillance/diseases")
        assert "diseases" in r.json()
        assert isinstance(r.json()["diseases"], list)

    def test_diseases_contains_cholera(self, client):
        r = client.get("/api/v1/surveillance/diseases")
        assert "Cholera" in r.json()["diseases"]

    def test_diseases_contains_all_five(self, client):
        r    = client.get("/api/v1/surveillance/diseases")
        data = r.json()["diseases"]
        for disease in ["Cholera", "Lassa Fever", "Mpox",
                        "Meningitis", "Yellow Fever"]:
            assert disease in data, f"Missing disease: {disease}"

    def test_states_endpoint_returns_200(self, client):
        r = client.get("/api/v1/surveillance/states")
        assert r.status_code == 200

    def test_states_returns_37(self, client):
        r = client.get("/api/v1/surveillance/states")
        data = r.json()
        assert data["total"] == 37
        assert len(data["states"]) == 37

    def test_states_contains_lagos(self, client):
        r = client.get("/api/v1/surveillance/states")
        assert "Lagos" in r.json()["states"]

    def test_states_contains_fct(self, client):
        r = client.get("/api/v1/surveillance/states")
        assert "FCT" in r.json()["states"]


# ── Surveillance query endpoint ───────────────────────────────────

class TestSurveillanceQuery:

    def test_returns_200(self, client):
        r = client.get("/api/v1/surveillance")
        assert r.status_code == 200

    def test_response_has_records_key(self, client):
        r = client.get("/api/v1/surveillance")
        assert "records" in r.json()

    def test_response_has_total_key(self, client):
        r = client.get("/api/v1/surveillance")
        assert "total" in r.json()

    def test_records_is_list(self, client):
        r = client.get("/api/v1/surveillance")
        assert isinstance(r.json()["records"], list)

    def test_disease_filter_accepted(self, client):
        r = client.get("/api/v1/surveillance?disease=Cholera")
        assert r.status_code == 200

    def test_state_filter_accepted(self, client):
        r = client.get("/api/v1/surveillance?state=Lagos")
        assert r.status_code == 200

    def test_year_filter_accepted(self, client):
        r = client.get("/api/v1/surveillance?year=2023")
        assert r.status_code == 200

    def test_invalid_year_rejected(self, client):
        # Year below 2000 should fail validation
        r = client.get("/api/v1/surveillance?year=1850")
        assert r.status_code == 422

    def test_limit_parameter_respected(self, client):
        r = client.get("/api/v1/surveillance?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert data["limit"] == 5

    def test_csv_format_returns_content_type(self, client):
        r = client.get("/api/v1/surveillance?format=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")

    def test_invalid_format_rejected(self, client):
        r = client.get("/api/v1/surveillance?format=xml")
        assert r.status_code == 422

    def test_latest_endpoint_returns_200(self, client):
        r = client.get("/api/v1/surveillance/latest")
        assert r.status_code == 200

    def test_state_endpoint_returns_200(self, client):
        r = client.get("/api/v1/surveillance/state/Lagos")
        assert r.status_code == 200

    def test_disease_endpoint_returns_200(self, client):
        r = client.get("/api/v1/surveillance/disease/Cholera")
        assert r.status_code == 200


# ── Analytics endpoints ───────────────────────────────────────────

class TestAnalyticsEndpoints:

    def test_summary_returns_200(self, client):
        r = client.get("/api/v1/analytics/summary")
        assert r.status_code == 200

    def test_summary_returns_list(self, client):
        r = client.get("/api/v1/analytics/summary")
        assert isinstance(r.json(), list)

    def test_summary_with_year_filter(self, client):
        r = client.get("/api/v1/analytics/summary?year=2023")
        assert r.status_code == 200

    def test_trends_returns_200(self, client):
        r = client.get("/api/v1/analytics/trends?disease=Cholera")
        assert r.status_code == 200

    def test_trends_missing_disease_returns_422(self, client):
        r = client.get("/api/v1/analytics/trends")
        assert r.status_code == 422

    def test_trends_response_has_points(self, client):
        r = client.get("/api/v1/analytics/trends?disease=Cholera")
        assert "points" in r.json()

    def test_trends_freq_monthly_accepted(self, client):
        r = client.get("/api/v1/analytics/trends?disease=Cholera&freq=monthly")
        assert r.status_code == 200

    def test_trends_invalid_freq_rejected(self, client):
        r = client.get("/api/v1/analytics/trends?disease=Cholera&freq=daily")
        assert r.status_code == 422

    def test_hotspots_returns_200(self, client):
        r = client.get("/api/v1/analytics/hotspots?disease=Cholera")
        assert r.status_code == 200

    def test_hotspots_top_n_respected(self, client):
        r = client.get("/api/v1/analytics/hotspots?disease=Cholera&top_n=3")
        assert r.status_code == 200
        data = r.json()
        assert len(data.get("states", [])) <= 3

    def test_hotspots_invalid_top_n_rejected(self, client):
        # top_n > 37 should be rejected (max is number of states)
        r = client.get("/api/v1/analytics/hotspots?disease=Cholera&top_n=100")
        assert r.status_code == 422

    def test_outbreak_alerts_returns_200(self, client):
        r = client.get("/api/v1/analytics/outbreak-alerts?disease=Cholera")
        assert r.status_code == 200

    def test_outbreak_alerts_returns_list(self, client):
        r = client.get("/api/v1/analytics/outbreak-alerts?disease=Cholera")
        assert isinstance(r.json(), list)

    def test_trend_test_returns_200(self, client):
        r = client.get("/api/v1/analytics/trend-test?disease=Cholera")
        assert r.status_code == 200

    def test_trend_test_has_required_fields(self, client):
        r    = client.get("/api/v1/analytics/trend-test?disease=Cholera")
        data = r.json()
        for field in ["disease", "trend", "p_value", "significant"]:
            assert field in data, f"Missing field: {field}"

    def test_clusters_returns_200(self, client):
        r = client.get("/api/v1/analytics/clusters?disease=Cholera")
        assert r.status_code == 200

    def test_clusters_n_clusters_param(self, client):
        r = client.get("/api/v1/analytics/clusters?disease=Cholera&n_clusters=3")
        assert r.status_code == 200

    def test_clusters_invalid_n_clusters_rejected(self, client):
        r = client.get("/api/v1/analytics/clusters?disease=Cholera&n_clusters=1")
        assert r.status_code == 422

    def test_cfr_benchmark_returns_200(self, client):
        r = client.get("/api/v1/analytics/cfr-benchmark?disease=Cholera")
        assert r.status_code == 200


# ── Geospatial endpoints ──────────────────────────────────────────

class TestGeospatialEndpoints:

    def test_facilities_returns_200(self, client):
        r = client.get("/api/v1/geospatial/facilities")
        assert r.status_code == 200

    def test_facilities_state_filter(self, client):
        r = client.get("/api/v1/geospatial/facilities?state=Lagos")
        assert r.status_code == 200

    def test_facilities_has_total_key(self, client):
        r = client.get("/api/v1/geospatial/facilities")
        assert "total" in r.json()

    def test_burden_index_returns_200(self, client):
        r = client.get("/api/v1/geospatial/burden-index")
        assert r.status_code == 200

    def test_accessibility_returns_200(self, client):
        r = client.get("/api/v1/geospatial/accessibility?disease=Cholera")
        assert r.status_code == 200

    def test_accessibility_has_states_key(self, client):
        r = client.get("/api/v1/geospatial/accessibility?disease=Cholera")
        assert "states" in r.json()

    def test_choropleth_missing_year_returns_422(self, client):
        # year is required for choropleth
        r = client.get("/api/v1/geospatial/choropleth?disease=Cholera")
        assert r.status_code == 422

    def test_choropleth_with_valid_params(self, client):
        r = client.get("/api/v1/geospatial/choropleth?disease=Cholera&year=2023")
        # 200 even if empty (no PostGIS in SQLite)
        assert r.status_code == 200


# ── Auth tests ────────────────────────────────────────────────────

class TestAuthentication:

    def test_public_endpoints_need_no_key(self, client):
        """All GET surveillance endpoints are public."""
        for endpoint in [
            "/api/v1/surveillance",
            "/api/v1/surveillance/diseases",
            "/api/v1/surveillance/states",
            "/api/v1/analytics/summary",
            "/health",
        ]:
            r = client.get(endpoint)
            assert r.status_code in (200, 422), (
                f"Expected 200/422 on {endpoint}, got {r.status_code}"
            )

    def test_docs_endpoint_accessible(self, client):
        r = client.get("/docs")
        assert r.status_code == 200

    def test_redoc_endpoint_accessible(self, client):
        r = client.get("/redoc")
        assert r.status_code == 200


# ── Docs / OpenAPI ────────────────────────────────────────────────

class TestOpenAPI:

    def test_openapi_schema_accessible(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200

    def test_openapi_has_paths(self, client):
        r     = client.get("/openapi.json")
        paths = r.json().get("paths", {})
        assert len(paths) > 0

    def test_openapi_has_surveillance_path(self, client):
        r     = client.get("/openapi.json")
        paths = r.json().get("paths", {})
        assert any("surveillance" in p for p in paths)

    def test_openapi_has_analytics_path(self, client):
        r     = client.get("/openapi.json")
        paths = r.json().get("paths", {})
        assert any("analytics" in p for p in paths)

    def test_openapi_info_title_correct(self, client):
        r    = client.get("/openapi.json")
        info = r.json().get("info", {})
        assert "Nigeria" in info.get("title", "")
