from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID
from app.core.db.base import get_db
from app.models.hub import Hub
from app.models.university import University
from pydantic import BaseModel

router = APIRouter()


class HubResponse(BaseModel):
    id: UUID
    name: str

    class Config:
        from_attributes = True


class UniversityResponse(BaseModel):
    id: UUID
    name: str
    hub_id: UUID

    class Config:
        from_attributes = True


@router.get("/hubs", response_model=List[HubResponse])
def get_hubs(db: Session = Depends(get_db)):
    hubs = db.query(Hub).order_by(Hub.name).all()
    return hubs


@router.get("/hubs/{hub_id}/universities", response_model=List[UniversityResponse])
def get_universities_by_hub(hub_id: UUID, db: Session = Depends(get_db)):
    universities = db.query(University).filter(
        University.hub_id == hub_id
    ).order_by(University.name).all()
    return universities


@router.get("/universities", response_model=List[UniversityResponse])
def get_all_universities(db: Session = Depends(get_db)):
    universities = db.query(University).order_by(University.name).all()
    return universities
