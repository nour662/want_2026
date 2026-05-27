from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/sbir", tags=["sbir"])

SBIR_BASE_URL = "https://api.www.sbir.gov/public/api"


class SBIRCompaniesResponse(BaseModel):
    companies: List[Dict[str, Any]]
    count: int


class SBIRAwardsResponse(BaseModel):
    awards: List[Dict[str, Any]]
    count: int


def _extract_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


@router.get("/companies", response_model=SBIRCompaniesResponse)
def get_sbir_companies(
    search_type: str = Query(..., pattern="^(keyword|name|uei)$"),
    query: str = Query(..., min_length=1),
    rows: int = Query(100, ge=1, le=5000),
    start: int = Query(0, ge=0),
    sort: Optional[str] = Query(None, pattern="^(name|uei|state)$"),
):
    params = {"rows": rows, "start": start}
    if search_type == "keyword":
        params["keyword"] = query
    elif search_type == "name":
        params["name"] = query
    else:
        params["uei"] = query

    if sort:
        params["sort"] = sort

    try:
        response = requests.get(f"{SBIR_BASE_URL}/firm", params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"SBIR request failed: {exc}") from exc

    payload = response.json()
    companies = _extract_list(payload)
    return SBIRCompaniesResponse(companies=companies, count=len(companies))


@router.get("/awards", response_model=SBIRAwardsResponse)
def get_sbir_awards(
    agency: Optional[str] = None,
    firm: Optional[str] = None,
    uei: Optional[str] = None,
    rows: int = Query(100, ge=1, le=5000),
    start: int = Query(0, ge=0),
):
    if not agency and not firm and not uei:
        raise HTTPException(status_code=400, detail="Provide agency, firm, or uei")

    params: Dict[str, Any] = {"rows": rows, "start": start}
    if agency:
        params["agency"] = agency
    if firm:
        params["firm"] = firm
    if uei:
        params["uei"] = uei

    try:
        response = requests.get(f"{SBIR_BASE_URL}/awards", params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"SBIR request failed: {exc}") from exc

    payload = response.json()
    awards = _extract_list(payload)
    if uei:
        awards = [award for award in awards if str(award.get("uei")) == uei]
    return SBIRAwardsResponse(awards=awards, count=len(awards))
