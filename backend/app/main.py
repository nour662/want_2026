import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.endpoints import (
    bulk_upload,
    companies,
    company_search,
    events,
    hubs,
    integrations,
    matching,
    participants,
    pipeline,
    sam_daily_sync,
    sam_db_updates,
    sbir,
    universities,
    users,
)
from app.core.db.base import Base, SessionLocal, engine
from app.core.db.init_db import *
from app.models import HUBS, UNIVERSITIES, Hub, University

app = FastAPI(title="WANT FullStack API", version="1.0.0")

configured_origins = os.getenv(
    "FRONTEND_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
)
allowed_origins = [origin.strip() for origin in configured_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sam_db_updates.router, prefix="/api")
app.include_router(sam_daily_sync.router, prefix="/api")
app.include_router(company_search.router, prefix="/api")
app.include_router(sbir.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(universities.router, prefix="/api")
app.include_router(hubs.router, prefix="/api")
app.include_router(participants.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(matching.router, prefix="/api")
app.include_router(companies.router, prefix="/api")
app.include_router(integrations.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(bulk_upload.router, prefix="/api")


def ensure_core_auth_schema() -> None:
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(200) NOT NULL DEFAULT ''")
        )
        connection.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255) NOT NULL DEFAULT ''")
        )
        connection.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS job_title VARCHAR(200) NULL")
        )
        connection.execute(
            text("ALTER TABLE users ALTER COLUMN university_id DROP NOT NULL")
        )
        connection.execute(
            text("UPDATE users SET full_name = split_part(email, '@', 1) WHERE COALESCE(full_name, '') = ''")
        )
        connection.execute(
            text("""
                INSERT INTO users (id, full_name, email, password_hash, role, is_active)
                VALUES ('00000000-0000-0000-0000-000000000001', 'WATN System Admin', 'system@watn.edu', '', 'admin', true)
                ON CONFLICT (email) DO NOTHING
            """)
        )


def ensure_reference_data() -> None:
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE universities ADD COLUMN IF NOT EXISTS hub_id UUID NULL")
        )

    db = SessionLocal()
    try:
        hub_map = {hub.name: hub.id for hub in db.query(Hub).all()}

        for hub_name in HUBS:
            if hub_name in hub_map:
                continue
            hub = Hub(name=hub_name)
            db.add(hub)
            db.flush()
            hub_map[hub_name] = hub.id

        existing_universities = {university.name: university for university in db.query(University).all()}
        for university_name, hub_name in UNIVERSITIES:
            hub_id = hub_map[hub_name]
            existing_university = existing_universities.get(university_name)
            if existing_university:
                if getattr(existing_university, "hub_id", None) != hub_id:
                    existing_university.hub_id = hub_id
                if not existing_university.country:
                    existing_university.country = "United States"
                continue

            db.add(
                University(
                    name=university_name,
                    hub_id=hub_id,
                    country="United States",
                )
            )

        db.commit()
    finally:
        db.close()


def ensure_event_schema() -> None:
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE events ADD COLUMN IF NOT EXISTS lead_university_id UUID NULL")
        )
        connection.execute(
            text("ALTER TABLE events ADD COLUMN IF NOT EXISTS partner_institutions_other TEXT NULL")
        )
        connection.execute(
            text("ALTER TABLE events ADD COLUMN IF NOT EXISTS is_seven_week_program BOOLEAN NOT NULL DEFAULT FALSE")
        )
        connection.execute(
            text("ALTER TABLE event_universities ADD COLUMN IF NOT EXISTS involvement_role VARCHAR NULL")
        )
        connection.execute(
            text("ALTER TABLE events ADD COLUMN IF NOT EXISTS cohort_id VARCHAR(50) NULL")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_events_cohort_id ON events (cohort_id)")
        )
        connection.execute(
            text("ALTER TABLE cohorts ADD COLUMN IF NOT EXISTS cohort_id VARCHAR(50) NULL")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_cohorts_cohort_id ON cohorts (cohort_id)")
        )
        connection.execute(
            text("ALTER TABLE cohorts ALTER COLUMN cohort_type TYPE VARCHAR USING cohort_type::VARCHAR")
        )
        connection.execute(
            text("ALTER TABLE cohorts ALTER COLUMN location TYPE VARCHAR USING location::VARCHAR")
        )


