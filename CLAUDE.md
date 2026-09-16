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
    so **for analysis, use whatever your camera or phone produces — no converting needed.**
    **Browsers are a different story.** Chrome and Edge will not play a `.mov` even when the
    video inside is ordinary H.264, so the review player cannot point at the original file.
    `tools/make_player.py` handles this automatically: it rewraps the video as MP4 by stream
    copy (seconds, no re-encoding, no quality loss) and points the page at that. HEVC sources
    get re-encoded instead, which is slow — another reason to prefer H.264 when recording.
    What also matters is **rotation**: phone footage carries a rotation flag
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

**The link is MCP over USB/ADB, not Wi-Fi. Verified working end to end 2026-09-15.**

`adb` ships with App Lab at
`%LOCALAPPDATA%\Arduino15\packages\arduino\tools\adb\32.0.0\adb.exe`. In Git Bash,
prefix every adb command with `MSYS_NO_PATHCONV=1` or it rewrites `/home/arduino`
into a Windows path and `adb push` fails with `secure_mkdirs failed`.

```
player page --POST /api/alert--> serve_player.py --MCP--> 127.0.0.1:3001
   --adb forward--> UNO Q mcp_server.py --> router socket --> alert_sketch.ino
```

Board facts, all checked on the device:
- Debian 13, Python 3.13.5, user `arduino`, `/var/run/arduino-router.sock` is world-writable.
- **`wlan0` was down out of the box**, so `hostname -I` returned only docker's
  `172.17.0.1`. Any plan that needs the board's IP is dead until Wi-Fi is up.
  It is now on 192.168.1.143, but USB remains the transport — no Wi-Fi to fail on stage.
- There was **no pip at all** and no internet, so `pip3 install msgpack` could
  never have run. `arduino_bridge.py` therefore encodes MessagePack itself:
  every message is an array of small ints and a short method name, which is
  three type tags and ~20 lines. Verified byte-exact, `notify alert(3)` ->
  `93 02 a5 "alert" 91 03`. **Do not reintroduce the msgpack dependency.**
- FastMCP 4.0.4 now on both board and laptop.
- On the **laptop**, `pip install fastmcp` fails: `cryptography` has no win-arm64
  source build. A prebuilt wheel exists but pip resolves to an older version
  first. Fix: `pip install --only-binary :all: cryptography` then `pip install fastmcp`.
  Same wheel trap as OpenCV — check `--only-binary` before concluding a package
  is unavailable on this laptop.

**`bridge.call()` vs `bridge.notify()` matters.** `alert` and `resetDrive` are
`void` in the sketch, so they never reply — calling them with `call()` blocks
until timeout. Use `notify` for those and `call` only for `mcu_ping`, which
returns a value. That is what makes `mcu_ping` worth having: it is the only
call that proves the *sketch* is running, not merely that the router accepted
our bytes.

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
LAPTOP                                          UNO Q
dashcam video -> PyAV decode                    mcp_server.py (Linux)
  fast layer: ego motion (every frame, numpy)       |
  smart layer: Qwen3-VL every ~3 s                  |
    -> JSON: {stop_sign, traffic_light,             |
             light_is_for_our_lane,                 |
             stop_line_visible, pedestrian}         |
  scene_filter: drop single-sample claims           |
  drive_review: state machine -> review.json        |
      |                                             |
      +-> player.html  --POST /api/alert-->  serve_player.py
      +-> report.html                          --MCP over USB/ADB-->
      +-> level track (exact timestamps)     Bridge.notify("alert", level)
                                                    |
                                       Sketch (C++): LEDs, vibration, matrix
