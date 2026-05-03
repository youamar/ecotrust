"""
EcoTrust FastAPI app — ingest, decide, store, query, export, stream.
"""
import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

log = logging.getLogger("ecotrust")

from .database import Base, engine, get_db
from .models import Room, SensorEvent, PowerDecision
from .schemas import SensorEventIn, DecisionOut, RoomOut
from .decision_engine import evaluate
from .mpc import (
    is_peak_hour, MD_RATE_RM_PER_KW, estimated_md_savings_rm,
    shave_peak, plan_horizon,
)
from .ghg import scope2_emissions_kg, avoided_emissions_kg, TNB_GRID_EF_2024
from .pdf_export import build_scope2_pdf
from .tariff import project_monthly_bill, extrapolate_30d
from .state import state, NUDGE_WEBHOOK_URL, API_KEY


# ---------- Auth dependency ----------
def require_api_key(x_api_key: str | None = Header(default=None)):
    """Open in dev mode (no API_KEY env). Required when ECOTRUST_API_KEY is set."""
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(401, "missing or invalid X-API-Key")


# ---------- Nudge webhook (slide 15) ----------
async def fire_nudge(text: str, blocks: list | None = None) -> None:
    """Best-effort post to Slack/Teams-compatible webhook. Never raises."""
    if not NUDGE_WEBHOOK_URL:
        return
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        async with httpx.AsyncClient(timeout=3.0) as cli:
            await cli.post(NUDGE_WEBHOOK_URL, json=payload)
    except Exception as e:
        log.warning("nudge webhook failed: %s", e)

BASE = Path(__file__).parent
app = FastAPI(title="EcoTrust API", version="0.2.0")

Base.metadata.create_all(bind=engine)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ---------- Helpers ----------
def _latest_kw_by_room(db: Session, exclude_room_id: int | None = None) -> dict[int, float]:
    """Latest applied_kw per room (used for MPC projection)."""
    rooms = db.query(Room).all()
    out = {}
    for r in rooms:
        if r.id == exclude_room_id:
            continue
        d = (db.query(PowerDecision)
             .filter(PowerDecision.room_id == r.id)
             .order_by(PowerDecision.timestamp.desc()).first())
        out[r.id] = d.applied_kw if d else 0.0
    return out


