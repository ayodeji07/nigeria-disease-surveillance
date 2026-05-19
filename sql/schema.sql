-- ─────────────────────────────────────────────────────────────────
-- sql/schema.sql
-- Nigeria Disease Surveillance — Database Schema
--
-- PostgreSQL 15 + PostGIS 3.3
--
-- Run once to initialise the database:
--   psql -U postgres -d nigeria_health -f sql/schema.sql
--
-- Or via Docker (automatically on first startup):
--   See docker-compose.yml — this file is mounted as an init script.
--
-- Tables:
--   Dimension : dim_states, dim_diseases, dim_date
--   Fact      : fact_disease_surveillance
--   Support   : health_facilities, rainfall_monthly
--   Audit     : data_quality_log, pipeline_runs
-- ─────────────────────────────────────────────────────────────────


-- Enable PostGIS (idempotent — safe to run multiple times)
CREATE EXTENSION IF NOT EXISTS postgis;


-- ── Drop tables in dependency order (for clean re-runs) ──────────
-- Only needed during development. Remove in production.

DROP TABLE IF EXISTS data_quality_log      CASCADE;
DROP TABLE IF EXISTS pipeline_runs         CASCADE;
DROP TABLE IF EXISTS rainfall_monthly      CASCADE;
DROP TABLE IF EXISTS health_facilities     CASCADE;
DROP TABLE IF EXISTS fact_disease_surveillance CASCADE;
DROP TABLE IF EXISTS dim_date              CASCADE;
DROP TABLE IF EXISTS dim_diseases          CASCADE;
DROP TABLE IF EXISTS dim_states            CASCADE;


-- ────────────────────────────────────────────────────────────────
-- DIMENSION TABLES
-- ────────────────────────────────────────────────────────────────

-- All 36 Nigerian states plus the Federal Capital Territory (FCT).
-- The geometry column stores the state boundary as PostGIS geometry,
-- enabling spatial queries (ST_Within, ST_Distance, etc.).

CREATE TABLE dim_states (
    state_id          SERIAL       PRIMARY KEY,
    state_name        VARCHAR(100) NOT NULL UNIQUE,
    geopolitical_zone VARCHAR(50),              -- North-West, South-South, etc.
    population        INTEGER,                  -- Most recent NBS estimate
    area_km2          FLOAT,
    geometry          GEOMETRY(MULTIPOLYGON, 4326),  -- WGS84 boundary polygon
    created_at        TIMESTAMPTZ  DEFAULT NOW()
);

COMMENT ON TABLE  dim_states              IS 'Nigerian states and FCT — one row per administrative unit.';
COMMENT ON COLUMN dim_states.geometry     IS 'State boundary polygon in WGS84. Source: GRID3 Nigeria.';
COMMENT ON COLUMN dim_states.population   IS 'Estimated population — used to calculate incidence rates.';


-- The five diseases tracked by NCDC that are included in this system.

CREATE TABLE dim_diseases (
    disease_id    SERIAL       PRIMARY KEY,
    disease_name  VARCHAR(100) NOT NULL UNIQUE,
    disease_code  VARCHAR(10),   -- ICD-10 code, e.g. A00 for Cholera
    category      VARCHAR(50),   -- Infectious, NCD, etc.
    transmission  VARCHAR(100),  -- Waterborne, Airborne, Vector-borne
    is_notifiable BOOLEAN        DEFAULT TRUE,
    created_at    TIMESTAMPTZ    DEFAULT NOW()
);

COMMENT ON TABLE dim_diseases IS 'Reference table of tracked diseases. Populated once from seed_lookups.sql.';


-- One row per distinct reporting date in the surveillance data.
-- Stores pre-computed calendar attributes so analytical queries
-- do not need to call date functions repeatedly.

CREATE TABLE dim_date (
    date_id      SERIAL  PRIMARY KEY,
    report_date  DATE    NOT NULL UNIQUE,
    week_number  SMALLINT,           -- ISO week number 1–53
    month        SMALLINT,           -- 1–12
    quarter      SMALLINT,           -- 1–4
    year         SMALLINT,
    -- Nigerian seasons:
    --   Dry   = November–March   (harmattan, meningitis belt risk)
    --   Rainy = April–October    (cholera risk)
    season       VARCHAR(10)         -- 'Dry' | 'Rainy'
);

COMMENT ON TABLE  dim_date        IS 'Date dimension — one row per reporting date in the data.';
COMMENT ON COLUMN dim_date.season IS 'Nigerian climatological season: Dry (Nov–Mar) or Rainy (Apr–Oct).';


-- ────────────────────────────────────────────────────────────────
-- FACT TABLE
-- ────────────────────────────────────────────────────────────────

