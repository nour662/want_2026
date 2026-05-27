import uuid

from sqlalchemy import (
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
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import synonym
from sqlalchemy.sql import func, text

from app.core.db.base import Base


class FundingRecord(Base):
	__tablename__ = "funding"

	id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
	funding_id = Column(String, nullable=True)
	award_number = Column(String, nullable=True)
	funding_source = Column(
		Enum(
			"Angel",
			"Federal",
			"Grant",
			"MIPS",
			"NIH",
			"NSF I-Corps",
			"Other",
			"SBIR",
			"Series",
			"STTR",
			"TEDCO",
			"Venture Capital",
			name="funding_source",
		),
		nullable=True,
	)
	funding_stage = Column(
		Enum(
			"Stage 1: Pre-Seed Funding",
			"Stage 2: Seed Funding",
			"Stage 3: Early Stage Investment (Series A & B)",
			"Stage 4: Later Stage Investment (Series C, D, etc.)",
			"Stage 5: Mezzanine Financing",
			name="funding_stage",
		),
		nullable=True,
	)
	amount_awarded = Column(Numeric(15, 2), nullable=True)
	currency = Column(String, nullable=False, server_default=text("'USD'"))
	date_awarded = Column(Date, nullable=True)
	award_end_month = Column(Integer, nullable=True)
	award_end_year = Column(Integer, nullable=True)
	phase = Column(String, nullable=True)
	pi_name = Column(String, nullable=True)
	award_title = Column(String, nullable=True)
	award_link = Column(String, nullable=True)
	investors = Column(Text, nullable=True)
	additional_info = Column(Text, nullable=True)

	funding_type = Column(String, nullable=True)
	funder_name = Column(String, nullable=True)
	program_name = Column(String, nullable=True)
	source_url = Column(String, nullable=True)
	agency = Column(String, nullable=True)
	branch = Column(String, nullable=True)
	program = Column(String, nullable=True)
	agency_tracking_number = Column(String, nullable=True)
	contract = Column(String, nullable=True)
	proposal_award_date = Column(Date, nullable=True)
	contract_end_date = Column(Date, nullable=True)
	solicitation_number = Column(String, nullable=True)
	solicitation_year = Column(String, nullable=True)
	topic_code = Column(String, nullable=True)
	award_year = Column(Integer, nullable=True)
	award_amount = Column(Numeric, nullable=True)
	poc_name = Column(String, nullable=True)
	poc_title = Column(String, nullable=True)
	poc_phone = Column(String, nullable=True)
	poc_email = Column(String, nullable=True)
	pi_phone = Column(String, nullable=True)
	pi_email = Column(String, nullable=True)
	ri_name = Column(String, nullable=True)
	ri_poc_name = Column(String, nullable=True)
	ri_poc_phone = Column(String, nullable=True)
	research_area_keywords = Column(Text, nullable=True)
	abstract = Column(Text, nullable=True)
	created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
	updated_at = Column(
	 DateTime(timezone=True),
	 nullable=False,
	 server_default=func.now(),
	 onupdate=func.now(),
	)

	__table_args__ = (
	 Index("ix_funding_company_award", "company_id", "date_awarded"),
	 Index("ix_funding_external_id", "funding_id"),
	)

	amount_usd = synonym("amount_awarded")
	award_date = synonym("date_awarded")
	external_id = synonym("funding_id")
