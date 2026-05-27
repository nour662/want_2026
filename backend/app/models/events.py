import uuid

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func, text

from app.core.db.base import Base


class Event(Base):
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("hub_organizations.id"),
        nullable=False,
    )
    name = Column(String, nullable=False)
    event_type = Column(String, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    location = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    lead_university_id = Column(UUID(as_uuid=True), ForeignKey("universities.id"), nullable=True)
    partner_institutions_other = Column(Text, nullable=True)
    is_seven_week_program = Column(Boolean, nullable=False, server_default=text("false"))
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class EventUniversity(Base):
    __tablename__ = "event_universities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), nullable=False)
    university_id = Column(UUID(as_uuid=True), ForeignKey("universities.id"), nullable=False)
    involvement_role = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("event_id", "university_id", name="uq_event_university"),
    )