```

**`output/review.json` is the single source of truth.** The player, the report
and the hardware all render it and nothing else. They used to each re-interpret
the raw samples, which is precisely how they came to disagree - see the three
bugs listed at the top of `laptop/drive_review.py`.

`laptop/config.py` holds every shared default, and the settings travel inside
review.json so a report can always say what produced it.

## Invariants - break these and the product is dishonest

Four rules that everything else is arranged around. They have unit tests; if you change
behaviour here, change the test first and be sure you mean it.

1. **Nothing signals a failure before `decision_at`.** Every event carries two timestamps:
   `detected_at` (when we first had grounds to say anything) and `decision_at` (when the
   outcome was settled). On a failed stop-sign approach they are ten seconds apart. The
   old build fired CRITICAL from the moment the sign left the frame - announcing the
   mistake before the driver had the chance to make it.
   Test: `test_a_failure_is_not_decided_before_its_deadline`.

2. **Only a validated approach reaches the hardware.** `stop_sign` is deliberately
   excluded from `scene_filter` confirmation, so `confirmed["stop_sign"]` is always the
   raw reading. Nothing may light an LED or score an error off that field - it must go
   through `sign_approaches`, which needs two sightings.
   Test: `test_an_unvalidated_sighting_never_reaches_the_hardware`.

3. **A brief stop is never an error.** California asks for a complete stop and puts no
   number on it. The 3s target is ours, so it is coaching and must not move the strike
   count. Test: `test_a_brief_stop_is_a_coaching_note_and_never_a_strike`.

4. **No DMV verdict, ever.** Not "pass", not "fail", not "you failed". We are reading a
   forward camera with no speed feed. Test: `test_no_official_dmv_claim_anywhere` greps
   the whole serialised review.

Two more that are not tested but matter just as much: a **replayed** run is never shown as
a live one, and the compute unit is reported as **requested**, never as verified - the
OpenAI-compatible API does not say which unit served a request.

## Two-layer detection (important)
- The vision model takes seconds per frame, so it cannot check every frame.
- Fast layer (PyAV + numpy, every frame): ego motion — is the car moving, slowing, or stopped?
- Smart layer (Qwen3-VL on NPU, every ~3 s): what is in the scene? Ask for JSON only, e.g.
  `{"stop_sign": true, "traffic_light": "red", "light_is_for_our_lane": true, "stop_line_visible": true, "pedestrian_in_crosswalk": false}`
- **Measured 2026-09-15:** ~2.7 s per vision call on a 640x480 frame once the model is warm
  (~15 s on the very first call, which includes model load). So the smart layer realistically
  samples every 3 s, not the 1-2 s originally assumed. Budget for that lag in the state machine.
- **Re-measured 2026-09-16 on the canonical run:** 3.47 s warm median, 3.5 s p95 over 38 calls,
  first call 14.3 s. A 113.7 s clip took 156.7 s to analyse - a real-time ratio of **0.73x**.
  This is review speed, not live speed, and the page says so.
- **`geniex serve --keepalive` defaults to 300 s.** Leave the server idle for five minutes and
  it unloads the model; the next call then pays the reload - measured 22.3 s, after which it is
  back to 3.8 s. It does not affect the demo, where the analysis is already done and `--reuse`
  makes no calls at all, but a fresh `./run.sh` after a long wait will look hung for twenty
  seconds. Raise it with `geniex serve -c npu --keepalive 3600` if that matters.
- Neither layer is enough on its own. Mistake logic is a **state machine** combining both
  over time. It lives in `laptop/drive_review.py`, does no I/O, and is unit tested without
  a model, a video or a board anywhere near it.
- **Never ask Qwen a judgement question** like "did the driver stop?". Ask it only what is visible in
  this one frame. All timing and motion judgements come from the numpy motion layer plus
  the state machine in `laptop/drive_review.py`.

## The model says yes too easily — and what fixed it

Our first prompt listed the fields as a bare `true/false` menu. Scored against hand-labelled
frames from IMG_9830, it produced **22 clear false alarms in 38 frames**: a close car ahead on
7 frames of empty road, a red light on 6 frames with no signal anywhere, a pedestrian on 5
frames with nobody in them. A menu of choices invites the model to pick something.

Two changes, both measured (2026-09-15):

**1. A prompt that says what each field excludes** (`STRICT_PROMPT` in `laptop/scene_vision.py`).
It states that false is the normal answer, that a false alarm is worse than a miss, and for each
field spells out the near-misses: a STOP sign is not a traffic light; a person on the pavement is
not a pedestrian in the road; crosswalk stripes and road lettering are not a stop line; parked,
oncoming and far-away cars are not a car ahead.

| prompt | right | FALSE ALARM | missed | s/call |
|---|---|---|---|---|
| terse (original) | 118 | **22** | 0 | 3.06 |
| strict | 133 | **0** | 7 | 3.48 |
| strict + "name what you see first" | 130 | 3 | 7 | 4.54 |

Identical to the digit across three repeat runs, so this is a real effect and not luck.
Making the model justify itself first was *worse* and 30% slower — it invents evidence to
match an answer it has already decided on. Don't go back to it.

The 7 misses are all in the 0-24s stretch, where a red light and a close car really are there and
the model sees them in most but not all samples. That is the right way to be wrong for us:
a miss costs one detection, a false alarm costs a fabricated error in front of the judges.

**2. Two samples must agree** (`laptop/scene_filter.py`). A stop sign, a red light, a pedestrian
or a car in front all last several seconds, so a real one appears in consecutive samples 3s
apart; a hallucination usually does not. Every soft field is reset unless a neighbouring sample
backs it up. Each sample now carries both `scene` (raw) and `confirmed` (filtered) — show the
raw one, act only on the filtered one. The review player draws rejected detections struck
through, which is the most honest thing on the page and demos well.

The cost is one sampling interval of latency (~3s) before a warning appears. Fine for review,
acceptable live.

**Why both halves are needed.** On the old terse timeline, confirmation alone caught 7 of the
hallucinations but not the car-ahead ones at 45+48s and 81+84s, because the model repeated the
same mistake in consecutive frames. The prompt removes them at the source; confirmation mops up
what the prompt still lets through. Neither is sufficient alone.

`tidy()` in `scene_vision.py` also repairs answers that contradict themselves — the model
returned `light_is_for_our_lane: true` alongside `traffic_light: "none"` three times in one pass.

**Re-measure before changing the prompt again:**
```bash
python tools/eval_scene_prompt.py            # scores every variant
```
Labels are in `tools/labels/IMG_9830.json`, hand-written from `output/frames`. A label of `"any"`
marks a genuinely borderline frame and is not scored either way. Label a new clip the same way
before trusting a prompt change on new footage.

## Detection design, per mistake

### 1. Possible incomplete stop (rolling stop) — feasible
- Qwen identifies that a stop sign is present and being approached.
- `laptop/ego_motion.py` measures ego motion continuously, on every frame (numpy, no OpenCV).
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
1. Open an approach window when a stop sign is first seen (2+ samples, so one misread frame
   cannot invent a junction and then a critical error at it).
2. Keep it open for ~8-10 s after the sign disappears, since that is when the stop happens.
3. Close it on a stop (pass) or when the window expires with no stop (possible violation).

**How long the stop lasted matters too, and it is a third outcome, not a pass.** California law
asks for a complete stop and puts no number on it, but every instructor teaches "count to three",
and a car that is stationary for an instant has not really looked. So `judge()` grades three ways:

| grade | meaning | badge |
|---|---|---|
| `pass` | stopped for at least the target (default 3.0s, `--full-stop`) | green |
| `brief` | stopped, but under the target — legal, still coaching feedback | amber |
| `fail` | no stop found in the window — possible incomplete stop | red |

A `brief` stop is **not** a DMV error and must never be counted as one.

It picks the *longest* stop in the window, not the first: an approach often dips under the
threshold for a moment before the real stop.

**On IMG_9830 the real stop is 1.07s, not 3s** — measured 63.97-65.03s, a clean single window,
no fragmentation. Either side of it the car is creeping (0.26-0.45), not stopped. Widening the
threshold to 0.40 only stretches it to ~2.1s because the trace is a deceleration ramp, not a
plateau. So this clip demos `brief`, and **we still need footage with a proper 3-second stop**
to demo `pass`. Add it to the curated-clips list.

Remember the motion layer cannot tell a true 0 mph from a slow crawl, so word the report as
"about 1.1s at a standstill", never as a measured speed.

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
   — DONE, `laptop/scene_filter.py`.
2. Make the prompt demand evidence — only report a light if the actual signal housing is visible.
   — DONE as a rule in `STRICT_PROMPT`. Note: asking the model to *write out* its evidence first
   scored worse (3 false alarms vs 0) and was 30% slower. Stating the rule works; making it
   narrate does not.
3. Cross-check against the stop-line rule: no stop line or intersection crossing, no violation.
   — NOT DONE, and `stop_line_visible` is still too loose to lean on (see below).
4. Consider dropping the traffic-light field entirely on frames where `stop_sign` is true.
   — no longer needed: after 1 and 2, red-light claims on the stop-sign stretch went to zero.

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

**Update (same day, after the prompt fix):** the strict prompt cut it to 4 samples out of 38, and
confirmation dropped 3 of those. It is much better behaved, but it is still the shakiest field
and still not solid enough to carry the intersection-crossing test on its own.

With the strict prompt, the red-light reading on this clip is: real at 9-27s (the model misses
some samples but never invents one), and **zero claims anywhere on the stop-sign stretch**, which
is what the whole section above was worried about.

### 3. Unsafe following-distance risk — experimental
- Qwen's `car_ahead_close` is a subjective answer; never present it as a measured distance.
- Hackathon label: "unsafe following-distance risk detected".
- **This was the worst field of the six.** With the terse prompt it fired on 9 frames of plainly
  empty road, including the 81-84s stretch inside a parking structure with nothing ahead at all.
  The strict prompt plus confirmation cleared all of them: it now fires only at 9-12s and 24s,
  where a car really is stopped ahead of us at the light. See the section above.
- Being stopped behind a car at a red light is not tailgating. Any following-distance *mistake*
  must require the car to be moving — the confirmed flag alone is not enough.
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

## How to run it

One command, from **Git Bash** (installed with Git for Windows):

```bash
./run.sh                    # analyse the video in the project root, open the player
./run.sh clips/drive2.mov   # a specific file
./run.sh --reuse            # skip the vision pass, just rebuild the player + report
./run.sh --unoq             # also drive the board over USB as the video plays
./run.sh --every 5          # sample the vision model every 5s instead of 3
./run.sh --compute cpu      # ask GenieX for a different compute unit (default npu)
./run.sh --demo fail        # synthetic clip that ends in a critical error
./run.sh --full-stop 2      # how long a stop must last to count as full
./run.sh --window-after 12  # how long after the sign leaves the frame to keep looking
./run.sh --calm             # stop the board flashing and exit, nothing else
./run.sh --no-serve         # open the page from disk instead of serving it
```

It starts `geniex serve -c npu` if nothing is listening, analyses the drive, builds the
player AND the report, serves them on localhost and opens the player. Ctrl-C stops the
server and returns the board to level 0. The vision pass is the slow part (~1 call per
sample at ~3.4s warm); `--reuse` skips it when you are only changing the player, the
report or a threshold.

**The page is always served, never opened from `file://`.** A file:// page has no origin
to post level changes to, so the hardware could never work from one; Chrome and Edge
refuse to play a video from file:// often enough to need a footnote; and the report link
and the evidence stills are relative paths that behave differently from disk. One path
beats a split with three caveats. `--no-serve` restores the old behaviour.

