# WATN pipeline

One process that replaces WATN-Project's `run.sh` chain (DUNS search → SAM →
match → SBIR Selenium → USAS → NSF → batch merges → cleaning → merging). It
runs many companies in parallel, can be left running in the background, and
resumes where it stopped. TEDCO press releases are a source. PitchBook is not
used for now.

```
participants.csv ──► load_participants ──► one CompanyInput per company
                                              │
          prepare once:  SAM lookup · SBIR bulk index · TEDCO archive
                                              │
          per company (thread pool, --workers):
             1. identity   input UEI (sanity-checked) → DUNS→UEI → scored name match
             2. sources    SAM · SBIR · USAspending · NSF · TEDCO · USPTO   (concurrently)
             3. classify   funding source + stage
             4. merge      cross-source dedupe with provenance
             5. evaluate   confidence, missing fields, post-I-Corps flag; score patents
             6. checkpoint records/<hash>.json
                                              │
          outputs: company_info.csv · funding_info.csv · new_funding_records.csv
                   patents.csv · review_queue.csv · run_report.json · progress.json
```

## Running

```bash
cd backend
pip install -r requirements.txt

python scripts/run_watn_pipeline.py \
  -i path/to/umd-report-2026.csv -i path/to/uei_lookups.csv \
  --tag umd-report-2026 --workers 12 \
  --baseline-funding path/to/baseline_funding.csv \
  --tedco-seed ../../tedco_scraper/relevant_articles.csv
```

In the background: prefix with `nohup … > /dev/null 2>&1 &`. Watch
`outputs/<tag>_<date>/progress.json` and `pipeline.log`. Run the same command
again to resume; add `--no-resume` to reprocess everything.

Each company is checkpointed when it finishes. Companies where a source
errored (timeouts, throttling) are saved as `partial` and retried on the next
run, so re-running the same command fills those gaps without redoing the rest.
Requests are rate-limited per host (`http.py`). USAspending drops connections
from clients that go much faster than about one request per second, so more
workers mostly speeds up the SBIR/TEDCO/matching work, not USAspending.

Environment:

| Variable | Used for |
| --- | --- |
| `SAM_DB_URL` (or `DATABASE_URL`) | Local `sam_entity_public_v2` extract — preferred SAM source |
| `SAM_API_KEY` | SAM.gov Entity API fallback (rate-limited) |
| `USPTO_API_KEY` | USPTO Open Data Portal patent search (free key from data.uspto.gov) |
| `WATN_CACHE_DIR` | Where the SBIR bulk file and TEDCO article cache live (default `backend/.cache/watn`) |
| `WATN_WORKERS` | Default parallelism |

Without either SAM setting the SAM source is reported as unavailable and
identity resolution falls back to SBIR firm records. Without `USPTO_API_KEY`
the patent source is reported as unavailable. The CLI reads keys from
`backend/.env` or `backend/scripts/.env` as well as the environment.

## Sources

| Source | Lookup | Notes |
| --- | --- | --- |
| SAM | UEI, name | Company profile (legal name, address, CAGE, district, incorporation). |
| SBIR | UEI, DUNS, name | SBIR.gov's public bulk award file, refreshed weekly. The SBIR JSON API answers 403, so this replaces the Selenium scraper. Also maps DUNS → UEI. |
| USAspending | UEI | Contracts (A–D) and grants (02–05); loans excluded. Each result's recipient UEI is re-checked. |
| NSF | UEI | Recipient UEI re-checked. |
| USPTO | company name (applicant or assignee) | Open Data Portal application search; granted and pending applications. Only the company is searched, never inventors. Also searches the registered SAM/SBIR names once the UEI is confirmed. |
| TEDCO | name, participant names | Crawls tedcomd.com articles once per run (incremental cache); can import the old `tedco_scraper` CSV. Finds the amount and TEDCO fund nearest each mention. |

TEDCO's site now sits behind Cloudflare bot protection and answers 403 to
scripted clients (as of Nov 2026). The pipeline does not try to get around
that. When the crawl is blocked it uses the cached articles plus
`--tedco-seed`, and `run_report.json` says so under `unavailable_sources`.
To get new articles, ask TEDCO to allowlist the pipeline or provide a feed or
export, then drop it in as a seed CSV (`Title, Release Date, URL, Content`,
plus optional `Related Companies` and `Related Funds`).

## How records are evaluated

Identity (company ↔ registered entity) is scored 0–100 and tiered:
**confirmed ≥ 90**, **probable ≥ 80** (both accepted), **review ≥ 65**,
rejected below. Scoring uses name similarity plus website domain, state and
participant/point-of-contact matches. Short or generic names ("Hale",
"Stacks") cannot pass on name alone. An input UEI whose registered name
does not resemble the company is still used, but flagged.

Each funding record carries a confidence (the identity score for UEI-based
sources, the mention score for TEDCO) and review flags. Records only reach
`funding_info.csv` when they are confident and have a positive amount and an
award date. The rest go to `review_queue.csv` with the reason.

Patents are scored the same way as identities: the applicant or assignee
name against the company name, plus state, plus a bonus when a participant
is one of the inventors. Confirmed and probable matches go to `patents.csv`
(and the `Patents` / `Granted Patents` counts in `company_info.csv`); review
matches go to `review_queue.csv` as `patent`; weaker ones are dropped. Each
patent records whether it was filed on or after the company's earliest
cohort date.

## How records are merged

Funding records are clustered across sources when they share a normalised
award number (`W911QX-24-P-0155` = `W911QX24P0155`), or when amount (±2%) and
award date (±45 days) agree across different sources. Two awards from the
same source with different numbers are never merged. Each field is taken from
the most reliable source for that field (amount/date from USAspending,
program/phase from SBIR, links from USAspending/NSF). The merged record lists
every contributing source, and amount disagreements over 10% are flagged.

Company fields use per-field source priority (SAM first for registration
data, SBIR for employees and ownership flags). `Field Sources` in
`company_info.csv` says where each value came from, and conflicts on state,
website or UEI are flagged.

With `--baseline-funding`, each published record is matched to the existing
export (source URL, award number, then amount + date). Matches keep their
Funding ID. Others are marked `New Record`, get the next `FID-####`, and are
also written to `new_funding_records.csv`.

## Salesforce / AWS (later)

The pipeline does not depend on the database or on CSV-only input. A
Salesforce loader can build `CompanyInput` objects from Accounts. A Salesforce
sink can consume the same rows `outputs.py` writes, and the baseline matcher
can decide between update and create. The CLI runs as-is in a container, for
example as an ECS task or AWS Batch job with `WATN_CACHE_DIR` on EFS/S3-synced
storage.
