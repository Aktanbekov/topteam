"""Smart layer: ask Qwen3-VL what is in the frame.

Runs on the NPU through the GenieX server, roughly every 3 seconds - a call
takes about 2.7s warm, so this cannot run per frame. The fast ego-motion layer
covers the gaps.

The prompt asks only about what is VISIBLE. No judgement questions ("did the
driver stop?"), because the model cannot see across time and would guess. All
timing and motion reasoning belongs to the state machine.
"""

import base64
import io
import json
import re
import statistics
import time

import requests
from PIL import Image

from config import GENIEX_URL as BASE_URL  # noqa: F401  (re-exported)
from config import VISION_MODEL as MODEL  # noqa: F401  (re-exported)

TIMEOUT_S = 180

JSON_SHAPE = (
    '{"stop_sign": true/false, '
    '"traffic_light": "red"/"yellow"/"green"/"none", '
    '"light_is_for_our_lane": true/false, '
    '"stop_line_visible": true/false, '
    '"pedestrian_in_crosswalk": true/false, '
    '"car_ahead_close": true/false}'
)

# The first prompt we shipped. Kept so tools/eval_scene_prompt.py can show the
# improvement, and so we can fall back if a change makes things worse.
TERSE_PROMPT = (
    "You are looking at one frame from a car's forward dashcam. "
    "Report only what is visible in this frame. Reply with JSON only, "
    "no explanation and no markdown:\n" + JSON_SHAPE
)

# Measured on IMG_9830 (2026-09-15): the terse prompt above answers true far
# too readily. It claimed a close car ahead on 7 frames of empty road, a red
# light on 4 frames with no signal anywhere, and a pedestrian on 4 frames with
# nobody in them. A bare menu of true/false invites the model to pick
# something; naming what each field excludes takes that invitation away.
STRICT_PROMPT = (
    "You are looking at ONE still frame from a car's forward dashcam.\n"
    "\n"
    "Answer only from what is clearly visible in THIS frame. Most frames of an\n"
    "ordinary drive contain none of these things, so false/none is the normal\n"
    "answer. A false alarm is worse than a miss: when in doubt, answer false.\n"
    "\n"
    "stop_sign: a red octagonal STOP sign facing the road we are driving on.\n"
    "traffic_light: the colour of a lit traffic signal. Only answer red, yellow\n"
    "  or green if you can see the signal housing itself, hanging over or beside\n"
    "  the road with a lamp lit in it. A red octagonal STOP sign is NOT a\n"
    "  traffic light. Brake lights, tail lights and red signs are NOT traffic\n"
    "  lights. If you cannot see a signal head, answer none.\n"
    "light_is_for_our_lane: false unless traffic_light is red, yellow or green\n"
    "  AND that signal governs the lane we are driving in.\n"
    "stop_line_visible: a solid white bar painted across our own lane, where a\n"
    "  car must stop. Crosswalk stripes, lane markings, arrows and words painted\n"
    "  on the road are NOT stop lines.\n"
    # The key is still named ..._in_crosswalk for compatibility, but the rule
    # deliberately does NOT require one. Measured 2026-09-16 on IMG_9840, a car
    # park: the old wording ("a person on the roadway") missed a man walking
    # straight across the lane in front of the car, in 3 frames out of 3.
    # Renaming the key to person_on_our_path changed nothing (still 0 of 3), so
    # the key was never the problem - the word "roadway" was, and a car park
    # aisle is not one. Asking for any person on foot gets 2 of 3.
    "pedestrian_in_crosswalk: true if ANY person on foot is visible ahead of us\n"
    "  standing or walking on the ground, outside a vehicle, whether or not\n"
    "  there are road markings. Look carefully - people are small in the frame.\n"
    "  Only a person inside a vehicle or behind a window is false.\n"
    "car_ahead_close: a vehicle in our own lane, directly ahead of us, near\n"
    "  enough that we would have to brake for it. Parked cars, cars on cross\n"
    "  streets, oncoming cars and cars far down the road are all false.\n"
    "\n"
    "Reply with JSON only, no explanation and no markdown:\n" + JSON_SHAPE
)

SCENE_PROMPT = STRICT_PROMPT

# What we fall back to when a call fails. Every field false/none means "saw
# nothing", which is the safe default: it can only cause a missed detection,
# never a false accusation against the driver.
EMPTY_SCENE = {
    "stop_sign": False,
    "traffic_light": "none",
    "light_is_for_our_lane": False,
    "stop_line_visible": False,
    "pedestrian_in_crosswalk": False,
    "car_ahead_close": False,
}


# A bare word where JSON wants a literal. The model occasionally writes Python's
# `none` instead of `"none"` for traffic_light - once in 38 calls on IMG_9830, at
# 84.0s, which failed the sample and put a warning banner on the review page.
#
# The pattern only matches a bare token in VALUE position: immediately after a
# colon, and immediately before a comma or a closing brace. A quoted "none", the
# word inside a sentence, and a longer word starting with "none" all fail to
# match, so this cannot reach into content.
BARE_NOTHING = re.compile(r"(?<=:)\s*(?:none|None|NULL|nil)\s*(?=[,}\]])")


