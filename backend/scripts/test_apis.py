"""
API Test Script — all 5 external APIs
To test one at a time, comment out the calls at the bottom.

Run from project root:
    python backend/scripts/test_apis.py

Address lookup strategy:
    SBIR API rate limited (429) — cannot use for address.
    SAM.gov bulk download is several GB — not feasible locally.
    USASpending recipient profile used temporarily to get address,
    which is then fed into Census Geocoder for congressional district
    (same logic as congressional_disct.ipynb, just automated per UEI).
"""

import json
import os
import re
import sys
import difflib
import requests
from pathlib import Path
from typing import Optional

try:
    from rapidfuzz.fuzz import token_sort_ratio as _fuzz_token_sort
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False

# Load .env from same directory as this script
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)

# ── Constants ────────────────────────────────────────────────────────────────
SBIR_BASE_URL          = "https://api.www.sbir.gov/public/api"
NSF_AWARDS_URL         = "https://api.nsf.gov/services/v1/awards.json"
USASPENDING_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
USASPENDING_RECIPIENT_SEARCH_URL = "https://api.usaspending.gov/api/v2/recipient/"
CENSUS_GEOCODE_URL     = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_GEOGRAPHY_URL   = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
USPTO_SEARCH_URL       = "https://api.uspto.gov/api/v1/patent/applications/search"
USPTO_API_KEY          = os.environ.get("USPTO_API_KEY", "")

if not USPTO_API_KEY:
    print("ERROR: USPTO_API_KEY not set. Add it to backend/scripts/.env")
    sys.exit(1)

# ── Test inputs ───────────────────────────────────────────────────────────────
TEST_UEI          = "TCT3X4JFLTP8"
TEST_COMPANY_NAME = "RESILIENT LIFESCIENCE, INC"

TEST_USPTO_COMPANIES = [
    "NIRPdots",
    "AirPhoton",
    "Airphoton LLC",
    "Resilient Lifescience",
    "EduMD",
    "respEQ",
    "Atrevida Science",
    "SecondWrite",
    "SpherIngenics",
    "Nanochon",
    "InventWood",
    "Gaskiya Diagnostics",
    "Emission Strategies",
    "Bashpole Software",
    "Tauros Engineering",
    "Cykloburn Technologies",
    "AkriVita",
    "Team CKI",
    "Datasembly",
    "TCE Incorporated",
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def pretty(data):
    print(json.dumps(data, indent=2, default=str))

def section(title):
    print("\n" + "=" * 60, flush=True)
    print(title, flush=True)
    print("=" * 60, flush=True)

def subsection(title):
    print("\n" + "-" * 40, flush=True)
    print(title, flush=True)
    print("-" * 40, flush=True)


# ── Name-matching helpers ─────────────────────────────────────────────────────
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited",
    "corp", "corporation", "co", "company",
    "pllc", "lp", "llp", "pc", "plc",
})

def normalize_company_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    tokens = cleaned.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)

def score_name_match(query: str, candidate: str) -> tuple[int, str]:
    a = normalize_company_name(query)
    b = normalize_company_name(candidate)
    if not a or not b:
        return 0, "NO_MATCH"
    if _RAPIDFUZZ_AVAILABLE:
        score = int(_fuzz_token_sort(a, b))
    else:
        score = int(difflib.SequenceMatcher(None, a, b).ratio() * 100)
    if score >= 85:
        label = "MATCH"
    elif score >= 70:
        label = "REVIEW"
    else:
        label = "NO_MATCH"
    return score, label


