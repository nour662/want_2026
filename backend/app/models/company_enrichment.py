import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class CompanyGeoEnrichment(Base):
    __tablename__ = "company_geo_enrichments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, unique=True)
    normalized_address = Column(Text, nullable=True)
    congressional_district = Column(String, nullable=True)
    state_legislative_upper = Column(String, nullable=True)
    state_legislative_lower = Column(String, nullable=True)
    county_fips = Column(String, nullable=True)
    place_fips = Column(String, nullable=True)
    census_tract = Column(String, nullable=True)
    geoid = Column(String, nullable=True)
    source = Column(String, nullable=False, default="census")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EnrichmentApiFailureLog(Base):
    __tablename__ = "enrichment_api_failure_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)
    source = Column(String, nullable=False)
    endpoint = Column(Text, nullable=True)
    status_code = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=False)
    attempt = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
