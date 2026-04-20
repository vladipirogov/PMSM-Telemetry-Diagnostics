# n8n Flow — PMSM Diagnostic (PMSM_Diagnostic_v5.json)

## Purpose

Linear n8n flow that receives a PMSM run-cycle payload via MQTT, performs
RAG-augmented diagnosis using Ollama + Qdrant, and publishes the LLM result
back via MQTT.

No AI Agent loop — one straight chain, no iteration limit risk.

---

## Infrastructure

| Service | URL | Notes |
|---|---|---|
| MQTT broker | `mqtt://192.168.1.115` | topic in: `node-red/request`, topic out: `node-red/data` |
| Ollama | `http://192.168.1.115:11434` | embed model + chat model |
| Qdrant | `http://192.168.1.115:6333` | collection: `motor_control_rag`, cosine similarity, dim=384 |

### Models
| Role | Model |
|---|---|
| Embedding | `qllama/bge-small-en-v1.5:latest` (384-dim) |
| Chat / diagnosis | `gemma4:31b` (or any Ollama chat model) |

### n8n Credentials
| Credential | ID | Name |
|---|---|---|
| MQTT | `OGAFAeBvsY3seM1y` | MQTT account |
| Ollama | `9tEKiMA2Ohtrhpop` | Ollama account 3 |

---

## Node Pipeline (8 nodes)

```
MQTT Trigger
    └─► Extract Telemetry Summary   (Code)
            └─► Ollama Embed        (HTTP Request)
                    └─► Qdrant Search (HTTP Request)
                            └─► Build Prompt  (Code)
                                    └─► Basic LLM Chain
                                            ├─ Ollama Chat Model  [sub-node]
                                            └─► MQTT (publish result)
```

---

## Node Details

---

### 1. MQTT Trigger

| Field | Value |
|---|---|
| Type | `n8n-nodes-base.mqttTrigger` v1 |
| Topic | `node-red/request` |
| Credential | MQTT account (`OGAFAeBvsY3seM1y`) |

Receives the JSON payload published by Node-RED on session flush.
The full payload (including `samples` array) arrives as `msg.message` (string).

---

### 2. Extract Telemetry Summary

| Field | Value |
|---|---|
| Type | `n8n-nodes-base.code` v2 |
| Language | JavaScript |

**Purpose:**
- Parses the raw MQTT JSON string.
- Validates that `telemetry_summary` and `qdrant_query` are present.
- Validates `qdrant_query` — rejects if > 80 chars or contains any of `{ } [ ] : " ,`.
- Builds a downsampled RUN-state time series (`samples_series`) for the LLM — max 60 rows.
- Drops the full `samples` array from what flows forward (LLM never sees raw sample array).

**Outputs:** `{ sessionId, flush_reason, qdrant_query, telemetry_summary, samples_series }`

**`samples_series` schema** (per row, RUN state only, downsampled):
```json
{ "t_ms": 0, "target_rpm": 1000, "speed_rpm": 132.0, "iq": 0.0216, "id": 0.0017 }
```
- `t_ms` — milliseconds from first RUN sample (relative time)
- `target_rpm` — snapshot of `flow.targetRpm` at that frame (null if not set)
- `speed_rpm` — actual motor speed (1 dp)
- `iq` — q-axis current A (4 dp)
- `id` — d-axis current A (4 dp)

**Code:**
```javascript
// Extract and validate telemetry summary.
// Drops the raw samples array — the LLM must never see it raw.
// Builds a downsampled RUN-state time series for LLM analysis.
const raw = JSON.parse($input.first().json.message);
const summary = raw.telemetry_summary;
if (!summary) throw new Error('No telemetry_summary in payload — update Node-RED collector');
if (!summary.qdrant_query) throw new Error('qdrant_query missing — update Node-RED collector');
const q = summary.qdrant_query;
if (q.length > 80 || /[{}\[\]:",]/.test(q)) {
  throw new Error('qdrant_query contains invalid characters: ' + q);
}

// Build downsampled time series from RUN state samples only
const RUN_STATE  = 6;
const MAX_ROWS   = 60;
const allSamples = raw.samples || [];
const runSamples = allSamples.filter(s => s.motor_state === RUN_STATE);
const step = Math.max(1, Math.floor(runSamples.length / MAX_ROWS));
const t0   = runSamples.length ? runSamples[0].ts : 0;
const samples_series = runSamples
  .filter((_, i) => i % step === 0)
  .map(s => ({
    t_ms:       s.ts - t0,
    target_rpm: s.target_rpm != null ? s.target_rpm : null,
    speed_rpm:  +(s.speed_rpm || 0).toFixed(1),
    iq:         +(s.iq || 0).toFixed(4),
    id:         +(s.id || 0).toFixed(4)
  }));

return [{ json: {
  sessionId:         raw.sessionId,
  flush_reason:      raw.flush_reason,
  qdrant_query:      q,
  telemetry_summary: summary,
  samples_series
} }];
```

