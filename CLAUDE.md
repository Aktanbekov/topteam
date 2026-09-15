# Project: AI Driving Test Coach (Qualcomm GenieX Hackathon)

## What we're building
An offline AI coach for **practice drives** before the DMV road test.
- Dashcam video is analyzed by a local vision model on the Snapdragon NPU.
- The model detects driving mistakes (minor = strike, critical = instant fail).
- An Arduino UNO Q gives live signals: LEDs + vibration motor + LED-matrix mistake counter.
- After the drive, a text model writes a report: PASS/FAIL, each mistake with timestamp + screenshot, and tips.
- Pitch: "No internet. The video never leaves the laptop."
- Positioning: a coach for practice drives and post-drive review. NOT for use during a real DMV exam.

## Scoring rules (based on the California DMV DL-955 score sheet; verify against the official DMV handbook)
- Critical driving error (e.g., running a red light, not stopping at a stop sign, failing to yield) = automatic FAIL.
- Minor errors during the drive: up to 15 allowed.
- Optional simplified "3 strikes" practice mode.

## Hackathon scope: detect these 4 mistakes first
1. Not fully stopping at a stop sign (critical)
2. Going through a red light (critical)
3. Following too close (minor)
4. No head check before a lane change (minor; needs the driver-facing camera)

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
  fast layer: moving/stopped (every frame)     |
  smart layer: Qwen3-VL every 1-2 s  --HTTP-->  Bridge.call("alert", level)
    -> JSON: {stop_sign, red_light,             |
             pedestrian, car_ahead_close}      Sketch (C++): LEDs, vibration, matrix
  mistake logic (rules above)
  end of drive -> Qwen3-4B writes report -> web page
```

## Two-layer detection (important)
- The vision model takes seconds per frame, so it cannot check every frame.
- Fast layer (plain OpenCV, instant): is the car moving or stopped?
- Smart layer (Qwen3-VL on NPU, every 1-2 s): what is in the scene? Ask for JSON only, e.g.
  `{"stop_sign": true, "traffic_light": "red", "pedestrian_in_crosswalk": false, "car_ahead_close": false}`
- Combine both: stop sign seen + car never reached "stopped" = rolling stop.

## Alert levels (laptop -> UNO Q)
| level | meaning | signal |
|---|---|---|
| 0 | driving fine | green LED |
| 1 | heads up (stop sign / red light / pedestrian ahead) | yellow LED + short buzz |
| 2 | minor mistake | red LED + long buzz, counter +1 |
| 3 | critical mistake | red flashing + 3 buzzes |
LED matrix shows the minor-error count, e.g. "4/15".

## Build order (keep a working demo at every step; commit at each checkpoint)
1. Laptop: read a dashcam video, sample frames, get JSON scene labels from Qwen3-VL.
2. Laptop: fast moving/stopped detection + mistake rules; print mistakes with timestamps.
3. UNO Q: Blink example in App Lab, then the alert app (Python listener + sketch).
4. Connect laptop alerts -> UNO Q signals.
5. End-of-drive report with Qwen3-4B + screenshots on a web page.
6. Driver-facing camera for head checks.
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
- Exact GenieX Python API for sending images (check GenieX docs / examples folder).
- UNO Q IP address on the local network.

## How to work with me (the developer)
- I'm new to hardware and Arduino; I know web development and Python.
- Explain things simply, step by step.
- Prefer small, runnable scripts I can test one at a time.
