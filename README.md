# EcoTrust — Zero-Trust Energy Ecosystem

UM Hackathon 2026 · Track: Sustainable Energy

> *Let every kilowatt-hour have a name, a reason, and verifiable proof.*

## Stack

- **Backend / API / Decision engine / MPC**: Python + FastAPI + SQLAlchemy (SQLite)
- **Dashboard**: Jinja2 + Tailwind (CDN) + Chart.js — served by FastAPI, no Node required
- **Edge simulator**: Python (`httpx`) emulating VL53L5CX ToF gateways
- **PDF export**: ReportLab (Bursa Sustainability Reporting Guide 3rd Edition style)

## Layout

```
backend/
  main.py            FastAPI app, all HTTP routes
  decision_engine.py Identity × Presence × Context  →  Grant / Deny
  mpc.py             Peak-shaving heuristic (TNB Tariff C)
  ghg.py             Scope 2 emissions math
  pdf_export.py      Bursa-format PDF generator
  models.py          Room / SensorEvent / PowerDecision tables
  seed.py            Seeds 8 rooms across 3 floors
  templates/         dashboard.html
  static/            dashboard.js
edge/
  sensor_simulator.py  Posts synthetic ToF events to /ingest/sensor
```

## Quick start (Windows)

```powershell
.\run.ps1
```

This creates a venv, installs deps, seeds the DB, launches the API, and starts
the sensor simulator. Open <http://127.0.0.1:8000>.

## Manual start

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # or  source .venv/bin/activate
pip install -r requirements.txt
python -m backend.seed
uvicorn backend.main:app --reload
# in another terminal:
python -m edge.sensor_simulator
```

## Key endpoints

| Method | Path                          | Purpose                                  |
| ------ | ----------------------------- | ---------------------------------------- |
| POST   | `/ingest/sensor`              | Edge gateway pushes ToF event           |
| GET    | `/events`                     | SSE live stream (decisions + overrides)  |
| GET    | `/rooms`                      | List rooms / RBAC config                 |
| GET    | `/occupancy/latest`           | Real-time room status (heatmap)          |
| GET    | `/consumption/daily`          | Hourly kW: actual vs baseline            |
| GET    | `/ghg/scope2`                 | Scope 2 auto-calc                        |
| GET    | `/peak/savings`               | MPC peak-shaving + RM/month              |
| GET    | `/departments/leaderboard`    | Behavioral nudge layer                   |
| GET    | `/tenants/attribution`        | Open API for per-tenant attribution      |
| GET    | `/audit?room_id=&limit=`      | Full algorithmic transparency log        |
| GET    | `/notifications`              | Facility engineer queue                  |
| POST   | `/notifications/{id}/ack`     | Acknowledge a notification               |
| POST   | `/override/{room_id}`         | Manual override (auth-gated)             |
| DELETE | `/override/{room_id}`         | Clear override (auth-gated)              |
| POST   | `/nudge/test`                 | Fire a sample webhook digest             |
| GET    | `/room/{id}`                  | Per-room drill-down page                 |
| GET    | `/room/{id}/timeseries`       | Per-room kW + headcount series           |
| GET    | `/export/scope2.pdf`          | One-click Bursa-format PDF               |

Swagger UI at `/docs`.

## Environment variables

| Name | Purpose |
| ---- | ------- |
| `ECOTRUST_API_KEY` | If set, `POST/DELETE /override/*` and `/nudge/test` require `X-API-Key` header. |
| `ECOTRUST_NUDGE_WEBHOOK_URL` | Slack/Teams-compatible webhook for the nudge layer (slide 15). |

## Mapping pitch → code

| Slide                                | Where it lives                        |
| ------------------------------------ | ------------------------------------- |
| Identity × Presence × Context (5)    | `backend/decision_engine.py`          |
| Identity-Presence Decoupling (6)     | `models.py` — no person ID stored     |
| Three control tiers (7)              | `Room.control_tier`, MPC respects it  |
| ToF count logic +1/-1 (8)            | Simulator emits headcount; engine consumes |
| MPC peak shaving 14:00–22:00 (9)     | `backend/mpc.py`                      |
| Compliance dashboard (11)            | `templates/dashboard.html` + `dashboard.js` |
| Bursa PDF export (11)                | `backend/pdf_export.py`               |
| Open API per-tenant attribution (16) | `GET /tenants/attribution`            |

## Next steps for production

1. Replace simulator with ESP32 firmware (MQTT over TLS) — only the transport changes.
2. Swap SQLite → TimescaleDB for time-series scale.
3. Replace MPC heuristic with `cvxpy` QP over thermal state.
4. Add Slack/Teams nudge-layer webhook.
5. Wire MyIPO provisional patent claims (Identity-Presence Decoupling).
