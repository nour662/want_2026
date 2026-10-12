import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status = Column(
        Enum("pending", "running", "completed", "failed", name="pipeline_job_status"),
        nullable=False,
        default="pending",
    )
    mode = Column(String, nullable=True)  # "full" | "incremental" | "single"
    requested_sources = Column(JSON, nullable=True)
    company_ids = Column(JSON, nullable=True)  # list of UUIDs as strings, or null = all companies
    total_companies = Column(Integer, nullable=True)
    processed_companies = Column(Integer, nullable=False, default=0)
    failed_companies = Column(Integer, nullable=False, default=0)
    results_summary = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    triggered_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
