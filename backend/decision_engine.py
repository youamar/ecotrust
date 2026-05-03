"""
Zero-Trust Energy Decision Engine.

Core rule (slide 5):
    GRANT POWER iff Identity AND Presence AND Context.
If any of the three fails, default to deny.

- Identity: room exists and has an active RBAC policy (control_tier != 'untouched').
- Presence: ToF headcount > 0 with confidence above threshold.
- Context:  current time within authorized working hours for the room.

This is the generational leap from "movement = power" to "authorization = power".
"""
from datetime import datetime
from .models import Room

CONFIDENCE_THRESHOLD = 0.6


def evaluate(room: Room, headcount: int, confidence: float, now: datetime,
             in_grace: bool = False) -> dict:
    # Slide 7: "Strictly Untouched" rooms are physically air-gapped from
    # EcoTrust. We never grant nor deny — the room runs on its own circuit
    # (fire safety, main access, elevators, server rooms). We only observe.
    if room is not None and room.control_tier == "untouched":
        return {
            "identity_ok": False,           # we have no RBAC authority here
            "presence_ok": True,
            "context_ok": True,
            "granted": True,                # always live; not our decision
            "reason": "Strictly untouched: air-gapped from EcoTrust (fire/critical infra).",
            "applied_kw": room.rated_kw,
            "baseline_kw": room.rated_kw,
        }

    # Identity check: RBAC says this room is under EcoTrust authority.
    identity_ok = room is not None and room.control_tier in ("full", "advisory")

    # Presence check: physical ToF verification, not access-card tailgating.
    raw_presence = headcount > 0 and confidence >= CONFIDENCE_THRESHOLD
    # 30s confirmation window: presence holds during phased power-down ramp.
    presence_ok = raw_presence or in_grace

    # Context check: working hours window.
    hour = now.hour
    context_ok = room.authorized_start_hour <= hour < room.authorized_end_hour

    granted = identity_ok and presence_ok and context_ok

    if granted and in_grace and not raw_presence:
        reason = f"Grace window: holding power for {room.authorized_start_hour}-{room.authorized_end_hour} after last verified presence"
    elif granted:
        reason = f"Authorized: {headcount} occupant(s), within hours {room.authorized_start_hour}-{room.authorized_end_hour}"
    else:
        fails = []
        if not identity_ok:
            fails.append("identity (RBAC denies)")
        if not presence_ok:
            fails.append("presence (no verified occupants)")
        if not context_ok:
            fails.append("context (outside working hours)")
        reason = "Denied: " + ", ".join(fails)

    # Baseline kW = always-on draw a legacy BMS would leave running.
    baseline_kw = room.rated_kw
    # Applied kW: full load if granted; phased-down floor (10% standby) if denied.
    applied_kw = room.rated_kw if granted else room.rated_kw * 0.10

    return {
        "identity_ok": identity_ok,
        "presence_ok": presence_ok,
        "context_ok": context_ok,
        "granted": granted,
        "reason": reason,
        "applied_kw": applied_kw,
        "baseline_kw": baseline_kw,
    }
