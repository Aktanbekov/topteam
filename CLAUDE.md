# Project: AI Driving Test Coach (Qualcomm GenieX Hackathon)

## What we're building
An offline AI coach for **practice drives** before the DMV road test.
- Dashcam video is analyzed by a local vision model on the Snapdragon NPU.
- The model detects likely driving mistakes (minor = strike, critical = instant fail).
- An Arduino UNO Q gives live signals: LEDs + vibration motor + LED-matrix mistake counter.
- After the drive, a text model writes a report: a practice result, each mistake with timestamp + screenshot, and tips.
- Pitch: "No internet. The video never leaves the laptop."
- Positioning: a coach for practice drives and post-drive review. NOT for use during a real DMV exam.

## Scoring rules (based on the California DMV DL-955 score sheet; verify against the official DMV handbook)
- Critical driving error (e.g., running a red light, not stopping at a stop sign, failing to yield) = automatic FAIL.
- Minor errors during the drive: up to 15 allowed.
- Optional simplified "3 strikes" practice mode.
- **Honest labelling:** mistakes are inferred from video alone, with no vehicle speed or GPS feed.
  Report every detection as *possible* ("possible incomplete stop") unless we have a trustworthy speed
  signal. This is coaching feedback, never an official DMV verdict — don't present it as one.

## Hackathon scope: 4 mistakes, ordered by how confidently we can detect them

| # | Mistake | Severity | Feasibility | Report it as |
|---|---|---|---|---|
| 1 | Not fully stopping at a stop sign | critical | feasible | "possible incomplete stop" |
| 2 | Going through a red light | critical | feasible, harder | "possible red-light violation" |
| 3 | Following too close | minor | experimental | "unsafe following-distance risk detected" |
| 4 | No head check before a lane change | minor | hardest — build last | "no head check detected" |

The forward dashcam cannot see head checks, mirror checks, turn signals, or hands.
A second, driver-facing camera (laptop webcam) covers head checks.

## Hardware
- Laptop: Snapdragon X (Windows ARM64) with NPU
- Arduino UNO Q: two "brains" on one board
  - Linux side (Python) receives messages from the laptop over the local network
  - Microcontroller side (Arduino C++ sketch) controls LEDs, vibration motor, LED matrix
  - The two sides talk through the Arduino Router Bridge (MessagePack RPC)
- Extension: vibration motor + LEDs

## Tech stack
### Laptop (main program, about 80% of the work)
- Python 3.12.10 (ARM64) at `%LOCALAPPDATA%\Programs\Python\Python312-arm64`
- **No OpenCV.** `opencv-python` and `opencv-python-headless` have no Windows ARM64
  wheel and fail to build from source on this laptop. Verified 2026-09-15. Instead:
  - **PyAV** (`av`) decodes the dashcam video frame by frame — ffmpeg bindings, ARM64 wheel works.
    Tested 2026-09-15: `.mp4`, `.mov` and HEVC-in-`.mov` all decode to byte-identical results,
    so **use whatever your camera or phone produces — no converting needed.** The container and
    codec do not matter. What does matter is **rotation**: phone footage carries a rotation flag
    that PyAV does not apply on decode, so frames can arrive sideways and confuse the vision
    model. Film in landscape, or pass `--rotate 90|180|270`.
  - **numpy** does the frame differencing / ego-motion maths.
  - Pillow encodes frames for the vision model and the report screenshots.
- GenieX CLI v0.6.0 (QAIRT 2.45), installed at `%LOCALAPPDATA%\GenieX CLI\geniex.exe`
  - Vision model: `qualcomm/Qwen3-VL-4B-Instruct:W4A16` — cached and WORKING.
    Note the `qualcomm/` prefix and the `:W4A16` precision suffix; the API rejects the bare name.
  - Report model: `qualcomm/Qwen3-4B` (not pulled yet)
  - `geniex serve` exposes an OpenAI-compatible API at `http://127.0.0.1:18181/v1`
  - `geniex serve -c npu|cpu|gpu|hybrid` picks the compute unit — this is how we build
    the NPU-vs-CPU speed panel for the demo.
