import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class SavedCompany(Base):
    __tablename__ = "saved_hub_companies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("hub_organizations.id"),
        nullable=False,
    )
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("hub_organization_id", "company_id", name="uq_saved_hub_company"),
    )

    @property
    def saved_at(self):
        return self.created_at


class SavedHubCompanyNote(Base):
    __tablename__ = "saved_hub_company_notes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_favorite_company_id = Column(
        UUID(as_uuid=True),
        ForeignKey("saved_hub_companies.id"),
        nullable=False,
    )
    note = Column(Text, nullable=False)
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
