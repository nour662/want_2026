"""
SAM.gov entity lookups.

Prefers the local ``sam_entity_public_v2`` extract (fast, no rate limit) and
falls back to the SAM.gov Entity API when SAM_API_KEY is set. Both return the
same normalised entity dict so the rest of the pipeline does not care.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

from app.services.watn_pipeline import http
from app.services.watn_pipeline.matching import normalize_id, normalize_name

logger = logging.getLogger(__name__)

SAM_ENTITY_URL = "https://api.sam.gov/entity-information/v3/entities"

_DB_COLUMNS = """
    uei, cage_code, legal_business_name, dba_name, registration_status, entity_type_code,
    registration_date, address_line1, address_line2, city, state, zip_code, country,
    website, congressional_district, incorporation_state, incorporation_country,
    primary_naics, is_active, last_update_date_1
"""


def _fmt_yyyymmdd(value: Optional[str]) -> Optional[str]:
    if value and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value or None


def _from_db_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "uei": row.get("uei"),
        "cage": row.get("cage_code"),
        "legal_name": row.get("legal_business_name"),
        "dba_name": row.get("dba_name"),
        "address1": row.get("address_line1"),
        "address2": row.get("address_line2"),
        "city": row.get("city"),
        "state": row.get("state"),
        "zip": row.get("zip_code"),
        "country": row.get("country"),
        "congressional_district": row.get("congressional_district"),
        "website": row.get("website"),
        "incorporation_state": row.get("incorporation_state"),
        "incorporation_country": row.get("incorporation_country"),
        "registration_date": _fmt_yyyymmdd(row.get("registration_date")),
        "naics": row.get("primary_naics"),
        "active": row.get("is_active"),
        "contacts": [],
    }


def _from_api_entity(entity: Dict[str, Any]) -> Dict[str, Any]:
    reg = entity.get("entityRegistration") or {}
    core = entity.get("coreData") or {}
    addr = core.get("physicalAddress") or core.get("mailingAddress") or {}
    info = core.get("entityInformation") or {}
    general = core.get("generalInformation") or {}
    contacts = []
    for poc in (entity.get("pointsOfContact") or {}).values():
        if isinstance(poc, dict) and (poc.get("firstName") or poc.get("lastName")):
            contacts.append(f"{poc.get('firstName') or ''} {poc.get('lastName') or ''}".strip())
    naics_list = ((entity.get("assertions") or {}).get("goodsAndServices") or {}).get("primaryNaics")
    return {
        "uei": reg.get("ueiSAM"),
        "cage": reg.get("cageCode"),
        "legal_name": reg.get("legalBusinessName"),
        "dba_name": reg.get("dbaName"),
        "address1": addr.get("addressLine1"),
        "address2": addr.get("addressLine2"),
        "city": addr.get("city"),
        "state": addr.get("stateOrProvinceCode"),
        "zip": addr.get("zipCode"),
        "country": addr.get("countryCode"),
        "congressional_district": core.get("congressionalDistrict"),
        "website": info.get("entityURL"),
        "incorporation_state": general.get("stateOfIncorporationCode"),
        "incorporation_country": general.get("countryOfIncorporationCode"),
        "registration_date": reg.get("registrationDate"),
        "naics": naics_list,
        "active": (reg.get("registrationStatus") or "").lower() == "active" if reg.get("registrationStatus") else None,
        "contacts": contacts,
    }


class SamLookup:
    def __init__(self, db_url: Optional[str] = None, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.getenv("SAM_API_KEY")
        self._pool = None
        self._lock = threading.Lock()
        db_url = db_url if db_url is not None else (os.getenv("SAM_DB_URL") or os.getenv("DATABASE_URL"))
        if db_url:
            self._init_db(db_url)

    @property
    def available(self) -> bool:
        return self._pool is not None or bool(self.api_key)

    def describe(self) -> str:
        parts = []
        if self._pool is not None:
            parts.append("local SAM extract")
        if self.api_key:
            parts.append("SAM API")
        return " + ".join(parts) or "unavailable (set SAM_DB_URL or SAM_API_KEY)"

    def _init_db(self, db_url: str) -> None:
        try:
            from psycopg2.pool import ThreadedConnectionPool

            pool = ThreadedConnectionPool(1, 16, dsn=db_url)
            conn = pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM sam_entity_public_v2 LIMIT 1")
                    has_rows = cur.fetchone() is not None
            finally:
                pool.putconn(conn)
            if has_rows:
                self._pool = pool
            else:
                logger.warning("sam_entity_public_v2 is empty; using SAM API only")
                pool.closeall()
        except Exception as exc:
            logger.warning("Local SAM extract unavailable (%s); using SAM API only", exc)

    def _query(self, sql: str, params: tuple) -> List[Dict[str, Any]]:
        from psycopg2.extras import RealDictCursor

        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.rollback()
            self._pool.putconn(conn)

    # ── Public API ──────────────────────────────────────────────────────

    def by_uei(self, uei: str) -> Optional[Dict[str, Any]]:
        uei = normalize_id(uei)
        if not uei:
            return None
        if self._pool is not None:
            rows = self._query(
                f"SELECT {_DB_COLUMNS} FROM sam_entity_public_v2 WHERE uei = %s "
                "ORDER BY last_update_date_1 DESC NULLS LAST LIMIT 1",
                (uei,),
            )
            if rows:
                return _from_db_row(rows[0])
        if self.api_key:
            entities = self._api({"ueiSAM": uei})
            if entities:
                return entities[0]
        return None

    def search_name(self, name: str, limit: int = 15) -> List[Dict[str, Any]]:
        term = normalize_name(name)
        if not term:
            return []
        results: List[Dict[str, Any]] = []
        if self._pool is not None:
            rows = self._query(
                f"SELECT {_DB_COLUMNS} FROM sam_entity_public_v2 "
                "WHERE LOWER(legal_business_name) LIKE %s OR LOWER(dba_name) LIKE %s "
                "ORDER BY last_update_date_1 DESC NULLS LAST LIMIT %s",
                (f"%{term}%", f"%{term}%", limit),
            )
            results = [_from_db_row(r) for r in rows]
        if not results and self.api_key:
            results = self._api({"legalBusinessName": name, "size": 10})
        return results

    def _api(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            payload = http.get_json(
                SAM_ENTITY_URL,
                params={
                    "api_key": self.api_key,
                    "includeSections": "entityRegistration,coreData,pointsOfContact",
                    **params,
                },
                timeout=30,
            )
        except Exception as exc:
            logger.warning("SAM API request failed (%s): %s", params, exc)
            return []
        return [_from_api_entity(e) for e in payload.get("entityData") or []]
