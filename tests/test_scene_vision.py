"""Reading the model's reply: what we repair, and what we refuse to guess at.

The model is asked for JSON and mostly gives it. When it does not, there are two
very different situations and they must not be confused:

  * a reply that is unambiguous but not valid JSON - `none` where `"none"` was
    meant - which we can repair with no guessing at all;
  * a reply that is actually missing information, which we must NOT repair,
    because inventing a field is how a coach fabricates an accusation.

A failed parse is already safe: the sample falls back to all-false, which can
only cost a detection. But a failure is still a sample the drive did not get,
and the page has to show a warning about it, so the ones we can legitimately
recover are worth recovering.
"""

import json

import pytest
from scene_vision import EMPTY_SCENE, extract_json, tidy

# The exact shape Qwen3-VL returned on IMG_9830 at 84.0s, which failed the pass
# and put a warning banner on the review page. `none` is Python's spelling; JSON
# has no such literal.
REAL_FAILURE = (
    '{"stop_sign": false, "traffic_light": none, "light_is_for_our_lane": false, '
    '"stop_line_visible": false, "pedestrian_in_crosswalk": false, '
    '"car_ahead_close": false}'
)


def test_a_bare_none_is_repaired_not_dropped():
    """The one real parse failure in 38 calls on the canonical drive."""
    parsed = extract_json(REAL_FAILURE)
    assert parsed is not None, "a reply we can read unambiguously was thrown away"
    assert parsed["stop_sign"] is False
    assert parsed["traffic_light"] in (None, "none")


@pytest.mark.parametrize("literal", ["none", "None", "null", "NULL", "nil"])
def test_every_spelling_of_nothing_lands_on_none(literal):
    """Whatever the model calls it, an absent signal is 'none' to us.

    tidy() already turns anything that is not a colour into 'none', so this
    repair cannot smuggle a value through - it only stops the whole sample being
    discarded over a quoting mistake.
    """
    scene = dict(EMPTY_SCENE)
    parsed = extract_json('{"traffic_light": %s, "stop_sign": false}' % literal)
    assert parsed is not None
    scene.update({k: v for k, v in parsed.items() if k in EMPTY_SCENE})
    assert tidy(scene)["traffic_light"] == "none"
    assert tidy(scene)["light_is_for_our_lane"] is False


def test_valid_json_is_untouched():
    """The repair must never alter a reply that was already correct."""
    good = json.dumps({**EMPTY_SCENE, "traffic_light": "red",
                       "light_is_for_our_lane": True})
    assert extract_json(good) == json.loads(good)


def test_a_quoted_none_inside_a_value_is_not_mangled():
    """Only a bare literal in value position is repaired, never text."""
    parsed = extract_json('{"traffic_light": "none", "note": "none seen here"}')
    assert parsed["traffic_light"] == "none"
    assert parsed["note"] == "none seen here"


def test_a_genuinely_broken_reply_still_fails():
    """We recover a quoting slip. We do not invent a reply that never arrived.

    A truncated answer is missing information, and guessing at it is exactly the
    thing this project must not do - a fabricated 'false' reads identically to
    an observed one downstream.
    """
    assert extract_json('{"stop_sign": false, "traffic_light":') is None
    assert extract_json("I could not see the road clearly.") is None
    assert extract_json("") is None


def test_prose_and_fences_around_the_json_still_work():
    """Pre-existing behaviour, pinned so the repair does not break it."""
    fenced = '```json\n{"stop_sign": true}\n```'
    assert extract_json(fenced) == {"stop_sign": True}
    chatty = 'Here is what I see: {"stop_sign": true} - hope that helps.'
    assert extract_json(chatty) == {"stop_sign": True}