**If GenieX was already running, run.sh records no compute unit and the NPU badge does not
appear.** It cannot know what an already-running server was started with, and a badge we
cannot stand behind is worse than none. Stop it and re-run to record it.

Tests — no model, no board, no video:

```bash
python -m pytest
```

Three files come out of a build, and the first is the important one:

    output/review.json    every fact, every decision, every caveat
    output/player.html    the video, synchronised with those decisions
    output/report.html    the same decisions, printable (Ctrl-P -> PDF)

A `--demo` build goes to `output/demo/<grade>/` so it never clobbers the real run.

The individual scripts still work on their own — `laptop/test_video.py` for motion-only
threshold calibration, `laptop/analyze_video.py` for a full pass, `tools/make_player.py` to
rebuild the page, `tools/serve_player.py` to serve it by hand,
`tools/eval_scene_prompt.py` to score a prompt change against hand-labelled frames,
`tools/benchmark_compute.py` to time the vision call, `laptop/calm_board.py` to stop the
board flashing.

Useful flags on `make_player.py`: `--full-stop 3` (how long a stop must last to count as a good
one) and `--window-after 10` (how long after the sign leaves the frame to keep looking for the
stop). Both are instant to change — rerun `./run.sh --reuse` after.

**Gotcha the script works around:** Windows ships a Microsoft Store stub named `python.exe`
that is not Python — it prints an advert and exits non-zero. `command -v python` finds it
happily, so `run.sh` runs a trivial import to prove a candidate is real, and falls back to
`~/AppData/Local/Programs/Python/Python3*/python.exe`. Same fallback for `geniex`. This also
covers the case of a terminal opened before either was installed, which carries a stale PATH.

