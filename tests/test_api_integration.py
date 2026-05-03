"""
End-to-end FastAPI integration: ingest → DB → query → export.

Each test gets a fresh SQLite DB so there's no cross-test contamination.
"""
import os
import tempfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    # Point the app at a throwaway DB before importing modules that bind to it.
    db_dir = tempfile.mkdtemp()
    db_path = os.path.join(db_dir, "test.db")
    monkeypatch.setenv("ECOTRUST_API_KEY", "")  # open dev mode

    # Force a fresh module graph so the SQLAlchemy engine binds to our path.
    import sys
    for m in list(sys.modules):
        if m.startswith("backend"):
            del sys.modules[m]

    from backend import database  # noqa: F401
    database.engine.dispose()
    database.engine = database.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    database.SessionLocal.configure(bind=database.engine)

    from backend.database import Base, engine
    from backend.models import Room
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Seed two rooms covering full + advisory tier.
    from backend.database import SessionLocal
    db = SessionLocal()
    db.add(Room(name="MR-1", floor=1, department="Eng",
                authorized_start_hour=0, authorized_end_hour=24,
                rated_kw=2.0, control_tier="full"))
    db.add(Room(name="Lab", floor=1, department="Eng",
                authorized_start_hour=0, authorized_end_hour=24,
                rated_kw=8.0, control_tier="advisory"))
    db.commit()
    db.close()

    from backend.main import app
    yield TestClient(app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_ingest_grants_when_occupied(client):
    r = client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": 2, "confidence": 0.9})
    assert r.status_code == 200
    body = r.json()
    assert body["granted"] is True
    assert body["applied_kw"] == 2.0


def test_ingest_denies_when_empty(client):
    r = client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": 0, "confidence": 0.9})
    assert r.status_code == 200
    assert r.json()["granted"] is False


def test_unknown_room_returns_404(client):
    r = client.post("/ingest/sensor",
                    json={"room_id": 999, "headcount": 1, "confidence": 0.9})
    assert r.status_code == 404


def test_audit_returns_recent_decisions(client):
    for h in (0, 2, 0):
        client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": h, "confidence": 0.9})
    r = client.get("/audit?limit=5")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 3
    assert {row["granted"] for row in rows} == {True, False}


def test_override_grants_with_zero_occupants(client):
    r = client.post("/override/1",
                    json={"granted": True, "ttl_seconds": 60, "reason": "test"})
    assert r.status_code == 200
    r = client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": 0, "confidence": 0.9})
    assert r.json()["granted"] is True
    assert "OVERRIDE" in r.json()["reason"]


def test_override_clear(client):
    client.post("/override/1",
                json={"granted": True, "ttl_seconds": 60, "reason": "x"})
    r = client.delete("/override/1")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_csv_export_is_well_formed(client):
    client.post("/ingest/sensor",
                json={"room_id": 1, "headcount": 1, "confidence": 0.9})
    r = client.get("/export/audit.csv?hours=24")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    head = r.text.splitlines()[0]
    for col in ("timestamp_utc", "room_id", "applied_kw", "reason"):
        assert col in head


def test_pdf_export_is_pdf(client):
    client.post("/ingest/sensor",
                json={"room_id": 1, "headcount": 2, "confidence": 0.9})
    r = client.get("/export/scope2.pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"


def test_tariff_projection_shows_savings(client):
    # Drive some occupancy so the run rate is non-zero.
    for _ in range(3):
        client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": 2, "confidence": 0.9})
    r = client.get("/tariff/projection")
    assert r.status_code == 200
    body = r.json()
    assert body["actual"]["total_rm"] >= 0
    assert body["baseline"]["total_rm"] >= body["actual"]["total_rm"]


def test_dept_budget_fires_warning(client):
    # Drive several events so kWh aggregation has a non-trivial dt.
    for _ in range(5):
        client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": 3, "confidence": 0.95})
    # Set the budget to zero so any consumption breaches it.
    client.post("/departments/budgets",
                json={"department": "Eng", "daily_kwh": 0.0})
    # Trigger one more ingest to evaluate the budget.
    client.post("/ingest/sensor",
                json={"room_id": 1, "headcount": 3, "confidence": 0.95})
    r = client.get("/notifications").json()
    assert any("budget" in n["message"].lower() for n in r)


def test_pitch_page_loads(client):
    r = client.get("/pitch")
    assert r.status_code == 200
    assert "EcoTrust" in r.text


def test_dashboard_page_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "EcoTrust" in r.text


def test_trends_page_and_endpoint(client):
    for _ in range(3):
        client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": 2, "confidence": 0.9})
    r = client.get("/trends")
    assert r.status_code == 200
    r = client.get("/trends/daily?days=7")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_room_budget_lifecycle(client):
    r = client.post("/rooms/1/budget", json={"daily_kwh": 5.0})
    assert r.status_code == 200
    assert client.get("/rooms/1/budget").json()["daily_kwh"] == 5.0
    r = client.delete("/rooms/1/budget")
    assert r.status_code == 200
    assert client.get("/rooms/1/budget").json()["daily_kwh"] is None


def test_room_budget_alarm_fires(client):
    for _ in range(5):
        client.post("/ingest/sensor",
                    json={"room_id": 1, "headcount": 2, "confidence": 0.9})
    client.post("/rooms/1/budget", json={"daily_kwh": 0.0})
    client.post("/ingest/sensor",
                json={"room_id": 1, "headcount": 2, "confidence": 0.9})
    msgs = [n["message"].lower() for n in client.get("/notifications").json()]
    assert any("budget" in m for m in msgs)


def test_sensors_csv_export(client):
    client.post("/ingest/sensor",
                json={"room_id": 1, "headcount": 2, "confidence": 0.9})
    r = client.get("/export/sensors.csv?hours=1")
    assert r.status_code == 200
    assert "headcount" in r.text.splitlines()[0]
