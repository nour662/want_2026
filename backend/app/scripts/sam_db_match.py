import csv
import logging
import os
import re
import sys
from argparse import ArgumentParser
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db.base import SessionLocal
from app.models.sam_entity_public_v2 import SamEntityPublicV2


SUFFIXES = (
    " inc",
    " llc",
    " ltd",
    " corp",
    " co",
    " company",
    " pllc",
    " lp",
    " llp",
    " incorporated",
    " corporation",
)


def normalize_company_name(name: str) -> str:
    name = re.sub(r"[^\w\s]", "", name.lower().strip())
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def score_name(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio() * 100.0


def read_input_rows(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader]


def extract_company(row: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    company = row.get("Company") or row.get("company")
    uei = row.get("UEI") or row.get("uei")
    company = company.strip() if company else None
    uei = uei.strip() if uei else None
    return company, uei


def fetch_by_uei(db: Session, ueis: Iterable[str]) -> Dict[str, SamEntityPublicV2]:
    uei_list = [uei for uei in ueis if uei]
    if not uei_list:
        return {}
    results = (
        db.query(SamEntityPublicV2)
        .filter(SamEntityPublicV2.uei.in_(uei_list))
        .all()
    )
    return {record.uei: record for record in results if record.uei}


def find_best_match(
    db: Session,
    company: str,
    max_candidates: int,
) -> Tuple[Optional[SamEntityPublicV2], float]:
    normalized = normalize_company_name(company)
    if not normalized:
        return None, 0.0

    tokens = normalized.split()
    search_term = " ".join(tokens[:2]) if len(tokens) >= 2 else tokens[0]

    candidates = (
        db.query(SamEntityPublicV2)
        .filter(SamEntityPublicV2.legal_business_name.ilike(f"%{search_term}%"))
        .limit(max_candidates)
        .all()
    )

    best_record = None
    best_score = 0.0
    for candidate in candidates:
        candidate_name = normalize_company_name(candidate.legal_business_name or "")
        score = score_name(normalized, candidate_name)
        if score > best_score:
            best_score = score
            best_record = candidate

    return best_record, best_score


def write_output(path: str, rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(input_path: str, output_path: str, threshold: float, max_candidates: int) -> None:
    logging.info("Reading input file: %s", input_path)
    input_rows = read_input_rows(input_path)

    db = SessionLocal()
    try:
        uei_values = [extract_company(row)[1] for row in input_rows]
        uei_map = fetch_by_uei(db, uei_values)

        output_rows: List[Dict[str, str]] = []
        for row in input_rows:
            company, uei = extract_company(row)
            match_type = ""
            score = 0.0
            record = None

            if uei and uei in uei_map:
                record = uei_map[uei]
                match_type = "uei"
                score = 100.0
            elif company:
                record, score = find_best_match(db, company, max_candidates)
                match_type = "name" if record else ""

            if record and score < threshold:
                record = None
                match_type = ""

            output_rows.append(
                {
                    **row,
                    "match_type": match_type,
                    "match_score": f"{score:.2f}" if record else "",
                    "sam_uei": record.uei if record else "",
                    "sam_legal_business_name": record.legal_business_name if record else "",
                    "sam_cage_code": record.cage_code if record else "",
                    "sam_address_line1": record.address_line1 if record else "",
                    "sam_city": record.city if record else "",
                    "sam_state": record.state if record else "",
                    "sam_zip_code": record.zip_code if record else "",
                    "sam_website": record.website if record else "",
                }
            )

        write_output(output_path, output_rows)
        logging.info("Wrote %s rows to %s", len(output_rows), output_path)
    finally:
        db.close()


def parse_args(arglist: List[str]) -> ArgumentParser:
    parser = ArgumentParser()
    parser.add_argument("--input_path", "-i", required=True, help="Path to input CSV")
    parser.add_argument("--output_path", "-o", required=True, help="Path to output CSV")
    parser.add_argument(
        "--threshold",
        "-t",
        type=float,
        default=80.0,
        help="Match score threshold (0-100)",
    )
    parser.add_argument(
        "--max_candidates",
        "-m",
        type=int,
        default=50,
        help="Max SAM candidates to score per company",
    )
    return parser.parse_args(arglist)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    main(args.input_path, args.output_path, args.threshold, args.max_candidates)