# ---------- Ingest ----------
@app.post("/ingest/sensor", response_model=DecisionOut)
async def ingest_sensor(evt: SensorEventIn, db: Session = Depends(get_db)):
    room = db.query(Room).filter(Room.id == evt.room_id).first()
    if not room:
        raise HTTPException(404, "room not found")

    now = evt.timestamp or datetime.utcnow()
    db.add(SensorEvent(
        room_id=room.id, timestamp=now,
        headcount=evt.headcount, confidence=evt.confidence,
    ))

    # 0) Update presence-grace tracker (slide 8 step 4)
    if evt.headcount > 0 and evt.confidence >= 0.6:
        state.mark_presence(room.id, now)
    in_grace = state.in_grace_window(room.id, now)

    # 1) Zero-trust evaluation (with 30s grace flag)
    result = evaluate(room, evt.headcount, evt.confidence, now, in_grace=in_grace)

    # 2) Manual override (slide 17: physical wall switch / human fallback)
    ov = state.get_override(room.id)
    if ov:
        result["granted"] = ov.granted
        result["applied_kw"] = room.rated_kw if ov.granted else room.rated_kw * 0.10
        result["reason"] = f"OVERRIDE ({ov.reason}, expires {ov.expires_at.strftime('%H:%M')})"

    # 3) MPC QP-optimal peak shaving — only dim advisory-tier rooms during
    # the peak window, only when total projected load exceeds the soft cap.
    mpc_applied = False
    if (is_peak_hour(now)
            and result["granted"]
            and room.control_tier == "advisory"
            and ov is None):
        others = _latest_kw_by_room(db, exclude_room_id=room.id)
        all_rooms = db.query(Room).all()
        # Build the load list for this tick.
        loads = []
        for r in all_rooms:
            if r.id == room.id:
                loads.append((r.id, result["applied_kw"], r.control_tier))
            else:
                loads.append((r.id, others.get(r.id, 0.0), r.control_tier))
        shaved = shave_peak(loads, state.peak_soft_cap_kw, now)
        # Pull this room's shaved kW.
        new_kw = next(kw for rid, kw, _ in shaved if rid == room.id)
        if new_kw < result["applied_kw"]:
            dim_pct = round((1 - new_kw / result["applied_kw"]) * 100, 1)
            result["applied_kw"] = new_kw
            result["reason"] += f" | MPC QP dim {dim_pct}% (cap {state.peak_soft_cap_kw}kW)"
            mpc_applied = True

    # 4) Persist decision
    decision = PowerDecision(
        room_id=room.id, timestamp=now, headcount=evt.headcount,
        granted=result["granted"], reason=result["reason"],
        identity_ok=result["identity_ok"], presence_ok=result["presence_ok"],
        context_ok=result["context_ok"],
        applied_kw=result["applied_kw"], baseline_kw=result["baseline_kw"],
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)

    # 5) Advisory-tier suggestions for facility engineer (slide 7)
    if room.control_tier == "advisory":
        if not result["granted"] and result["context_ok"]:
            state.push_notification(
                room.id, room.name, "advisory",
                f"Suggest HVAC standby for {room.name} — zero verified occupants.",
            )
        elif mpc_applied:
            state.push_notification(
                room.id, room.name, "info",
                f"MPC dimmed {room.name} 50% to protect TNB peak demand.",
            )

    # 6) Broadcast SSE event
    out = DecisionOut(
        room_id=room.id, room_name=room.name, timestamp=decision.timestamp,
        headcount=decision.headcount, granted=decision.granted,
        reason=decision.reason,
        identity_ok=decision.identity_ok, presence_ok=decision.presence_ok,
        context_ok=decision.context_ok,
        applied_kw=decision.applied_kw, baseline_kw=decision.baseline_kw,
    )
    await state.broadcast({"type": "decision", "data": out.model_dump(mode="json")})
    _check_room_budget(db, room)
    _check_dept_budgets(db)
    return out


# ---------- Queries ----------
@app.get("/rooms", response_model=list[RoomOut])
def list_rooms(db: Session = Depends(get_db)):
    return db.query(Room).order_by(Room.floor, Room.name).all()


@app.get("/occupancy/latest")
def latest_occupancy(db: Session = Depends(get_db)):
    rooms = db.query(Room).all()
    out = []
    for r in rooms:
        d = (db.query(PowerDecision)
             .filter(PowerDecision.room_id == r.id)
             .order_by(PowerDecision.timestamp.desc()).first())
        ov = state.get_override(r.id)
        out.append({
            "room_id": r.id, "name": r.name, "floor": r.floor,
            "department": r.department, "tier": r.control_tier,
            "headcount": d.headcount if d else 0,
            "granted": bool(d.granted) if d else False,
            "applied_kw": d.applied_kw if d else 0.0,
            "baseline_kw": r.rated_kw,
            "reason": d.reason if d else "no data",
            "timestamp": d.timestamp.isoformat() if d else None,
            "override": (None if ov is None else {
                "granted": ov.granted, "reason": ov.reason,
                "expires_at": ov.expires_at.isoformat(),
            }),
        })
    return out


def _aggregate_window(db: Session, hours: int = 24):
    since = datetime.utcnow() - timedelta(hours=hours)
    decisions = (db.query(PowerDecision)
                 .filter(PowerDecision.timestamp >= since)
                 .order_by(PowerDecision.room_id, PowerDecision.timestamp).all())
    by_room = defaultdict(list)
    for d in decisions:
        by_room[d.room_id].append(d)
    actual_kwh = defaultdict(float)
    baseline_kwh = defaultdict(float)
    for rid, lst in by_room.items():
        for i, d in enumerate(lst):
            if i + 1 < len(lst):
                dt_h = (lst[i + 1].timestamp - d.timestamp).total_seconds() / 3600.0
            else:
                dt_h = max(0.0, (datetime.utcnow() - d.timestamp).total_seconds() / 3600.0)
            dt_h = min(dt_h, 1.0)
            actual_kwh[rid] += d.applied_kw * dt_h
            baseline_kwh[rid] += d.baseline_kw * dt_h
    return actual_kwh, baseline_kwh


