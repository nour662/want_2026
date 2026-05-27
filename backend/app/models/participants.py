import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.base import Base


class Participants(Base):
    __tablename__ = "participants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    primary_email = Column(String(320), unique=True, nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    _full_name = Column("full_name", String(255), nullable=True)
    linkedin_url = Column("linkedin_url", String, nullable=True)
    orcid = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_participants_primary_email", "primary_email"),
        Index("ix_participants_last_first", "last_name", "first_name"),
    )

    @property
    def email(self):
        return self.primary_email

    @email.setter
    def email(self, value):
        self.primary_email = value

    @property
    def linkedin(self):
        return self.linkedin_url

    @linkedin.setter
    def linkedin(self, value):
        self.linkedin_url = value

    @property
    def full_name(self):
        if self._full_name:
            return self._full_name
        return " ".join([part for part in [self.first_name, self.last_name] if part]).strip()

    @full_name.setter
    def full_name(self, value):
        self._full_name = (value or "").strip() or None


class ParticipantListEntry(Base):
    __tablename__ = "participant_list_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_list_id = Column(
        UUID(as_uuid=True),
        ForeignKey("participant_lists.id"),
        nullable=False,
    )
    participant_id = Column(UUID(as_uuid=True), ForeignKey("participants.id"), nullable=True)
    row_number = Column(Integer, nullable=True)
    raw_full_name = Column(String, nullable=True)
    raw_email = Column(String, nullable=True)
    raw_affiliation = Column(String, nullable=True)
    raw_title = Column(String, nullable=True)
    raw_team_name = Column(String, nullable=True)
    raw_role = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "participant_list_id",
            "row_number",
            name="uq_participant_list_row",
        ),
        Index("ix_participant_list_entries_raw_email", "raw_email"),
        Index("ix_participant_list_entries_raw_full_name", "raw_full_name"),
    )


class ParticipantEntryUniversityLink(Base):
    __tablename__ = "participant_entry_university_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_list_entry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("participant_list_entries.id"),
        nullable=False,
    )
    university_id = Column(UUID(as_uuid=True), ForeignKey("universities.id"), nullable=False)
    match_method = Column(String, nullable=True)
    match_confidence = Column(Numeric, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "participant_list_entry_id",
            "university_id",
            name="uq_participant_entry_university",
        ),
    )




