import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func, text

from app.core.db.base import Base


class ParticipantList(Base):
    __tablename__ = "participant_lists"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), nullable=False)
    published_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    university_id = Column(UUID(as_uuid=True), ForeignKey("universities.id"), nullable=True)
    title = Column(String, nullable=False)
    source_type = Column(String, nullable=True)
    source_filename = Column(String, nullable=True)
    source_uri = Column(String, nullable=True)
    status = Column(String, nullable=False, server_default=text("'draft'"))
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
