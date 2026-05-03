"""
Model Predictive Control (MPC) for TNB Tariff C peak shaving.

Two layers:

1. **Per-tick QP-optimal shedding** (`shave_peak`): given the current loads
   under cap, find dim factors `x_i ∈ [0.5, 1.0]` for advisory rooms that
   minimize squared comfort loss `Σ(1 - x_i)²` subject to the linear
   constraint `Σ a_i x_i + fixed_load ≤ soft_cap`. Closed-form via Lagrangian
   bisection — no scipy/cvxpy dependency.

2. **Rolling horizon planner** (`plan_horizon`): forecasts the next H ticks
   with a persistence model from the last N minutes, then applies the QP at
   each forecasted tick. Returns the planned dim schedule. Only the first
   tick's decision is binding — subsequent ticks are recomputed each cycle
   (the rolling-horizon discipline).

The greedy heuristic that previously lived here was an honest approximation;
this module is the production-shape MPC.
"""
from datetime import datetime, timedelta
from typing import Iterable

# TNB 2025 Tariff C
MD_RATE_RM_PER_KW = 35.50
PEAK_START = 14
PEAK_END = 22

# Allowed dimming range for advisory tier. 1.0 = full power, 0.5 = 50% dim.
ADVISORY_X_LO = 0.5
ADVISORY_X_HI = 1.0

# Default rolling horizon (15-min ticks ahead, slide 9).
DEFAULT_HORIZON = 4
DEFAULT_TICK_MINUTES = 15


def is_peak_hour(now: datetime) -> bool:
    return PEAK_START <= now.hour < PEAK_END


# ---------- 1) Closed-form QP shedding ----------
def _solve_dim_factors(advisory_kw: list[float], target_total: float,
                       lo: float = ADVISORY_X_LO,
                       hi: float = ADVISORY_X_HI) -> list[float]:
    """Solve: minimize Σ(1-x_i)² s.t. Σ a_i x_i ≤ target_total, lo ≤ x_i ≤ hi.

    KKT condition: x_i = clip(1 - λ a_i / 2, lo, hi). Find λ ≥ 0 that makes
    the inequality tight (or λ=0 if already feasible at x_i=hi).
    """
    if not advisory_kw:
        return []
    full_total = sum(a * hi for a in advisory_kw)
    if full_total <= target_total:
        return [hi] * len(advisory_kw)
    floor_total = sum(a * lo for a in advisory_kw)
    if floor_total >= target_total:
        # Even maximum dim insufficient — clamp everyone to lo.
        return [lo] * len(advisory_kw)

    # Bisect λ such that Σ a_i x_i(λ) = target_total.
    def total_at(lam: float) -> float:
        return sum(a * max(lo, min(hi, hi - lam * a / 2.0))
                   for a in advisory_kw)

    lam_lo, lam_hi = 0.0, 100.0
    for _ in range(60):
        lam = 0.5 * (lam_lo + lam_hi)
        s = total_at(lam)
        if s > target_total:
            lam_lo = lam
        else:
            lam_hi = lam
    lam = 0.5 * (lam_lo + lam_hi)
    return [max(lo, min(hi, hi - lam * a / 2.0)) for a in advisory_kw]


def shave_peak(room_loads: list[tuple[int, float, str]],
               soft_cap_kw: float,
               now: datetime) -> list[tuple[int, float, str]]:
    """Apply QP-optimal dimming to advisory rooms during peak hours."""
    if not is_peak_hour(now):
        return room_loads

    advisory = [(rid, kw) for rid, kw, t in room_loads if t == "advisory"]
    fixed = sum(kw for _, kw, t in room_loads if t != "advisory")

    if not advisory:
        return room_loads

    target_advisory = max(0.0, soft_cap_kw - fixed)
    factors = _solve_dim_factors([kw for _, kw in advisory], target_advisory)
    factor_by_id = {rid: f for (rid, _), f in zip(advisory, factors)}

    return [
        (rid, kw * factor_by_id.get(rid, 1.0), tier)
        for rid, kw, tier in room_loads
    ]


# ---------- 2) Rolling horizon planner ----------
def forecast_room_load(history: list[tuple[datetime, float]],
                       horizon_ticks: int = DEFAULT_HORIZON,
                       tick_minutes: int = DEFAULT_TICK_MINUTES,
                       window_minutes: int = 60) -> list[float]:
    """Persistence forecast: use the recent average as the next-N-tick estimate.

    `history` is `[(timestamp, applied_kw), ...]`. Production would use an
    ARIMA / LSTM here; persistence is the correct hackathon baseline.
    """
    if not history:
        return [0.0] * horizon_ticks
    cutoff = history[-1][0] - timedelta(minutes=window_minutes)
    recent = [kw for ts, kw in history if ts >= cutoff]
    avg = sum(recent) / len(recent) if recent else history[-1][1]
    return [avg] * horizon_ticks


def plan_horizon(rooms: list[tuple[int, float, str]],
                 history_per_room: dict[int, list[tuple[datetime, float]]],
                 soft_cap_kw: float,
                 start_at: datetime,
                 horizon_ticks: int = DEFAULT_HORIZON,
                 tick_minutes: int = DEFAULT_TICK_MINUTES,
                 ) -> list[dict]:
    """Project H ticks ahead and run the QP at each. Returns the planned dim
    schedule. Only the first tick's plan is binding (rolling horizon)."""
    forecasts = {rid: forecast_room_load(history_per_room.get(rid, []),
                                         horizon_ticks, tick_minutes)
                 for rid, _, _ in rooms}
    plan = []
    for h in range(horizon_ticks):
        t = start_at + timedelta(minutes=h * tick_minutes)
        # Build per-tick load list using rated-kw × occupancy-driven factor.
        tick_loads = []
        for rid, rated_kw, tier in rooms:
            forecast_kw = forecasts[rid][h] if forecasts[rid] else 0.0
            # Bound to rated.
            forecast_kw = max(0.0, min(rated_kw, forecast_kw))
            tick_loads.append((rid, forecast_kw, tier))
        shaved = shave_peak(tick_loads, soft_cap_kw, t)
        plan.append({
            "tick": h,
            "timestamp": t.isoformat(),
            "is_peak": is_peak_hour(t),
            "rooms": [
                {"room_id": rid,
                 "forecast_kw": round(orig, 3),
                 "applied_kw": round(applied, 3),
                 "dim_factor": round(applied / orig, 3) if orig > 0 else 1.0,
                 "tier": tier}
                for (rid, orig, tier), (_, applied, _) in zip(tick_loads, shaved)
            ],
            "total_forecast_kw": round(sum(kw for _, kw, _ in tick_loads), 3),
            "total_applied_kw": round(sum(kw for _, kw, _ in shaved), 3),
        })
    return plan


def estimated_md_savings_rm(baseline_peak_kw: float, shaved_peak_kw: float) -> float:
    delta = max(0.0, baseline_peak_kw - shaved_peak_kw)
    return delta * MD_RATE_RM_PER_KW