@app.get("/consumption/daily")
def consumption_daily(db: Session = Depends(get_db)):
    since = datetime.utcnow() - timedelta(hours=24)
    rows = db.query(PowerDecision).filter(PowerDecision.timestamp >= since).all()
    by_hr_room_latest = {}
    for d in rows:
        hr = d.timestamp.replace(minute=0, second=0, microsecond=0)
        key = (hr, d.room_id)
        prev = by_hr_room_latest.get(key)
        if prev is None or d.timestamp > prev.timestamp:
            by_hr_room_latest[key] = d
    hr_actual = defaultdict(float)
    hr_base = defaultdict(float)
    for (hr, _rid), d in by_hr_room_latest.items():
        hr_actual[hr] += d.applied_kw
        hr_base[hr] += d.baseline_kw
    return [
        {
            "hour": k.isoformat(),
            "actual_kw_total": round(hr_actual[k], 2),
            "baseline_kw_total": round(hr_base[k], 2),
        } for k in sorted(hr_actual.keys())
    ]


@app.get("/ghg/scope2")
def ghg_scope2(db: Session = Depends(get_db)):
    actual, baseline = _aggregate_window(db, hours=24)
    total_actual = sum(actual.values())
    total_base = sum(baseline.values())
    return {
        "window_hours": 24,
        "emission_factor": TNB_GRID_EF_2024,
        "actual_kwh": round(total_actual, 2),
        "baseline_kwh": round(total_base, 2),
        "avoided_kwh": round(max(0.0, total_base - total_actual), 2),
        "scope2_kg": round(scope2_emissions_kg(total_actual), 2),
        "avoided_kg": round(avoided_emissions_kg(total_base, total_actual), 2),
    }


@app.get("/mpc/horizon")
def mpc_horizon(horizon_ticks: int = 4, tick_minutes: int = 15,
                db: Session = Depends(get_db)):
    """Rolling horizon MPC plan — projects N quarter-hour ticks ahead and
    runs the QP at each. Only the first tick is binding; subsequent ticks
    are recomputed every cycle."""
    horizon_ticks = max(1, min(horizon_ticks, 16))
    rooms = db.query(Room).all()
    triples = [(r.id, r.rated_kw, r.control_tier) for r in rooms]

    # Pull last 60 minutes of decisions per room as the persistence forecast.
    cutoff = datetime.utcnow() - timedelta(minutes=60)
    history: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    rows = (db.query(PowerDecision)
            .filter(PowerDecision.timestamp >= cutoff)
            .order_by(PowerDecision.timestamp).all())
    for d in rows:
        history[d.room_id].append((d.timestamp, d.applied_kw))

    plan = plan_horizon(
        rooms=triples, history_per_room=history,
        soft_cap_kw=state.peak_soft_cap_kw,
        start_at=datetime.utcnow(),
        horizon_ticks=horizon_ticks, tick_minutes=tick_minutes,
    )
    return {"soft_cap_kw": state.peak_soft_cap_kw, "horizon": plan}


@app.get("/peak/savings")
def peak_savings(db: Session = Depends(get_db)):
    since = datetime.utcnow() - timedelta(hours=24)
    rows = db.query(PowerDecision).filter(PowerDecision.timestamp >= since).all()
    by_ts_actual = defaultdict(float)
    by_ts_base = defaultdict(float)
    for d in rows:
        if is_peak_hour(d.timestamp):
            key = d.timestamp.replace(second=0, microsecond=0)
            by_ts_actual[key] += d.applied_kw
            by_ts_base[key] += d.baseline_kw
    peak_actual = max(by_ts_actual.values()) if by_ts_actual else 0.0
    peak_base = max(by_ts_base.values()) if by_ts_base else 0.0
    return {
        "peak_actual_kw": round(peak_actual, 2),
        "peak_baseline_kw": round(peak_base, 2),
        "md_rate_rm_per_kw": MD_RATE_RM_PER_KW,
        "estimated_md_savings_rm_per_month": round(
            estimated_md_savings_rm(peak_base, peak_actual), 2),
        "soft_cap_kw": state.peak_soft_cap_kw,
    }