---

### 3. Ollama Embed

| Field | Value |
|---|---|
| Type | `n8n-nodes-base.httpRequest` v4.2 |
| Method | POST |
| URL | `http://192.168.1.115:11434/api/embed` |
| Body (raw JSON) | `={{ JSON.stringify({ model: "qllama/bge-small-en-v1.5:latest", input: $json.qdrant_query }) }}` |
| Timeout | 30000 ms |

Embeds the `qdrant_query` string into a 384-dim vector.
Response field used downstream: `$json.embeddings[0]` (array of 384 floats).

---

### 4. Qdrant Search

| Field | Value |
|---|---|
| Type | `n8n-nodes-base.httpRequest` v4.2 |
| Method | POST |
| URL | `http://192.168.1.115:6333/collections/motor_control_rag/points/search` |
| Body (raw JSON) | `={{ JSON.stringify({ vector: $json.embeddings[0], limit: 5, with_payload: true }) }}` |
| Timeout | 30000 ms |

Returns top-5 scored knowledge-base hits.
Each hit: `{ id, score, payload: { topic, subtopic, content, engineering_context: { problem, when_to_use, related_parameters } } }`

---

### 5. Build Prompt

| Field | Value |
|---|---|
| Type | `n8n-nodes-base.code` v2 |
| Language | JavaScript |

**Purpose:** Assembles the complete LLM prompt from:
- `telemetry_summary` (read back from `Extract Telemetry Summary` node)
- `samples_series` (read back from `Extract Telemetry Summary` node)
- Qdrant search hits (from `$input`)

**Reads across nodes:** uses `$('Extract Telemetry Summary').item.json.*` pattern.

**Prompt structure:**
1. Instruction header — output format mandate (raw JSON only, no markdown)
2. `TELEMETRY SUMMARY` section — all pre-computed scalar metrics
3. `NOTE` — explains `steady_state_err_rpm` sign convention and warns to use `settled_speed_avg`, not `speed_avg_rpm`, for regulation quality
4. `TIME SERIES` table — RUN-state samples (target_rpm, speed_rpm, iq, id per row)
5. `RETRIEVED KNOWLEDGE` — top-4 Qdrant hits formatted as labelled blocks
6. JSON template — exact schema the LLM must fill in

**Key metrics in prompt:**
| Field | Source | Meaning |
|---|---|---|
| `speed_avg_rpm` | `telemetry.speed.avg` | Full-run average incl. ramp-up — for context only |
| `settled_speed_avg` | `telemetry.settled_speed.avg` | Average within ≥90% of target — regulation quality |
| `settled_speed_std` | `telemetry.settled_speed.std_dev` | Oscillation in settled region only |
| `settled_sample_count` | `telemetry.settled_speed.sample_count` | How many samples used for settled stats |
| `steady_state_err_rpm` | `telemetry.speed_error.steady_state_rpm` | `target_rpm − settled_speed_avg`; positive = undershoot |
| `steady_state_err_pct` | `telemetry.speed_error.steady_state_pct` | Relative error % |

**LLM output schema:**
```json
{
  "state": "<normal|unstable|inefficient|stalled>",
  "confidence": 0.0,
  "diagnosis": "<2-3 sentences citing target_rpm, settled_speed_avg vs target_rpm, steady_state_err_rpm and settled_speed_std by number; note if motor never settled>",
  "root_cause": "<single most likely engineering cause>",
  "recommendations": [
    {
      "parameter": "<exact param name e.g. pid_speed_kp>",
      "current_value": null,
      "action": "<increase|decrease|check|monitor>",
      "reason": "<cite knowledge base topic and problem field>"
    }
  ],
  "used_context": ["<topic/subtopic from knowledge results>"],
  "qdrant_query_used": "<the query string>"
}
```

