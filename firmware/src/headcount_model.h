/*
 * Lightweight CNN for human/object disambiguation from VL53L5CX 8x8 ToF frames.
 *
 * MODEL ARCHITECTURE (target):
 *   Input:   [1, 8, 8, 1] float32 — normalized depth in meters, 0–4 m range
 *   Conv2D:  3x3, 8 filters, ReLU  ->  [1, 6, 6, 8]
 *   Conv2D:  3x3, 16 filters, ReLU ->  [1, 4, 4, 16]
 *   Flatten  ->  [1, 256]
 *   Dense:   32, ReLU
 *   Dense:   8 (one-hot headcount 0–7)
 *   Softmax
 *
 * Trained against synthetic 8x8 frames + a small lab-collected set
 * (occupancy 0..7 with various postures + non-human objects).
 *
 * QUANTIZATION:
 *   int8 post-training quantization, ~12 KB model, < 100 ms inference on
 *   ESP32-WROOM-32 @ 240 MHz.
 *
 * THIS HEADER IS A PLACEHOLDER. To enable real inference:
 *   1. Train the model in TF (see scripts/train_headcount.py).
 *   2. tflite_convert --quantize → headcount_model.tflite
 *   3. xxd -i headcount_model.tflite > headcount_model_data.h
 *   4. Replace `kModelData` below with the generated array.
 *
 * Until the model is supplied, infer_headcount_cnn() returns -1 and the
 * caller falls back to the centroid heuristic in main.cpp.
 */
#pragma once
#include <stdint.h>

// Replace this stub with a real flatbuffer when you have a trained model.
// Length 0 means "no model loaded — use heuristic fallback".
static const uint8_t kModelData[] = { /* paste xxd output here */ };
static const int kModelDataLen = 0;

// Tensor arena size (rule of thumb: 2x model size, max activations).
static const int kTensorArenaSize = 32 * 1024;
