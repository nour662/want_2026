"""
Data pipeline API endpoints.

POST  /pipeline/run          — trigger a full or targeted pipeline run
GET   /pipeline/jobs/{job_id} — poll job status
GET   /pipeline/jobs          — list recent jobs
POST  /pipeline/company/{company_id}/run — run pipeline for a single company (sync)
"""

import threading
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.models.pipeline_jobs import PipelineJob
from app.services.data_pipeline import orchestrator

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

VALID_SOURCES = {"sbir", "nsf", "usaspending", "census", "uspto"}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class PipelineRunRequest(BaseModel):
    company_ids: Optional[List[str]] = Field(
        default=None,
        description="List of company UUIDs to process. Omit to run across all companies.",
    )
    sources: Optional[List[str]] = Field(
        default=None,
        description=(
            "Enrichment sources to pull from. "
            "Valid values: sbir, nsf, usaspending, census, uspto. "
            "Defaults to all sources."
        ),
    )
    mode: str = Field(
        default="full",
        description="Pipeline mode: 'full' or 'incremental'.",
        pattern="^(full|incremental)$",
    )


class PipelineJobResponse(BaseModel):
    job_id: str
    status: str
    mode: Optional[str]
    total_companies: Optional[int]
    processed_companies: int
    failed_companies: int
    requested_sources: Optional[List[str]]
    results_summary: Optional[dict]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class SingleCompanyRunResponse(BaseModel):
    company_id: str
    status: str
    uei_resolved: bool
    sam_updated: bool
    enrichment: dict
    funding_classified: int
    errors: List[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/run", response_model=PipelineJobResponse, status_code=202)
def trigger_pipeline(
    request: PipelineRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger a background data pipeline run.

    The job is created immediately and the pipeline runs asynchronously.
    Poll ``GET /pipeline/jobs/{job_id}`` to track progress.
    """
    # Validate sources
    sources = _validate_sources(request.sources)

    # Validate company_ids if provided
    if request.company_ids:
        for cid in request.company_ids:
            try:
                UUID(cid)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid company_id format: {cid}",
                )

    job = PipelineJob(
        status="pending",
        mode=request.mode,
        requested_sources=sources,
        company_ids=request.company_ids,
        processed_companies=0,
        failed_companies=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(
        orchestrator.run_pipeline_batch,
        job.id,
        request.company_ids,
        sources,
    )

    return _job_to_response(job)


@router.post(
    "/company/{company_id}/run",
    response_model=SingleCompanyRunResponse,
)
def run_pipeline_for_company(
    company_id: UUID,
    sources: Optional[List[str]] = Query(default=None),
    db: Session = Depends(get_db),
):
    """
    Run the pipeline synchronously for a single company and return results.

    Useful for on-demand enrichment and testing.
    """
    validated_sources = _validate_sources(sources)
    result = orchestrator.run_pipeline_for_company(company_id, validated_sources, db)
    return result


@router.get("/jobs/{job_id}", response_model=PipelineJobResponse)
def get_pipeline_job(job_id: UUID, db: Session = Depends(get_db)):
    """Return the current status of a pipeline job."""
    job = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Pipeline job not found")
    return _job_to_response(job)


@router.get("/jobs", response_model=List[PipelineJobResponse])
def list_pipeline_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Return a paginated list of pipeline jobs, most recent first."""
    jobs = (
        db.query(PipelineJob)
        .order_by(PipelineJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_job_to_response(job) for job in jobs]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_sources(sources: Optional[List[str]]) -> Optional[List[str]]:
    """Validate and normalise requested enrichment sources."""
    if not sources:
        return None  # means "all"
    normalised = [s.strip().lower() for s in sources]
    invalid = [s for s in normalised if s not in VALID_SOURCES]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid sources: {invalid}. Valid options: {sorted(VALID_SOURCES)}",
        )
    return normalised


def _job_to_response(job: PipelineJob) -> PipelineJobResponse:
    return PipelineJobResponse(
        job_id=str(job.id),
        status=job.status,
        mode=job.mode,
        total_companies=job.total_companies,
        processed_companies=job.processed_companies,
        failed_companies=job.failed_companies,
        requested_sources=job.requested_sources,
        results_summary=job.results_summary,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )
