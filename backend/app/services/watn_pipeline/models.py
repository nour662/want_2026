"""
Plain data containers shared by every stage of the WATN pipeline.

Nothing in here touches the database, the network or Salesforce, so the same
objects can be produced by a CSV loader today and a Salesforce query later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass
class CompanyInput:
    """One participant company, aggregated from every input row that names it."""

    key: str  # stable identifier used for checkpoints (normalised name)
    name: str
    uei: Optional[str] = None
    duns: Optional[str] = None
    website: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    contacts: List[str] = field(default_factory=list)
    university: Optional[str] = None
    cohort_date: Optional[date] = None  # earliest I-Corps regional start date
    corp_ids: List[str] = field(default_factory=list)
    eids: List[str] = field(default_factory=list)
    external_id: Optional[str] = None  # e.g. Salesforce Account Id, later


@dataclass
class MatchEvidence:
    """Why we believe a remote entity is the same company as the input."""

    score: float  # 0-100
    tier: str  # "confirmed" | "probable" | "review" | "rejected"
    method: str  # "uei" | "duns" | "name" | "name+domain" | ...
    reasons: List[str] = field(default_factory=list)


@dataclass
class FieldValue:
    value: Any
    source: str


@dataclass
class FundingRecord:
    """Canonical funding record, normalised from any source."""

    source: str  # "sbir" | "usas_contracts" | "usas_grants" | "nsf" | "tedco"
    amount: Optional[float] = None
    date_awarded: Optional[date] = None
    end_date: Optional[date] = None
    award_number: Optional[str] = None
    external_id: Optional[str] = None
    title: Optional[str] = None
    investor: Optional[str] = None  # agency / fund / funder
    phase: Optional[str] = None
    program: Optional[str] = None
    raw_type: Optional[str] = None  # award type string as the source reported it
    funding_source: Optional[str] = None
    funding_stage: Optional[str] = None
    source_url: Optional[str] = None
    additional_info: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_uei: Optional[str] = None
    # Evaluation
    confidence: float = 0.0
    match_method: str = ""
    review_reasons: List[str] = field(default_factory=list)
    # Merge provenance
    sources: List[str] = field(default_factory=list)
    merged_count: int = 1
    post_icorps: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FundingRecord":
        data = dict(data)
        for key in ("date_awarded", "end_date"):
            if isinstance(data.get(key), str):
                data[key] = date.fromisoformat(data[key])
        return cls(**data)


@dataclass
class PatentRecord:
    """A US patent application (granted or pending) owned by the company."""

    application_number: str
    title: Optional[str] = None
    patent_number: Optional[str] = None
    publication_number: Optional[str] = None
    status: Optional[str] = None
    application_type: Optional[str] = None
    filing_date: Optional[date] = None
    publication_date: Optional[date] = None
    grant_date: Optional[date] = None
    owner: Optional[str] = None  # applicant/assignee name that matched the company
    owner_role: Optional[str] = None  # "applicant" | "assignee"
    owner_state: Optional[str] = None
    inventors: List[str] = field(default_factory=list)
    cpc_codes: List[str] = field(default_factory=list)
    source_url: Optional[str] = None
    # Evaluation
    confidence: float = 0.0
    tier: str = ""
    match_method: str = ""
    match_reasons: List[str] = field(default_factory=list)
    post_icorps: Optional[bool] = None  # filed on/after the earliest cohort date

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PatentRecord":
        data = dict(data)
        for key in ("filing_date", "publication_date", "grant_date"):
            if isinstance(data.get(key), str):
                data[key] = date.fromisoformat(data[key])
        return cls(**data)


@dataclass
class SourceResult:
    """What one source returned for one company."""

    source: str
    status: str = "ok"  # "ok" | "skipped" | "error" | "no_data"
    message: str = ""
    profile: Dict[str, Any] = field(default_factory=dict)
    funding: List[FundingRecord] = field(default_factory=list)
    patents: List[PatentRecord] = field(default_factory=list)


@dataclass
class CompanyResult:
    company: CompanyInput
    identity: Optional[MatchEvidence] = None
    resolved_uei: Optional[str] = None
    profile: Dict[str, FieldValue] = field(default_factory=dict)
    funding: List[FundingRecord] = field(default_factory=list)
    patents: List[PatentRecord] = field(default_factory=list)  # accepted and review-tier matches
    source_status: Dict[str, str] = field(default_factory=dict)
    review_reasons: List[str] = field(default_factory=list)
    duplicates_merged: int = 0
    status: str = "completed"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompanyResult":
        company = dict(data["company"])
        if isinstance(company.get("cohort_date"), str):
            company["cohort_date"] = date.fromisoformat(company["cohort_date"])
        identity = data.get("identity")
        return cls(
            company=CompanyInput(**company),
            identity=MatchEvidence(**identity) if identity else None,
            resolved_uei=data.get("resolved_uei"),
            profile={k: FieldValue(**v) for k, v in (data.get("profile") or {}).items()},
            funding=[FundingRecord.from_dict(f) for f in data.get("funding") or []],
            patents=[PatentRecord.from_dict(p) for p in data.get("patents") or []],
            source_status=data.get("source_status") or {},
            review_reasons=data.get("review_reasons") or [],
            duplicates_merged=data.get("duplicates_merged", 0),
            status=data.get("status", "completed"),
            error=data.get("error"),
        )
