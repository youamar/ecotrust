from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class SensorEventIn(BaseModel):
    room_id: int
    headcount: int
    confidence: float = 1.0
    timestamp: Optional[datetime] = None


class DecisionOut(BaseModel):
    room_id: int
    room_name: str
    timestamp: datetime
    headcount: int
    granted: bool
    reason: str
    identity_ok: bool
    presence_ok: bool
    context_ok: bool
    applied_kw: float
    baseline_kw: float

    class Config:
        from_attributes = True


class RoomOut(BaseModel):
    id: int
    name: str
    floor: int
    department: str
    area_m2: float
    authorized_start_hour: int
    authorized_end_hour: int
    rated_kw: float
    control_tier: str

    class Config:
        from_attributes = True