@app.get("/departments/leaderboard")
def dept_leaderboard(db: Session = Depends(get_db)):
    actual, baseline = _aggregate_window(db, hours=24)
    rooms = {r.id: r for r in db.query(Room).all()}
    by_dept_actual = defaultdict(float)
    by_dept_base = defaultdict(float)
    for rid, kwh in actual.items():
        by_dept_actual[rooms[rid].department] += kwh
    for rid, kwh in baseline.items():
        by_dept_base[rooms[rid].department] += kwh
    leaderboard = []
    for dept, base in by_dept_base.items():
        act = by_dept_actual.get(dept, 0.0)
        saved = max(0.0, base - act)
        score = round((saved / base * 100.0) if base > 0 else 0.0, 2)
        leaderboard.append({
            "department": dept,
            "actual_kwh": round(act, 2),
            "baseline_kwh": round(base, 2),
            "saved_kwh": round(saved, 2),
            "score": score,
        })
    leaderboard.sort(key=lambda x: -x["score"])
    return leaderboard


@app.get("/tenants/attribution")
def tenant_attribution(db: Session = Depends(get_db)):
    return dept_leaderboard(db)


# ---------- Multi-day trends ----------
@app.get("/trends/daily")
def trends_daily(days: int = 7, db: Session = Depends(get_db)):
    """Per-day actual vs baseline kWh, projected RM cost, avoided CO2."""
    days = max(1, min(days, 90))
    since = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(PowerDecision)
            .filter(PowerDecision.timestamp >= since)
            .order_by(PowerDecision.room_id, PowerDecision.timestamp).all())

    # Group by (room_id, day) and integrate kWh.
    by_room_day_actual = defaultdict(float)
    by_room_day_base = defaultdict(float)
    by_room = defaultdict(list)
    for d in rows:
        by_room[d.room_id].append(d)
    for rid, lst in by_room.items():
        for i, d in enumerate(lst):
            day = d.timestamp.date()
            if i + 1 < len(lst):
                dt_h = (lst[i + 1].timestamp - d.timestamp).total_seconds() / 3600.0
            else:
                dt_h = max(0.0, (datetime.utcnow() - d.timestamp).total_seconds() / 3600.0)
            dt_h = min(dt_h, 1.0)
            by_room_day_actual[(rid, day)] += d.applied_kw * dt_h
            by_room_day_base[(rid, day)] += d.baseline_kw * dt_h

    # Roll up by day across all rooms.
    days_actual = defaultdict(float)
    days_base = defaultdict(float)
    for (rid, day), kwh in by_room_day_actual.items():
        days_actual[day] += kwh
    for (rid, day), kwh in by_room_day_base.items():
        days_base[day] += kwh

    out = []
    for day in sorted(days_actual.keys()):
        a = days_actual[day]
        b = days_base[day]
        out.append({
            "date": day.isoformat(),
            "actual_kwh": round(a, 2),
            "baseline_kwh": round(b, 2),
            "saved_kwh": round(max(0.0, b - a), 2),
            "scope2_kg": round(scope2_emissions_kg(a), 2),
            "avoided_kg": round(avoided_emissions_kg(b, a), 2),
            "actual_rm": round(a * 0.365, 2),  # energy charge approx
            "baseline_rm": round(b * 0.365, 2),
        })
    return out


# ---------- Department budgets ----------
class DeptBudgetIn(BaseModel):
    department: str
    daily_kwh: float


@app.get("/departments/budgets")
def get_dept_budgets():
    return state.dept_daily_kwh_budget


@app.post("/departments/budgets", dependencies=[Depends(require_api_key)])
def set_dept_budget(body: DeptBudgetIn):
    state.dept_daily_kwh_budget[body.department] = body.daily_kwh
    return {"ok": True, "budgets": state.dept_daily_kwh_budget}


class RoomBudgetIn(BaseModel):
    daily_kwh: float


@app.get("/rooms/{room_id}/budget")
def get_room_budget(room_id: int):
    return {"room_id": room_id,
            "daily_kwh": state.room_daily_kwh_budget.get(room_id)}