- requests: alerts to the UNO Q, and the GenieX HTTP API
- Report page: HTML/JS or React

### UNO Q (about 20% of the work)
- Sketch is deployed with **Arduino App Lab**; the Linux side is a plain Python script.
- Bridge syntax verified 2026-09-15 against the Arduino Router RPC docs:
  - Sketch (C++): `#include "Arduino_RouterBridge.h"`, then `Bridge.begin()` and
    `Bridge.provide("alert", alert)` in `setup()`. `Monitor.begin()` / `Monitor.println()` for debug.
  - Linux (Python): connect to the Unix socket `/var/run/arduino-router.sock` and send
    MessagePack RPC. `notify` is `[2, "method", [args]]` — fire-and-forget, which is all we need.
    Needs `pip3 install msgpack --break-system-packages` on the board.
  - **The built-in LED is active-low**: `digitalWrite(LED_BUILTIN, LOW)` turns it ON.
  - The UNO Q ADC is 14-bit, so `analogRead` returns 0-16383 (not 0-1023).
- Docs: https://docs.arduino.cc/tutorials/uno-q/routerbridge-multilanguage/
- Reference template (laptop -> MCP -> UNO Q): https://github.com/DerrickJ1612/snapdragon-mcp-arduino

## Architecture
```
LAPTOP                                     UNO Q
dashcam video -> OpenCV                    Python listener (Linux)
  fast layer: ego motion (every frame)         |
  smart layer: Qwen3-VL every 1-2 s  --HTTP-->  Bridge.call("alert", level)
    -> JSON: {stop_sign, traffic_light,         |
             light_is_for_our_lane,            Sketch (C++): LEDs, vibration, matrix
             stop_line_visible, pedestrian}
  state machine combines both over time
  end of drive -> Qwen3-4B writes report -> web page
```

## Two-layer detection (important)
- The vision model takes seconds per frame, so it cannot check every frame.
- Fast layer (PyAV + numpy, every frame): ego motion — is the car moving, slowing, or stopped?
- Smart layer (Qwen3-VL on NPU, every ~3 s): what is in the scene? Ask for JSON only, e.g.
  `{"stop_sign": true, "traffic_light": "red", "light_is_for_our_lane": true, "stop_line_visible": true, "pedestrian_in_crosswalk": false}`
- **Measured 2026-09-15:** ~2.7 s per vision call on a 640x480 frame once the model is warm
  (~15 s on the very first call, which includes model load). So the smart layer realistically
  samples every 3 s, not the 1-2 s originally assumed. Budget for that lag in the state machine.
- Neither layer is enough on its own. Mistake logic is a **state machine** combining both over time.
- **Never ask Qwen a judgement question** like "did the driver stop?". Ask it only what is visible in
  this one frame. All timing and motion judgements come from OpenCV plus the state machine.

## Detection design, per mistake

### 1. Possible incomplete stop (rolling stop) — feasible
- Qwen identifies that a stop sign is present and being approached.
- OpenCV measures ego motion continuously, on every frame.
- A state machine records the **minimum** motion during the approach window.
- Motion stays low enough for ~0.5-1 s -> record a genuine stop.
- Car passes the intersection without ever entering that low-motion state -> possible rolling stop.
- Call it "possible incomplete stop" unless we also have trustworthy vehicle speed.

**Measured on the synthetic test clips (2026-09-15).** The motion score is the frame-to-frame
difference divided by the frame's spatial gradient, which approximates pixels of scene shift
per frame. A raw difference does NOT work — it scales with how textured the scene is, so a
bland road reads as "stopped" at 12 m/s. Numbers with `MOTION_THRESHOLD = 0.25`:

