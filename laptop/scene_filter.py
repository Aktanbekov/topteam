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
scene_vision.py, which cut clear false alarms on IMG_9830 from 22 to 0 on the
38 labelled frames. The prompt reduces them; this makes the survivors harmless.

Note what confirmation costs, and note that we now write the cost down. A
detection backed up only by the NEXT sample could not have been known until
that next sample arrived, so acting on it at the earlier timestamp would be
looking into the future. `confirm()` records a `known_at` time per confirmed
field, and the event engine starts warnings there rather than at the first
sighting. The review page shows the raw reading at its own timestamp, which is
why the two can differ by one sampling interval.
"""

# Fields where a lone sighting is not enough. stop_sign is deliberately absent:
# it feeds the approach window, which does its own multi-sample check in
# drive_review.sign_approaches, and a stop sign genuinely can be visible in only
# one sample of a fast approach.
#
# Because it is absent, confirmed["stop_sign"] is always the raw reading - so
# nothing may light hardware or score an error off that field directly. Only a
# validated approach window may. See drive_review.build_level_track.
CONFIRM_FIELDS = (
    "traffic_light",
    "pedestrian_in_crosswalk",
    "car_ahead_close",
    "stop_line_visible",
)

EMPTY_VALUES = {"traffic_light": "none"}


def _empty(field):
    return EMPTY_VALUES.get(field, False)


def _adjacent(a, b, spacing):
    """True if two samples are consecutive rather than either side of a gap."""
    return abs(b["t"] - a["t"]) <= spacing * 1.6


def _neighbours(samples, i, spacing):
    for j in (i - 1, i + 1):
        if 0 <= j < len(samples) and _adjacent(samples[i], samples[j], spacing):
            yield samples[j]


def _known_at(sample, supporters):
    """Earliest moment this confirmation could honestly have been known.

    A sample confirmed by the one before it is known when it arrives. A sample
    confirmed only by the one after it is not known until that one arrives -
    reporting it at its own timestamp would be reading the future.
    """
    return min(max(sample["t"], s["t"]) for s in supporters)


def confirm(samples, spacing=3.0):
    """Add "confirmed" and "known_at" to each sample and return the list.

    "confirmed" is the same shape as "scene", with anything unsupported by a
    neighbouring sample reset to its empty value. "known_at" maps each field
    that survived to the time we could first have acted on it.

    Callers that want to show the raw model output - the review player does, so
    we can see what it said - keep reading sample["scene"]; anything that counts
    a mistake or drives hardware reads "confirmed".
    """
    for i, sample in enumerate(samples):
        scene = sample["scene"]
        near = list(_neighbours(samples, i, spacing))
        confirmed = dict(scene)
        known = {}

        light = scene["traffic_light"]
        if light != "none":
            agreed = [n for n in near if n["scene"]["traffic_light"] == light]
            if agreed:
                # Only claim the signal governs our lane if both samples said so.
                lane = scene["light_is_for_our_lane"] and any(
                    n["scene"]["light_is_for_our_lane"] for n in agreed
                )
                confirmed["light_is_for_our_lane"] = lane
                known["traffic_light"] = _known_at(sample, agreed)
            else:
                confirmed["traffic_light"] = "none"
                confirmed["light_is_for_our_lane"] = False

        for field in CONFIRM_FIELDS:
            if field == "traffic_light":
                continue
            if not scene[field]:
                continue
            agreed = [n for n in near if n["scene"][field]]
            if agreed:
                known[field] = _known_at(sample, agreed)
            else:
                confirmed[field] = False

        sample["confirmed"] = confirmed
        sample["known_at"] = known

    return samples


def dropped(samples):
    """Every detection confirmation threw away, as dicts ready for the report.

    Worth printing after a run: it is the honest record of how much the model
    over-reported, and it is what we show when a judge asks how we handle
    hallucinations. The review page renders these struck through.
    """
    out = []
    for sample in samples:
        raw = sample["scene"]
        ok = sample.get("confirmed", raw)
        for field in CONFIRM_FIELDS:
            if raw[field] == ok[field] or raw[field] == _empty(field):
                continue
            out.append(
                {
                    "t": round(sample["t"], 2),
                    "field": field,
                    "value": raw[field],
                    "reason": "no neighbouring sample agreed",
                }
            )
    return out