def ensure_pipeline_schema() -> None:
    """Add new company/funding/patent columns and pipeline tracking tables."""
    # Participants schema updates
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE participants ADD COLUMN IF NOT EXISTS eid VARCHAR(50) NULL")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_participants_eid ON participants (eid)")
        )
        conn.execute(
            text("ALTER TABLE participants ALTER COLUMN primary_email DROP NOT NULL")
        )
        conn.execute(
            text("ALTER TABLE participant_lists ALTER COLUMN published_by_user_id DROP NOT NULL")
        )
        conn.execute(
            text("ALTER TABLE funding ADD COLUMN IF NOT EXISTS received_post_icorps BOOLEAN NULL")
        )

    # New participant entry columns
    entry_columns = [
        ("raw_company_name", "VARCHAR NULL"),
        ("raw_uei", "VARCHAR(50) NULL"),
        ("raw_duns", "VARCHAR(50) NULL"),
        ("raw_website", "VARCHAR NULL"),
    ]
    with engine.begin() as conn:
        for col_name, col_def in entry_columns:
            conn.execute(
                text(f"ALTER TABLE participant_list_entries ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
            )

    # New company columns
    company_columns = [
        ("cage_code", "VARCHAR(20) NULL"),
        ("entity_type_code", "VARCHAR(50) NULL"),
        ("dba_name", "VARCHAR(500) NULL"),
        ("congressional_district", "VARCHAR(20) NULL"),
        ("legislative_district", "VARCHAR(20) NULL"),
        ("incorporation_state", "VARCHAR(100) NULL"),
        ("incorporation_country", "VARCHAR(100) NULL"),
        ("naics", "VARCHAR(20) NULL"),
        ("minority_owned", "BOOLEAN NULL"),
        ("annual_revenue_range", "VARCHAR(100) NULL"),
        ("currently_active", "BOOLEAN NULL"),
        ("merger_info", "TEXT NULL"),
        ("pitchbook_id", "VARCHAR(100) NULL"),
    ]
    with engine.begin() as conn:
        for col_name, col_def in company_columns:
            conn.execute(
                text(f"ALTER TABLE companies ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
            )

    # New patent columns
    patent_columns = [
        ("cpc_codes", "TEXT NULL"),
        ("publication_number", "VARCHAR(100) NULL"),
    ]
    with engine.begin() as conn:
        for col_name, col_def in patent_columns:
            conn.execute(
                text(f"ALTER TABLE patents ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
            )

    # Add new funding_source enum values (PostgreSQL 12+ supports this in a transaction)
    new_funding_sources = [
        "Accelerator or Incubator",
        "Competition or Prize",
        "Cooperative Agreement",
        "Definitive Contract",
        "Delivery Order",
        "Equity Crowdfunding",
        "Purchase Order",
    ]
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        for value in new_funding_sources:
            conn.execute(
                text(
                    "DO $$ BEGIN "
                    f"  IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = '{value}' "
                    "    AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'funding_source')) "
                    f" THEN ALTER TYPE funding_source ADD VALUE '{value}'; "
                    "  END IF; "
                    "END $$;"
                )
            )

    # Add Stage 6 to funding_stage enum
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(
            text(
                "DO $$ BEGIN "
                "  IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'Stage 6: Revenue & Commercial Traction' "
                "    AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'funding_stage')) "
                " THEN ALTER TYPE funding_stage ADD VALUE 'Stage 6: Revenue & Commercial Traction'; "
                "  END IF; "
                "END $$;"
            )
        )


@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    ensure_core_auth_schema()
    ensure_reference_data()
    ensure_event_schema()
    ensure_pipeline_schema()


@app.get("/")
def root():
    return {"message": "WANT FullStack API", "status": "running"}


@app.get("/health")
def health():
    return {"status": "healthy"}
