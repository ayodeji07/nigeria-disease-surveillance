# 🖥️ VSCode Run Guide — Nigeria Disease Surveillance Dashboard

Complete step-by-step guide to running the project from scratch
in Visual Studio Code on Windows, Mac, or Linux.

---

## Prerequisites — Install These First

| Tool | Version | Download |
|------|---------|----------|
| Python | 3.11+ | [python.org](https://python.org) |
| Git | Any | [git-scm.com](https://git-scm.com) |
| Docker Desktop | Any | [docker.com/products/docker-desktop](https://docker.com/products/docker-desktop) |
| VSCode | Any | [code.visualstudio.com](https://code.visualstudio.com) |
| DBeaver (optional) | Any | [dbeaver.io](https://dbeaver.io) — database GUI |

---

## VSCode Extensions to Install

Open VSCode → press `Ctrl+Shift+X` (Windows/Linux) or `Cmd+Shift+X` (Mac)
and install these:

| Extension | ID | Why |
|-----------|-----|-----|
| Python | `ms-python.python` | Python language support |
| Pylance | `ms-python.vscode-pylance` | Intellisense + type checking |
| Jupyter | `ms-toolsai.jupyter` | Run notebooks inside VSCode |
| Docker | `ms-azuretools.vscode-docker` | Manage containers from sidebar |
| SQLTools | `mtxr.sqltools` | Query PostgreSQL from VSCode |
| SQLTools PostgreSQL | `mtxr.sqltools-driver-pg` | PostgreSQL driver for SQLTools |
| GitLens | `eamodio.gitlens` | Better git history |

---

## Phase 0 — Project Setup

### 0.1 Unzip and open in VSCode

```bash
# Unzip the downloaded project
unzip nigeria_surveillance_final.zip

# Open in VSCode
code nigeria_surveillance/
```

VSCode will open the project. You'll see the full folder structure
in the Explorer sidebar on the left.

---

### 0.2 Create and activate a virtual environment

Open the **integrated terminal** in VSCode:
`Terminal → New Terminal` or `` Ctrl+` ``

```bash
# Create virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate

# Mac / Linux:
source venv/bin/activate
```

You should see `(venv)` appear at the start of your terminal prompt.

> **VSCode tip:** After activating, press `Ctrl+Shift+P` → type
> `Python: Select Interpreter` → choose the one that shows `venv`.
> VSCode will now use your venv for IntelliSense and running files.

---

### 0.3 Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This takes 3–5 minutes the first time. You will see many packages
being downloaded. This is normal.

> **Windows note:** If geopandas fails to install, run this instead:
> ```bash
> pip install wheel
> conda install geopandas  # if using Anaconda
> # OR use the pre-built wheel from:
> # https://www.lfd.uci.edu/~gohlke/pythonlibs/#gdal
> ```

---

### 0.4 Set up your environment file

```bash
# Copy the template
cp .env.example .env
```

Open `.env` in VSCode and fill in your values:

```env
# For local development (Docker DB):
DATABASE_URL=postgresql://postgres:localdev@localhost:5432/nigeria_health

# Generate a key: python -c "import secrets; print(secrets.token_hex(32))"
API_KEY=your-secret-key-here

APP_ENV=development
LOG_LEVEL=INFO
API_BASE_URL=http://localhost:8000
```

> **Never commit `.env` to Git.** It is already in `.gitignore`.

---

## Phase 1 — Start the Database

### 1.1 Start Docker Desktop

Open Docker Desktop and wait until it shows **"Docker is running"**.

### 1.2 Start PostgreSQL + PostGIS

In your VSCode terminal:

```bash
docker-compose up db -d
```

This pulls the `postgis/postgis:15-3.3` image (first run only, ~500MB)
and starts a PostgreSQL database with PostGIS enabled.

**Verify it started:**

```bash
docker-compose ps
```

You should see:

```
NAME                    STATUS
nigeria_health_db       running (healthy)
```

### 1.3 Initialise the database schema

```bash
# Create all tables, indexes, views, and triggers
docker exec -i nigeria_health_db \
    psql -U postgres -d nigeria_health \
    -f /dev/stdin < sql/schema.sql

# Seed the dimension tables (states + diseases)
docker exec -i nigeria_health_db \
    psql -U postgres -d nigeria_health \
    -f /dev/stdin < sql/seed_lookups.sql
```

**Verify the schema loaded:**

```bash
docker exec -it nigeria_health_db \
    psql -U postgres -d nigeria_health \
    -c "\dt"
```

You should see 8 tables listed:
`data_quality_log`, `dim_date`, `dim_diseases`, `dim_states`,
`fact_disease_surveillance`, `health_facilities`,
`pipeline_runs`, `rainfall_monthly`

> **Optional — connect DBeaver:**
> Host: `localhost` Port: `5432` Database: `nigeria_health`
> User: `postgres` Password: `localdev`
> This gives you a visual schema diagram and lets you query tables.

---

## Phase 2 — Download Source Data

You need to manually download these files before running the ETL.
The pipeline cannot work without them.

### 2.1 NCDC Situation Reports (PDFs)

1. Go to [ncdc.gov.ng/diseases/sitreps](https://ncdc.gov.ng/diseases/sitreps)
2. Download weekly situation reports for each disease
3. Place them in the correct folder:

```
data/raw/ncdc_pdfs/cholera/          ← Cholera PDFs
data/raw/ncdc_pdfs/lassa_fever/      ← Lassa Fever PDFs
data/raw/ncdc_pdfs/mpox/             ← Mpox PDFs
data/raw/ncdc_pdfs/meningitis/       ← Meningitis PDFs
data/raw/ncdc_pdfs/yellow_fever/     ← Yellow Fever PDFs
```

> **Tip:** Start with just 2–3 years of Cholera PDFs to test the
> pipeline before downloading everything.

### 2.2 State shapefiles

1. Go to [grid3.org](https://grid3.org) → Nigeria → Admin Boundaries
2. Download the state-level shapefile
3. Extract and place these files in `data/shapefiles/`:

```
data/shapefiles/nigeria_states.shp
data/shapefiles/nigeria_states.shx
data/shapefiles/nigeria_states.dbf
data/shapefiles/nigeria_states.prj
```

### 2.3 Health facilities

1. Go to [data.humdata.org](https://data.humdata.org)
2. Search: `Nigeria health facilities`
3. Download the CSV → save as `data/raw/health_facilities.csv`

### 2.4 Population data

1. Go to [nigerianstat.gov.ng](https://nigerianstat.gov.ng)
2. Download state population estimates
3. Save as `data/raw/nigeria_population.xlsx`

### 2.5 NASA rainfall (fetched automatically)

The NASA POWER rainfall API is called automatically by the ETL pipeline.
No manual download needed. Requires internet access during pipeline run.

---

## Phase 3 — Run the Notebooks (Optional but Recommended)

Running the notebooks before the pipeline lets you inspect each
data source interactively and understand the raw data.

### 3.1 Open a notebook in VSCode

In the Explorer sidebar, click on:
`notebooks/00_extract_walkthrough.ipynb`

VSCode will open the Jupyter notebook interface.

### 3.2 Select the kernel

Click **"Select Kernel"** in the top-right of the notebook →
choose **"Python (venv)"** — the interpreter from your virtual environment.

### 3.3 Run all cells

Click **"Run All"** (▶▶ button) at the top, or run cells one by one
with `Shift+Enter`.

**Recommended notebook order:**

```
00_extract_walkthrough.ipynb   ← understand extract.py functions
01_data_ingestion.ipynb        ← run all extractors, inspect raw data
02_data_cleaning.ipynb         ← clean all sources, save to data/processed/
03_eda.ipynb                   ← explore patterns visually
04_statistical_analysis.ipynb  ← Mann-Kendall, CUSUM, clustering
05_geospatial_analysis.ipynb   ← choropleth maps, Moran's I
06_forecasting.ipynb           ← Prophet forecasts
```

> **Note:** Each notebook reads from the previous one's output.
> Run them in order.

---

## Phase 4 — Run the ETL Pipeline

### 4.1 Dry run first (validate without writing to DB)

```bash
python -m src.etl.pipeline --dry-run
```

This runs all four stages (Extract → Transform → Validate → Load)
but skips the actual database writes. Use it to catch data quality
issues before touching the DB.

**Expected output:**

```
══════════════════════════════════════════════════════
Nigeria Disease Surveillance ETL pipeline
Started at : 2024-01-15 09:00:00
Dry run    : True
══════════════════════════════════════════════════════
── Stage 1: EXTRACT ──────────────────────────────
  NCDC Cholera        1,248 rows
  ...
── Stage 2: TRANSFORM ────────────────────────────
  Cleaned Cholera: 1,248 → 987 rows
  ...
── Stage 3: VALIDATE ─────────────────────────────
  fact_disease_surveillance: 11/11 checks passed
── Stage 4: LOAD ─────────────────────────────────
  DRY RUN — skipping DB writes
```

### 4.2 Full pipeline run (loads to database)

```bash
python -m src.etl.pipeline
```

**With extra options:**

```bash
# Verbose output (see every log line)
python -m src.etl.pipeline --log-level DEBUG

# Force load even if validation warnings
python -m src.etl.pipeline --force

# Both dry run and verbose
python -m src.etl.pipeline --dry-run --log-level DEBUG
```

### 4.3 Verify data loaded

Open DBeaver (or use the terminal) and run:

```sql
-- Check row counts per disease
SELECT d.disease_name, COUNT(*) as rows
FROM fact_disease_surveillance f
JOIN dim_diseases d ON f.disease_id = d.disease_id
GROUP BY d.disease_name
ORDER BY rows DESC;

-- Check pipeline run history
SELECT * FROM pipeline_runs ORDER BY run_at DESC LIMIT 5;

-- Check data quality log
SELECT table_name, check_name, status, records_affected
FROM data_quality_log
ORDER BY checked_at DESC
LIMIT 20;
```

---

## Phase 5 — Start the FastAPI Service

### 5.1 Start the API

```bash
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```

The `--reload` flag restarts the server automatically when you
edit source files — essential for development.

**Expected output:**

```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Database connectivity verified ✓
INFO:     API ready — docs at /docs
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 5.2 Open the Swagger docs

Open your browser and go to:

```
http://localhost:8000/docs
```

You will see the interactive Swagger UI with all endpoints listed.
Click any endpoint → **"Try it out"** → fill parameters → **"Execute"**.

### 5.3 Test a few endpoints manually

```bash
# Health check
curl http://localhost:8000/health

# List diseases
curl http://localhost:8000/api/v1/surveillance/diseases

# List states
curl http://localhost:8000/api/v1/surveillance/states

# Get surveillance data (if DB is loaded)
curl "http://localhost:8000/api/v1/surveillance?disease=Cholera&limit=5"

# National summary
curl http://localhost:8000/api/v1/analytics/summary
```

> **VSCode tip:** Install the **REST Client** extension (`humao.rest-client`)
> and create a `test.http` file to run API calls directly from VSCode.

---

## Phase 6 — Start the Streamlit Dashboard

Open a **second terminal** in VSCode
(`Terminal → New Terminal` or click the `+` icon in the terminal panel).

Make sure your venv is activated in this terminal too:

```bash
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Start the dashboard
streamlit run dashboard/app.py
```

**Expected output:**

```
  You can now view your Streamlit app in your browser.
  Local URL: http://localhost:8501
  Network URL: http://192.168.x.x:8501
```

Your browser will open automatically at `http://localhost:8501`.

> **Keep both terminals running** — the API (port 8000) must be
> running for the dashboard to fetch data.

---

## Phase 7 — Run the Tests

Open a **third terminal** in VSCode with venv activated:

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_transform.py -v
pytest tests/test_validate.py -v
pytest tests/test_api.py -v
pytest tests/test_extract.py -v

# Run with coverage report
pytest tests/ --cov=src --cov-report=html
# Then open: htmlcov/index.html in your browser

# Run only fast tests (skip slow ones)
pytest tests/ -v -m "not slow"

# Stop on first failure
pytest tests/ -x
```

**Expected summary:**

```
=================== 224 passed in 12.34s ===================
```

---

## Phase 8 — Full Stack with Docker (Alternative)

Instead of running each service manually, you can run the
entire stack (DB + API + ETL) with one command:

```bash
# Start DB + API together
docker-compose up db api

# In a separate terminal — run ETL once
docker-compose run --rm etl

# Or start everything (DB + API, ETL runs once then exits)
docker-compose up
```

The dashboard still runs locally (outside Docker) because
Streamlit works best that way during development:

```bash
streamlit run dashboard/app.py
```

---

## Summary — Terminal Layout in VSCode

The recommended VSCode terminal layout during development:

```
Terminal 1 (DB/Docker)  → docker-compose up db -d
Terminal 2 (API)        → uvicorn src.api.main:app --reload
Terminal 3 (Dashboard)  → streamlit run dashboard/app.py
Terminal 4 (ETL/Tests)  → python -m src.etl.pipeline  /  pytest tests/
```

Split the terminal panel: click the split icon (⧉) in the
terminal panel top-right to have multiple terminals visible at once.

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'src'"

You are running Python from the wrong directory.
Always run commands from the **project root**
(`nigeria_surveillance/`), not from inside a subdirectory.

```bash
# Wrong
cd src && python etl/pipeline.py

# Correct
cd nigeria_surveillance
python -m src.etl.pipeline
```

### "could not connect to server: Connection refused (port 5432)"

The database container is not running.

```bash
docker-compose up db -d
docker-compose ps   # should show "healthy"
```

### "geopandas not found" or GDAL errors on Windows

```bash
# Option 1: Use conda
conda install geopandas

# Option 2: Install pre-built wheel
pip install pipwin
pipwin install gdal
pipwin install fiona
pip install geopandas
```

### "pdfplumber extracted 0 tables"

The PDF is either scanned (image-based) rather than text-based,
or the table formatting is unusual. Try opening the PDF in your
browser — if you cannot select/copy the text, it is a scanned image
and cannot be parsed by pdfplumber. In that case, manually re-type
the key table data into a CSV and place it in `data/raw/`.

### "Prophet installation fails"

Prophet requires `cmdstanpy` which compiles C++ code.

```bash
# Install build tools first (Windows)
pip install pystan==2.19.1.1
pip install prophet

# Mac
xcode-select --install
pip install prophet

# Linux
sudo apt-get install libgomp1
pip install prophet
```

### API returns empty results after ETL

Check that the pipeline actually loaded data:

```bash
# In the VSCode terminal
docker exec -it nigeria_health_db \
    psql -U postgres -d nigeria_health \
    -c "SELECT COUNT(*) FROM fact_disease_surveillance;"
```

If count is 0, the pipeline ran but did not load. Check the
`pipeline_runs` table for error messages:

```bash
docker exec -it nigeria_health_db \
    psql -U postgres -d nigeria_health \
    -c "SELECT status, error_message FROM pipeline_runs ORDER BY run_at DESC LIMIT 3;"
```

### Streamlit "Connection refused" on dashboard

The FastAPI server is not running. Make sure Terminal 2 is
active and showing the uvicorn output. The dashboard calls
`http://localhost:8000` — if that is not responding, the
dashboard will show all empty charts.

---

## Quick Reference — All Commands

```bash
# ── Environment ──────────────────────────────────────────────────
python -m venv venv && source venv/bin/activate   # create + activate
pip install -r requirements.txt                    # install deps

# ── Database ─────────────────────────────────────────────────────
docker-compose up db -d                            # start DB
docker-compose down                                # stop DB
docker-compose down -v                             # stop + delete data

# ── Schema ───────────────────────────────────────────────────────
docker exec -i nigeria_health_db psql -U postgres -d nigeria_health -f /dev/stdin < sql/schema.sql
docker exec -i nigeria_health_db psql -U postgres -d nigeria_health -f /dev/stdin < sql/seed_lookups.sql

# ── ETL Pipeline ─────────────────────────────────────────────────
python -m src.etl.pipeline --dry-run               # validate only
python -m src.etl.pipeline                         # full run
python -m src.etl.pipeline --log-level DEBUG       # verbose
python -m src.etl.pipeline --force                 # ignore validation

# ── API ───────────────────────────────────────────────────────────
uvicorn src.api.main:app --reload                  # dev server
uvicorn src.api.main:app --host 0.0.0.0 --port 8000  # production

# ── Dashboard ────────────────────────────────────────────────────
streamlit run dashboard/app.py                     # start dashboard

# ── Tests ────────────────────────────────────────────────────────
pytest tests/ -v                                   # all tests
pytest tests/ --cov=src --cov-report=html          # with coverage
pytest tests/test_extract.py -v                    # one file
pytest tests/ -x                                   # stop on first fail

# ── Docker full stack ─────────────────────────────────────────────
docker-compose up db api                           # DB + API
docker-compose run --rm etl                        # run ETL once
docker-compose up                                  # everything
```
