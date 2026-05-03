from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class Room(Base):
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    floor = Column(Integer, default=1)
    department = Column(String, default="General")
    area_m2 = Column(Float, default=20.0)
    # Authorized hours window (24h). Outside this -> deny by default.
    authorized_start_hour = Column(Integer, default=8)
    authorized_end_hour = Column(Integer, default=20)
    # Rated load in kW for this room (HVAC + lighting + plug loads)
    rated_kw = Column(Float, default=2.0)
    # Control tier: full | advisory | untouched
    control_tier = Column(String, default="full")


class SensorEvent(Base):
    """Raw ToF event from edge gateway. Identity is decoupled (no person ID)."""
    __tablename__ = "sensor_events"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    headcount = Column(Integer, default=0)
    confidence = Column(Float, default=1.0)
    room = relationship("Room")


class PowerDecision(Base):
    """Output of the zero-trust decision engine."""
    __tablename__ = "power_decisions"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    headcount = Column(Integer)
    granted = Column(Boolean, default=False)
    reason = Column(String)  # why granted/denied
    identity_ok = Column(Boolean)
    presence_ok = Column(Boolean)
    context_ok = Column(Boolean)
    # Snapshot of load applied after decision (kW)
    applied_kw = Column(Float, default=0.0)
    # Baseline kW that would have been drawn without EcoTrust
    baseline_kw = Column(Float, default=0.0)
    room = relationship("Room")
