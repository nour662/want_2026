"""
WATN pipeline runner: one process, many companies in parallel.

    prepare shared data (SAM lookup, SBIR bulk index, TEDCO archive)   once
    for each company, in a thread pool:                                parallel
        resolve identity  -> UEI + scored evidence
        query sources     -> SAM, SBIR, USAspending, NSF, TEDCO (concurrently)
        classify, merge, evaluate funding; merge company profile
        checkpoint result to records/<key>.json
    write outputs (CSV + review queue + run report)                    once

Checkpoints make the run resumable: restarting with the same output folder
skips companies already completed, so a crash or redeploy loses nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.services.watn_pipeline import classify, merge
from app.services.watn_pipeline.evaluate import evaluate_funding
from app.services.watn_pipeline.matching import (
    CONFIRMED,
    PROBABLE,
    extract_domain,
    is_accepted,
    name_similarity,
    normalize_id,
    score_entity,
    tier_for,
)
from app.services.watn_pipeline.models import CompanyInput, CompanyResult, FieldValue, MatchEvidence, SourceResult
from app.services.watn_pipeline.sources import nsf, sbir, tedco, usaspending
from app.services.watn_pipeline.sources.sam import SamLookup

logger = logging.getLogger(__name__)

ALL_SOURCES = ("sam", "sbir", "usaspending", "nsf", "tedco")


@dataclass
class PipelineConfig:
    output_dir: Path
    cache_dir: Path
    workers: int = 8
    sources: Sequence[str] = ALL_SOURCES
    resume: bool = True
    tedco_seed: Optional[Path] = None
    tedco_crawl: bool = True
    sbir_csv: Optional[Path] = None
    sbir_max_age_days: int = 7
    baseline_funding: Optional[Path] = None
    sam_db_url: Optional[str] = None


@dataclass
class _Context:
    config: PipelineConfig
    sam: Optional[SamLookup] = None
    sbir: Optional[sbir.SbirIndex] = None
    tedco: Optional[tedco.TedcoArchive] = None
    unavailable: Dict[str, str] = field(default_factory=dict)


ProgressCallback = Callable[[Dict[str, Any]], None]


# ── Public entry point ─────────────────────────────────────────────────


def run_pipeline(
    companies: List[CompanyInput],
    config: PipelineConfig,
    progress: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    from app.services.watn_pipeline import outputs

    started = time.monotonic()
    out = Path(config.output_dir)
    records_dir = out / "records"
    records_dir.mkdir(parents=True, exist_ok=True)

    ctx = _prepare(config)
    results: Dict[str, CompanyResult] = {}
    todo: List[CompanyInput] = []
    for company in companies:
        cached = _load_checkpoint(records_dir, company) if config.resume else None
        if cached is not None:
            results[company.key] = cached
        else:
            todo.append(company)

    state = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total": len(companies),
        "completed": len(results),
        "resumed": len(results),
        "failed": 0,
        "sources": list(config.sources),
        "unavailable_sources": ctx.unavailable,
    }
    lock = threading.Lock()
    _write_progress(out, state, progress)
    logger.info("Processing %d companies (%d resumed) with %d workers", len(todo), len(results), config.workers)

    with ThreadPoolExecutor(max_workers=max(1, config.workers), thread_name_prefix="watn") as pool:
        futures = {pool.submit(process_company, company, ctx): company for company in todo}
        for future in as_completed(futures):
            company = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # process_company already guards; this is belt and braces
                logger.exception("Worker crashed for %s", company.name)
                result = CompanyResult(company=company, status="failed", error=str(exc))
            results[company.key] = result
            if result.status in ("completed", "partial"):
                _save_checkpoint(records_dir, result)
            with lock:
                state["completed"] += 1
                state["failed"] += result.status == "failed"
                state["partial"] = state.get("partial", 0) + (result.status == "partial")
                if state["completed"] % 10 == 0 or state["completed"] == state["total"]:
                    _write_progress(out, state, progress)

    ordered = [results[c.key] for c in companies if c.key in results]
    report = outputs.write_all(ordered, out, config, ctx.unavailable)
    state.update(status="completed", finished_at=datetime.now(timezone.utc).isoformat(),
                 runtime_seconds=round(time.monotonic() - started, 1))
    _write_progress(out, state, progress)
    report["runtime_seconds"] = state["runtime_seconds"]
    return report


# ── Shared resources ───────────────────────────────────────────────────


def _prepare(config: PipelineConfig) -> _Context:
    ctx = _Context(config=config)
    cache = Path(config.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    jobs: Dict[str, Callable[[], None]] = {}

    if "sam" in config.sources:
        def _sam() -> None:
            ctx.sam = SamLookup(db_url=config.sam_db_url)
            if not ctx.sam.available:
                ctx.unavailable["sam"] = ctx.sam.describe()
        jobs["sam"] = _sam

    if "sbir" in config.sources:
        def _sbir() -> None:
            ctx.sbir = sbir.SbirIndex(cache, config.sbir_max_age_days, config.sbir_csv)
            if not ctx.sbir.ensure_loaded():
                ctx.unavailable["sbir"] = ctx.sbir.error or "failed to load"
        jobs["sbir"] = _sbir

    if "tedco" in config.sources:
        def _tedco() -> None:
            ctx.tedco = tedco.TedcoArchive(cache, seed_csv=config.tedco_seed)
            ctx.tedco.refresh(crawl=config.tedco_crawl)
            if not ctx.tedco.articles:
                ctx.unavailable["tedco"] = ctx.tedco.crawl_error or "no articles"
            elif ctx.tedco.crawl_error:
                ctx.unavailable["tedco_crawl"] = (
                    f"{ctx.tedco.crawl_error}; used {len(ctx.tedco.articles)} cached articles"
                )
        jobs["tedco"] = _tedco

    with ThreadPoolExecutor(max_workers=3) as pool:
        for name, future in {name: pool.submit(job) for name, job in jobs.items()}.items():
            try:
                future.result()
            except Exception as exc:
                logger.exception("Preparing %s failed", name)
                ctx.unavailable[name] = str(exc)
    for name, reason in ctx.unavailable.items():
        logger.warning("Source %s unavailable for this run: %s", name, reason)
    return ctx


# ── Per-company work ───────────────────────────────────────────────────


def process_company(company: CompanyInput, ctx: _Context) -> CompanyResult:
    result = CompanyResult(company=company)
    try:
        _resolve_identity(company, ctx, result)
        source_results = _query_sources(company, ctx, result)
        _assemble(company, result, source_results)
        transient = [n for n, st in result.source_status.items() if st.startswith("error") and "unavailable" not in st]
        if transient:
            result.status = "partial"  # checkpointed, but retried on resume
            result.error = "source errors: " + ", ".join(sorted(transient))
    except Exception as exc:
        logger.exception("Pipeline failed for %s", company.name)
        result.status = "failed"
        result.error = str(exc)
    return result


def _resolve_identity(company: CompanyInput, ctx: _Context, result: CompanyResult) -> None:
    # 1. Input UEI, sanity-checked against the registered name.
    if company.uei:
        result.identity = _check_supplied_id(company, ctx, company.uei, 100.0, "uei", "UEI supplied in input", result)
        result.resolved_uei = company.uei
        return

    # 2. DUNS -> UEI through SBIR awards, with the same name check.
    if company.duns and ctx.sbir and not ctx.sbir.error:
        uei = ctx.sbir.uei_for_duns(company.duns)
        if uei:
            result.identity = _check_supplied_id(company, ctx, uei, 98.0, "duns", "DUNS maps to UEI in SBIR awards", result)
            result.resolved_uei = uei
            return

    # 3. Name search across SAM and SBIR firms, scored.
    candidates: List[Dict[str, Any]] = []
    if ctx.sam and ctx.sam.available:
        for ent in ctx.sam.search_name(company.name):
            candidates.append({"origin": "sam", "uei": ent.get("uei"), "names": [ent.get("legal_name"), ent.get("dba_name")],
                               "website": ent.get("website"), "state": ent.get("state"), "contacts": ent.get("contacts") or [],
                               "duns": None})
    if ctx.sbir and not ctx.sbir.error:
        for row in ctx.sbir.firms_named(company.name):
            candidates.append({"origin": "sbir", "uei": normalize_id(row.get("UEI")), "names": [row.get("Company")],
                               "website": row.get("Company Website"), "state": row.get("State"),
                               "contacts": [c for c in (row.get("PI Name"), row.get("Contact Name")) if c], "duns": row.get("Duns")})

    scored = []
    for cand in candidates:
        if not cand["uei"]:
            continue
        ev = score_entity(
            input_name=company.name, candidate_names=cand["names"], input_duns=company.duns,
            candidate_duns=cand["duns"], input_website=company.website, candidate_website=cand["website"],
            input_state=company.state, candidate_state=cand["state"], input_contacts=company.contacts,
            candidate_contacts=cand["contacts"],
        )
        ev.method = f"{cand['origin']}:{ev.method}"
        scored.append((ev, cand))
    scored.sort(key=lambda pair: pair[0].score, reverse=True)

    # Collapse candidates pointing at the same UEI; keep the best evidence per UEI.
    best_by_uei: Dict[str, tuple] = {}
    for ev, cand in scored:
        best_by_uei.setdefault(cand["uei"], (ev, cand))
    ranked = list(best_by_uei.values())

    if not ranked:
        result.identity = MatchEvidence(0.0, "rejected", "none", ["no SAM/SBIR candidates by name"])
        return

    top_ev, top = ranked[0]
    if len(ranked) > 1 and is_accepted(ranked[1][0]) and top_ev.score - ranked[1][0].score < 5:
        top_ev.reasons.append(f"ambiguous: also matches UEI {ranked[1][1]['uei']} ({ranked[1][0].score:.0f})")
        top_ev.score = min(top_ev.score, PROBABLE - 1)
        top_ev.tier = tier_for(top_ev.score)

    result.identity = top_ev
    if is_accepted(top_ev):
        result.resolved_uei = top["uei"]
    elif top_ev.tier == "review":
        result.review_reasons.append(
            f"possible UEI {top['uei']} ('{top['names'][0]}') scored {top_ev.score:.0f}: " + "; ".join(top_ev.reasons)
        )


MISMATCHED_ID_SCORE = 75.0  # review tier: data is pulled but nothing is published


def _check_supplied_id(
    company: CompanyInput, ctx: _Context, uei: str, score: float, method: str, reason: str, result: CompanyResult
) -> MatchEvidence:
    """Trust a supplied identifier only if the entity it points to looks like this company."""
    entity = ctx.sam.by_uei(uei) if ctx.sam and ctx.sam.available else None
    sbir_rows = ctx.sbir.by_uei.get(uei, []) if ctx.sbir and not ctx.sbir.error else []
    names = [n for n in ((entity or {}).get("legal_name"), (entity or {}).get("dba_name")) if n]
    names += sorted({r.get("Company") for r in sbir_rows if r.get("Company")})
    websites = [(entity or {}).get("website")] + [r.get("Company Website") for r in sbir_rows[:5]]
    if not names:
        return MatchEvidence(score, tier_for(score), method, [reason, "no registered name to cross-check"])
    best = max(names, key=lambda n: name_similarity(company.name, n))
    similarity = name_similarity(company.name, best)
    if similarity >= 50 or any(_same_domain(company.website, w) for w in websites):
        return MatchEvidence(score, tier_for(score), method, [reason, f"registered name '{best}' ({similarity:.0f})"])
    detail = f"{method.upper()} points to '{best}' (name {similarity:.0f}), not '{company.name}'; renamed or wrong ID?"
    result.review_reasons.append(detail)
    return MatchEvidence(MISMATCHED_ID_SCORE, tier_for(MISMATCHED_ID_SCORE), method, [reason, detail])


def _same_domain(a: Optional[str], b: Optional[str]) -> bool:
    return bool(extract_domain(a)) and extract_domain(a) == extract_domain(b)


def _query_sources(company: CompanyInput, ctx: _Context, result: CompanyResult) -> List[SourceResult]:
    uei = result.resolved_uei
    tasks: Dict[str, Callable[[], SourceResult]] = {}
    sources = ctx.config.sources

    def needs_uei(name: str, fn: Callable[[], SourceResult]) -> None:
        if name in ctx.unavailable:
            tasks[name] = lambda: SourceResult(name, "error", f"unavailable: {ctx.unavailable[name]}")
        elif not uei:
            tasks[name] = lambda: SourceResult(name, "skipped", "no confirmed UEI")
        else:
            tasks[name] = fn

    if "sam" in sources:
        needs_uei("sam", lambda: _sam_source(ctx, uei))
    if "sbir" in sources:
        if "sbir" in ctx.unavailable:
            tasks["sbir"] = lambda: SourceResult("sbir", "error", f"unavailable: {ctx.unavailable['sbir']}")
        else:
            tasks["sbir"] = lambda: _sbir_source(ctx, uei, company.duns)
    if "usaspending" in sources:
        needs_uei("usaspending", lambda: _usas_source(uei))
    if "nsf" in sources:
        needs_uei("nsf", lambda: SourceResult("nsf", funding=nsf.fetch(uei)))
    if "tedco" in sources:
        if "tedco" in ctx.unavailable:
            tasks["tedco"] = lambda: SourceResult("tedco", "error", f"unavailable: {ctx.unavailable['tedco']}")
        else:
            tasks["tedco"] = lambda: SourceResult("tedco", funding=ctx.tedco.find_funding(company))

    out: List[SourceResult] = []
    with ThreadPoolExecutor(max_workers=len(tasks) or 1) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                res = future.result()
                if res.status == "ok" and not res.funding and not res.profile:
                    res.status = "no_data"
            except Exception as exc:
                logger.warning("%s failed for %s: %s", name, company.name, exc)
                res = SourceResult(name, "error", str(exc)[:300])
            result.source_status[name] = res.status if not res.message else f"{res.status}: {res.message}"
            out.append(res)
    return out


def _sam_source(ctx: _Context, uei: str) -> SourceResult:
    entity = ctx.sam.by_uei(uei)
    return SourceResult("sam", profile=entity or {})


def _sbir_source(ctx: _Context, uei: Optional[str], duns: Optional[str]) -> SourceResult:
    rows = ctx.sbir.awards(uei, duns)
    funding = [sbir.to_funding(r) for r in rows]
    if not uei:
        for rec in funding:  # retrieved by DUNS alone
            rec.confidence, rec.match_method = 98.0, "duns"
    return SourceResult("sbir", profile=sbir.firm_profile(rows), funding=funding)


def _usas_source(uei: str) -> SourceResult:
    funding, profile = usaspending.fetch(uei)
    return SourceResult("usaspending", profile=profile, funding=funding)


def _assemble(company: CompanyInput, result: CompanyResult, source_results: List[SourceResult]) -> None:
    identity = result.identity
    funding = []
    profiles: Dict[str, Dict[str, Any]] = {
        "input": {"uei": company.uei, "duns": company.duns, "website": company.website,
                  "city": company.city, "state": company.state, "university": company.university}
    }
    for res in source_results:
        if res.profile:
            profiles[res.source] = res.profile
        for rec in res.funding:
            if rec.source != "tedco" and not rec.match_method:
                rec.confidence = identity.score if identity else 0.0
                rec.match_method = identity.method if identity else ""
            funding.append(classify.classify(rec))

    before = len(funding)
    merged = merge.merge_funding(funding)
    result.duplicates_merged = before - len(merged)
    for rec in merged:
        classify.classify(rec)
        evaluate_funding(rec, company.cohort_date)
    result.funding = merged

    result.profile = merge.merge_profile(profiles)
    if result.resolved_uei:
        result.profile["uei"] = FieldValue(result.resolved_uei, (identity.method if identity else "input"))
    result.review_reasons.extend(merge.profile_conflicts({k: v for k, v in profiles.items() if k != "input"}))
    if identity and identity.score < CONFIRMED and is_accepted(identity) and result.resolved_uei and not company.uei:
        result.review_reasons.append(f"UEI resolved by name ({identity.score:.0f}); spot-check: " + "; ".join(identity.reasons))


# ── Checkpoints / progress ─────────────────────────────────────────────


def _checkpoint_path(records_dir: Path, company: CompanyInput) -> Path:
    digest = hashlib.sha1(company.key.encode("utf-8")).hexdigest()[:16]
    return records_dir / f"{digest}.json"


def _load_checkpoint(records_dir: Path, company: CompanyInput) -> Optional[CompanyResult]:
    path = _checkpoint_path(records_dir, company)
    if not path.exists():
        return None
    try:
        cached = CompanyResult.from_dict(json.loads(path.read_text()))
    except Exception:
        logger.warning("Ignoring unreadable checkpoint %s", path)
        return None
    cached.company = company  # input may have been edited (new cohort date, contacts)
    return cached if cached.status == "completed" else None


def _save_checkpoint(records_dir: Path, result: CompanyResult) -> None:
    path = _checkpoint_path(records_dir, result.company)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result.to_dict(), default=str))
    os.replace(tmp, path)


def _write_progress(out: Path, state: Dict[str, Any], progress: Optional[ProgressCallback]) -> None:
    tmp = out / "progress.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, out / "progress.json")
    if progress:
        try:
            progress(dict(state))
        except Exception:
            logger.warning("Progress callback failed", exc_info=True)