-- Central fact table — one row per (state, disease, reporting week).
-- This is the table that all dashboard queries hit.
-- The unique constraint on the three FKs enforces idempotent upserts.

CREATE TABLE fact_disease_surveillance (
    record_id    SERIAL  PRIMARY KEY,

    -- Foreign keys to dimension tables
    state_id     INTEGER NOT NULL REFERENCES dim_states(state_id),
    disease_id   INTEGER NOT NULL REFERENCES dim_diseases(disease_id),
    date_id      INTEGER          REFERENCES dim_date(date_id),  -- nullable (data may lack week info)

    -- Raw case counts as reported by NCDC / WHO
    suspected_cases  INTEGER     NOT NULL DEFAULT 0,
    confirmed_cases  INTEGER     NOT NULL DEFAULT 0,
    deaths           INTEGER     NOT NULL DEFAULT 0,

    -- Derived metrics (computed during transform stage)
    incidence_per_100k  FLOAT,        -- confirmed / population * 100,000
    cfr_pct             FLOAT,        -- deaths / confirmed * 100

    -- Rolling metrics (computed during transform stage)
    cases_4wk_avg   FLOAT,            -- 4-week rolling mean of confirmed_cases
    pct_change_wow  FLOAT,            -- week-on-week % change in confirmed_cases

    -- Data provenance
    data_source        VARCHAR(100),  -- e.g. 'NCDC SitRep W12 2023.pdf'
    data_quality_flag  VARCHAR(30)    DEFAULT 'CLEAN',

    -- Audit timestamps
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW(),

    -- Prevent duplicates — ETL uses INSERT ... ON CONFLICT
    CONSTRAINT uq_surveillance_state_disease_date
        UNIQUE (state_id, disease_id, date_id)
);

COMMENT ON TABLE  fact_disease_surveillance IS 'Core surveillance fact table — one row per (state, disease, week).';
COMMENT ON COLUMN fact_disease_surveillance.data_quality_flag IS
    'CLEAN | IMPUTED | DATE_APPROXIMATED | SUSPECT_HIGH_COUNT | CONFIRMED_EXCEEDS_SUSPECTED';


-- Trigger to auto-update updated_at on row changes
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_surveillance_updated_at
    BEFORE UPDATE ON fact_disease_surveillance
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ────────────────────────────────────────────────────────────────
-- SUPPLEMENTARY TABLES
-- ────────────────────────────────────────────────────────────────

-- Health facility locations from HDX Nigeria.
-- Used for facility accessibility analysis alongside disease burden maps.

CREATE TABLE health_facilities (
    facility_id    SERIAL       PRIMARY KEY,
    facility_name  VARCHAR(200),
    facility_type  VARCHAR(50),   -- Hospital, Primary Health Centre, Clinic
    state_id       INTEGER        REFERENCES dim_states(state_id),
    lga_name       VARCHAR(100),
    ownership      VARCHAR(50),   -- Federal, State, Private, Faith-based
    latitude       FLOAT,
    longitude      FLOAT,
    geometry       GEOMETRY(POINT, 4326),  -- PostGIS point
    created_at     TIMESTAMPTZ    DEFAULT NOW()
);

COMMENT ON TABLE health_facilities IS 'Health facility locations. Source: Humanitarian Data Exchange (HDX).';


-- Monthly precipitation per state from NASA POWER API.
-- Used for rainfall-disease correlation analysis (cholera, meningitis).

CREATE TABLE rainfall_monthly (
    rainfall_id  SERIAL   PRIMARY KEY,
    state_id     INTEGER  NOT NULL REFERENCES dim_states(state_id),
    year         SMALLINT NOT NULL,
    month        SMALLINT NOT NULL,
    rainfall_mm  FLOAT,            -- NULL = NASA fill value (-999) replaced

    CONSTRAINT uq_rainfall_state_year_month
        UNIQUE (state_id, year, month)
);

COMMENT ON TABLE rainfall_monthly IS 'Monthly precipitation per state. Source: NASA POWER API (PRECTOTCORR parameter).';


-- ────────────────────────────────────────────────────────────────
-- AUDIT / OPERATIONAL TABLES
-- ────────────────────────────────────────────────────────────────

-- One row per validation check per pipeline run.
-- Provides a permanent audit trail of data quality over time.

CREATE TABLE data_quality_log (
    log_id            SERIAL       PRIMARY KEY,
    table_name        VARCHAR(100) NOT NULL,
    check_name        VARCHAR(200) NOT NULL,
    status            VARCHAR(20)  NOT NULL,   -- PASS | FAIL_WARNING | FAIL_ERROR
    records_affected  INTEGER      DEFAULT 0,
    total_records     INTEGER      DEFAULT 0,
    pass_rate         FLOAT,
    message           TEXT,
    failed_examples   TEXT,        -- JSON-serialised sample of failing values
    checked_at        TIMESTAMPTZ  DEFAULT NOW()
);

