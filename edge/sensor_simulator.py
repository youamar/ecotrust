"""
ToF Sensor Simulator — emulates VL53L5CX edge gateways.

Each room has its own occupancy pattern. Posts events to the FastAPI ingest
endpoint at a configurable cadence. Demonstrates:
  - Stationary occupants (PIR would miss these — ToF doesn't)
  - Tailgating immunity (in/out +1/-1 logic)
  - Confidence scores from the lightweight CNN

Run:  python -m edge.sensor_simulator
"""
import argparse
import json
import math
import os
import random
import time
from datetime import datetime, timezone

import httpx

DEFAULT_API = "http://127.0.0.1:8000"


def occupancy_for_room(room_id: int, name: str, tier: str, hour: float) -> int:
    """Plausible headcount as a function of room and hour-of-day."""
    if "Server" in name:
        return 0  # untouched, but still reports
    if tier == "advisory":  # large shared spaces
        peak = 30 if "Hall" in name else 15
        return max(0, int(peak * math.exp(-((hour - 13) ** 2) / 18) + random.randint(-2, 2)))
    # office / meeting
    if 9 <= hour <= 18:
        base = random.choices([0, 1, 2, 3, 4], weights=[1, 2, 3, 2, 1])[0]
    elif 18 < hour <= 21:
        base = random.choices([0, 1, 2], weights=[5, 2, 1])[0]
    else:
        base = 0
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between event batches (default 2s)")
    ap.add_argument("--speedup", type=float, default=1.0,
                    help="virtual-time multiplier (1.0 = real time, 60 = 1min/sec)")
    ap.add_argument("--transport", choices=["http", "mqtt"], default="http",
                    help="http: POST to /ingest/sensor; mqtt: publish ecotrust/sensor/{id}")
    args = ap.parse_args()

    mqtt_pub = None
    if args.transport == "mqtt":
        import paho.mqtt.client as mqtt
        mqtt_pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                               client_id=f"ecotrust-sim-{os.getpid()}")
        host = os.environ.get("ECOTRUST_MQTT_HOST", "localhost")
        port = int(os.environ.get("ECOTRUST_MQTT_PORT", "1883"))
        mqtt_pub.connect(host, port, keepalive=30)
        mqtt_pub.loop_start()
        print(f"MQTT publishing to {host}:{port} on ecotrust/sensor/+")

    client = httpx.Client(timeout=5.0)
    rooms = client.get(f"{args.api}/rooms").json()
    if not rooms:
        print("No rooms found. Run: python -m backend.seed")
        return
    print(f"Simulating {len(rooms)} rooms against {args.api} (speedup x{args.speedup})")

    t0 = time.time()
    while True:
        elapsed = (time.time() - t0) * args.speedup
        # Project virtual time-of-day starting from now
        virtual_now = datetime.fromtimestamp(time.time() + elapsed - (time.time() - t0))
        hour = (virtual_now.hour + virtual_now.minute / 60.0) % 24

        for r in rooms:
            head = occupancy_for_room(r["id"], r["name"], r["control_tier"], hour)
            conf = round(random.uniform(0.75, 0.99), 2) if head > 0 else 0.95
            payload = {
                "room_id": r["id"],
                "headcount": head,
                "confidence": conf,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                if mqtt_pub is not None:
                    mqtt_pub.publish(
                        f"ecotrust/sensor/{r['id']}",
                        json.dumps({"headcount": head, "confidence": conf}),
                        qos=1,
                    )
                    print(f"  → mqtt publish {r['name']:<20} head={head} conf={conf}")
                else:
                    resp = client.post(f"{args.api}/ingest/sensor", json=payload)
                    if resp.status_code != 200:
                        print(f"[!] {r['name']}: {resp.status_code} {resp.text}")
                    else:
                        d = resp.json()
                        flag = "✓" if d["granted"] else "✗"
                        print(f"  {flag} {r['name']:<20} head={head} {d['applied_kw']:.2f}kW  ({d['reason']})")
            except Exception as e:
                print(f"[err] {r['name']}: {e}")
        print("-" * 60)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
