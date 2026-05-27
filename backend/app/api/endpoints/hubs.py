from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.models.hub import Hub
from app.models.users import HubUserRole, User

router = APIRouter(prefix="/hubs", tags=["hubs"])


class HubCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    website_url: Optional[str] = None


class HubResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    website_url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class HubListResponse(BaseModel):
    hubs: List[HubResponse]
    total: int


class HubUserAssignRequest(BaseModel):
    user_id: UUID
    role: str


class HubUserAssignResponse(BaseModel):
    id: UUID
    hub_id: UUID
    user_id: UUID
    role: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/", response_model=HubResponse, status_code=status.HTTP_201_CREATED)
def create_hub(hub: HubCreate, db: Session = Depends(get_db)):
    db_hub = Hub(
        name=hub.name,
        description=hub.description,
        website_url=hub.website_url,
    )
    db.add(db_hub)
    db.commit()
    db.refresh(db_hub)
    return db_hub


@router.get("/", response_model=HubListResponse)
def list_hubs(db: Session = Depends(get_db)):
    hubs = db.query(Hub).order_by(Hub.name).all()
    return HubListResponse(hubs=hubs, total=len(hubs))


@router.get("/{hub_id}", response_model=HubResponse)
def get_hub(hub_id: UUID, db: Session = Depends(get_db)):
    hub = db.query(Hub).filter(Hub.id == hub_id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")
    return hub


@router.post("/{hub_id}/users", response_model=HubUserAssignResponse, status_code=status.HTTP_201_CREATED)
def assign_user_to_hub(
    hub_id: UUID,
    request: HubUserAssignRequest,
    db: Session = Depends(get_db),
):
    hub = db.query(Hub).filter(Hub.id == hub_id).first()
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = (
        db.query(HubUserRole)
        .filter(
            HubUserRole.hub_organization_id == hub_id,
            HubUserRole.user_id == request.user_id,
        )
        .first()
    )
    if existing:
        return HubUserAssignResponse(
            id=existing.id,
            hub_id=existing.hub_organization_id,
            user_id=existing.user_id,
            role=existing.hub_role,
            created_at=existing.created_at,
        )

    assignment = HubUserRole(
        hub_organization_id=hub_id,
        user_id=request.user_id,
        hub_role=request.role,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return HubUserAssignResponse(
        id=assignment.id,
        hub_id=assignment.hub_organization_id,
        user_id=assignment.user_id,
        role=assignment.hub_role,
        created_at=assignment.created_at,
    )