| state | score |
|---|---|
| genuinely stopped | 0.00 - 0.05 |
| crawling at 3 m/s | 0.27 - 0.6 |
| driving at 12 m/s | 0.9 - 1.9 |

Detected 8.8-11.7s against a true stop of 9.0-11.0s, and correctly found no stop at all in
the rolling clip. **The margin is thin**: a 3 m/s crawl scores 0.27 against a 0.25 threshold,
so a slower crawl (~1 m/s) would fall below it and be read as a genuine stop — a missed
violation. Real footage also has sensor noise that lifts the "stopped" floor above 0.00,
squeezing the gap further. Recalibrate on real video before the demo.

**Confirmed on real footage — IMG_9830.MOV, 1920x1072, 114s (2026-09-15).** The pipeline found
a real stop sign at 45-60s and a real 1.1s stop at 64.0-65.0s. Verified frame by frame. The
deceleration curve is clean: 0.57 -> 0.36 -> **0.18 / 0.19 (stopped)** -> 0.31 -> 1.54.

Real-footage score bands, which supersede the synthetic ones:

| state | score |
|---|---|
| stopped (engine running, real sensor noise) | 0.07 - 0.24 |
| just starting to move | 0.31 - 0.40 |
| normal driving | 0.40 - 1.60 |

`MOTION_THRESHOLD = 0.25` works, but the gap between stopped (0.24 max) and moving (0.31 min)
is only ~25%. Do not tighten it without re-testing. Note the ceiling is ~1.6 on real video, not
the ~9 the synthetic clips produced — synthetic footage exaggerates the dynamic range.

**THE STOP SIGN LEAVES THE FRAME BEFORE THE CAR REACHES THE LINE.** This is the single most
important thing the real video taught us. The sign was visible 45-60s, gone from the forward
view by 63s (only a sliver at the right edge, which the model did not report), and the car
stopped at 64-65s. So `stop_sign == true` and `stopped == true` **never co-occur in the same
frame** — a detector that requires both at once will flag every correct stop as a violation.

The state machine must therefore:
1. Open an approach window when a stop sign is first seen.
2. Keep it open for ~8-10 s after the sign disappears, since that is when the stop happens.
3. Close it on a stop (pass) or when the window expires with no stop (possible violation).

### 2. Possible red-light violation — feasible but harder
`traffic_light == red` + `moving == true` is NOT enough — the driver may simply be approaching the light.
Both halves are required:

```
red signal relevant to our lane
             +
vehicle crosses the stop line / intersection boundary
             =
possible red-light violation
```

- Use carefully chosen test clips where the signal and the stop line are both clearly visible.
- "Relevant to our lane" is the hard part: side-street signals and turn arrows cause false positives.

**Qwen3-VL hallucinates traffic lights (measured 2026-09-15).** On the synthetic test clips,
which contain NO traffic light of any kind, the model reported `traffic_light: "red"` at two
separate timestamps — and at one of them also said `light_is_for_our_lane: true`. Reproducible
across both clips at `temperature: 0`. It very likely reads the red octagonal stop sign as a
red light, and the prompt's menu of colours pushes it to pick one.

This is the single biggest threat to the demo: a hallucinated red light plus "moving" would
fire a false **critical** error and sink our credibility on stage. Mitigations, in order:
1. **Never act on a single frame.** Require the same detection in 2+ consecutive vision samples.
2. Make the prompt demand evidence — only report a light if the actual signal housing is visible.
3. Cross-check against the stop-line rule: no stop line or intersection crossing, no violation.
4. Consider dropping the traffic-light field entirely on frames where `stop_sign` is true.

**Real footage is better than the synthetic clips suggested, but not clean (2026-09-15).**
On IMG_9830.MOV the model called `light=red (ours)` through 0-24s and it was **correct** — the
frames show a red left-arrow and red ball on the mast, with a car stopped ahead. So the
synthetic hallucination was partly an artefact of crude synthetic imagery, and real red-light
detection is genuinely viable.

