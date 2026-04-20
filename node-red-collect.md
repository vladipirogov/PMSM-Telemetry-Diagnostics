# Node-RED — PMSM Run-Cycle Collector & Analyser

## Purpose

This is a **single Node-RED Function node** that replaces a five-node chain
(Convert → Normalize → merge_state → buffer → Flush).

It receives raw SocketCAN frames, accumulates them into a run-cycle session,
and on flush emits a fully-analysed payload containing a `telemetry_summary`
ready for downstream AI diagnostic processing (n8n → Ollama → Qdrant).

While collecting the node returns `null`. On flush it returns one `msg` with
the full session data.

---

## Context and Dependencies

| Item | Detail |
|---|---|
| Global context key | `canDriver` — the JS CAN driver object loaded in `settings.js` |
| Input topic | raw SocketCAN frame via `msg.payload = { canid, data, timestamp }` |
| Output topic | none (msg passes through unchanged with new `msg.payload`) |

The `canDriver` global must expose:
- `FRAME_ID` — object mapping message name → CAN ID
- Unpack functions: `speedFeedbackMsgUnpack`, `currAmpFeedbackMsgUnpack`, etc.
- Decode functions: `speedFeedbackMsgSpeedRpmDecode`, `currAmpFeedbackMsgIDDecode`, etc.

---

## Flow Variables (configure without editing code)

| Variable | Type | Default | Description |
|---|---|---|---|
| `collection_max_ms` | number | 30000 | Hard time cap per session in ms |
| `collection_max_samples` | number | 600 | Max samples before force-flush |
| `stop_collection` | boolean | false | Set `true` from inject/link to flush immediately |
| `sessionId` | string | `"sess-<epoch>"` | Optional label attached to every flush |
| `targetRpm` | number | null | **Commanded setpoint from Node-RED UI controls** — snapshotted into every sample so per-sample target vs actual speed can be analysed |

> `targetRpm` is **not** read from CAN. It is set externally via dashboard or
> inject node into `flow.targetRpm`. The function reads it at each incoming
> frame and stamps it into `current.target_rpm` so the buffer preserves the
> full history of setpoint changes.

---

## CAN Frames Parsed (feedback only)

| `canid` constant | Fields extracted |
|---|---|
| `SPEED_FEEDBACK_MSG` | `speed_rpm`, `motor_state` |
| `CURR_AMP_FEEDBACK_MSG` | `id` (d-axis current A), `iq` (q-axis current A) |
| `VOLTAGE_TEMPER_FEEDBACK_MSG` | `bus_voltage`, `temperature` |
| `TORQUE_POWER_FEEDBACK_MSG` | `torque`, `power` |
| `PID_FEEDBACK_MSG` | `pid_speed_kp`, `pid_speed_ki`, `pid_torque_kp`, `pid_torque_ki` |
| anything else | `return null` — frame ignored |

Control frames (e.g. `SPEED_CONTROL_MSG`) are **deliberately ignored** —
the target setpoint is provided by `flow.targetRpm` from the UI instead.

---

## Motor State Codes

| Code | Name |
|---|---|
| 0 | IDLE |
| 4 | ALIGNMENT |
| 6 | RUN |
| 8 | STOP_RAMP |

---

## Processing Stages

### 1 — Parse CAN Frame
Build a `partial` object with only the fields carried by the incoming frame.
All other fields remain from the previous state.

### 2 — Merge into Current State
`Object.assign(current, partial)` — merge partial update.
Stamp `current.ts` from `msg.payload.timestamp` or `Date.now()`.
Stamp `current.target_rpm` from `flow.get("targetRpm")`.
Persist as `flow.rc_current`.

Wait until `current.motor_state` is defined before proceeding.

### 3 — Session Lifecycle
- Session starts when `motor_state !== IDLE` and no session is active.
- On start: `rc_active = true`, `rc_start_ts = current.ts`, `rc_buffer = []`.
- Every frame while active: push a deep-clone of `current` into `rc_buffer`.