COMMENT ON TABLE data_quality_log IS 'Validation check results — one row per check per ETL run.';


-- One row per ETL pipeline execution.
-- Used to monitor pipeline health, track trends, and diagnose failures.

CREATE TABLE pipeline_runs (
    run_id             SERIAL       PRIMARY KEY,
    pipeline_name      VARCHAR(100) NOT NULL,
    status             VARCHAR(20)  NOT NULL,  -- SUCCESS | FAILED | PARTIAL
    records_extracted  INTEGER      DEFAULT 0,
    records_loaded     INTEGER      DEFAULT 0,
    records_failed     INTEGER      DEFAULT 0,
    duration_seconds   FLOAT,
    error_message      TEXT,
    run_at             TIMESTAMPTZ  DEFAULT NOW()
);

COMMENT ON TABLE pipeline_runs IS 'ETL run history — one row per pipeline execution.';


-- ────────────────────────────────────────────────────────────────
-- INDEXES
-- ────────────────────────────────────────────────────────────────
-- Indexes on the most-queried columns. The dashboard's primary
-- queries filter on disease, state, and date — all three are indexed.

-- Fact table
CREATE INDEX ix_fact_state_id    ON fact_disease_surveillance(state_id);
CREATE INDEX ix_fact_disease_id  ON fact_disease_surveillance(disease_id);
CREATE INDEX ix_fact_date_id     ON fact_disease_surveillance(date_id);
CREATE INDEX ix_fact_quality     ON fact_disease_surveillance(data_quality_flag);

-- Date dimension (commonly filtered by year)
CREATE INDEX ix_dim_date_year    ON dim_date(year);
CREATE INDEX ix_dim_date_date    ON dim_date(report_date);

-- Spatial indexes — speeds up PostGIS spatial queries significantly
CREATE INDEX ix_states_geom      ON dim_states         USING GIST(geometry);
CREATE INDEX ix_facilities_geom  ON health_facilities  USING GIST(geometry);

-- Rainfall
CREATE INDEX ix_rainfall_state   ON rainfall_monthly(state_id);
CREATE INDEX ix_rainfall_year    ON rainfall_monthly(year);

-- Audit tables
CREATE INDEX ix_quality_log_table    ON data_quality_log(table_name);
CREATE INDEX ix_quality_log_checked  ON data_quality_log(checked_at);
CREATE INDEX ix_pipeline_runs_at     ON pipeline_runs(run_at);


-- ────────────────────────────────────────────────────────────────
-- VIEWS
-- ────────────────────────────────────────────────────────────────
-- Pre-built views for common dashboard queries.
-- The dashboard can query these views directly rather than
-- writing the full join every time.

-- National weekly case counts per disease
CREATE OR REPLACE VIEW vw_national_weekly AS
SELECT
    d.disease_name,
    dt.report_date,
    dt.week_number,
    dt.year,
    dt.season,
    SUM(f.confirmed_cases) AS confirmed_cases,
    SUM(f.deaths)          AS deaths,
    AVG(f.cfr_pct)         AS avg_cfr_pct,
    COUNT(DISTINCT f.state_id) AS states_reporting
FROM  fact_disease_surveillance f
JOIN  dim_diseases d  ON f.disease_id = d.disease_id
JOIN  dim_date     dt ON f.date_id    = dt.date_id
GROUP BY d.disease_name, dt.report_date, dt.week_number,
         dt.year, dt.season
ORDER BY dt.report_date DESC;

COMMENT ON VIEW vw_national_weekly IS 'National weekly aggregates per disease — used by trend charts.';


-- State-level annual burden summary
CREATE OR REPLACE VIEW vw_state_annual_burden AS
SELECT
    s.state_name,
    s.geopolitical_zone,
    d.disease_name,
    dt.year,
    SUM(f.confirmed_cases)           AS total_cases,
    SUM(f.deaths)                    AS total_deaths,
    ROUND(AVG(f.incidence_per_100k)::numeric, 2) AS avg_incidence_per_100k,
    ROUND(AVG(f.cfr_pct)::numeric, 3)            AS avg_cfr_pct
FROM  fact_disease_surveillance f
JOIN  dim_states   s  ON f.state_id   = s.state_id
JOIN  dim_diseases d  ON f.disease_id = d.disease_id
JOIN  dim_date     dt ON f.date_id    = dt.date_id
GROUP BY s.state_name, s.geopolitical_zone, d.disease_name, dt.year
ORDER BY dt.year DESC, total_cases DESC;

COMMENT ON VIEW vw_state_annual_burden IS 'Annual disease burden per state — used by choropleth maps and hotspot analysis.';