@app.post("/rooms/{room_id}/budget", dependencies=[Depends(require_api_key)])
def set_room_budget(room_id: int, body: RoomBudgetIn, db: Session = Depends(get_db)):
    if not db.query(Room).filter(Room.id == room_id).first():
        raise HTTPException(404, "room not found")
    state.room_daily_kwh_budget[room_id] = body.daily_kwh
    return {"ok": True, "daily_kwh": body.daily_kwh}


@app.delete("/rooms/{room_id}/budget", dependencies=[Depends(require_api_key)])
def clear_room_budget(room_id: int):
    state.room_daily_kwh_budget.pop(room_id, None)
    return {"ok": True}


def _check_room_budget(db: Session, room: Room) -> None:
    budget = state.room_daily_kwh_budget.get(room.id)
    if budget is None:
        return
    actual, _ = _aggregate_window(db, hours=24)
    used = actual.get(room.id, 0.0)
    now = datetime.utcnow()
    if used >= budget and state.should_alert_room(room.id, now):
        state.push_notification(
            room_id=room.id, room_name=room.name, severity="warning",
            message=(f"{room.name} exceeded its daily budget: "
                     f"{used:.2f} kWh > {budget:.2f} kWh."),
        )
        asyncio.create_task(fire_nudge(
            f":warning: Room *{room.name}* over budget — {used:.2f}/{budget:.2f} kWh today."
        ))


def _check_dept_budgets(db: Session) -> None:
    """Push a notification + webhook when any dept exceeds its daily budget."""
    if not state.dept_daily_kwh_budget:
        return
    lb = dept_leaderboard(db)
    now = datetime.utcnow()
    for row in lb:
        budget = state.dept_daily_kwh_budget.get(row["department"])
        if budget is None:
            continue
        if row["actual_kwh"] >= budget and state.should_alert_dept(row["department"], now):
            state.push_notification(
                room_id=0, room_name=row["department"], severity="warning",
                message=(f"{row['department']} exceeded daily budget: "
                         f"{row['actual_kwh']:.1f} kWh > {budget:.1f} kWh."),
            )
            asyncio.create_task(fire_nudge(
                f":warning: *{row['department']}* over daily energy budget — "
                f"{row['actual_kwh']:.1f}/{budget:.1f} kWh today."
            ))


# ---------- Tariff projection ----------
@app.get("/tariff/projection")
def tariff_projection(db: Session = Depends(get_db)):
    """Project this month's TNB Tariff C bill from current 24h run rate."""
    g = ghg_scope2(db)
    p = peak_savings(db)
    actual = project_monthly_bill(extrapolate_30d(g["actual_kwh"]), p["peak_actual_kw"])
    baseline = project_monthly_bill(extrapolate_30d(g["baseline_kwh"]), p["peak_baseline_kw"])
    return {
        "actual": actual.__dict__,
        "baseline": baseline.__dict__,
        "savings_rm": round(baseline.total_rm - actual.total_rm, 2),
        "savings_pct": round(
            (baseline.total_rm - actual.total_rm) / baseline.total_rm * 100.0
            if baseline.total_rm > 0 else 0.0, 2),
    }


