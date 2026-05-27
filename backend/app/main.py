import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.endpoints import (
    companies,
    company_search,
    events,
    hubs,
    integrations,
    matching,
    participants,
    sam_daily_sync,
    sam_db_updates,
    sbir,
    universities,
    users,
)
from app.core.db.base import Base, SessionLocal, engine
from app.core.db.init_db import *
from app.models.hub import Hub
from app.models.hubs import HUBS, UNIVERSITIES
from app.models.university import University

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


@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    ensure_core_auth_schema()
    ensure_reference_data()
    ensure_event_schema()


@app.get("/")
def root():
    return {"message": "WANT FullStack API", "status": "running"}


@app.get("/health")
def health():
    return {"status": "healthy"}