# ── 1. SBIR ───────────────────────────────────────────────────────────────────
def test_sbir():
    section("1. SBIR API")

    subsection("Company profile (by UEI)")
    resp = requests.get(
        f"{SBIR_BASE_URL}/firm",
        params={"uei": TEST_UEI, "rows": 1, "start": 0},
        timeout=15,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        firms = data if isinstance(data, list) else data.get("data", data.get("results", [data]))
        if firms:
            print(f"Keys: {list(firms[0].keys())}")
            print("\nFirst record:")
            pretty(firms[0])
        else:
            print("Empty — no company found for this UEI")
            pretty(data)
    else:
        print(f"Error: {resp.text[:300]}")

    subsection("Awards (by UEI, first 5)")
    resp = requests.get(
        f"{SBIR_BASE_URL}/awards",
        params={"uei": TEST_UEI, "rows": 5, "start": 0},
        timeout=15,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        awards = data if isinstance(data, list) else data.get("data", data.get("results", []))
        print(f"Awards returned: {len(awards)}")
        if awards:
            print(f"Keys: {list(awards[0].keys())}")
            print("\nFirst award:")
            pretty(awards[0])
    else:
        print(f"Error: {resp.text[:300]}")


# ── 2. NSF Awards ─────────────────────────────────────────────────────────────
def test_nsf():
    section("2. NSF Awards API")

    resp = requests.get(
        NSF_AWARDS_URL,
        params={"ueiNumber": TEST_UEI, "rpp": 5, "offset": 0},
        timeout=15,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        response_obj = data.get("response", data)
        awards = response_obj.get("award", [])
        print(f"Awards returned: {len(awards)}")
        if awards:
            print(f"Keys: {list(awards[0].keys())}")
            print("\nFirst award:")
            pretty(awards[0])
        else:
            print("No NSF awards found for this UEI")
            pretty(data)
    else:
        print(f"Error: {resp.text[:300]}")


# ── 3. USASpending Awards ─────────────────────────────────────────────────────
def test_usaspending():
    section("3. USASpending API")

    for label, codes in [("Grants", ["02", "03", "04", "05"]), ("Contracts", ["A", "B", "C", "D"])]:
        subsection(f"Award type group: {label} ({codes})")
        payload = {
            "fields": [
                "Award ID", "Recipient Name", "Action Date",
                "Award Amount", "Award Type", "Awarding Agency",
                "Awarding Sub Agency", "Description", "UEI", "Internal ID",
            ],
            "filters": {
                "recipient_uei": [TEST_UEI],
                "recipient_search_text": [TEST_UEI],
                "award_type_codes": codes,
            },
            "page": 1,
            "limit": 3,
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False,
        }
        resp = requests.post(USASPENDING_AWARDS_URL, json=payload, timeout=20)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            meta = data.get("page_metadata", {})
            print(f"Awards returned: {len(results)}  |  Total: {meta.get('total', '?')}")
            if results:
                print(f"Keys: {list(results[0].keys())}")
                print("\nFirst award:")
                pretty(results[0])
            else:
                print("No awards found for this UEI")
        else:
            print(f"Error: {resp.text[:300]}")


# ── 4. Address + Congressional District ───────────────────────────────────────
# Step A: USASpending recipient profile → get verified address
# Step B: Census Geocoder → address → coordinates → congressional district
# (mirrors congressional_disct.ipynb logic, automated per UEI)

def get_address_from_usaspending(uei: str) -> Optional[str]:
    """Fetch verified street address for a UEI via USASpending recipient profile."""
    section("4a. USASpending Recipient Profile (address source)")

    subsection("Step 1 — search by UEI → get recipient_id")
    resp = requests.post(
        USASPENDING_RECIPIENT_SEARCH_URL,
        json={"keyword": uei, "page": 1, "limit": 5},
        timeout=15,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"Error: {resp.text[:300]}")
        return None

    results = resp.json().get("results", [])
    if not results:
        print("No recipient found for this UEI")
        return None

    recipient = results[0]
    recipient_id = recipient.get("id")
    print(f"Found: {recipient.get('name')} | recipient_id: {recipient_id}")

    subsection("Step 2 — fetch full profile → extract address")
    resp2 = requests.get(
        f"https://api.usaspending.gov/api/v2/recipient/{recipient_id}/",
        timeout=15,
    )
    print(f"Status: {resp2.status_code}")
    if resp2.status_code != 200:
        print(f"Error: {resp2.text[:300]}")
        return None

    data = resp2.json()
    loc = data.get("location", {})
    address = f"{loc.get('address_line1', '')}, {loc.get('city_name', '')}, {loc.get('state_code', '')} {loc.get('zip', '')}".strip(", ")
    print(f"\nAddress extracted: {address}")
    print(f"USASpending congressional_code (reference): {loc.get('congressional_code')}")
    return address if loc.get("address_line1") else None


def test_census(address: str):
    """Run Census Geocoder on given address → get congressional district."""
    section("4b. Census Geocoder → Congressional District")

    subsection("Step 1 — address → coordinates")
    print(f"Address: {address}")
    resp = requests.get(
        CENSUS_GEOCODE_URL,
        params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
        timeout=15,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"Error: {resp.text[:300]}")
        return

    data = resp.json()
    matches = data.get("result", {}).get("addressMatches", [])
    print(f"Address matches: {len(matches)}")
    if not matches:
        print("No match — address not geocoded")
        return

    first = matches[0]
    coords = first.get("coordinates", {})
    x, y = coords.get("x"), coords.get("y")
    print(f"Matched address: {first.get('matchedAddress')}")
    print(f"Coordinates: x={x}, y={y}")

    subsection("Step 2 — coordinates → congressional district")
    resp2 = requests.get(
        CENSUS_GEOGRAPHY_URL,
        params={
            "x": x, "y": y,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        },
        timeout=15,
    )
    print(f"Status: {resp2.status_code}")
    if resp2.status_code == 200:
        geo_data = resp2.json()
        geos = geo_data.get("result", {}).get("geographies", {})
        print(f"Geography types returned: {list(geos.keys())}")
        for key, items in geos.items():
            if "congressional" in key.lower() and items:
                print(f"\n{key}:")
                pretty(items[0])
                break
    else:
        print(f"Error: {resp2.text[:300]}")


# ── 5. USPTO Patent Applications Search ──────────────────────────────────────
def _uspto_search(query: str, headers: dict) -> list:
    """Run one USPTO search, return patent list or empty list on failure."""
    payload = {"q": f'applicationMetaData.firstApplicantName:{query}'}
    resp = requests.post(USPTO_SEARCH_URL, json=payload, headers=headers, timeout=20)
    print(f"  Status: {resp.status_code}  Query: {query}", flush=True)
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data.get("patentFileWrapperDataBag", data.get("results", []))


def test_uspto(company_name: str = TEST_COMPANY_NAME):
    section("5. USPTO Patent Applications Search (api.uspto.gov)")

    name_query = normalize_company_name(company_name)
    subsection(f"Search by company name: {company_name}")
    print(f"Normalized query: \"{name_query}\"", flush=True)

    headers = {"X-API-KEY": USPTO_API_KEY, "Content-Type": "application/json"}

    # Attempt 1 — exact phrase (quoted)
    patents = _uspto_search(f'"{name_query}"', headers)

    # Attempt 2 — unquoted (Lucene matches docs containing all words, any order)
    if not patents:
        print("  No results — retrying without quotes (broader match)...", flush=True)
        patents = _uspto_search(name_query, headers)
        if patents:
            print(f"  Fallback hit: {len(patents)} results (scores may be lower — verify carefully)", flush=True)

    print(f"Results: {len(patents)}", flush=True)

    backend_label = "rapidfuzz" if _RAPIDFUZZ_AVAILABLE else "difflib (fallback)"
    print(f"Scoring: {backend_label}", flush=True)

    counts = {"MATCH": 0, "REVIEW": 0, "NO_MATCH": 0}
    for patent in patents:
        meta      = patent.get("applicationMetaData", {})
        app_num   = patent.get("applicationNumberText", "N/A")
        title     = meta.get("inventionTitle", "N/A")[:60]
        applicant = meta.get("firstApplicantName", "")
        if not applicant:
            bag = meta.get("applicantBag", [])
            if bag:
                applicant = bag[0].get("applicantNameText", "")

        score, label = score_name_match(name_query, applicant)
        counts[label] += 1
        print(f"  [{label:8s} {score:3d}]  {app_num}  |  {applicant[:40]}  |  {title}", flush=True)

    total = len(patents)
    print(f"\nTotal: {total} — MATCH:{counts['MATCH']}  REVIEW:{counts['REVIEW']}  NO_MATCH:{counts['NO_MATCH']}", flush=True)


def get_official_name_from_uei(uei: str) -> Optional[str]:
    """Look up the SAM-registered company name via USASpending recipient profile."""
    resp = requests.post(
        USASPENDING_RECIPIENT_SEARCH_URL,
        json={"keyword": uei, "page": 1, "limit": 5},
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    results = resp.json().get("results", [])
    if not results:
        return None
    return results[0].get("name")


def test_uspto_by_inventor(inventor_name: str, score_against: str = ""):
    """Search USPTO patents by inventor name. Optionally score results against a company name."""
    section("5b. USPTO Patent Search — by Inventor Name")
    subsection(f"Inventor: {inventor_name}")

    headers = {"X-API-KEY": USPTO_API_KEY, "Content-Type": "application/json"}

    # Try exact phrase first, then unquoted fallback
    patents = _uspto_search(f'applicationMetaData.firstInventorName:"{inventor_name}"', headers)
    if not patents:
        print("  No results on firstInventorName — trying inventorBag...")
        patents = _uspto_search(f'applicationMetaData.inventorBag.inventorNameText:"{inventor_name}"', headers)

    print(f"Results: {len(patents)}")
    if not patents:
        print("  No patents found for this inventor name.")
        return

    company_query = normalize_company_name(score_against) if score_against else ""
    backend_label = "rapidfuzz" if _RAPIDFUZZ_AVAILABLE else "difflib (fallback)"
    if score_against:
        print(f"Scoring against: \"{company_query}\" ({backend_label})")

    counts = {"MATCH": 0, "REVIEW": 0, "NO_MATCH": 0}
    for patent in patents:
        meta      = patent.get("applicationMetaData", {})
        app_num   = patent.get("applicationNumberText", "N/A")
        title     = meta.get("inventionTitle", "N/A")[:55]
        applicant = meta.get("firstApplicantName", "")
        if not applicant:
            bag = meta.get("applicantBag", [])
            if bag:
                applicant = bag[0].get("applicantNameText", "")
        inventor  = meta.get("firstInventorName", inventor_name)

        if score_against:
            score, label = score_name_match(company_query, applicant)
            counts[label] += 1
            print(f"  [{label:8s} {score:3d}]  {app_num}  |  {applicant[:35]}  |  {inventor[:25]}  |  {title}")
        else:
            print(f"  {app_num}  |  {applicant[:35]}  |  {inventor[:25]}  |  {title}")

    if score_against:
        total = len(patents)
        print(f"\nTotal: {total} — MATCH:{counts['MATCH']}  REVIEW:{counts['REVIEW']}  NO_MATCH:{counts['NO_MATCH']}")


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Comment out whichever APIs you don't want to test right now
    #test_sbir()
    #test_nsf()
    #test_usaspending()
    #address = get_address_from_usaspending(TEST_UEI)
    #if address:
    #    test_census(address)
    
    for company in TEST_USPTO_COMPANIES:
        test_uspto(company)
