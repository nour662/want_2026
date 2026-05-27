import uuid

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import synonym
from sqlalchemy.sql import func, text

from app.core.db.base import Base


class Company(Base):
    __tablename__ = "companies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    normalized_name = Column(String, nullable=True)
    website_url = Column(String, nullable=True)
    domain = Column(String, nullable=True)
    hq_city = Column(String, nullable=True)
    hq_state = Column(String, nullable=True)
    hq_country = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    sbir_url = Column(String, nullable=True)
    uei = Column(String, nullable=True)
    duns = Column(String, nullable=True)
    address1 = Column(String, nullable=True)
    address2 = Column(String, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    zip = Column(String, nullable=True)
    company_url = Column(String, nullable=True)
    hubzone_owned = Column(Boolean, nullable=True)
    socially_economically_disadvantaged = Column(Boolean, nullable=True)
    woman_owned = Column(Boolean, nullable=True)
    number_awards = Column(Integer, nullable=True)
    number_employees = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_companies_name", "name"),
        Index("ix_companies_normalized_name", "normalized_name"),
        Index("ix_companies_domain", "domain"),
        Index("ix_companies_uei", "uei"),
        Index("ix_companies_duns", "duns"),
    )

    company_name = synonym("name")
    legal_name = synonym("name")
    website = synonym("website_url")
    state_province = synonym("state")
    country = synonym("hq_country")
    employees = synonym("number_employees")


class CompanyAlias(Base):
    __tablename__ = "company_aliases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    alias = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("company_id", "alias", name="uq_company_alias"),
        Index("ix_company_aliases_alias", "alias"),
    )


class ParticipantCompanyMatch(Base):
    __tablename__ = "participant_company_matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_list_entry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("participant_list_entries.id"),
        nullable=False,
    )
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    match_type = Column(String, nullable=False)
    match_source = Column(String, nullable=False)
    confidence = Column(Numeric, nullable=False)
    matched_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    matched_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    rationale = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "participant_list_entry_id",
            "company_id",
            "match_type",
            name="uq_participant_company_match",
        ),
        Index("ix_participant_company_matches_company", "company_id"),
    )