### 4 — Flush Conditions (any one triggers flush)
| Condition | Variable |
|---|---|
| Motor returns to IDLE (buffer > 2 samples) | `motorIdle` |
| Session exceeds `collection_max_ms` | `timedOut` |
| Buffer reaches `collection_max_samples` | `overLimit` |
| External `flow.stop_collection === true` | `externalStop` |

### 5 — Reset Flow State
Clear `rc_buffer`, `rc_start_ts`, `rc_active`, `rc_current`, `stop_collection`.

### 6 — Analyse

#### Helper functions
- `stateSequence(samples)` — deduplicated ordered list of state names from the buffer.
- `calcStats(values)` — returns `{max, min, avg, std_dev}` rounded to 2 dp.
- `detectSpeedSteps(samples, threshold=150)` — finds frame-to-frame speed jumps ≥ threshold RPM; returns `[{ts, delta, to}]`.

#### Derived metrics

**Speed stats (full RUN)**
```
runSamples = buffer.filter(motor_state === RUN)
speedStat  = calcStats(speed_rpm values)   // includes ramp-up
```
Used only for `speed_max/min/avg/std_dev`; **not used** for error or anomaly detection.

**Target RPM tracking**
```
lastTargetRpm  = last non-null target_rpm in buffer
targetHistory  = [{ts, target_rpm}] each time the setpoint changed
```

**Settled samples** — the key metric for regulation quality:
```
settledSamples = runSamples where:
    s.target_rpm === lastTargetRpm          // only at the last (final) setpoint
    AND s.speed_rpm >= 0.9 * lastTargetRpm  // speed is within 10% of target

settledStat = calcStats(settledSamples.speed_rpm)
```
This excludes all ramp-up transients. If the motor ramped from 100 → 1000 RPM,
only the samples at 1000 RPM target where speed ≥ 900 are counted.

**Steady-state error**
```
steadyStateError    = lastTargetRpm - settledStat.avg   // positive = undershoot
steadyStateErrorPct = |steadyStateError| / lastTargetRpm * 100
```
`null` when no settled samples exist.

**Speed steps** — detected only within `settledSamples` (threshold 150 RPM).
Ramp-up jumps are expected and not flagged.

**Iq stats**
```
iqStat   = calcStats(runSamples.iq)
maxIqAbs = max(|iq|) across runSamples
```

**PID** — last sample in buffer that has `pid_speed_kp` defined.

#### Symptom rules (in order)
| Condition | Symptom text |
|---|---|
| no RUN state at all | `"no RUN state reached — startup failure likely"` |
| `zeroTorque AND zeroPower AND hasRun` | `"zero torque and power throughout RUN"` |
| `settledSamples.length === 0 AND hasRun AND lastTargetRpm != null` | `"motor never reached 90% of target N RPM — possible stall or undershoot"` |
| `settledStat.std_dev > 30` | `"speed oscillation in settled region std_dev X RPM"` |
| `maxIqAbs > 1.0` | `"high Iq peak X A"` |
| `steps.length > 0` (in settled region) | `"speed step changes detected: N"` |
| `|steadyStateError| > 50` | `"steady-state speed error X RPM vs target Y RPM"` |
| none of the above | `"no anomalies detected"` |

#### Qdrant query selection (deterministic, priority order)
| Condition | Query phrase |
|---|---|
| no RUN | `"FOC startup failure alignment revup current"` |
| `maxIqAbs > 1.0` | `"FOC current saturation anti-windup Iq limit"` |
| `settledStat.std_dev > 30` | `"FOC speed loop PI oscillation gain tuning"` |
| `hasLargeError OR neverSettled` | `"FOC speed steady-state error PI gain integral"` |
| `zeroTorque AND speedStat.avg > 50` | `"FOC zero torque Iq reference power loss"` |
| `steps.length > 1` (settled) | `"FOC speed ramp step response PI gain"` |
| seq includes STOP_RAMP | `"FOC speed ramp normal shutdown deceleration"` |
| default | `"PMSM FOC speed control normal operation"` |