The stop-sign confusion is real though. Across the six samples where the stop sign was visible,
the model claimed a red light at 45s, 48s and 54s but **not** at 51s, 57s or 60s — flickering
in and out across a stretch with no traffic light anywhere. That inconsistency is itself the
signal: a real light does not blink in and out between samples. Requiring agreement across
consecutive samples would have rejected all three false positives here.

`stop_line_visible` over-triggers badly — it fired on most samples, including at 90s where the
frame shows only pavement lettering beside a parking-structure wall. Do not lean on it as the
intersection-crossing test without tightening the prompt first.

### 3. Unsafe following-distance risk — experimental
- Qwen's `car_ahead_close` is a subjective answer; never present it as a measured distance.
- Hackathon label: "unsafe following-distance risk detected".
- Better version if time allows: track the lead car's bounding box over several frames and estimate
  time-to-collision from how fast the box grows. That is a measurement rather than an opinion.

### 4. Head check — build last
- A head check can take well under a second, so sampling Qwen every 2 s will simply miss it.
- Needs a **fast head-pose layer on every driver-camera frame**, not the VLM.
- Then intersect that head-pose signal with a lane-change window detected from the forward camera.
- For the first complete demo: postpone this, or use synchronised prerecorded driver video.

## Alert levels (laptop -> UNO Q)
| level | meaning | signal |
|---|---|---|
| 0 | driving fine | green LED |
| 1 | heads up (stop sign / red light / pedestrian ahead) | yellow LED + short buzz |
| 2 | minor mistake | red LED + long buzz, counter +1 |
| 3 | critical mistake | red flashing + 3 buzzes |
LED matrix shows the minor-error count, e.g. "4/15".

## Build order (keep a working demo at every step; commit at each checkpoint)
1. Stop-sign recognition + possible rolling-stop detection: Qwen scene JSON, OpenCV ego motion, state machine.
2. Red-light warning, plus one controlled violation clip showing signal and stop line clearly.
3. UNO Q: Blink in App Lab, then the alert app (Python listener + sketch) — LEDs and vibration.
4. Timestamped screenshots + end-of-drive practice report (Qwen3-4B -> web page).
5. Following-distance risk, if time remains.
6. Head checks — only after everything above works.
7. Polish: NPU speed panel (NPU vs CPU), backup demo recording.

## Demo plan
- Play a dashcam video as if live; LEDs/buzzer react; counter goes up; report appears at the end.
- Laptop disconnected from the internet (local hotspot only for the UNO Q link).
- Always keep a backup recording of a working run.

## Constraints and ethics
- Do not break traffic laws to create demo footage.
- Only use videos with consent from people in them, or datasets whose license allows this use.

## Business angle
- Customers: parents of teen drivers (subscription), driving schools, later insurance and fleets.
- Value: safer new drivers; privacy because video stays on the device.

## Open questions
- Where the dashcam videos come from (own recordings vs dataset).
- Curated test clips needed: a clear stop-sign approach, and one red-light clip where both the
  lane-relevant signal and the stop line are plainly visible.
- What counts as "low motion" for a stop, in ego-motion units — needs calibration on real footage.
- UNO Q IP address on the local network (`hostname -I` on the board).
- Whether the external LEDs and vibration motor are wired yet, and to which pins.
  The signal smoke test deliberately uses only the built-in LED so no wiring is needed.

## Answered
- **Sending images to GenieX:** the OpenAI-compatible `/v1/chat/completions` endpoint accepts
  `content: [{"type": "text", ...}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]`.
  Verified working, and Qwen3-VL returned clean parseable JSON with no markdown fence.

## How to work with me (the developer)
- I'm new to hardware and Arduino; I know web development and Python.
- Explain things simply, step by step.
- Prefer small, runnable scripts I can test one at a time.
