import uuid

from sqlalchemy import Column, Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class Patent(Base):
	__tablename__ = "patents"

	id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)
	title = Column(String, nullable=False)
	patent_number = Column(String, nullable=True)
	application_number = Column(String, nullable=True)
	jurisdiction = Column(String, nullable=True)
	status = Column(String, nullable=True)
	filing_date = Column(Date, nullable=True)
	publication_date = Column(Date, nullable=True)
	grant_date = Column(Date, nullable=True)
	assignee_name = Column(String, nullable=True)
	inventors = Column(Text, nullable=True)
	source_url = Column(String, nullable=True)
	created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
	updated_at = Column(
	 DateTime(timezone=True),
	 nullable=False,
	 server_default=func.now(),
	 onupdate=func.now(),
	)

	__table_args__ = (
	 Index("ix_patents_patent_number", "patent_number"),
	 Index("ix_patents_application_number", "application_number"),
	 Index("ix_patents_company", "company_id"),
	)
