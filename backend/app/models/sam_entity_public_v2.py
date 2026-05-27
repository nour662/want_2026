from sqlalchemy import BigInteger, Boolean, Column, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db.base import Base


class SamEntityPublicV2(Base):
    __tablename__ = "sam_entity_public_v2"


    id = Column(BigInteger, primary_key=True, autoincrement=True)
    load_id = Column(String(32), nullable=False, index=True)
    row_no = Column(BigInteger, nullable=False, index=True)


    uei = Column(String(12), index=True)
    cage_code = Column(String(5), index=True)

    registration_status = Column(String(16), index=True)
    entity_type_code = Column(String(16), index=True)

    registration_date = Column(String(8), index=True)
    expiration_date = Column(String(8), index=True)
    last_update_date_1 = Column(String(8), index=True)
    last_update_date_2 = Column(String(8), index=True)

    legal_business_name = Column(Text, index=True)
    dba_name = Column(Text)



    address_line1 = Column(Text)
    address_line2 = Column(Text)
    city = Column(Text)
    state = Column(String(64))
    zip_code = Column(String(16))
    zip4 = Column(String(16))
    country = Column(String(8))
    congressional_district = Column(String(16))

    website = Column(Text)
    fiscal_year_end_mmdd = Column(String(4))

    incorporation_state = Column(String(64))
    incorporation_country = Column(String(8))


    primary_naics = Column(String(6), index=True)
    business_types_raw = Column(Text)
    naics_list_raw = Column(Text)
    psc_list_raw = Column(Text)


    is_active = Column(Boolean)


    raw_line = Column(Text, nullable=False)
    raw_fields = Column(JSONB, nullable=False)


    row_ended = Column(Boolean, default=True)
