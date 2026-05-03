"""MPC peak shaving + tariff math."""
from datetime import datetime

from backend.mpc import (
    is_peak_hour, shave_peak, estimated_md_savings_rm, MD_RATE_RM_PER_KW,
)
from backend.tariff import project_monthly_bill, ENERGY_RATE_RM_PER_KWH


PEAK = datetime(2026, 5, 3, 16, 0)
OFFPEAK = datetime(2026, 5, 3, 9, 0)


def test_peak_window_detection():
    assert is_peak_hour(PEAK) is True
    assert is_peak_hour(OFFPEAK) is False
    assert is_peak_hour(datetime(2026, 5, 3, 22, 0)) is False  # exclusive end


def test_shave_noop_offpeak():
    loads = [(1, 10.0, "advisory"), (2, 10.0, "full")]
    assert shave_peak(loads, soft_cap_kw=5.0, now=OFFPEAK) == loads


def test_shave_skips_full_tier():
    loads = [(1, 30.0, "full")]
    out = shave_peak(loads, soft_cap_kw=10.0, now=PEAK)
    # Full tier never shed — employee UX trumps savings.
    assert out == loads


def test_shave_dims_advisory_when_over_cap():
    loads = [(1, 20.0, "advisory"), (2, 5.0, "full")]
    out = shave_peak(loads, soft_cap_kw=15.0, now=PEAK)
    advisory_kw = next(kw for rid, kw, _ in out if rid == 1)
    full_kw = next(kw for rid, kw, _ in out if rid == 2)
    assert advisory_kw < 20.0
    assert full_kw == 5.0


def test_qp_shave_respects_lower_bound():
    """When even maximum dim is insufficient, advisory rooms clamp to 50%."""
    loads = [(1, 100.0, "advisory")]
    out = shave_peak(loads, soft_cap_kw=10.0, now=PEAK)
    advisory_kw = next(kw for rid, kw, _ in out if rid == 1)
    assert advisory_kw == 50.0  # 100 * 0.5 lower bound


def test_qp_shave_distributes_pain_across_rooms():
    """Two equal advisory rooms should be dimmed by the same factor."""
    loads = [(1, 10.0, "advisory"), (2, 10.0, "advisory")]
    out = shave_peak(loads, soft_cap_kw=15.0, now=PEAK)
    a = next(kw for rid, kw, _ in out if rid == 1)
    b = next(kw for rid, kw, _ in out if rid == 2)
    assert abs(a - b) < 0.01
    assert abs((a + b) - 15.0) < 0.1


def test_horizon_plan_returns_n_ticks():
    from datetime import datetime as dt
    from backend.mpc import plan_horizon
    rooms = [(1, 10.0, "advisory"), (2, 5.0, "full")]
    history = {1: [(dt(2026, 5, 3, 15, 0), 8.0)]}
    plan = plan_horizon(rooms, history, soft_cap_kw=12.0,
                        start_at=dt(2026, 5, 3, 15, 0), horizon_ticks=4)
    assert len(plan) == 4
    assert all("rooms" in tick and "total_applied_kw" in tick for tick in plan)


def test_md_savings_math():
    # 100 kW shaved off peak = 100 * 35.50 = RM 3,550 per month
    assert estimated_md_savings_rm(200.0, 100.0) == 100.0 * MD_RATE_RM_PER_KW
    assert estimated_md_savings_rm(100.0, 200.0) == 0.0  # never negative


def test_tariff_breakdown_components_match_rates():
    bill = project_monthly_bill(total_kwh_30d=10_000, peak_kw=50,
                                contracted_demand_kw=50)
    assert bill.energy_rm == round(10_000 * ENERGY_RATE_RM_PER_KWH, 2)
    assert bill.md_rm == round(50 * MD_RATE_RM_PER_KW, 2)
    # Total should be greater than the sum of charges (tax adds on top).
    bare = bill.energy_rm + bill.md_rm + bill.capacity_rm + bill.kwbb_rm
    assert bill.total_rm > bare


def test_tariff_lower_peak_yields_lower_bill():
    high = project_monthly_bill(10_000, 100, 100)
    low = project_monthly_bill(10_000, 50, 50)
    assert low.total_rm < high.total_rm
    assert (high.total_rm - low.total_rm) > 50 * MD_RATE_RM_PER_KW
