#!/usr/bin/env python3
"""
CLI Script to ingest WATN Salesforce Input Files into the Database.

Usage examples:
    python backend/scripts/ingest_watn_data.py \
        --cohorts "/path/to/WATN Input -- Cohorts-*.xlsx" \
        --companies "/path/to/WATN Input -- Companies-*.xlsx" \
        --participants "/path/to/WATN Input -- Participants-*.xlsx" \
        --funding "/path/to/WATN Input -- Funding-*.xlsx"

Or point directly to a directory:
    python backend/scripts/ingest_watn_data.py --dir ~/Downloads
"""

import argparse
import glob
import os
import sys
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.core.db.base import SessionLocal, engine
from app.core.db.init_db import *
from app.main import ensure_core_auth_schema, ensure_event_schema, ensure_pipeline_schema, ensure_reference_data
from app.services.data_ingestion import ingest_all_watn_files


def find_file(directory: str, pattern: str) -> str:
    matches = glob.glob(os.path.join(os.path.expanduser(directory), pattern))
    if not matches:
        return ""
    # Return the newest file if multiple
    matches.sort(key=os.path.getmtime, reverse=True)
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description="Ingest WATN Salesforce Input Files into PostgreSQL")
    parser.add_argument("--cohorts", help="Path to Cohorts Excel or CSV file")
    parser.add_argument("--companies", help="Path to Companies Excel or CSV file")
    parser.add_argument("--participants", help="Path to Participants Excel or CSV file")
    parser.add_argument("--funding", help="Path to Funding Excel or CSV file")
    parser.add_argument("--dir", help="Directory to search for WATN Input files (e.g. ~/Downloads)")

    args = parser.parse_args()

    cohorts_path = args.cohorts
    companies_path = args.companies
    participants_path = args.participants
    funding_path = args.funding

    if args.dir:
        dir_path = os.path.expanduser(args.dir)
        if not cohorts_path:
            cohorts_path = find_file(dir_path, "*Cohorts*.xlsx") or find_file(dir_path, "*Cohorts*.csv")
        if not companies_path:
            companies_path = find_file(dir_path, "*Companies*.xlsx") or find_file(dir_path, "*Companies*.csv")
        if not participants_path:
            participants_path = find_file(dir_path, "*Participants*.xlsx") or find_file(dir_path, "*Participants*.csv")
        if not funding_path:
            funding_path = find_file(dir_path, "*Funding*.xlsx") or find_file(dir_path, "*Funding*.csv")

    print("=" * 60)
    print("🚀 WATN Data Ingestion Pipeline")
    print("=" * 60)
    print(f"Cohorts file:      {cohorts_path or 'Not provided'}")
    print(f"Companies file:    {companies_path or 'Not provided'}")
    print(f"Participants file: {participants_path or 'Not provided'}")
    print(f"Funding file:      {funding_path or 'Not provided'}")
    print("=" * 60)

    if not any([cohorts_path, companies_path, participants_path, funding_path]):
        print("❌ Error: No input files provided or found. Please specify file paths.")
        sys.exit(1)

    print("\n📦 Ensuring database schema & reference data...")
    Base.metadata.create_all(bind=engine)
    ensure_core_auth_schema()
    ensure_reference_data()
    ensure_event_schema()
    ensure_pipeline_schema()

    db = SessionLocal()
    try:
        print("\n⏳ Ingesting data...")
        result = ingest_all_watn_files(
            db=db,
            cohorts_source=cohorts_path,
            companies_source=companies_path,
            participants_source=participants_path,
            funding_source=funding_path,
        )

        print("\n✅ Ingestion Results:")
        print("-" * 60)

        if "cohorts" in result and result["cohorts"]:
            c = result["cohorts"]
            print(f"📁 Cohorts:      Total={c.get('total_records', 0)} | "
                  f"Created={c.get('cohorts_created', 0)} | "
                  f"Updated={c.get('cohorts_updated', 0)} | "
                  f"Events Created={c.get('events_created', 0)}")

        if "companies" in result and result["companies"]:
            cmp = result["companies"]
            print(f"🏢 Companies:    Total={cmp.get('total_records', 0)} | "
                  f"Created={cmp.get('companies_created', 0)} | "
                  f"Updated/Merged={cmp.get('companies_updated', 0)}")

        if "participants" in result and result["participants"]:
            p = result["participants"]
            print(f"👥 Participants: Total={p.get('total_records', 0)} | "
                  f"Created={p.get('participants_created', 0)} | "
                  f"Updated={p.get('participants_updated', 0)} | "
                  f"Entries={p.get('entries_created', 0)} | "
                  f"Company Matches={p.get('matches_created', 0)} | "
                  f"Teams={p.get('teams_created', 0)}")

        if "funding" in result and result["funding"]:
            f = result["funding"]
            print(f"💰 Funding:      Total={f.get('total_records', 0)} | "
                  f"Created={f.get('funding_created', 0)} | "
                  f"Skipped Existing={f.get('funding_skipped_existing', 0)}")

        all_errors = []
        for stage in ("cohorts", "companies", "participants", "funding"):
            if stage in result and result[stage].get("errors"):
                all_errors.extend(result[stage]["errors"][:5])

        if all_errors:
            print("\n⚠️ Warnings / Errors (first 5):")
            for err in all_errors[:5]:
                print(f"  - {err}")

        print("\n✨ Done! All data has been successfully pushed to the database.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
