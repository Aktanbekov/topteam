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

import requests
from PIL import Image

BASE_URL = "http://127.0.0.1:18181/v1"
MODEL = "qualcomm/Qwen3-VL-4B-Instruct:W4A16"
TIMEOUT_S = 180

SCENE_PROMPT = (
    "You are looking at one frame from a car's forward dashcam. "
    "Report only what is visible in this frame. Reply with JSON only, "
    "no explanation and no markdown:\n"
    '{"stop_sign": true/false, '
    '"traffic_light": "red"/"yellow"/"green"/"none", '
    '"light_is_for_our_lane": true/false, '
    '"stop_line_visible": true/false, '
    '"pedestrian_in_crosswalk": true/false, '
    '"car_ahead_close": true/false}'
)

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


def extract_json(text):
    """Pull a JSON object out of a reply that may be wrapped in prose or fences."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


class SceneVision:
    """One scene-labelling call per frame handed to it."""

    def __init__(self, base_url=BASE_URL, model=MODEL, jpeg_quality=85):
        self.base_url = base_url
        self.model = model
        self.jpeg_quality = jpeg_quality
        self.calls = 0
        self.failures = 0

    def _data_url(self, rgb):
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=self.jpeg_quality)
        encoded = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{encoded}"

    def describe(self, rgb):
        """Label one frame. Returns (scene_dict, error_or_None).

        Never raises: a dropped call should not end the drive analysis.
        """
        self.calls += 1
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SCENE_PROMPT},
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

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions", json=payload, timeout=TIMEOUT_S
            )
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            self.failures += 1
            return dict(EMPTY_SCENE), f"request failed: {exc}"

        parsed = extract_json(reply)
        if parsed is None:
            self.failures += 1
            return dict(EMPTY_SCENE), f"unparseable reply: {reply.strip()[:120]}"

        # Fill in anything the model left out rather than raising KeyError later.
        scene = dict(EMPTY_SCENE)
        scene.update({k: v for k, v in parsed.items() if k in EMPTY_SCENE})
        return scene, None

    def health(self):
        """True if the GenieX server answers."""
        try:
            return requests.get(f"{self.base_url}/models", timeout=10).status_code == 200
        except requests.RequestException:
            return False
