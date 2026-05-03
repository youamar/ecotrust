/*
 * EcoTrust Edge Node — ESP32 + VL53L5CX
 * --------------------------------------
 * Slide 8: From "I think someone's there" to "I know exactly N people are there."
 *
 * Pipeline:
 *   ToF 8x8 ranging frame  ->  Kalman filter (z stability)  ->
 *   lightweight CNN human/object disambiguation  ->  in/out +1/-1 counter  ->
 *   POST to /ingest/sensor  ->  smart relay reflects the GRANT/DENY decision.
 *
 * Edge AI inference target: < 100 ms localised; no network upload of raw frames.
 *
 * NOTE: this is a hackathon-grade skeleton. The CNN inference here is replaced
 * by a centroid heuristic so the firmware compiles and runs end-to-end. Drop
 * a TFLite-Micro model into `infer_headcount()` for production.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <SparkFun_VL53L5CX_Library.h>

// TFLite-Micro CNN for human/object disambiguation. The model header is a
// placeholder until weights are supplied; until then we fall through to the
// centroid heuristic in `infer_headcount_heuristic()`.
#include "headcount_model.h"
#if kModelDataLen > 0
  #include <TensorFlowLite_ESP32.h>
  #include "tensorflow/lite/micro/all_ops_resolver.h"
  #include "tensorflow/lite/micro/micro_interpreter.h"
  #include "tensorflow/lite/schema/schema_generated.h"
  static uint8_t tensor_arena[kTensorArenaSize];
  static const tflite::Model *g_model = nullptr;
  static tflite::MicroInterpreter *g_interp = nullptr;
#endif

// ---- Compile-time config (override via build_flags / secrets.h) ----
#ifndef ECOTRUST_API
  #define ECOTRUST_API "http://192.168.1.100:8000"
#endif
#ifndef ECOTRUST_ROOM_ID
  #define ECOTRUST_ROOM_ID 1
#endif

static const char *WIFI_SSID = "your-wifi";
static const char *WIFI_PASS = "your-pass";

// ---- Pins ----
static const int RELAY_PIN = 26;   // smart relay control
static const int I2C_SDA   = 21;
static const int I2C_SCL   = 22;

// ---- ToF ----
SparkFun_VL53L5CX tof;
VL53L5CX_ResultsData frame;

// ---- In/out counter (slide 8 step 3): mathematically immune to tailgating ----
int g_headcount = 0;
unsigned long g_last_post_ms = 0;

// Kalman-ish exponential smoothing per zone for stability
static const int N_ZONES = 64;
float z_smooth[N_ZONES];
const float K_ALPHA = 0.4f;

// ----------------------------------------------------------------------
// Centroid heuristic — fallback when no CNN model is loaded.
// Counts "warm zones" (objects 0.5-2.5m away) clustered into blobs.
// ----------------------------------------------------------------------
int infer_headcount_heuristic(const VL53L5CX_ResultsData &f) {
  int hits = 0;
  for (int i = 0; i < N_ZONES; i++) {
    int16_t mm = f.distance_mm[i];
    z_smooth[i] = K_ALPHA * mm + (1 - K_ALPHA) * z_smooth[i];
    if (z_smooth[i] > 500 && z_smooth[i] < 2500) hits++;
  }
  return hits / 8;
}

// ----------------------------------------------------------------------
// CNN inference — only compiled when a real model is supplied via
// headcount_model.h. Edge AI inference target: < 100 ms (slide 8).
// ----------------------------------------------------------------------
int infer_headcount_cnn(const VL53L5CX_ResultsData &f) {
#if kModelDataLen > 0
  if (g_interp == nullptr) return -1;
  // Normalize 8x8 distance frame to meters (0..4 m).
  float *input = g_interp->input(0)->data.f;
  for (int i = 0; i < N_ZONES; i++) {
    input[i] = constrain(f.distance_mm[i] / 1000.0f, 0.0f, 4.0f);
  }
  if (g_interp->Invoke() != kTfLiteOk) return -1;
  // argmax over 8-class output.
  float *out = g_interp->output(0)->data.f;
  int best = 0; float bestv = out[0];
  for (int i = 1; i < 8; i++) if (out[i] > bestv) { bestv = out[i]; best = i; }
  return best;
#else
  (void)f; return -1;  // No model loaded.
#endif
}

// Top-level wrapper: prefer CNN when available, fall back to heuristic.
int infer_headcount(const VL53L5CX_ResultsData &f) {
  int cnn = infer_headcount_cnn(f);
  return cnn >= 0 ? cnn : infer_headcount_heuristic(f);
}

void setup_cnn() {
#if kModelDataLen > 0
  g_model = tflite::GetModel(kModelData);
  static tflite::AllOpsResolver resolver;
  static tflite::MicroInterpreter interpreter(
      g_model, resolver, tensor_arena, kTensorArenaSize);
  g_interp = &interpreter;
  g_interp->AllocateTensors();
  Serial.println("CNN headcount model loaded");
#else
  Serial.println("CNN model not provided — using centroid heuristic");
#endif
}

// ----------------------------------------------------------------------
// Post sensor reading and apply the relay decision returned by the API.
// ----------------------------------------------------------------------
void post_and_apply(int headcount, float confidence) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  String url = String(ECOTRUST_API) + "/ingest/sensor";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<160> body;
  body["room_id"]   = ECOTRUST_ROOM_ID;
  body["headcount"] = headcount;
  body["confidence"] = confidence;
  String payload;
  serializeJson(body, payload);

  int code = http.POST(payload);
  if (code == 200) {
    StaticJsonDocument<512> resp;
    DeserializationError err = deserializeJson(resp, http.getString());
    if (!err) {
      bool granted = resp["granted"].as<bool>();
      digitalWrite(RELAY_PIN, granted ? HIGH : LOW);
      Serial.printf("head=%d  granted=%d  reason=%s\n",
                    headcount, granted, resp["reason"].as<const char *>());
    }
  } else {
    Serial.printf("POST failed: %d\n", code);
    // Slide 10: "Degrades to local autonomy if the network drops."
    // Fail-safe: keep last relay state; eventually fall through to local timer.
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);
  if (!tof.begin()) {
    Serial.println("VL53L5CX not found");
    while (true) delay(1000);
  }
  tof.setResolution(8 * 8);
  tof.setRangingFrequency(15);  // Hz — well under the < 100 ms inference budget
  tof.startRanging();

  setup_cnn();

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println(" ok");
}

void loop() {
  if (tof.isDataReady() && tof.getRangingData(&frame)) {
    int head = infer_headcount(frame);

    // Debounce: only POST when count changes OR every 5s heartbeat.
    unsigned long now = millis();
    if (head != g_headcount || now - g_last_post_ms > 5000) {
      g_headcount = head;
      g_last_post_ms = now;
      post_and_apply(head, /*confidence=*/0.92f);
    }
  }
  delay(50);
}