# ---------- Audit (algorithmic transparency, slide 17) ----------
@app.get("/audit")
def audit(limit: int = 50, room_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(PowerDecision).order_by(PowerDecision.timestamp.desc())
    if room_id is not None:
        q = q.filter(PowerDecision.room_id == room_id)
    rows = q.limit(min(limit, 500)).all()
    rooms = {r.id: r for r in db.query(Room).all()}
    return [{
        "id": d.id,
        "room_id": d.room_id,
        "room_name": rooms[d.room_id].name if d.room_id in rooms else "?",
        "timestamp": d.timestamp.isoformat(),
        "headcount": d.headcount,
        "granted": bool(d.granted),
        "reason": d.reason,
        "identity_ok": bool(d.identity_ok),
        "presence_ok": bool(d.presence_ok),
        "context_ok": bool(d.context_ok),
        "applied_kw": d.applied_kw,
        "baseline_kw": d.baseline_kw,
    } for d in rows]


# ---------- Notifications (advisory tier → facility engineer) ----------
class NotificationOut(BaseModel):
    id: int
    room_id: int
    room_name: str
    severity: str
    message: str
    created_at: datetime
    acknowledged: bool


@app.get("/notifications", response_model=list[NotificationOut])
def list_notifications(only_open: bool = True, limit: int = 50):
    items = list(state.notifications)
    if only_open:
        items = [n for n in items if not n.acknowledged]
    return [NotificationOut(**n.__dict__) for n in items[:limit]]


@app.post("/notifications/{nid}/ack")
def ack_notification(nid: int):
    if not state.ack_notification(nid):
        raise HTTPException(404, "notification not found")
    return {"ok": True}


# ---------- Manual override ----------
class OverrideIn(BaseModel):
    granted: bool
    ttl_seconds: int = 4 * 3600
    reason: str = "manual"


@app.post("/override/{room_id}", dependencies=[Depends(require_api_key)])
async def set_override(room_id: int, body: OverrideIn, db: Session = Depends(get_db)):
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(404, "room not found")
    ov = state.set_override(room_id, body.granted, body.ttl_seconds, body.reason)
    state.push_notification(
        room_id, room.name, "warning",
        f"Manual override: {'GRANT' if body.granted else 'DENY'} for {body.ttl_seconds // 60} min ({body.reason})",
    )
    await state.broadcast({"type": "override", "data": {
        "room_id": room_id, "granted": ov.granted,
        "expires_at": ov.expires_at.isoformat(), "reason": ov.reason,
    }})
    asyncio.create_task(fire_nudge(
        f":zap: *EcoTrust override*: {'GRANT' if body.granted else 'DENY'} on *{room.name}* "
        f"for {body.ttl_seconds // 60} min — {body.reason}"
    ))
    return {"ok": True, "expires_at": ov.expires_at.isoformat()}


@app.delete("/override/{room_id}", dependencies=[Depends(require_api_key)])
async def clear_override(room_id: int):
    cleared = state.clear_override(room_id)
    if cleared:
        await state.broadcast({"type": "override_cleared", "data": {"room_id": room_id}})
    return {"ok": cleared}


@app.post("/nudge/test", dependencies=[Depends(require_api_key)])
async def nudge_test(db: Session = Depends(get_db)):
    """Manually fire a sample leaderboard digest to the configured webhook."""
    if not NUDGE_WEBHOOK_URL:
        raise HTTPException(400, "ECOTRUST_NUDGE_WEBHOOK_URL not configured")
    lb = dept_leaderboard(db)
    lines = [f"• {d['department']}: {d['score']:.1f}% saved ({d['saved_kwh']:.1f} kWh)" for d in lb[:5]]
    await fire_nudge(":herb: *EcoTrust Daily Green Points*\n" + "\n".join(lines))
    return {"ok": True}


# ---------- SSE live stream ----------
@app.get("/events")
async def sse_events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    state.sse_clients.add(q)

    async def gen():
        try:
            yield f"event: hello\ndata: {json.dumps({'ts': datetime.utcnow().isoformat()})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"event: {evt['type']}\ndata: {json.dumps(evt['data'], default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            state.sse_clients.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


# ---------- Export ----------
@app.get("/export/audit.csv")
def export_audit_csv(hours: int = 24, room_id: int | None = None,
                     db: Session = Depends(get_db)):
    since = datetime.utcnow() - timedelta(hours=hours)
    q = db.query(PowerDecision).filter(PowerDecision.timestamp >= since)
    if room_id is not None:
        q = q.filter(PowerDecision.room_id == room_id)
    rows = q.order_by(PowerDecision.timestamp).all()
    rooms = {r.id: r for r in db.query(Room).all()}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "timestamp_utc", "room_id", "room_name", "department", "tier",
        "headcount", "granted", "identity_ok", "presence_ok", "context_ok",
        "applied_kw", "baseline_kw", "reason",
    ])
    for d in rows:
        r = rooms.get(d.room_id)
        w.writerow([
            d.timestamp.isoformat(),
            d.room_id, r.name if r else "", r.department if r else "",
            r.control_tier if r else "",
            d.headcount, int(d.granted), int(d.identity_ok),
            int(d.presence_ok), int(d.context_ok),
            f"{d.applied_kw:.4f}", f"{d.baseline_kw:.4f}",
            d.reason or "",
        ])

    return Response(content=buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="ecotrust_audit.csv"'
    })


