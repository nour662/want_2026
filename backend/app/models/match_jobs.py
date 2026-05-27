import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class MatchJob(Base):
    __tablename__ = "match_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_list_id = Column(UUID(as_uuid=True), ForeignKey("participant_lists.id"), nullable=False)
    initiated_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    job_type = Column(String, nullable=False, server_default="company_matching")
    status = Column(String, nullable=False, server_default="queued")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MatchJobResult(Base):
    __tablename__ = "match_job_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_job_id = Column(UUID(as_uuid=True), ForeignKey("match_jobs.id"), nullable=False)
    participant_list_entry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("participant_list_entries.id"),
        nullable=False,
    )
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)
    confidence = Column(Numeric, nullable=True)
    result_status = Column(String, nullable=False, server_default="matched")
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
