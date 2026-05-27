import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import object_session
from sqlalchemy.sql import func, text

from app.core.db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name = Column(String(200), nullable=False, server_default=text("''"))
    password_hash = Column(String(255), nullable=False, server_default=text("''"))
    email = Column(String, unique=True, nullable=False, index=True)
    job_title = Column(String(200), nullable=True)
    university_id = Column(UUID(as_uuid=True), ForeignKey("universities.id"), nullable=True)
    role = Column(String, nullable=False, server_default=text("'user'"))
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    @property
    def hub_id(self):
        if not self.university_id:
            return None

        session = object_session(self)
        if session is None:
            return None

        from app.models.university import University

        university = session.query(University).filter(University.id == self.university_id).first()
        return getattr(university, "hub_id", None) if university else None


class HubUserRole(Base):
    __tablename__ = "hub_user_roles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("hub_organizations.id"),
        nullable=False,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    hub_role = Column(String, nullable=False)
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
            "user_id",
            name="uq_hub_user_role",
        ),
    )






