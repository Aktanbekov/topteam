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
- Python
- OpenCV: read dashcam video frame by frame; frame differencing to detect moving vs stopped
- GenieX (Qualcomm on-device runtime): `pip install geniex`
  - Vision model: `ai-hub-models/Qwen3-VL-4B-Instruct` (WORKING on this laptop already)
  - Backup vision model: `ai-hub-models/Qwen2.5-VL-7B-Instruct`
  - Report model: `ai-hub-models/Qwen3-4B` (or Qwen3-4B-Instruct-2507)
  - `geniex serve` exposes an OpenAI-compatible API at `http://127.0.0.1:18181/v1`
- requests: send alerts to the UNO Q
- Report page: HTML/JS or React

### UNO Q (about 20% of the work)
- Written and run with **Arduino App Lab** (one app = a Python file + a sketch)
- Python listener (Linux side) -> `Bridge.call("alert", level)`
- Sketch (C++) -> `Bridge.provide("alert", alert)` and drives the pins
- Verify exact Bridge syntax: https://docs.arduino.cc/tutorials/uno-q/routerbridge-multilanguage/
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
- Fast layer (plain OpenCV, every frame): ego motion — is the car moving, slowing, or stopped?
- Smart layer (Qwen3-VL on NPU, every 1-2 s): what is in the scene? Ask for JSON only, e.g.
  `{"stop_sign": true, "traffic_light": "red", "light_is_for_our_lane": true, "stop_line_visible": true, "pedestrian_in_crosswalk": false}`
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
- Exact GenieX Python API for sending images (check GenieX docs / examples folder).
- UNO Q IP address on the local network.

## How to work with me (the developer)
- I'm new to hardware and Arduino; I know web development and Python.
- Explain things simply, step by step.
- Prefer small, runnable scripts I can test one at a time.
