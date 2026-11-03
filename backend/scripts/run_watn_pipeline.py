#!/usr/bin/env python3
"""
Run the unified WATN pipeline over a participant list.

Examples
--------
    # Foreground, 8 companies at a time
    python scripts/run_watn_pipeline.py -i inputs/umd-report-2026.csv --tag umd-report-2026

    # Background; progress in <output>/progress.json, log in <output>/pipeline.log
    nohup python scripts/run_watn_pipeline.py -i inputs/umd-report-2026.csv \
        --tag umd-report-2026 --workers 12 --baseline-funding inputs/baseline_funding.csv \
        > /dev/null 2>&1 &

Re-running with the same --output-dir resumes: finished companies are skipped.
"""

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.services.watn_pipeline import ALL_SOURCES, PipelineConfig, load_participants, run_pipeline  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--input", action="append", required=True, type=Path,
                        help="Participant CSV (repeat to combine files, e.g. a UEI lookup sheet)")
    parser.add_argument("-o", "--output-dir", type=Path, help="Output folder (default outputs/<tag>_<date>)")
    parser.add_argument("--tag", default="watn", help="Run name used for the default output folder")
    parser.add_argument("-w", "--workers", type=int, default=int(os.getenv("WATN_WORKERS", "8")),
                        help="Companies processed in parallel")
    parser.add_argument("--sources", default=",".join(ALL_SOURCES),
                        help=f"Comma-separated subset of {','.join(ALL_SOURCES)}")
    parser.add_argument("--baseline-funding", type=Path,
                        help="Existing funding export; marks records Existing vs New and assigns new IDs")
    parser.add_argument("--tedco-seed", type=Path, help="relevant_articles.csv from the old tedco_scraper")
    parser.add_argument("--no-tedco-crawl", action="store_true", help="Use cached/seed TEDCO articles only")
    parser.add_argument("--sbir-csv", type=Path, help="Local SBIR award_data.csv instead of downloading")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(os.getenv("WATN_CACHE_DIR", BACKEND_DIR / ".cache" / "watn")))
    parser.add_argument("--no-resume", action="store_true", help="Reprocess companies already checkpointed")
    parser.add_argument("--limit", type=int, help="Only process the first N companies (testing)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir or Path("outputs") / f"{args.tag}_{date.today().isoformat()}"
    output_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(output_dir / "pipeline.log"), logging.StreamHandler()],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    unknown = sorted(set(sources) - set(ALL_SOURCES))
    if unknown:
        logging.error("Unknown sources: %s", unknown)
        return 2

    companies = load_participants(args.input)
    if args.limit:
        companies = companies[: args.limit]
    logging.info("Loaded %d companies from %s", len(companies), ", ".join(map(str, args.input)))

    config = PipelineConfig(
        output_dir=output_dir,
        cache_dir=args.cache_dir,
        workers=args.workers,
        sources=sources,
        resume=not args.no_resume,
        tedco_seed=args.tedco_seed,
        tedco_crawl=not args.no_tedco_crawl,
        sbir_csv=args.sbir_csv,
        baseline_funding=args.baseline_funding,
    )
    report = run_pipeline(companies, config)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
