import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class Investor(Base):
    """Standalone investor entity table."""

    __tablename__ = "investors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    normalized_name = Column(String, nullable=True)
    investor_type = Column(
        String, nullable=True
    )  # e.g. "Venture Capital", "Angel", "Corporate", "Government"
    source = Column(String, nullable=True)  # "pitchbook", "sbir", "usaspending", "tedco"
    source_id = Column(String, nullable=True)  # external ID from the source system
    website = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_investors_name", "name"),
        Index("ix_investors_normalized_name", "normalized_name"),
        UniqueConstraint("source", "source_id", name="uq_investor_source_id"),
    )


class FundingInvestor(Base):
    """Junction table linking funding records to investors."""

    __tablename__ = "funding_investors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    funding_id = Column(UUID(as_uuid=True), ForeignKey("funding.id", ondelete="CASCADE"), nullable=False)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("investors.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("funding_id", "investor_id", name="uq_funding_investor"),
        Index("ix_funding_investors_funding", "funding_id"),
        Index("ix_funding_investors_investor", "investor_id"),
    )