Query must be 3–8 words, no JSON characters, no punctuation other than spaces.

### 7 — Output

`msg.payload` emitted on flush:

```json
{
  "sessionId": "sess-<epoch>",
  "start_ts": 1776533010696,
  "end_ts": 1776533020696,
  "flush_reason": "motor_idle | timeout | max_samples | external_stop",
  "samples": [ /* full buffer — all CAN frames merged per timestamp */ ],
  "telemetry_summary": {
    "state_sequence": "ALIGNMENT → RUN → IDLE",
    "duration_ms": 10000,
    "sample_count": 71,
    "run_sample_count": 63,
    "target_rpm": 1000,
    "target_rpm_history": [
      { "ts": 1776533010696, "target_rpm": 100 },
      { "ts": 1776533014695, "target_rpm": 1000 }
    ],
    "speed": {
      "max": 1002, "min": 60, "avg": 626, "std_dev": 404.87,
      "steps": [{ "ts": ..., "delta": 408, "to": 498 }]
    },
    "settled_speed": {
      "max": 1002, "min": 990, "avg": 996.5, "std_dev": 4.2,
      "sample_count": 32
    },
    "speed_error": {
      "steady_state_rpm": 3.5,
      "steady_state_pct": 0.4
    },
    "current": { "max": 0.03, "min": 0, "avg": 0.02, "std_dev": 0.01, "max_abs_iq": 0.0308 },
    "pid": { "speed_kp": 2730, "speed_ki": 562, "torque_kp": 3378, "torque_ki": 2252 },
    "torque_zero": true,
    "power_zero": false,
    "symptoms": ["no anomalies detected"],
    "qdrant_query": "PMSM FOC speed control normal operation"
  }
}
```

**Key design decisions:**
- `speed.avg/std_dev` covers the full RUN including ramp-up — use only for range info.
- `settled_speed.avg/std_dev` is ramp-excluded — use for regulation quality assessment.
- `speed_error` is based on `settled_speed.avg`, not `speed.avg`.
- `speed.steps` and oscillation symptoms are evaluated only within the settled region.

---

## Complete Code

