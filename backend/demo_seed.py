"""
Backfill 24h of synthetic decisions so the dashboard is populated on first load.

Runs the same `evaluate()` logic as the live ingest path, with a per-room
occupancy pattern that varies through the day. Use after `python -m backend.seed`.

Usage:  python -m backend.demo_seed [--hours 24]
"""
import argparse
import math
import random
from datetime import datetime, timedelta

from .database import SessionLocal, Base, engine
from .models import Room, PowerDecision, SensorEvent
from .decision_engine import evaluate
from .mpc import is_peak_hour
from .seed import seed as base_seed


def _occupancy(room: Room, hour: float) -> int:
    if "Server" in room.name:
        return 0
    if room.control_tier == "advisory":
        peak = 30 if "Hall" in room.name else 15
        return max(0, int(peak * math.exp(-((hour - 13) ** 2) / 18) + random.randint(-2, 2)))
    if 9 <= hour <= 18:
        return random.choices([0, 1, 2, 3, 4], weights=[1, 2, 3, 2, 1])[0]
    if 18 < hour <= 21:
        return random.choices([0, 1, 2], weights=[5, 2, 1])[0]
    return 0


def backfill(hours: int = 24, step_minutes: int = 10, soft_cap_kw: float = 25.0):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        rooms = db.query(Room).all()
        if not rooms:
            print("No rooms — running base seed first.")
            base_seed()
            rooms = db.query(Room).all()

        # Wipe prior decisions so the demo is reproducible.
        db.query(PowerDecision).delete()
        db.query(SensorEvent).delete()
        db.commit()

        now = datetime.utcnow()
        start = now - timedelta(hours=hours)
        ticks = int(hours * 60 / step_minutes)
        last_seen: dict[int, datetime] = {}
        rooms_count = 0
        decisions_count = 0

        for i in range(ticks):
            t = start + timedelta(minutes=i * step_minutes)
            hour = t.hour + t.minute / 60.0

            # First pass: collect this tick's room loads under primary evaluation.
            tick_rows = []
            for r in rooms:
                head = _occupancy(r, hour)
                conf = round(random.uniform(0.78, 0.98), 2) if head > 0 else 0.95
                if head > 0 and conf >= 0.6:
                    last_seen[r.id] = t
                in_grace = (r.id in last_seen
                            and (t - last_seen[r.id]).total_seconds() < 30)
                res = evaluate(r, head, conf, t, in_grace=in_grace)
                tick_rows.append((r, head, conf, res))

            # Second pass: apply MPC peak shaving against the tick total.
            total = sum(res["applied_kw"] for _, _, _, res in tick_rows)
            for r, head, conf, res in tick_rows:
                if (is_peak_hour(t) and res["granted"]
                        and r.control_tier == "advisory"
                        and total > soft_cap_kw):
                    res["applied_kw"] *= 0.5
                    res["reason"] += " | MPC dim 50% (peak)"
                db.add(SensorEvent(
                    room_id=r.id, timestamp=t,
                    headcount=head, confidence=conf,
                ))
                db.add(PowerDecision(
                    room_id=r.id, timestamp=t, headcount=head,
                    granted=res["granted"], reason=res["reason"],
                    identity_ok=res["identity_ok"], presence_ok=res["presence_ok"],
                    context_ok=res["context_ok"],
                    applied_kw=res["applied_kw"], baseline_kw=res["baseline_kw"],
                ))
                decisions_count += 1
            rooms_count = len(rooms)

        db.commit()
        print(f"Backfilled {decisions_count} decisions across {rooms_count} rooms "
              f"({hours}h @ {step_minutes}min step).")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--days", type=int, default=0,
                    help="overrides --hours: backfill this many days at 30-min steps")
    ap.add_argument("--step-minutes", type=int, default=10)
    args = ap.parse_args()
    if args.days:
        backfill(hours=args.days * 24, step_minutes=30)
    else:
        backfill(args.hours, args.step_minutes)
