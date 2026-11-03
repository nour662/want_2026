"""
Unified WATN ("Where Are They Now") pipeline.

Replaces the run.sh chain in WATN-Project (scrape -> batch merge -> clean ->
merge) with one parallel, resumable process. See ``runner.run_pipeline`` and
``scripts/run_watn_pipeline.py``.
"""

from app.services.watn_pipeline.inputs import load_participants
from app.services.watn_pipeline.runner import ALL_SOURCES, PipelineConfig, run_pipeline

__all__ = ["ALL_SOURCES", "PipelineConfig", "load_participants", "run_pipeline"]
