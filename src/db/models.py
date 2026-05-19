"""
src/db/models.py
────────────────────────────────────────────────────────────────
SQLAlchemy ORM table definitions.

These models mirror the SQL schema in sql/schema.sql exactly.
They serve two purposes:
  1. The repository layer uses them to build type-safe queries.
  2. They document the database structure in Python — a developer
     can understand the whole schema by reading this file.

Why ORM + raw SQL?
  We use ORM models for schema documentation and simple queries,
  but the repository layer (repository.py) also runs raw SQL for
  complex analytical queries. This is a pragmatic trade-off:
  ORM is readable for simple CRUD; raw SQL is clearer for
  multi-join analytical queries.

PostGIS geometry columns:
  We store geometry as TEXT (WKT) in the ORM layer and let
  PostGIS handle the conversion in raw SQL. This keeps the ORM
  models independent of the GeoAlchemy2 library, which requires
  a compiled C extension. Applications that don't need spatial
  queries can use these models without any extra dependencies.
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


# ── Base class ───────────────────────────────────────────────────

class Base(DeclarativeBase):
    """
    Shared base for all ORM models.

    All tables inherit from this. SQLAlchemy uses it to maintain
    the metadata registry (the full list of tables and their
    column definitions).
    """
    pass


# ── Dimension tables ─────────────────────────────────────────────
# Dimension tables hold descriptive reference data that changes
# rarely. They are populated once (from seed_lookups.sql) and
# joined to the fact table for analysis.


class DimState(Base):
    """
    One row per Nigerian administrative unit (36 states + FCT).

    The `geometry` column stores the state boundary as WKT text.
    PostGIS functions operate on it via raw SQL in the repository.
    """
    __tablename__ = "dim_states"

    state_id           = Column(Integer, primary_key=True, autoincrement=True)
    state_name         = Column(String(100), unique=True, nullable=False)
    geopolitical_zone  = Column(String(50), nullable=True)
    population         = Column(Integer, nullable=True)
    area_km2           = Column(Float, nullable=True)

    # Stored as WKT text; PostGIS casts it to geometry in queries
    # e.g. ST_GeomFromText(geometry, 4326)
    geometry           = Column(Text, nullable=True)

    # Relationships
    surveillance_records = relationship(
        "FactDiseaseSurveillance",
        back_populates="state",
        lazy="dynamic",
    )
    health_facilities = relationship(
        "HealthFacility",
        back_populates="state",
        lazy="dynamic",
    )
    rainfall_records = relationship(
        "RainfallMonthly",
        back_populates="state",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<DimState id={self.state_id} name={self.state_name!r}>"


class DimDisease(Base):
    """
    One row per tracked disease.

    ICD-10 codes and transmission routes allow the dashboard to
    display contextual clinical information alongside the numbers.
    """
    __tablename__ = "dim_diseases"

    disease_id    = Column(Integer, primary_key=True, autoincrement=True)
    disease_name  = Column(String(100), unique=True, nullable=False)
    disease_code  = Column(String(10),  nullable=True)   # ICD-10
    category      = Column(String(50),  nullable=True)   # Infectious, NCD
    transmission  = Column(String(100), nullable=True)   # Waterborne, Airborne
    is_notifiable = Column(Boolean, default=True)

    surveillance_records = relationship(
        "FactDiseaseSurveillance",
        back_populates="disease",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<DimDisease id={self.disease_id} name={self.disease_name!r}>"


class DimDate(Base):
    """
    One row per reporting date.

    Pre-computing week, month, quarter, and year here avoids
    repeated date arithmetic in analytical queries.

    Season is based on Nigerian climate: Dry (Nov–Mar) / Rainy (Apr–Oct).
    This is relevant for cholera (peaks in rainy season) and
    meningitis (peaks in dry season / harmattan).
    """
    __tablename__ = "dim_date"

    date_id      = Column(Integer, primary_key=True, autoincrement=True)
    report_date  = Column(Date, unique=True, nullable=False)
    week_number  = Column(Integer, nullable=True)
    month        = Column(Integer, nullable=True)
    quarter      = Column(Integer, nullable=True)
    year         = Column(Integer, nullable=True)

    # Nigerian seasons: Dry = November–March, Rainy = April–October
    season       = Column(String(10), nullable=True)

    surveillance_records = relationship(
        "FactDiseaseSurveillance",
        back_populates="date",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<DimDate id={self.date_id} date={self.report_date}>"


# ── Fact table ───────────────────────────────────────────────────

class FactDiseaseSurveillance(Base):
    """
    Core fact table — one row per (state, disease, report_date).

    This is the central table that all analysis queries hit.
    It stores both raw counts and pre-computed derived metrics
    (incidence rate, CFR, rolling averages) to keep dashboard
    queries fast.

    Unique constraint on (state_id, disease_id, date_id) ensures
    the ETL upsert logic cannot create duplicates.
    """
    __tablename__ = "fact_disease_surveillance"

    record_id    = Column(Integer, primary_key=True, autoincrement=True)

    # Foreign keys to dimension tables
    state_id     = Column(Integer, ForeignKey("dim_states.state_id"),   nullable=False)
    disease_id   = Column(Integer, ForeignKey("dim_diseases.disease_id"), nullable=False)
    date_id      = Column(Integer, ForeignKey("dim_date.date_id"),      nullable=True)

    # ── Raw counts (as reported by NCDC) ──────────────────────────
    suspected_cases  = Column(Integer, default=0, nullable=False)
    confirmed_cases  = Column(Integer, default=0, nullable=False)
    deaths           = Column(Integer, default=0, nullable=False)

    # ── Derived metrics (calculated during transform) ──────────────
    incidence_per_100k  = Column(Float, nullable=True)
    cfr_pct             = Column(Float, nullable=True)   # Case Fatality Rate %

    # ── Rolling/comparative metrics (calculated during transform) ──
    cases_4wk_avg    = Column(Float, nullable=True)   # 4-week rolling average
    pct_change_wow   = Column(Float, nullable=True)   # Week-on-week % change

    # ── Data provenance ───────────────────────────────────────────
    data_source        = Column(String(100), nullable=True)  # e.g. "NCDC SitRep W12 2023"
    data_quality_flag  = Column(String(30),  default="CLEAN")

    # ── Audit timestamps ──────────────────────────────────────────
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(),
                         onupdate=func.now())

    # Relationships
    state   = relationship("DimState",   back_populates="surveillance_records")
    disease = relationship("DimDisease", back_populates="surveillance_records")
    date    = relationship("DimDate",    back_populates="surveillance_records")

    # ── Constraints ───────────────────────────────────────────────
    __table_args__ = (
        UniqueConstraint(
            "state_id", "disease_id", "date_id",
            name="uq_surveillance_state_disease_date",
        ),
        # Indexes on the most-queried columns
        Index("ix_surveillance_state_id",   "state_id"),
        Index("ix_surveillance_disease_id", "disease_id"),
        Index("ix_surveillance_date_id",    "date_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<FactDiseaseSurveillance "
            f"state_id={self.state_id} "
            f"disease_id={self.disease_id} "
            f"date_id={self.date_id} "
            f"confirmed={self.confirmed_cases}>"
        )


# ── Supplementary tables ─────────────────────────────────────────

class HealthFacility(Base):
    """
    Location and classification of Nigerian health facilities.

    Sourced from the Humanitarian Data Exchange (HDX).
    Used for facility accessibility analysis in the geospatial module.

    The `geometry` column stores the facility's point location
    as WKT text (e.g. "POINT(3.3792 6.5244)").
    """
    __tablename__ = "health_facilities"

    facility_id    = Column(Integer, primary_key=True, autoincrement=True)
    facility_name  = Column(String(200), nullable=True)
    facility_type  = Column(String(50),  nullable=True)   # Hospital, PHC, Clinic
    state_id       = Column(Integer, ForeignKey("dim_states.state_id"), nullable=True)
    lga_name       = Column(String(100), nullable=True)
    ownership      = Column(String(50),  nullable=True)   # Federal, State, Private

    # WKT point geometry: "POINT(longitude latitude)"
    geometry       = Column(Text, nullable=True)
    latitude       = Column(Float, nullable=True)
    longitude      = Column(Float, nullable=True)

    state = relationship("DimState", back_populates="health_facilities")

    __table_args__ = (
        Index("ix_facility_state_id", "state_id"),
    )

    def __repr__(self) -> str:
        return f"<HealthFacility id={self.facility_id} name={self.facility_name!r}>"


class RainfallMonthly(Base):
    """
    Monthly precipitation per state from NASA POWER API.

    Used for correlation analysis: cholera burden vs. rainfall,
    and meningitis burden vs. dry season intensity.
    """
    __tablename__ = "rainfall_monthly"

    rainfall_id  = Column(Integer, primary_key=True, autoincrement=True)
    state_id     = Column(Integer, ForeignKey("dim_states.state_id"), nullable=False)
    year         = Column(Integer, nullable=False)
    month        = Column(Integer, nullable=False)
    rainfall_mm  = Column(Float,   nullable=True)

    state = relationship("DimState", back_populates="rainfall_records")

    __table_args__ = (
        UniqueConstraint("state_id", "year", "month", name="uq_rainfall_state_year_month"),
        Index("ix_rainfall_state_id", "state_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<RainfallMonthly state_id={self.state_id} "
            f"year={self.year} month={self.month} "
            f"mm={self.rainfall_mm}>"
        )


# ── Operational / audit tables ───────────────────────────────────

class DataQualityLog(Base):
    """
    One row per validation check run per ETL pipeline execution.

    This gives us a permanent audit trail: we can look back and
    see exactly which checks passed or failed on any given date.
    Invaluable when a downstream analyst questions the data quality.
    """
    __tablename__ = "data_quality_log"

    log_id            = Column(Integer, primary_key=True, autoincrement=True)
    table_name        = Column(String(100), nullable=False)
    check_name        = Column(String(200), nullable=False)
    status            = Column(String(20),  nullable=False)   # PASS, FAIL_WARNING, FAIL_ERROR
    records_affected  = Column(Integer, default=0)
    total_records     = Column(Integer, default=0)
    pass_rate         = Column(Float,   nullable=True)
    message           = Column(Text,    nullable=True)
    failed_examples   = Column(Text,    nullable=True)   # JSON-serialised sample
    checked_at        = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"<DataQualityLog table={self.table_name!r} "
            f"check={self.check_name!r} status={self.status!r}>"
        )


class PipelineRun(Base):
    """
    One row per ETL pipeline execution.

    Records outcome, record counts, and duration so we can monitor
    pipeline health over time and diagnose regressions.
    """
    __tablename__ = "pipeline_runs"

    run_id             = Column(Integer, primary_key=True, autoincrement=True)
    pipeline_name      = Column(String(100), nullable=False)
    status             = Column(String(20),  nullable=False)  # SUCCESS, FAILED, PARTIAL
    records_extracted  = Column(Integer, default=0)
    records_loaded     = Column(Integer, default=0)
    records_failed     = Column(Integer, default=0)
    duration_seconds   = Column(Float,   nullable=True)
    error_message      = Column(Text,    nullable=True)
    run_at             = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"<PipelineRun id={self.run_id} "
            f"status={self.status!r} "
            f"loaded={self.records_loaded}>"
        )