```javascript
// =============================================================================
// PMSM Run-Cycle Collector & Analyser  — single Function node
// Replaces: Convert + Normalize + merge_state + buffer + Flush
//
// Input:  raw socketcan frame  msg.payload = { canid, data, timestamp, ... }
// Output: null while collecting
//         full session payload with telemetry_summary on flush
//
// Flow variables (set from outside to configure without editing code):
//   collection_max_ms      — hard time cap in ms         (default: 30000)
//   collection_max_samples — max samples before force-flush (default: 600)
//   stop_collection        — set true from any inject/link node to flush now
//   sessionId              — optional label for the session
//   targetRpm              — commanded setpoint from Node-RED UI (number or null)
// =============================================================================

const can = global.get('canDriver');
if (!can) {
    node.error('canDriver not found in global context — check settings.js');
    return null;
}

const { canid, data } = msg.payload;
if (!data || canid === undefined) return null;

const buf = Buffer.from(data);

// ── 1. PARSE CAN FRAME ────────────────────────────────────────────────────────
// Only feedback frames are relevant for telemetry; control frames are ignored.
let partial = {};

switch (canid) {

    case can.FRAME_ID.SPEED_FEEDBACK_MSG: {
        const raw = can.speedFeedbackMsgUnpack(buf);
        partial.speed_rpm   = can.speedFeedbackMsgSpeedRpmDecode(raw.speed_rpm);
        partial.motor_state = raw.motor_state;
        break;
    }

    case can.FRAME_ID.CURR_AMP_FEEDBACK_MSG: {
        const raw = can.currAmpFeedbackMsgUnpack(buf);
        partial.id = can.currAmpFeedbackMsgIDDecode(raw.i_d);
        partial.iq = can.currAmpFeedbackMsgIQDecode(raw.i_q);
        break;
    }

    case can.FRAME_ID.VOLTAGE_TEMPER_FEEDBACK_MSG: {
        const raw = can.voltageTemperFeedbackMsgUnpack(buf);
        partial.bus_voltage = can.voltageTemperFeedbackMsgBusVoltageDecode(raw.bus_voltage);
        partial.temperature = can.voltageTemperFeedbackMsgTemperatureDecode(raw.temperature);
        break;
    }

    case can.FRAME_ID.TORQUE_POWER_FEEDBACK_MSG: {
        const raw = can.torquePowerFeedbackMsgUnpack(buf);
        partial.torque = can.torquePowerFeedbackMsgTorqueDecode(raw.torque);
        partial.power  = can.torquePowerFeedbackMsgPowerDecode(raw.power);
        break;
    }

    case can.FRAME_ID.PID_FEEDBACK_MSG: {
        const raw = can.pidFeedbackMsgUnpack(buf);
        partial.pid_speed_kp  = can.pidFeedbackMsgPidSpeedKpDecode(raw.pid_speed_kp);
        partial.pid_speed_ki  = can.pidFeedbackMsgPidSpeedKiDecode(raw.pid_speed_ki);
        partial.pid_torque_kp = can.pidFeedbackMsgPidTorqueKpDecode(raw.pid_torque_kp);
        partial.pid_torque_ki = can.pidFeedbackMsgPidTorqueKiDecode(raw.pid_torque_ki);
        break;
    }

    default:
        return null;
}

// ── 2. MERGE PARTIAL UPDATE INTO CURRENT STATE ────────────────────────────────
let current = flow.get("rc_current") || {};
Object.assign(current, partial);
current.ts         = msg.payload.timestamp || Date.now();
current.target_rpm = flow.get("targetRpm") || null;   // snapshot UI setpoint per-sample
flow.set("rc_current", current);

// Wait until at least motor_state is known before any session logic
if (typeof current.motor_state === "undefined") return null;

// ── 3. SESSION LIFECYCLE ──────────────────────────────────────────────────────
const STATE_IDLE   = 0;
const MAX_MS       = flow.get("collection_max_ms")      || 30000;
const MAX_SAMPLES  = flow.get("collection_max_samples") || 600;

let buffer  = flow.get("rc_buffer")   || [];
let startTs = flow.get("rc_start_ts") || null;
let active  = flow.get("rc_active")   || false;

// Start session when motor leaves IDLE for the first time
if (!active && current.motor_state !== STATE_IDLE) {
    active  = true;
    startTs = current.ts;
    buffer  = [];
    flow.set("stop_collection", false);
    node.log("Run cycle started  ts=" + startTs + "  state=" + current.motor_state);
}

if (active) {
    buffer.push(JSON.parse(JSON.stringify(current)));
    flow.set("rc_buffer",   buffer);
    flow.set("rc_start_ts", startTs);
    flow.set("rc_active",   active);
}

// ── 4. FLUSH CONDITIONS ───────────────────────────────────────────────────────
const externalStop = flow.get("stop_collection") === true;
const motorIdle    = active && current.motor_state === STATE_IDLE && buffer.length > 2;
const timedOut     = active && startTs !== null && (current.ts - startTs) >= MAX_MS;
const overLimit    = active && buffer.length >= MAX_SAMPLES;

if (!active || (!externalStop && !motorIdle && !timedOut && !overLimit)) return null;

// ── 5. RESET FLOW STATE ───────────────────────────────────────────────────────
flow.set("rc_buffer",       []);
flow.set("rc_start_ts",     null);
flow.set("rc_active",       false);
flow.set("rc_current",      {});
flow.set("stop_collection", false);

const flushReason = externalStop ? "external_stop"
                  : motorIdle    ? "motor_idle"
                  : timedOut     ? "timeout"
                  :                "max_samples";

node.log("Run cycle flushed  reason=" + flushReason + "  samples=" + buffer.length);

// ── 6. ANALYSE ────────────────────────────────────────────────────────────────
const STATE_NAME = { 0: "IDLE", 4: "ALIGNMENT", 6: "RUN", 8: "STOP_RAMP" };
const RUN_STATE  = 6;

function stateSequence(samples) {
    const seq = [];
    let prev = null;
    for (const s of samples) {
        const name = STATE_NAME[s.motor_state] || String(s.motor_state);
        if (name !== prev) { seq.push(name); prev = name; }
    }
    return seq;
}

function calcStats(values) {
    if (!values.length) return { max: 0, min: 0, avg: 0, std_dev: 0 };
    const max = Math.max(...values);
    const min = Math.min(...values);
    const avg = values.reduce((a, b) => a + b, 0) / values.length;
    const std_dev = Math.sqrt(values.reduce((a, v) => a + (v - avg) ** 2, 0) / values.length);
    return {
        max:     +max.toFixed(2),
        min:     +min.toFixed(2),
        avg:     +avg.toFixed(2),
        std_dev: +std_dev.toFixed(2)
    };
}

function detectSpeedSteps(runSamples, threshold) {
    const steps = [];
    for (let i = 1; i < runSamples.length; i++) {
        const delta = (runSamples[i].speed_rpm || 0) - (runSamples[i - 1].speed_rpm || 0);
        if (Math.abs(delta) >= threshold) {
            steps.push({
                ts:    runSamples[i].ts,
                delta: Math.round(delta),
                to:    Math.round(runSamples[i].speed_rpm || 0)
            });
        }
    }
    return steps;
}

const runSamples = buffer.filter(s => s.motor_state === RUN_STATE);
const seq        = stateSequence(buffer);
const speedStat  = calcStats(runSamples.map(s => s.speed_rpm || 0));
const iqStat     = calcStats(runSamples.map(s => s.iq || 0));
const hasRun     = runSamples.length > 0;
const zeroTorque = buffer.every(s => (s.torque || 0) === 0);
const zeroPower  = buffer.every(s => (s.power  || 0) === 0);
const maxIqAbs   = runSamples.length ? Math.max(...runSamples.map(s => Math.abs(s.iq || 0))) : 0;

// Last known PID state (arrives from PID_FEEDBACK_MSG frames)
const pidSamples = buffer.filter(s => s.pid_speed_kp !== undefined);
const lastPid    = pidSamples.length ? pidSamples[pidSamples.length - 1] : null;

// Target RPM — derive from per-sample snapshots so history of setpoint changes is preserved
const targetSamples = buffer.filter(s => s.target_rpm !== null && s.target_rpm !== undefined);
const lastTargetRpm = targetSamples.length
    ? targetSamples[targetSamples.length - 1].target_rpm
    : null;
const targetHistory = [];
let prevTarget = null;
for (const s of buffer) {
    if (s.target_rpm != null && s.target_rpm !== prevTarget) {
        targetHistory.push({ ts: s.ts, target_rpm: s.target_rpm });
        prevTarget = s.target_rpm;
    }
}

// Settled samples: RUN-state where speed reached ≥90% of the LAST commanded target.
// Excludes ramp-up transients — these stats reflect actual regulation quality.
const settledSamples = (lastTargetRpm !== null)
    ? runSamples.filter(s =>
          s.target_rpm === lastTargetRpm && (s.speed_rpm || 0) >= 0.9 * lastTargetRpm)
    : [];
const settledStat = settledSamples.length
    ? calcStats(settledSamples.map(s => s.speed_rpm || 0))
    : { max: 0, min: 0, avg: 0, std_dev: 0 };
// Speed disturbances counted only in settled region — ramp-up jumps are expected, not anomalies
const steps = detectSpeedSteps(settledSamples, 150);

// Steady-state error: target_rpm - settled_avg. Positive = undershoot. Negative = overshoot.
const steadyStateError = (lastTargetRpm !== null && settledSamples.length > 0)
    ? +(lastTargetRpm - settledStat.avg).toFixed(2)
    : null;
const steadyStateErrorPct = (lastTargetRpm && lastTargetRpm > 0 && steadyStateError !== null)
    ? +((Math.abs(steadyStateError) / lastTargetRpm) * 100).toFixed(1)
    : null;

const symptoms = [];
if (!hasRun)                                           symptoms.push("no RUN state reached — startup failure likely");
if (zeroTorque && zeroPower && hasRun)                 symptoms.push("zero torque and power throughout RUN");
if (lastTargetRpm !== null && settledSamples.length === 0 && hasRun)
                                                       symptoms.push("motor never reached 90% of target " + lastTargetRpm + " RPM — possible stall or undershoot");
if (settledStat.std_dev > 30)                          symptoms.push("speed oscillation in settled region std_dev " + settledStat.std_dev + " RPM");
if (maxIqAbs > 1.0)                                    symptoms.push("high Iq peak " + maxIqAbs.toFixed(3) + " A");
if (steps.length)                                      symptoms.push("speed step changes detected: " + steps.length);
if (steadyStateError !== null && Math.abs(steadyStateError) > 50)
                                                       symptoms.push("steady-state speed error " + steadyStateError + " RPM vs target " + lastTargetRpm + " RPM");
if (!symptoms.length)                                  symptoms.push("no anomalies detected");

// Qdrant query — deterministic 3-8 word phrase, no JSON, no punctuation
let qdrant_query;
const hasLargeError = steadyStateError !== null && Math.abs(steadyStateError) > 50;
const neverSettled  = lastTargetRpm !== null && settledSamples.length === 0 && hasRun;
if      (!hasRun)                               qdrant_query = "FOC startup failure alignment revup current";
else if (maxIqAbs > 1.0)                        qdrant_query = "FOC current saturation anti-windup Iq limit";
else if (settledStat.std_dev > 30)              qdrant_query = "FOC speed loop PI oscillation gain tuning";
else if (hasLargeError || neverSettled)         qdrant_query = "FOC speed steady-state error PI gain integral";
else if (zeroTorque && speedStat.avg > 50)      qdrant_query = "FOC zero torque Iq reference power loss";
else if (steps.length > 1)                      qdrant_query = "FOC speed ramp step response PI gain";
else if (seq.includes("STOP_RAMP"))             qdrant_query = "FOC speed ramp normal shutdown deceleration";
else                                            qdrant_query = "PMSM FOC speed control normal operation";

// ── 7. OUTPUT ─────────────────────────────────────────────────────────────────
msg.payload = {
    sessionId:    flow.get("sessionId") || ("sess-" + Date.now()),
    start_ts:     startTs,
    end_ts:       buffer[buffer.length - 1].ts,
    flush_reason: flushReason,
    samples:      buffer,
    telemetry_summary: {
        state_sequence:   seq.join(" → "),
        duration_ms:      buffer[buffer.length - 1].ts - startTs,
        sample_count:     buffer.length,
        run_sample_count: runSamples.length,
        target_rpm:           lastTargetRpm,
        target_rpm_history:   targetHistory,
        speed:            { ...speedStat, steps },
        settled_speed:    { ...settledStat, sample_count: settledSamples.length },
        speed_error: {
            steady_state_rpm: steadyStateError,
            steady_state_pct: steadyStateErrorPct
        },
        current:          { ...iqStat, max_abs_iq: +maxIqAbs.toFixed(4) },
        pid:              lastPid ? {
                              speed_kp:  lastPid.pid_speed_kp,
                              speed_ki:  lastPid.pid_speed_ki,
                              torque_kp: lastPid.pid_torque_kp,
                              torque_ki: lastPid.pid_torque_ki
                          } : null,
        torque_zero:  zeroTorque,
        power_zero:   zeroPower,
        symptoms:     symptoms,
        qdrant_query: qdrant_query
    }
};

return msg;
```
