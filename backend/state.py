"""In-process app state: overrides, notifications, SSE subscribers,
presence-grace tracking, webhook config, and API-key auth."""
import asyncio
import os
from collections import deque
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

# 30-second confirmation window (slide 8 step 4) — keeps power granted briefly
# after the last verified occupant leaves, preventing flicker.
PRESENCE_GRACE_SECONDS = 30

# Slack/Teams nudge-layer webhook (slide 15). Set via env. Falsy disables it.
NUDGE_WEBHOOK_URL = os.environ.get("ECOTRUST_NUDGE_WEBHOOK_URL", "").strip()

# Optional API key gating writes (POST/DELETE /override). Unset = open dev mode.
API_KEY = os.environ.get("ECOTRUST_API_KEY", "").strip()


@dataclass
class Override:
    room_id: int
    granted: bool
    expires_at: datetime
    reason: str


@dataclass
class Notification:
    id: int
    room_id: int
    room_name: str
    severity: str  # "info" | "advisory" | "warning"
    message: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    acknowledged: bool = False


class AppState:
    def __init__(self):
        self.overrides: dict[int, Override] = {}
        self.notifications: deque[Notification] = deque(maxlen=200)
        self._notif_seq: int = 0
        self.sse_clients: set[asyncio.Queue] = set()
        # Soft cap for MPC peak shaving during 14:00-22:00 (kW total building load).
        self.peak_soft_cap_kw: float = 25.0
        # Last UTC time each room had verified presence — used for the 30s grace.
        self.last_presence_seen: dict[int, datetime] = {}
        # Per-department daily kWh budget (None = no budget set).
        self.dept_daily_kwh_budget: dict[str, float] = {}
        # Per-room daily kWh budget.
        self.room_daily_kwh_budget: dict[int, float] = {}
        # Tracks dept names already alerted today so we don't spam.
        self._dept_alerted_on: dict[str, datetime] = {}
        self._room_alerted_on: dict[int, datetime] = {}

    def should_alert_dept(self, dept: str, now: datetime) -> bool:
        last = self._dept_alerted_on.get(dept)
        if last is None or last.date() != now.date():
            self._dept_alerted_on[dept] = now
            return True
        return False

    def should_alert_room(self, room_id: int, now: datetime) -> bool:
        last = self._room_alerted_on.get(room_id)
        if last is None or last.date() != now.date():
            self._room_alerted_on[room_id] = now
            return True
        return False

    # --- Presence grace (slide 8 step 4) ---
    def mark_presence(self, room_id: int, when: datetime) -> None:
        self.last_presence_seen[room_id] = when

    def in_grace_window(self, room_id: int, now: datetime) -> bool:
        last = self.last_presence_seen.get(room_id)
        if last is None:
            return False
        return (now - last).total_seconds() < PRESENCE_GRACE_SECONDS

    # --- Overrides ---
    def set_override(self, room_id: int, granted: bool, ttl_s: int, reason: str) -> Override:
        ov = Override(
            room_id=room_id, granted=granted,
            expires_at=datetime.utcnow() + timedelta(seconds=ttl_s),
            reason=reason,
        )
        self.overrides[room_id] = ov
        return ov

    def clear_override(self, room_id: int) -> bool:
        return self.overrides.pop(room_id, None) is not None

    def get_override(self, room_id: int) -> Optional[Override]:
        ov = self.overrides.get(room_id)
        if ov is None:
            return None
        if datetime.utcnow() > ov.expires_at:
            self.overrides.pop(room_id, None)
            return None
        return ov

    # --- Notifications ---
    def push_notification(self, room_id: int, room_name: str, severity: str, message: str) -> Notification:
        self._notif_seq += 1
        n = Notification(id=self._notif_seq, room_id=room_id, room_name=room_name,
                         severity=severity, message=message)
        self.notifications.appendleft(n)
        return n

    def ack_notification(self, nid: int) -> bool:
        for n in self.notifications:
            if n.id == nid:
                n.acknowledged = True
                return True
        return False

    # --- SSE pub/sub ---
    async def broadcast(self, event: dict) -> None:
        dead = []
        for q in list(self.sse_clients):
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(q)
        for q in dead:
            self.sse_clients.discard(q)


state = AppState()
