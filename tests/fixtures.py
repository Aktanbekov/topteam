"""Deterministic drives to test the rules against.

These are FIXTURES, not recordings. Nothing here has been near the vision model
and none of it may ever be shown as model output - the review page labels a
fixture drive as such, loudly, for exactly this reason. What they are good for
is pinning down behaviour that real footage cannot cover on demand: we have no
clip of a driver running a stop sign and we are not going to go and make one.

Three canonical drives, matching the three grades:

    PASS    stop sign, then 4.0s at a standstill    -> meets the 3s target
    BRIEF   stop sign, then 1.1s at a standstill    -> legal, coaching note
    FAIL    stop sign, never stops at all           -> possible incomplete stop

The BRIEF numbers are taken from IMG_9830, where the real stop measured 1.07s
between 63.97s and 65.03s.
"""

from scene_vision import EMPTY_SCENE

SPACING = 3.0
FPS = 30.0


def scene(**over):
    s = dict(EMPTY_SCENE)
    s.update(over)
    return s


def samples(marks, spacing=SPACING, start=0.0):
    """One sample per entry in `marks`, each a dict of scene overrides."""
    return [
        {
            "t": round(start + i * spacing, 2),
            "motion_score": 0.0,
            "stopped": None,
            "scene": scene(**mark),
            "error": None,
            "frame": None,
        }
        for i, mark in enumerate(marks)
    ]


def readings(duration, stopped_spans=(), fps=FPS):
    """Per-frame motion readings with the car stationary over `stopped_spans`.

    Scores are set either side of the 0.25 threshold using the bands measured
    on real footage - 0.18 for stopped, 0.9 for moving - rather than 0 and 100,
    so a test cannot pass on a margin the real signal does not have.
    """
    out = []
    n = int(round(duration * fps))
    for i in range(n + 1):
        t = round(i / fps, 3)
        stopped = any(a <= t <= b for a, b in stopped_spans)
        out.append((t, 0.18 if stopped else 0.9, stopped))
    return out


def frames(times, spacing=SPACING):
    return [
        {"t": round(t, 2), "path": f"frames/frame_{t:06.2f}s.jpg".replace(".", "_", 1)}
        for t in times
    ]


# --------------------------------------------------------------- the drives
# Sign visible in samples 3, 4 and 5 (9s, 12s, 15s), gone by 18s. The stop
# happens after the sign has left the frame, which is the whole point.
_SIGN_MARKS = [{}, {}, {}, {"stop_sign": True}, {"stop_sign": True},
               {"stop_sign": True}, {}, {}, {}, {}, {}]

PASS_STOP = (20.0, 24.0)    # 4.0s, at or above the 3s target
BRIEF_STOP = (20.0, 21.07)  # 1.07s, the IMG_9830 measurement
DURATION = 30.0


def drive(grade):
    """(samples, readings) for "pass", "brief" or "fail"."""
    spans = {
        "pass": (PASS_STOP,),
        "brief": (BRIEF_STOP,),
        "fail": (),
    }[grade]
    return samples(_SIGN_MARKS), readings(DURATION, spans)


SETTINGS = {
    "vision_interval_s": 3.0,
    "motion_threshold": 0.25,
    "min_stop_s": 0.4,
    "window_after_s": 10.0,
    "full_stop_s": 3.0,
    "min_sign_samples": 2,
    "sign_gap_tolerance_s": 6.0,
    "critical_hold_s": 4.0,
}

DRIVE_META = {
    "file": "fixture.mp4",
    "source": "fixture",
    "duration_s": DURATION,
    "fps": FPS,
    "width": 640,
    "height": 480,
}

PROCESSING = {
    "model": "fixture",
    "endpoint": None,
    "compute_requested": None,
    "compute_verified": None,
    "compute_note": "fixture - no inference was run",
    "analysis_s": 0.0,
    "real_time_ratio": None,
    "calls": 0,
    "failed": 0,
    "first_call_s": None,
    "median_s": None,
    "p95_s": None,
    "min_s": None,
    "max_s": None,
}


def review(grade, **over):
    """A full review dict for one of the three canonical drives."""
    from drive_review import build_review

    s, r = drive(grade)
    kwargs = dict(
        samples=s,
        readings=r,
        drive=dict(DRIVE_META),
        processing=dict(PROCESSING),
        settings=dict(SETTINGS),
        frames=frames([x["t"] for x in s]),
        motion_points=[],
        rejected=[],
        spacing=SPACING,
    )
    kwargs.update(over)
    return build_review(**kwargs)