def extract_json(text):
    """Pull a JSON object out of a reply that may be wrapped in prose or fences.

    One repair is applied, and only one: a bare `none` in value position becomes
    `null`. That is a quoting slip with exactly one possible reading, and
    tidy() maps the result onto "none" anyway, so recovering it cannot smuggle a
    value past any check - it only stops a whole sample being discarded.

    Nothing else is repaired. A truncated or evasive reply is MISSING
    information, and filling it in would be indistinguishable downstream from
    something the model actually observed. A dropped sample costs a detection; an
    invented one costs the driver a fabricated error, and that is the trade this
    project is built around.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None

    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    repaired = BARE_NOTHING.sub(" null", candidate)
    if repaired == candidate:
        return None
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def tidy(scene):
    """Repair answers that contradict themselves.

    The model happily returns light_is_for_our_lane=true alongside
    traffic_light="none" (three times in the IMG_9830 pass). Whatever it meant,
    a lane-relevant signal with no signal is not a fact we can act on, so the
    field goes back to false. Same for an unrecognised colour.
    """
    if scene.get("traffic_light") not in ("red", "yellow", "green"):
        scene["traffic_light"] = "none"
        scene["light_is_for_our_lane"] = False
    return scene


class SceneVision:
    """One scene-labelling call per frame handed to it."""

    def __init__(self, base_url=BASE_URL, model=MODEL, jpeg_quality=85):
        self.base_url = base_url
        self.model = model
        self.jpeg_quality = jpeg_quality
        self.calls = 0
        self.failures = 0
        # Wall-clock seconds per successful call. The review page reports the
        # median and p95 from these rather than quoting the number in the
        # brief - a measurement that comes from the run itself cannot go stale.
        self.latencies = []

    def reset_stats(self):
        """Forget every measurement so far.

        The live path warms the model with one throwaway frame before the drive
        starts, so the weights load outside the drive rather than blinding its
        first twenty seconds. That call is real inference and would otherwise be
        reported as the drive's cold start - which would be the opposite of the
        truth, since the whole point was to move it out of the drive.
        """
        self.calls = 0
        self.failures = 0
        self.latencies = []

    def _data_url(self, rgb):
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=self.jpeg_quality)
        encoded = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{encoded}"

    def describe(self, rgb, prompt=None):
        """Label one frame. Returns (scene_dict, error_or_None).

        Never raises: a dropped call should not end the drive analysis.
        `prompt` overrides SCENE_PROMPT, which is what the eval tool uses.
        """
        self.calls += 1
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or SCENE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": self._data_url(rgb)},
                        },
                    ],
                }
            ],
            "max_tokens": 200,
            "temperature": 0,
        }

        started = time.perf_counter()
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions", json=payload, timeout=TIMEOUT_S
            )
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            self.failures += 1
            return dict(EMPTY_SCENE), f"request failed: {exc}"
        self.latencies.append(time.perf_counter() - started)

        parsed = extract_json(reply)
        if parsed is None:
            self.failures += 1
            return dict(EMPTY_SCENE), f"unparseable reply: {reply.strip()[:120]}"

        # Fill in anything the model left out rather than raising KeyError later.
        scene = dict(EMPTY_SCENE)
        scene.update({k: v for k, v in parsed.items() if k in EMPTY_SCENE})
        return tidy(scene), None

    def stats(self):
        """Measured call latencies, for the review page's processing panel.

        The FIRST call is reported separately and left out of the warm figures.
        It carries the model load - measured at ~15s against ~3s warm - so
        folding it into a median over a short run would be reporting a number
        that describes nothing that happens again. Both are here: the cold
        start is real and worth showing, it is just a different quantity.

        Returns None where there is nothing to report rather than a zero: an
        empty run should read as "not measured", not as "infinitely fast".
        """
        empty = {
            "calls": self.calls,
            "failed": self.failures,
            "first_call_s": None,
            "median_s": None,
            "p95_s": None,
            "min_s": None,
            "max_s": None,
        }
        if not self.latencies:
            return empty

        empty["first_call_s"] = round(self.latencies[0], 2)
        warm = sorted(self.latencies[1:])
        if not warm:
            return empty

        # Nearest-rank p95. With a few dozen samples an interpolated percentile
        # would be pretending to a precision we do not have.
        p95 = warm[min(len(warm) - 1, int(round(0.95 * len(warm))) - 1)]
        empty.update(
            median_s=round(statistics.median(warm), 2),
            p95_s=round(p95, 2),
            min_s=round(warm[0], 2),
            max_s=round(warm[-1], 2),
        )
        return empty

    def health(self):
        """True if the GenieX server answers."""
        try:
            return requests.get(f"{self.base_url}/models", timeout=10).status_code == 200
        except requests.RequestException:
            return False
