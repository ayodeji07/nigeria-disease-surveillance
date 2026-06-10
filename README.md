# 🏥 Nigeria Disease Surveillance Dashboard

> End-to-end health data engineering platform tracking **5 diseases** across **37 states** (2015–present)

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-green)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+PostGIS-blue)](https://postgis.net)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.33-red)](https://streamlit.io)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## 🔴 Live Demo

| Service | URL |
|---------|-----|
| 📊 Dashboard | [Open Streamlit App](https://nigeria-disease-surveillance.streamlit.app) |
| ⚡ API (Swagger) | [Open API Docs](https://nigeria-disease-api.onrender.com/docs) |
| 🔗 GitHub | [View Source](https://github.com/ayodeji07/nigeria-disease-surveillance) |

---

## 📌 Key Findings

- Cholera burden peaks in **June–September** (rainy season) — significant seasonal pattern confirmed by Kruskal-Wallis test (p < 0.01)
- **Borno, Bauchi, and Kebbi** consistently account for 60%+ of annual Meningitis burden
- Spatial autocorrelation (Moran's I) confirms Northern states cluster for Meningitis while Southern states cluster for Cholera
- 8 states flagged CRITICAL for facility accessibility — high disease burden with fewer than 0.5 facilities per 100,000 population
- Lassa Fever shows a statistically significant upward trend (Mann-Kendall, p < 0.05) across the study period

---

## 🗂️ Project Structure

```
nigeria-disease-surveillance/
├── data/
│   ├── raw/                    # Downloaded source files (gitignored)
│   ├── processed/              # Cleaned outputs (gitignored)
│   └── shapefiles/             # GRID3 boundary files (gitignored)
├── src/
│   ├── utils/                  # config, logger, state_maps
│   ├── etl/                    # extract, transform, validate, load, pipeline
│   ├── db/                     # connection, models, repository
│   ├── analysis/               # statistics, geospatial, forecasting
│   └── api/                    # FastAPI app, routes, schemas, auth
├── dashboard/
│   ├── app.py                  # Streamlit entry point
│   ├── api_client.py           # HTTP client for dashboard → API
│   └── _pages/                 # overview, state_view, geo_atlas, forecasting
├── sql/
│   ├── schema.sql              # PostgreSQL + PostGIS schema
│   └── seed_lookups.sql        # Dimension table seed data
├── tests/                      # pytest test suite (224 tests)
├── .github/workflows/          # CI/CD: weekly ETL + deploy
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| **Data extraction** | pdfplumber, requests, pandas, openpyxl |
| **Database** | PostgreSQL 15 + PostGIS 3.3 |
| **ORM / queries** | SQLAlchemy 2.0 |
| **Statistical analysis** | scipy, pymannkendall, scikit-learn |
| **Geospatial** | GeoPandas, Folium, libpysal, esda |
| **Forecasting** | Prophet |
| **API** | FastAPI, Pydantic v2, uvicorn |
| **Dashboard** | Streamlit, Plotly, streamlit-folium |
| **CI/CD** | GitHub Actions |
| **Containerisation** | Docker + docker-compose |
| **Cloud hosting** | Supabase (DB) · Render (API) · Streamlit Cloud (dashboard) |

---

## 📊 Screenshots

*(Add screenshots of your dashboard here after first deployment)*

| National Overview | Geospatial Atlas |
|------------------|-----------------|
| ![Overview](docs/screenshots/overview.png) | ![Map](docs/screenshots/geo_atlas.png) |

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 15 with PostGIS extension (or Docker)
- Git

### 1. Clone and install

```bash
git clone https://github.com/ayodeji07/nigeria-disease-surveillance.git
cd nigeria-disease-surveillance

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and set DATABASE_URL and API_KEY
```

### 3. Start the database (Docker)

```bash
docker-compose up db -d
```

### 4. Initialise the database schema

```bash
psql -U postgres -d nigeria_health -f sql/schema.sql
psql -U postgres -d nigeria_health -f sql/seed_lookups.sql
```

### 5. Download source data

Place the following files in `data/raw/`:

| File | Source | Path |
|------|--------|------|
| NCDC Cholera PDFs | ncdc.gov.ng/diseases/sitreps | `data/raw/ncdc_pdfs/cholera/` |
| NCDC Lassa Fever PDFs | ncdc.gov.ng/diseases/sitreps | `data/raw/ncdc_pdfs/lassa_fever/` |
| NCDC Mpox PDFs | ncdc.gov.ng/diseases/sitreps | `data/raw/ncdc_pdfs/mpox/` |
| NCDC Meningitis PDFs | ncdc.gov.ng/diseases/sitreps | `data/raw/ncdc_pdfs/meningitis/` |
| NCDC Yellow Fever PDFs | ncdc.gov.ng/diseases/sitreps | `data/raw/ncdc_pdfs/yellow_fever/` |
| Nigeria state shapefiles | grid3.org | `data/shapefiles/nigeria_states.shp` |
| Health facilities CSV | data.humdata.org | `data/raw/health_facilities.csv` |
| Population data | nigerianstat.gov.ng | `data/raw/nigeria_population.xlsx` |

### 6. Run the ETL pipeline

```bash
# Full run
python -m src.etl.pipeline

# Dry run (validate only, no DB writes)
python -m src.etl.pipeline --dry-run

# Verbose output
python -m src.etl.pipeline --log-level DEBUG
```

### 7. Start the API

```bash
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000

# Swagger docs: http://localhost:8000/docs
```

### 8. Start the dashboard

```bash
streamlit run dashboard/app.py

# Dashboard: http://localhost:8501
```

---

## 🐳 Docker (full stack)

```bash
# Start everything: DB + API + ETL
docker-compose up

# Run ETL only
docker-compose run --rm etl

# API only
docker-compose up db api
```

---

## 🧪 Running Tests

```bash
# All tests
pytest tests/ -v

# Specific test file
pytest tests/test_transform.py -v
pytest tests/test_validate.py -v
pytest tests/test_api.py -v

# With coverage
pytest tests/ --cov=src --cov-report=html
```

**Test summary:**
- `test_transform.py` — 49 tests (ETL cleaning logic)
- `test_validate.py` — 71 tests (data quality checks)
- `test_api.py` — 63 tests (API endpoints)
- `test_extract.py` — 41 tests (data extraction)

---

## ⚡ API Reference

Base URL: `https://nigeria-disease-api.onrender.com/api/v1`

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/surveillance` | Query surveillance records |
| GET | `/surveillance/latest` | Most recent week nationwide |
| GET | `/surveillance/state/{state}` | Full history for one state |
| GET | `/surveillance/disease/{disease}` | Full history for one disease |
| GET | `/surveillance/diseases` | List tracked diseases |
| GET | `/surveillance/states` | List all 37 states |
| GET | `/analytics/summary` | National KPI aggregates |
| GET | `/analytics/trends` | Time series per disease |
| GET | `/analytics/hotspots` | Top N states by burden |
| GET | `/analytics/forecast` | Prophet 52-week forecast |
| GET | `/analytics/outbreak-alerts` | CUSUM outbreak detection |
| GET | `/analytics/trend-test` | Mann-Kendall trend test |
| GET | `/analytics/clusters` | K-means state clustering |
| GET | `/analytics/cfr-benchmark` | CFR vs. national mean |
| GET | `/geospatial/choropleth` | GeoJSON for choropleth maps |
| GET | `/geospatial/facilities` | Health facility locations |
| GET | `/geospatial/burden-index` | Composite burden score |
| GET | `/geospatial/accessibility` | Facility access gap analysis |
| GET | `/geospatial/morans-i` | Spatial autocorrelation |
| GET | `/health` | API health check |

Full interactive documentation: [`/docs`](https://nigeria-disease-api.onrender.com/docs)

All GET endpoints support `?format=csv` for direct download.

---

## 📁 Data Sources

| Source | Description | Update frequency |
|--------|-------------|-----------------|
| [NCDC Nigeria](https://ncdc.gov.ng/diseases/sitreps) | Weekly situation reports for 5 notifiable diseases | Weekly |
| [WHO AFRO](https://afro.who.int/health-topics) | Cross-validation surveillance data | Monthly |
| [NASA POWER API](https://power.larc.nasa.gov) | Monthly precipitation per state centroid | Monthly |
| [HDX Nigeria](https://data.humdata.org) | Health facility locations and types | Annual |
| [NBS Nigeria](https://nigerianstat.gov.ng) | State population estimates | Annual |
| [GRID3 Nigeria](https://grid3.org) | State and LGA boundary shapefiles | Annual |

---

## 📄 Policy Brief

A 3-page data-driven policy brief summarising key findings is available in [`reports/policy_brief.pdf`](reports/policy_brief.pdf).

---

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch: `git checkout -b feature/my-analysis`
3. Run tests before committing: `pytest tests/ -v`
4. Submit a pull request

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 👤 Built by

**Ayodeji** — HealthTech Data Scientist

Anatomist turned Data Scientist, building AI solutions for healthcare.

[LinkedIn](https://linkedin.com/in/ayodeji) · [GitHub](https://github.com/ayodeji07)
