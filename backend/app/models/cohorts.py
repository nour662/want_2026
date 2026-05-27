import uuid

from sqlalchemy import Column, Date, DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class Cohort(Base):
    __tablename__ = "cohorts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cohort_name = Column(String, nullable=False)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    cohort_type = Column(
        Enum(
            "3 week program",
            "2 week program",
            "7 week program",
            name="cohort_type",
        ),
        nullable=True,
    )
    location = Column(
        Enum(
            "Virtual",
            "Hybrid",
            "In-Person",
            name="cohort_location",
        ),
        nullable=True,
    )
    hub_id = Column(UUID(as_uuid=True), ForeignKey("hub_organizations.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
