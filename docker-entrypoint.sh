#!/bin/sh
set -e
# Seed + backfill only if the DB has no rooms yet (first boot).
python - <<'PY'
from backend.database import Base, engine, SessionLocal
from backend.models import Room
Base.metadata.create_all(bind=engine)
db = SessionLocal()
empty = db.query(Room).count() == 0
db.close()
exit(0 if empty else 1)
PY
if [ $? -eq 0 ]; then
  echo "[entrypoint] seeding fresh DB"
  python -m backend.seed
  python -m backend.demo_seed --hours 24 --step-minutes 15
else
  echo "[entrypoint] DB already populated, skipping seed"
fi
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
