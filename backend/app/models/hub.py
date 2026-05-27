import uuid

from sqlalchemy import Column, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class Hub(Base):
    __tablename__ = "hub_organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    website_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class HubUniversityMembership(Base):
    __tablename__ = "hub_university_memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("hub_organizations.id"),
        nullable=False,
    )
    university_id = Column(UUID(as_uuid=True), ForeignKey("universities.id"), nullable=False)
    role = Column(String, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "hub_organization_id",
            "university_id",
            name="uq_hub_university_membership",
        ),
    )
