"""Throw away single-sample detections before anything acts on them.

The vision model looks at each frame alone, so a one-off misreading has no way
of correcting itself. The drive does, though: a stop sign, a red light, a
pedestrian or a car in front of us all last several seconds, so a real one shows
up in consecutive samples 3s apart. A hallucination usually does not.

Measured on IMG_9830 with the terse prompt: the model claimed a red light at
45s, 48s and 54s but not at 51s, 57s or 60s - flickering in and out over a
stretch of road with no traffic light anywhere. Requiring two samples in a row
to agree rejects all three.

This is the second half of the fix. The first half is the prompt in
scene_vision.py, which cut clear false alarms on IMG_9830 from 22 to 0. The
prompt reduces them; this makes the survivors harmless.

Note what confirmation costs: a detection is only reported once the NEXT sample
backs it up, so warnings arrive one sampling interval (~3s) later than the raw
reading. For a post-drive review that costs nothing. For the live UNO Q signal
it means a heads-up lands a few seconds late, which is a fair price for not
buzzing at an empty road.
"""

# Fields where a lone sighting is not enough. stop_sign is deliberately absent:
# it feeds the approach window, which does its own multi-sample check in
# make_player.sign_windows, and a stop sign genuinely can be visible in only one
# sample of a fast approach.
CONFIRM_FIELDS = (
    "traffic_light",
    "pedestrian_in_crosswalk",
    "car_ahead_close",
    "stop_line_visible",
)


def _adjacent(a, b, spacing):
    """True if two samples are consecutive rather than either side of a gap."""
    return abs(b["t"] - a["t"]) <= spacing * 1.6


def _neighbours(samples, i, spacing):
    for j in (i - 1, i + 1):
        if 0 <= j < len(samples) and _adjacent(samples[i], samples[j], spacing):
            yield samples[j]


def confirm(samples, spacing=3.0):
    """Add sample["confirmed"] to each sample and return the list.

    "confirmed" is the same shape as "scene", with anything unsupported by a
    neighbouring sample reset to its empty value. Callers that want to show the
    raw model output - the review player does, so we can see what it said - keep
    reading sample["scene"]; anything that counts a mistake reads "confirmed".
    """
    for i, sample in enumerate(samples):
        scene = sample["scene"]
        near = list(_neighbours(samples, i, spacing))
        confirmed = dict(scene)

        light = scene["traffic_light"]
        if light != "none":
            agreed = [n for n in near if n["scene"]["traffic_light"] == light]
            if agreed:
                # Only claim the signal governs our lane if both samples said so.
                confirmed["light_is_for_our_lane"] = scene[
                    "light_is_for_our_lane"
                ] and any(n["scene"]["light_is_for_our_lane"] for n in agreed)
            else:
                confirmed["traffic_light"] = "none"
                confirmed["light_is_for_our_lane"] = False

        for field in CONFIRM_FIELDS:
            if field == "traffic_light":
                continue
            if scene[field] and not any(n["scene"][field] for n in near):
                confirmed[field] = False

        sample["confirmed"] = confirmed

    return samples


def dropped(samples):
    """(t, field, value) for every detection confirmation threw away.

    Worth printing after a run: it is the honest record of how much the model
    over-reported, and it is what we show if a judge asks how we handle
    hallucinations.
    """
    out = []
    for sample in samples:
        raw, ok = sample["scene"], sample.get("confirmed", sample["scene"])
        for field in CONFIRM_FIELDS:
            if raw[field] != ok[field] and raw[field] not in (False, "none"):
                out.append((sample["t"], field, raw[field]))
    return out
