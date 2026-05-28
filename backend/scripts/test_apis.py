"""
API Test Script — all 5 external APIs
To test one at a time, comment out the calls at the bottom.

Run from project root:
    python backend/scripts/test_apis.py
"""

import json
import requests

# ── Constants (copied from company_enrichment.py) ───────────────────────────
SBIR_BASE_URL         = "https://api.www.sbir.gov/public/api"
NSF_AWARDS_URL        = "https://api.nsf.gov/services/v1/awards.json"
USASPENDING_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
CENSUS_GEOCODE_URL    = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_GEOGRAPHY_URL  = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
PATENTSVIEW_QUERY_URL = "https://api.patentsview.org/patents/query"

# ── Test inputs ──────────────────────────────────────────────────────────────
# Swap UEI once you have real ones from the DB
TEST_UEI          = "LA9LCVM7HMK5"
TEST_COMPANY_NAME = "Inventwood"
TEST_ADDRESS      = "5971 Jefferson Station Ct, Frederick, MD 21703"  # Inventwood real address from USASpending


# ── Helpers ──────────────────────────────────────────────────────────────────
def pretty(data):
    print(json.dumps(data, indent=2, default=str))

def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)

def subsection(title):
    print("\n" + "-" * 40)
    print(title)
    print("-" * 40)


# ── 1. SBIR ──────────────────────────────────────────────────────────────────
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


# ── 2. NSF Awards ────────────────────────────────────────────────────────────
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


# ── 3b. USASpending Recipient Profile ────────────────────────────────────────
def test_usaspending_recipient():
    section("3b. USASpending Recipient Profile (address lookup)")

    # Step 1 — search by UEI to get recipient_id
    subsection("Step 1 — search by UEI → get recipient_id")
    resp = requests.post(
        "https://api.usaspending.gov/api/v2/recipient/",
        json={"keyword": TEST_UEI, "page": 1, "limit": 5},
        timeout=15,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"Error: {resp.text[:300]}")
        return

    results = resp.json().get("results", [])
    if not results:
        print("No recipient found for this UEI")
        return

    recipient = results[0]
    recipient_id = recipient.get("id")
    print(f"Found: {recipient.get('name')} | recipient_id: {recipient_id}")

    # Step 2 — fetch full profile by recipient_id
    subsection("Step 2 — fetch full profile → address")
    resp2 = requests.get(
        f"https://api.usaspending.gov/api/v2/recipient/{recipient_id}/",
        timeout=15,
    )
    print(f"Status: {resp2.status_code}")
    if resp2.status_code == 200:
        data = resp2.json()
        loc = data.get("location", {})
        print(f"\nAddress: {loc.get('address_line1')}, {loc.get('city_name')}, {loc.get('state_code')} {loc.get('zip')}")
        print(f"Congressional district: {loc.get('congressional_code')}")
        print("\nFull profile:")
        pretty(data)
    else:
        print(f"Error: {resp2.text[:300]}")


# ── 3. USASpending ───────────────────────────────────────────────────────────
def test_usaspending():
    section("3. USASpending API")

    # Must send separate requests per award type group
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


# ── 4. Census Geocoder ───────────────────────────────────────────────────────
def test_census():
    section("4. Census Geocoder API")

    subsection("Step 1 — address → coordinates")
    resp = requests.get(
        CENSUS_GEOCODE_URL,
        params={"address": TEST_ADDRESS, "benchmark": "Public_AR_Current", "format": "json"},
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
        print("No match — try a different address")
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
        # Show congressional district
        for key, items in geos.items():
            if "congressional" in key.lower() and items:
                print(f"\n{key}:")
                pretty(items[0])
                break
    else:
        print(f"Error: {resp2.text[:300]}")


# ── 5. USPTO PatentsView ─────────────────────────────────────────────────────
def test_uspto():
    section("5. USPTO PatentsView API")

    # Try by UEI first, then by company name
    for label, query in [
        ("by UEI",          {"_eq": {"assignee_organization_uei": TEST_UEI}}),
        ("by company name", {"_contains": {"assignee_organization": TEST_COMPANY_NAME}}),
    ]:
        subsection(f"Query {label}")
        payload = {
            "q": query,
            "f": [
                "patent_number", "patent_title", "patent_date",
                "patent_type", "patent_kind", "patent_num_claims",
                "assignee_organization", "assignee_lastknown_city",
                "assignee_lastknown_state", "application_number",
            ],
            "o": {"per_page": 3, "page": 1},
        }
        resp = requests.post(PATENTSVIEW_QUERY_URL, json=payload, timeout=15)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            patents = data.get("patents", [])
            total = data.get("total_patent_count", "?")
            print(f"Patents returned: {len(patents)}  |  Total: {total}")
            if patents:
                print(f"Keys: {list(patents[0].keys())}")
                print("\nFirst patent:")
                pretty(patents[0])
                break  # found results, skip name query
            else:
                print("No results — trying next query...")
        else:
            print(f"Error: {resp.text[:300]}")


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Comment out whichever APIs you don't want to test right now
    # test_sbir()
    # test_nsf()
    # test_usaspending()
    # test_usaspending_recipient()
    test_census()
    # test_uspto()