## Build order (keep a working demo at every step; commit at each checkpoint)
1. **DONE** — Stop-sign recognition + possible rolling-stop detection: Qwen scene JSON,
   ego motion, state machine. Graded three ways (full / brief / possible incomplete stop).
2. **PARTLY** — Red-light warning works and is confirmed across samples. Still missing the
   controlled violation clip showing the signal and the stop line clearly.
3. **DONE** — UNO Q: matrix, Modulino Pixels and Vibro, driven live from the review player
   over MCP on a USB/ADB tunnel. End to end on real hardware.
4. Timestamped screenshots + end-of-drive practice report (Qwen3-4B -> web page).
   Screenshots exist in `output/frames`; the report model is not pulled yet.
5. Following-distance risk, if time remains.
6. Head checks — only after everything above works.
7. Polish: NPU speed panel (NPU vs CPU), backup demo recording.

### First working demo — tagged `demo-1`, 2026-09-16

One command does everything: `./run.sh --unoq`. GenieX, the vision pass, the board's
MCP server, the USB tunnel, the player, the browser. Press play and the hardware follows.

**The one thing standing between this and a demo that lands: footage.** IMG_9830 contains
no violation, so the drive only ever reaches level 1. The strip pulses amber and the matrix
shows the octagon, but the buzz for a mistake, the strike counter and the X never fire in a
real run — `laptop/test_signals.py` is the only way to see them. Get a clip with a genuine
rolling stop or red-light crossing before anything else.

