# EcoTrust Edge Firmware (ESP32 + VL53L5CX)

Hackathon-grade skeleton for the per-room edge gateway from slide 10.
Total BOM: **RM 430** (ToF RM 280 + ESP32 RM 60 + smart relay RM 90).

## What it does

1. Reads an 8×8 distance frame from the VL53L5CX ToF sensor at 15 Hz.
2. Runs a Kalman-style exponential smoother per zone.
3. Calls `infer_headcount()` — currently a centroid heuristic; replace with a
   TFLite-Micro CNN for production-grade human/object disambiguation.
4. POSTs `{room_id, headcount, confidence}` to the EcoTrust API.
5. Drives a smart relay with the API's `granted` decision.

Production transport per slide 10 is **MQTT over TLS** with local autonomy on
network drop — `PubSubClient` is in the dependencies; swap `HTTPClient` for it
once a broker is online.

## Build

```bash
pio run -t upload
pio device monitor
```

Override `ECOTRUST_API` and `ECOTRUST_ROOM_ID` per node:

```ini
build_flags =
    -DECOTRUST_API="\"http://10.0.1.5:8000\""
    -DECOTRUST_ROOM_ID=4
```

## Privacy note (slide 6)

This firmware never transmits raw frames. Only an integer headcount and a
confidence float leave the edge — the **Identity-Presence Decoupling** patent
hinges on this. PDPA 2010 §4 compliant by construction.