@app.get("/export/sensors.csv")
def export_sensors_csv(hours: int = 24, room_id: int | None = None,
                      db: Session = Depends(get_db)):
    """Raw ToF events — what the edge gateway pushed before any decision logic."""
    since = datetime.utcnow() - timedelta(hours=hours)
    q = db.query(SensorEvent).filter(SensorEvent.timestamp >= since)
    if room_id is not None:
        q = q.filter(SensorEvent.room_id == room_id)
    rows = q.order_by(SensorEvent.timestamp).all()
    rooms = {r.id: r for r in db.query(Room).all()}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc", "room_id", "room_name", "headcount", "confidence"])
    for s in rows:
        r = rooms.get(s.room_id)
        w.writerow([
            s.timestamp.isoformat(), s.room_id,
            r.name if r else "", s.headcount, f"{s.confidence:.3f}",
        ])
    return Response(content=buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="ecotrust_sensors.csv"'
    })


@app.get("/export/scope2.pdf")
def export_pdf(building: str = "UM Faculty Building A", db: Session = Depends(get_db)):
    g = ghg_scope2(db)
    p = peak_savings(db)
    lb = dept_leaderboard(db)
    rows = [[r["department"], f"{r['actual_kwh']:.1f}",
             f"{(r['actual_kwh'] * TNB_GRID_EF_2024):.2f}",
             f"{r['score']:.1f}"] for r in lb]
    pdf = build_scope2_pdf(
        building_name=building,
        period_label=f"Last 24h ({datetime.utcnow().strftime('%Y-%m-%d')})",
        total_kwh=g["actual_kwh"], baseline_kwh=g["baseline_kwh"],
        avoided_kwh=g["avoided_kwh"], emission_factor=g["emission_factor"],
        scope2_kg=g["scope2_kg"], avoided_kg=g["avoided_kg"],
        md_savings_rm=p["estimated_md_savings_rm_per_month"],
        department_rows=rows,
    )
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="ecotrust_scope2.pdf"'
    })


# ---------- Per-room timeseries (drill-down) ----------
@app.get("/room/{room_id}/timeseries")
def room_timeseries(room_id: int, hours: int = 6, db: Session = Depends(get_db)):
    if not db.query(Room).filter(Room.id == room_id).first():
        raise HTTPException(404, "room not found")
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = (db.query(PowerDecision)
            .filter(PowerDecision.room_id == room_id,
                    PowerDecision.timestamp >= since)
            .order_by(PowerDecision.timestamp).all())
    return [{
        "timestamp": d.timestamp.isoformat(),
        "headcount": d.headcount,
        "applied_kw": d.applied_kw,
        "baseline_kw": d.baseline_kw,
        "granted": bool(d.granted),
        "reason": d.reason,
    } for d in rows]


# ---------- Dashboard ----------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/pitch", response_class=HTMLResponse)
def pitch(request: Request):
    return templates.TemplateResponse("pitch.html", {"request": request})


@app.get("/trends", response_class=HTMLResponse)
def trends_page(request: Request):
    return templates.TemplateResponse("trends.html", {"request": request})


@app.get("/compliance", response_class=HTMLResponse)
def compliance_page(request: Request):
    return templates.TemplateResponse(
        "compliance.html",
        {"request": request, "today": datetime.utcnow().strftime("%Y-%m-%d")},
    )


@app.get("/room/{room_id}", response_class=HTMLResponse)
def room_page(request: Request, room_id: int, db: Session = Depends(get_db)):
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(404, "room not found")
    return templates.TemplateResponse("room.html", {"request": request, "room": room})


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.utcnow().isoformat(),
            "sse_clients": len(state.sse_clients),
            "open_notifications": sum(1 for n in state.notifications if not n.acknowledged)}