## It is NOT live, and we never say it is

Worth being blunt, because it is the easiest claim to overstate and the easiest to catch:

- **There is no camera capture path at all.** `VideoSource` takes a file path and raises
  `FileNotFoundError` on anything else. Nothing reads a webcam or a device node.
- **The analysis is slower than the footage.** 0.73x real time on the canonical run, because
  38 calls at 3.47 s each is 132 s of inference for 114 s of video.
- **Confirmation costs one sampling interval by design.** A claim is only acted on once a
  neighbouring sample agrees, so a warning is ~3 s behind the event even in principle.
- **What IS synchronised live: the replay.** As the video plays, the board reacts at the exact
  timestamps from the level track, through the USB relay. That is genuine hardware reacting in
  real time - to decisions computed earlier.

So the honest phrasing is "post-drive review with synchronised evidence replay", which is what
the page, the report and the README all say. Calling it live detection would be the one claim
on stage that a judge could disprove with a stopwatch.

A real live mode would need: a capture source, a sampling interval above the call latency
(5 s+, or a smaller model), and the state machine fed incrementally rather than over a finished
timeline. It is on the deferred list in the implementation plan for good reason.

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
- Curated test clips needed: a clear stop-sign approach **with a full 3-second stop** (IMG_9830
  only has 1.1s, so it can only demo the amber "brief stop" grade), and one red-light clip where
  both the lane-relevant signal and the stop line are plainly visible.
- What counts as "low motion" for a stop, in ego-motion units — needs calibration on real footage.
- UNO Q IP address on the local network (`hostname -I` on the board).
- Whether the external LEDs and vibration motor are wired yet, and to which pins.
  The signal smoke test deliberately uses only the built-in LED so no wiring is needed.

## Answered
- **Why the board would not stop flashing:** the sketch holds its last level
  forever and has no way to know the laptop has gone. A `--demo fail` run ends on
  level 3 and stays there; a later run without `--unoq` never touches it, and the
  page has no relay, so play/pause genuinely does nothing. Four fixes, none of
  which need a re-flash: `./run.sh --calm`, a **Stop the board** button on the
  page, a reset sent on tab unload, and `run.sh` calming an idle board at the
  start of any run that is not driving it. On Windows, killing the terminal
  abruptly still leaves it lit - Python gets no chance to clean up - so `--calm`
  is the one to remember.
- **Is it really running on the NPU?** The QAIRT plugin will not run anywhere else
  and says so. `geniex serve -c cpu` logs:
  `qairt plugin only supports NPU inference; ignoring device='cpu' and running on NPU`
  So an NPU-vs-CPU benchmark is impossible on this machine - the CPU run gives an
  identical 3.4s because it IS the NPU run. `tools/benchmark_compute.py` reads that
  line, records it, and refuses to print a speedup between two labels that ran on
  the same silicon. The page still says "requested" because a build cannot check
  this; the presenter should say the stronger thing.
  Measured 2026-09-16: warm median 3.44s over 5 calls, first call 13.4s with load.
- **What counts as "low motion" for a stop:** calibrated on IMG_9830. Stopped tops out at
  0.24, moving starts at 0.31, threshold 0.25. The gap is only 25% - do not tighten it
  without re-running `laptop/test_video.py` on the footage in question.
- **UNO Q IP address:** not needed and deliberately not used. Everything goes over
  USB/ADB; `wlan0` is one more thing to fail on stage.
- **External LEDs and vibration motor wiring:** none needed. Both Modulinos daisy-chain on
  the QWIIC connector - no resistors, no transistor, the Vibro has its own MOSFET.
- **Where levels 2 and 3 come from:** level 3 from a failed stop-sign approach, at the
  deadline. Level 2 from nothing - a brief stop is legal and must not be counted as an
  error, and following distance is experimental. `laptop/test_signals.py` exercises it.
- **Sending images to GenieX:** the OpenAI-compatible `/v1/chat/completions` endpoint accepts
  `content: [{"type": "text", ...}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]`.
  Verified working, and Qwen3-VL returned clean parseable JSON with no markdown fence.

## How to work with me (the developer)
- I'm new to hardware and Arduino; I know web development and Python.
- Explain things simply, step by step.
- Prefer small, runnable scripts I can test one at a time.
