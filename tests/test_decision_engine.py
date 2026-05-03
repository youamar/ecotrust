"""Zero-trust decision engine: verify every branch of Identity × Presence × Context."""
from datetime import datetime
from types import SimpleNamespace

from backend.decision_engine import evaluate


def make_room(tier="full", start=8, end=20, kw=2.5):
    return SimpleNamespace(
        id=1, name="R1", control_tier=tier,
        authorized_start_hour=start, authorized_end_hour=end,
        rated_kw=kw,
    )


WORKING_NOON = datetime(2026, 5, 3, 12, 0)
NIGHT = datetime(2026, 5, 3, 23, 0)


def test_grant_when_all_three_pass():
    r = evaluate(make_room(), headcount=3, confidence=0.95, now=WORKING_NOON)
    assert r["granted"] is True
    assert r["identity_ok"] and r["presence_ok"] and r["context_ok"]
    assert r["applied_kw"] == 2.5


def test_deny_outside_working_hours():
    r = evaluate(make_room(), headcount=3, confidence=0.95, now=NIGHT)
    assert r["granted"] is False
    assert r["context_ok"] is False
    # Phased-down floor is 10% of rated load.
    assert r["applied_kw"] == 0.25


def test_deny_when_no_presence():
    r = evaluate(make_room(), headcount=0, confidence=0.95, now=WORKING_NOON)
    assert r["granted"] is False
    assert r["presence_ok"] is False


def test_deny_when_low_confidence_even_with_headcount():
    r = evaluate(make_room(), headcount=2, confidence=0.3, now=WORKING_NOON)
    assert r["granted"] is False
    assert r["presence_ok"] is False


def test_untouched_tier_is_passthrough_air_gap():
    """Slide 7: fire / critical infra is physically air-gapped. EcoTrust
    must never deny these rooms — full rated load, baseline = applied."""
    room = make_room(tier="untouched", kw=6.0)
    r = evaluate(room, headcount=3, confidence=0.95, now=WORKING_NOON)
    assert r["granted"] is True
    assert r["identity_ok"] is False  # we have no authority over it
    assert r["applied_kw"] == r["baseline_kw"] == 6.0
    assert "air-gapped" in r["reason"].lower()


def test_untouched_passthrough_holds_at_night_too():
    """24/7 pass-through — context check does not apply."""
    r = evaluate(make_room(tier="untouched"), headcount=0, confidence=0.95, now=NIGHT)
    assert r["granted"] is True
    assert r["applied_kw"] == r["baseline_kw"]


def test_grace_window_holds_power_when_just_emptied():
    r = evaluate(make_room(), headcount=0, confidence=0.95,
                 now=WORKING_NOON, in_grace=True)
    assert r["granted"] is True
    assert "Grace window" in r["reason"]


def test_grace_does_not_override_context_failure():
    r = evaluate(make_room(), headcount=0, confidence=0.95,
                 now=NIGHT, in_grace=True)
    assert r["granted"] is False
    assert r["context_ok"] is False


def test_advisory_tier_still_grantable():
    r = evaluate(make_room(tier="advisory"), headcount=10, confidence=0.95,
                 now=WORKING_NOON)
    assert r["granted"] is True
    assert r["identity_ok"] is True