**Code:**
```javascript
const telemetry  = $('Extract Telemetry Summary').item.json.telemetry_summary;
const qdrantQuery = $('Extract Telemetry Summary').item.json.qdrant_query;
const sessionId   = $('Extract Telemetry Summary').item.json.sessionId;
const series      = $('Extract Telemetry Summary').item.json.samples_series || [];
const hits = $input.first().json.result || [];

const ctxLines = [];
hits.slice(0, 4).forEach((hit, i) => {
  const p   = hit.payload || {};
  const ctx = p.engineering_context || {};
  ctxLines.push('[' + (i+1) + '] score=' + hit.score.toFixed(3) + '  topic=' + p.topic + '  subtopic=' + p.subtopic);
  ctxLines.push('    problem       : ' + (ctx.problem || 'n/a'));
  ctxLines.push('    when_to_use   : ' + (ctx.when_to_use || 'n/a'));
  ctxLines.push('    related_params: ' + JSON.stringify(ctx.related_parameters || []));
  ctxLines.push('    content       : ' + (p.content || '').slice(0, 350));
  ctxLines.push('');
});

const pid = telemetry.pid || {};
const err = telemetry.speed_error || {};
const settled = telemetry.settled_speed || {};
const targetHistory = (telemetry.target_rpm_history || [])
  .map(e => e.target_rpm + ' RPM').join(' -> ');

// Build compact time-series table
const hdr  = '  t_ms    | target_rpm | speed_rpm | iq_A    | id_A';
const sep  = '  --------|------------|-----------|---------|--------';
const rows = series.map(r =>
  '  ' + String(r.t_ms).padEnd(7) +
  ' | ' + String(r.target_rpm != null ? r.target_rpm : 'N/A').padEnd(10) +
  ' | ' + String(r.speed_rpm).padEnd(9) +
  ' | ' + String(r.iq).padEnd(7) +
  ' | ' + String(r.id)
);

const lines = [
  'Diagnose this PMSM motor run cycle.',
  'Output ONLY a raw JSON object. No markdown. No backticks. No text before or after.',
  '',
  'TELEMETRY SUMMARY:',
  '  state_sequence       = ' + telemetry.state_sequence,
  '  duration_ms          = ' + telemetry.duration_ms,
  '  target_rpm           = ' + (telemetry.target_rpm != null ? telemetry.target_rpm : 'N/A'),
  '  target_rpm_history   = ' + (targetHistory || 'N/A'),
  '  speed_max_rpm        = ' + telemetry.speed.max,
  '  speed_min_rpm        = ' + telemetry.speed.min,
  '  speed_avg_rpm        = ' + telemetry.speed.avg,
  '  speed_std_dev        = ' + telemetry.speed.std_dev,
  '  speed_steps          = ' + telemetry.speed.steps.length + ' sudden change(s) detected',
  '  settled_speed_avg    = ' + (settled.avg     != null ? settled.avg     : 'N/A'),
  '  settled_speed_std    = ' + (settled.std_dev != null ? settled.std_dev : 'N/A'),
  '  settled_sample_count = ' + (settled.sample_count != null ? settled.sample_count : '0'),
  '  steady_state_err_rpm = ' + (err.steady_state_rpm != null ? err.steady_state_rpm : 'N/A'),
  '  steady_state_err_pct = ' + (err.steady_state_pct != null ? err.steady_state_pct + '%' : 'N/A'),
  '  avg_iq_A             = ' + telemetry.current.avg,
  '  max_abs_iq_A         = ' + telemetry.current.max_abs_iq,
  '  pid_speed_kp         = ' + (pid.speed_kp  != null ? pid.speed_kp  : 'N/A'),
  '  pid_speed_ki         = ' + (pid.speed_ki  != null ? pid.speed_ki  : 'N/A'),
  '  pid_torque_kp        = ' + (pid.torque_kp != null ? pid.torque_kp : 'N/A'),
  '  pid_torque_ki        = ' + (pid.torque_ki != null ? pid.torque_ki : 'N/A'),
  '  torque_zero          = ' + telemetry.torque_zero,
  '  power_zero           = ' + telemetry.power_zero,
  '  symptoms             = ' + telemetry.symptoms.join(' | '),
  '',
  'NOTE: steady_state_err_rpm = target_rpm - settled_speed_avg (ramp-up excluded).',
  '      Positive = undershoot (settled below target).',
  '      Negative = overshoot (settled above target).',
  '      Use settled_speed_avg/std for regulation quality, not speed_avg_rpm.',
  '',
  'TIME SERIES — RUN state, ' + series.length + ' sample(s):',
  hdr,
  sep,
  rows.join('\n'),
  '',
  'RETRIEVED KNOWLEDGE (qdrant query: ' + qdrantQuery + '):',
  ctxLines.join('\n'),
  'Return ONLY this JSON (fill in all placeholders, no other text):',
  '{',
  '  "state": "<normal|unstable|inefficient|stalled>",',
  '  "confidence": 0.0,',
  '  "diagnosis": "<2-3 sentences: cite target_rpm, settled_speed_avg vs target_rpm, steady_state_err_rpm and settled_speed_std by number; note if motor never settled>",',
  '  "root_cause": "<single most likely engineering cause>",',
  '  "recommendations": [',
  '    {',
  '      "parameter": "<exact param name e.g. pid_speed_kp>",',
  '      "current_value": null,',
  '      "action": "<increase|decrease|check|monitor>",',
  '      "reason": "<cite knowledge base topic and problem field>"',
  '    }',
  '  ],',
  '  "used_context": ["<topic/subtopic from knowledge results>"],',
  '  "qdrant_query_used": "' + qdrantQuery + '"',
  '}'
];

return [{ json: { prompt: lines.join('\n'), sessionId } }];
```

