import uuid

from sqlalchemy import Column, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.core.db.base import Base


class CompanyMembers(Base):
    __tablename__ = "company_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    participant_id = Column(UUID(as_uuid=True), ForeignKey("participants.id"), nullable=False)
    formal_title = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("participant_id", "company_id", name="uq_company_member"),
    )

