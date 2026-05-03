"""
MQTT bridge — production transport per slide 10.

Subscribes to `ecotrust/sensor/<room_id>` and forwards each message to the
local FastAPI ingest endpoint. Opt-in via env vars; the rest of the stack
runs fine without it.

Topic schema (matches firmware):
    Topic:    ecotrust/sensor/{room_id}
    Payload:  {"headcount": int, "confidence": float}

Configuration (all optional):
    ECOTRUST_MQTT_HOST      — defaults to localhost
    ECOTRUST_MQTT_PORT      — defaults to 1883 (8883 for TLS)
    ECOTRUST_MQTT_TLS       — "1" enables TLS
    ECOTRUST_MQTT_USERNAME  — broker auth
    ECOTRUST_MQTT_PASSWORD  — broker auth
    ECOTRUST_API            — local API base URL (default http://127.0.0.1:8000)
"""
import json
import logging
import os
import sys

import httpx
import paho.mqtt.client as mqtt

log = logging.getLogger("ecotrust.mqtt")

HOST = os.environ.get("ECOTRUST_MQTT_HOST", "localhost")
PORT = int(os.environ.get("ECOTRUST_MQTT_PORT", "1883"))
TLS = os.environ.get("ECOTRUST_MQTT_TLS", "0") == "1"
USERNAME = os.environ.get("ECOTRUST_MQTT_USERNAME") or None
PASSWORD = os.environ.get("ECOTRUST_MQTT_PASSWORD") or None
API = os.environ.get("ECOTRUST_API", "http://127.0.0.1:8000")
TOPIC = "ecotrust/sensor/+"


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("MQTT connected to %s:%s — subscribing %s", HOST, PORT, TOPIC)
        client.subscribe(TOPIC, qos=1)
    else:
        log.error("MQTT connect failed (rc=%s)", rc)


def on_message(client, userdata, msg):
    try:
        room_id = int(msg.topic.rsplit("/", 1)[-1])
        payload = json.loads(msg.payload.decode("utf-8"))
        body = {
            "room_id": room_id,
            "headcount": int(payload["headcount"]),
            "confidence": float(payload.get("confidence", 0.9)),
        }
        r = httpx.post(f"{API}/ingest/sensor", json=body, timeout=3.0)
        if r.status_code != 200:
            log.warning("ingest failed: %s %s", r.status_code, r.text)
    except Exception as e:
        log.warning("MQTT message dropped: %s — payload=%s", e, msg.payload[:120])


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="ecotrust-bridge")
    if USERNAME:
        client.username_pw_set(USERNAME, PASSWORD)
    if TLS:
        client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message

    log.info("Connecting to MQTT %s:%s (tls=%s) → forwarding to %s", HOST, PORT, TLS, API)
    client.connect(HOST, PORT, keepalive=30)
    client.loop_forever()


if __name__ == "__main__":
    sys.exit(main())