---

### 6. Basic LLM Chain

| Field | Value |
|---|---|
| Type | `@n8n/n8n-nodes-langchain.chainLlm` v1.9 |
| Prompt type | `define` |
| Text | `={{ $json.prompt }}` |

Single-shot LLM call — no ReAct loop, no tool use, no iteration limit.
Sub-node: Ollama Chat Model (connected via `ai_languageModel` port).

---

### 7. Ollama Chat Model

| Field | Value |
|---|---|
| Type | `@n8n/n8n-nodes-langchain.lmChatOllama` v1 |
| Model | `gemma4:31b` |
| Credential | Ollama account 3 (`9tEKiMA2Ohtrhpop`) |

Connected as sub-node to Basic LLM Chain via `ai_languageModel` port.

---

### 8. MQTT (publish)

| Field | Value |
|---|---|
| Type | `n8n-nodes-base.mqtt` v1 |
| Topic | `node-red/data` |
| Credential | MQTT account (`OGAFAeBvsY3seM1y`) |

Publishes the LLM response back to Node-RED.
The `Basic LLM Chain` output is `{ text: "<LLM response string>" }`.
The LLM response string should be a raw JSON object (per the prompt instruction).

---

## Connection Map

```json
{
  "MQTT Trigger":              [["Extract Telemetry Summary"]],
  "Extract Telemetry Summary": [["Ollama Embed"]],
  "Ollama Embed":              [["Qdrant Search"]],
  "Qdrant Search":             [["Build Prompt"]],
  "Build Prompt":              [["Basic LLM Chain"]],
  "Basic LLM Chain":           [["MQTT"]],
  "Ollama Chat Model":         "ai_languageModel → Basic LLM Chain"
}
```

---

## Design Decisions

### Why no AI Agent?
Agent nodes use a ReAct loop with an iteration limit. When the limit is hit the
flow fails silently. A linear `Basic LLM Chain` makes a single call and always
produces output.

### Why pre-compute qdrant_query in Node-RED?
Prevents the LLM from sending raw telemetry JSON to Qdrant as a query string.
The query is a deterministic 3–8 word phrase selected by rule in Node-RED.

### Why settled_speed vs speed_avg?
`speed_avg` includes ramp-up transients. A motor going from 0 to 1000 RPM will
have `speed_avg ≈ 500` even if regulation at 1000 RPM is perfect.
`settled_speed` uses only samples where `speed >= 90% of lastTargetRpm` —
this cleanly separates regulation quality from ramp-up duration.

### Why send samples_series instead of full samples?
The full `samples` array can be hundreds of objects. The `samples_series` is
downsampled to max 60 rows (RUN state only) so the LLM can reason about
time-domain dynamics without exceeding context length.

---

## Complete JSON (PMSM_Diagnostic_v5.json)

To regenerate the flow file: copy this JSON exactly.
Substitute credential IDs if your n8n instance uses different ones.

The complete JSON is the file `PMSM_Diagnostic_v5.json` in this repository.
The jsCode fields for Code nodes are exactly as documented in the sections above.

Key node IDs for reference:
| Node name | ID |
|---|---|
| MQTT Trigger | `7b5e1c74-b910-4ca0-86a4-0be38dbd16df` |
| Extract Telemetry Summary | `d2b6f842-a6a2-48e1-a771-3d4a5f57e229` |
| Ollama Embed | `a1b2c3d4-e5f6-7890-aaaa-111122223333` |
| Qdrant Search | `b2c3d4e5-f6a7-8901-bbbb-222233334444` |
| Build Prompt | `c3d4e5f6-a7b8-9012-cccc-333344445555` |
| Basic LLM Chain | `4ad4f146-8283-4e2e-97dc-74330cd1cd06` |
| Ollama Chat Model | `e9f144ed-80e1-4862-bdf6-14d640ce389e` |
| MQTT | `79cce48d-06aa-4a49-b0cd-24f5bdac1d58` |
