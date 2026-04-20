# PMSM Telemetry Diagnostics

Real-time diagnostics pipeline for PMSM (Permanent Magnet Synchronous Motor) drives using **Node-RED**, **n8n**, **Qdrant RAG** and a local **LLM** (Ollama). Instead of training a supervised ML model, the system applies deterministic engineering analysis in Node-RED and delegates natural-language diagnosis to a language model enriched with SDK documentation.

---

## Architecture

```
  STM32 Motor Controller
  (FOC / MC SDK 6.4.x)
        │ CAN bus
        ▼
  ┌──────────────────────────┐
  │  Node-RED Function Node  │  ← node-red.js
  │  • Parse 5 CAN frame types
  │  • Session lifecycle
  │  • Settled-state analysis
  │  • Symptom & RAG query generation
  └────────────┬─────────────┘
               │ MQTT  (node-red/request)
               ▼
  ┌──────────────────────────────────────┐
  │              n8n Flow                │  ← PMSM_Diagnostic.json
  │  Extract Summary → Ollama Embed      │
  │       ↓                              │
  │  Qdrant Search → Build Prompt        │
  │       ↓                              │
  │  Basic LLM Chain (gemma4:31b)        │
  │       ↓                              │
  │  MQTT publish (node-red/data)        │
  └──────────────────────────────────────┘
               │
               ▼
     JSON diagnosis result
```

---

## Repository Contents

| File | Description |
|---|---|
| `flows.json` | **Node-RED project flows** — import as a Node-RED project or copy to your userDir |
| `settings.js` | **Node-RED runtime settings** — points `flowFile` to `flows.json`; copy to your Node-RED userDir |
| `node-red.js` | Source of the collector Function node (also embedded inside `flows.json`) |
| `can_driver.js` | CAN frame pack/unpack helpers generated from `PMSM-demo.dbc` |
| `PMSM_Diagnostic.json` | n8n flow — import directly into n8n |
| `ingest_rag.py` | One-time script: parse STM32 SDK HTML docs → chunk → embed → upsert into Qdrant |
| `node-red-collect.md` | Full prompt + spec for `node-red.js` (recreate or modify via LLM) |
| `n8n-PMSM_Diagnostic.md` | Full prompt + spec for the n8n flow (recreate or modify via LLM) |

---

## Infrastructure

| Component | Address | Notes |
|---|---|---|
| Qdrant | `http://192.168.1.115:6333` | Collection `motor_control_rag`, cosine, dim=384 |
| Ollama embed | `http://192.168.1.115:11434` | `qllama/bge-small-en-v1.5:latest` |
| Ollama chat | same host | `gemma4:31b` |
| MQTT broker | configured in n8n credentials | topics: `node-red/request`, `node-red/data` |

---

## Quick Start

### 1. Ingest SDK documentation into Qdrant

```bash
pip install beautifulsoup4 lxml qdrant-client sentence-transformers
python ingest_rag.py
```

Edit the `HTML_DIR` path in `ingest_rag.py` to point to your STM32 MC SDK documentation folder before running.

### 2. Deploy Node-RED

**Option A — project import (recommended)**

1. Copy `flows.json` and `settings.js` into your Node-RED userDir (default: `~/.node-red/`).
2. Restart Node-RED — it will load `flows.json` automatically.
3. Set `flow.targetRpm` from the dashboard UI slider/input widget.

**Option B — manual**

1. Open Node-RED → create a new Function node.
2. Paste the contents of `node-red.js` into the *On Message* tab.
3. Connect the node to your CAN input node.
4. Set `flow.targetRpm` from your dashboard UI slider/input widget.

### 3. Import the n8n flow

1. In n8n, go to **Workflows → Import from file**.
2. Select `PMSM_Diagnostic.json`.
3. Update MQTT and Ollama credentials to match your environment.
4. Activate the workflow.

---

## How It Works

### Node-RED: deterministic analysis

The function node processes every CAN frame and maintains a rolling session buffer. When the motor returns to IDLE (or a timeout fires), the session is flushed and analysed:

- **Settled-state separation** — samples where `speed_rpm ≥ 90 % × lastTargetRpm` are isolated from ramp-up. All regulation quality metrics (`settled_speed_avg`, `settled_speed_std`, `steady_state_err`) are computed only on this subset, so a normal ramp-up never looks like instability.
- **Symptom detection** — rules fire on settled metrics: oscillation if `std_dev > 30 RPM`, steady-state error if `err > 50 RPM`, current saturation if `peak Iq > 1 A`, etc.
- **RAG query selection** — a short plain-text search phrase is chosen deterministically based on detected symptoms. The LLM never generates the query, preventing injection or hallucinated Qdrant searches.

### n8n: RAG + LLM diagnosis

1. `Extract Telemetry Summary` — validates the payload and builds a downsampled RUN-state time series (≤ 60 rows).
2. `Ollama Embed` — embeds the pre-computed RAG query string.
3. `Qdrant Search` — retrieves the 5 most relevant SDK documentation chunks.
4. `Build Prompt` — assembles the full prompt: telemetry summary, settled-speed note, time-series table, and retrieved knowledge.
5. `Basic LLM Chain` — single-shot inference with `gemma4:31b`.
6. `MQTT` — publishes the JSON diagnosis back to Node-RED.

### Output schema

```json
{
  "state": "normal | unstable | inefficient | stalled",
  "confidence": 0.0,
  "diagnosis": "2-3 sentences citing specific numbers",
  "root_cause": "single most likely engineering cause",
  "recommendations": [
    {
      "parameter": "pid_speed_kp",
      "current_value": 2730,
      "action": "increase | decrease | check | monitor",
      "reason": "cited from knowledge base"
    }
  ],
  "used_context": ["topic/subtopic from Qdrant hits"],
  "qdrant_query_used": "the exact query string"
}
```

---

## Motor States Reference

| Value | State | Meaning |
|---|---|---|
| 0 | IDLE | Motor stopped |
| 4 | ALIGNMENT | Rotor alignment phase |
| 6 | RUN | Active speed control |
| 8 | STOP_RAMP | Deceleration ramp |

---

## Regenerating or Modifying Code

Both main components were generated via LLM prompts. The prompts are preserved as the primary specification:

- To modify the Node-RED collector logic → edit `node-red-collect.md` and regenerate.
- To modify the n8n flow logic → edit `n8n-PMSM_Diagnostic.md` and regenerate.

This makes the `.md` files the source of truth, not comments inside the code.

---

## Requirements

| Tool | Version |
|---|---|
| Node-RED | ≥ 3.x |
| n8n | ≥ 1.x |
| Qdrant | ≥ 1.7 |
| Ollama | ≥ 0.3 |
| Python | ≥ 3.10 (for `ingest_rag.py` only) |
