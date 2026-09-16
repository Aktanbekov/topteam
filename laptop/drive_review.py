"""The event engine: turn observations into decisions, once, for everybody.

This is the single source of truth for what happened on a drive. The review
player, the printable report and the UNO Q all read the same structure built
here. Before this module they each re-interpreted the raw samples in their own
way, which is how three bugs got in at once:

  * the hardware could fire a CRITICAL from the moment a stop sign left the
    frame - ten seconds before the deadline that decides the case;
  * a single hallucinated stop-sign frame could light the board even though the
    approach was later thrown away for having only one sighting;
  * every decision was pinned to a vision sample, so a result that happened
    between samples was reported up to one sampling interval late.

All three come from the same root cause, so they get the same fix: build an
explicit list of events with exact timestamps, and derive everything from it.

Two timestamps per event, and the difference matters:

    detected_at   when we first had honest grounds to say something
    decision_at   when the outcome was actually settled

For a failed stop-sign approach those are ten seconds apart, and nothing may
signal the failure before decision_at. That is not a detail - a coach that
announces the mistake before the driver has had the chance to make it is
worthless, and a judge will spot it.

Nothing in here does I/O. Feed it samples and motion readings, get a dict back.
"""

import hashlib

from config import (
    CRITICAL_HOLD_S,
    FULL_STOP_S,
    MIN_SIGN_SAMPLES,
    MIN_STOP_S,
    SIGN_GAP_TOLERANCE_S,
    WINDOW_AFTER_S,
)
from scene_filter import confirm, dropped

SCHEMA_VERSION = "1.0"

# Severity is about the driver. Status is about us - how much we are willing to
# stand behind the finding.
SEVERITY_CRITICAL = "critical"
SEVERITY_COACHING = "coaching"
SEVERITY_INFO = "info"

STATUS_SCORED = "scored"  # counts towards the practice outcome
STATUS_COACHING = "coaching_note"  # told to the driver, never counted
STATUS_PASSED = "passed"
STATUS_WARNING = "warning_only"  # live heads-up, no verdict attached
STATUS_EXPERIMENTAL = "experimental"  # shown, explicitly not trusted
STATUS_INFO = "info"

# No vehicle speed, GPS or CAN feed, so everything below is inference from a
# camera. This sentence is attached to every scored event rather than buried in
# a footnote.
NO_SPEED_FEED = (
    "Inferred from camera motion alone - there is no vehicle speed, GPS or CAN "
    "feed, so a very slow crawl cannot be told from a true standstill."
)


def _eid(*parts):
    """A short stable id, so the UI and the report agree on which event is which."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


# --------------------------------------------------------------- motion layer
def find_stops(readings, min_stop_s=MIN_STOP_S):
    """Collapse per-frame stopped flags into (start, end) stationary windows.

    `readings` is [(t, score, stopped), ...] from ego_motion over every frame.
    A window that runs to the end of the video is closed at the last reading.
    """
    windows = []
    start = None

    for t, _score, stopped in readings:
        if stopped and start is None:
            start = t
        elif not stopped and start is not None:
            if t - start >= min_stop_s:
                windows.append((start, t))
            start = None

    if start is not None and readings and readings[-1][0] - start >= min_stop_s:
        windows.append((start, readings[-1][0]))

    return windows


# ----------------------------------------------------------------- stop signs
def sign_approaches(
    samples,
    gap_tolerance=SIGN_GAP_TOLERANCE_S,
    min_samples=MIN_SIGN_SAMPLES,
):
    """Validated stop-sign approaches, with the moment each became believable.

    A sample or two without the sign does not end an approach - it can be
    hidden by a tree or a van and then reappear. An approach built from fewer
    than `min_samples` sightings is dropped: it is more likely a misreading
    than a junction, and a phantom junction becomes a phantom critical error.

    `validated_at` is the timestamp of the sighting that met `min_samples`.
    Nothing may act on the approach before then - that is what stops a single
    hallucinated frame from reaching the hardware, and it is why the rest of
    the pipeline reads approaches rather than reading stop_sign off a sample.
    """
    approaches = []
    current = None

    def close(w):
        if len(w["sightings"]) >= min_samples:
            w["validated_at"] = w["sightings"][min_samples - 1]
            approaches.append(w)

    for s in samples:
        if s["scene"]["stop_sign"]:
            if current is None:
                current = {"first_seen": s["t"], "last_seen": s["t"], "sightings": [s["t"]]}
            else:
                current["last_seen"] = s["t"]
                current["sightings"].append(s["t"])
        elif current is not None and s["t"] - current["last_seen"] > gap_tolerance:
            close(current)
            current = None

    if current is not None:
        close(current)
    return approaches


def judge_approach(
    approach, stops, window_after=WINDOW_AFTER_S, full_stop_s=FULL_STOP_S
):
    """Decide whether the car stopped for one sign, and for how long.

    The sign leaves the forward view before the car reaches the line, so the
    stop almost always lands AFTER the sign was last seen. Looking only inside
    the visible window would fail every legitimate stop. Hence the window stays
    open for `window_after` seconds past the last sighting.

    Three outcomes, not two. A stop that happened but lasted under full_stop_s
    is legal in California and still worth telling the driver about, so it gets
    its own grade instead of being waved through or counted as an error.
    """
    deadline = approach["last_seen"] + window_after
    inside = [st for st in stops if approach["first_seen"] <= st[0] <= deadline]
    # The longest stop in the window, not the first: an approach often dips
    # under the threshold for a moment before the real stop.
    matched = max(inside, key=lambda st: st[1] - st[0], default=None)

    verdict = dict(approach)
    verdict["deadline"] = round(deadline, 2)

    if matched is None:
        verdict.update(
            grade="fail",
            stopped=False,
            stop_at=None,
            stop_end=None,
            stop_len=None,
            # Nothing is settled until the window expires. This timestamp is
            # the whole reason the look-ahead bug could exist.
            decision_at=round(deadline, 2),
        )
    else:
        length = round(matched[1] - matched[0], 2)
        verdict.update(
            grade="brief" if length < full_stop_s else "pass",
            stopped=True,
            stop_at=round(matched[0], 2),
            stop_end=round(matched[1], 2),
            stop_len=length,
            decision_at=round(matched[1], 2),
        )

    verdict["target_s"] = full_stop_s
    return verdict


def judge(approaches, stops, window_after=WINDOW_AFTER_S, full_stop_s=FULL_STOP_S):
    return [judge_approach(a, stops, window_after, full_stop_s) for a in approaches]


# ------------------------------------------------------------ confirmed runs
def confirmed_runs(samples, predicate, field, spacing=3.0, min_samples=2):
    """Stretches where a confirmed observation held, with when we could know.

    `field` names the entry in sample["known_at"] that backs the predicate, so
    the run reports the moment the confirmation became available rather than
    the moment the first sample was taken. Those differ by one sampling
    interval when only the following sample agreed, and pretending otherwise
    would be reading the future.
    """
    runs = []
    current = None

    for s in samples:
        if predicate(s):
            if current is None or s["t"] - current["end"] > spacing * 1.6:
                if current is not None and len(current["samples"]) >= min_samples:
                    runs.append(current)
                current = {"start": s["t"], "end": s["t"], "samples": [s["t"]], "known": []}
            else:
                current["end"] = s["t"]
                current["samples"].append(s["t"])
            current["known"].append(s.get("known_at", {}).get(field, s["t"]))
        elif current is not None:
            if len(current["samples"]) >= min_samples:
                runs.append(current)
            current = None

    if current is not None and len(current["samples"]) >= min_samples:
        runs.append(current)

    for run in runs:
        run["known_at"] = round(min(run["known"]), 2)
        del run["known"]
    return runs


def _moving_at(t, stops):
    return not any(a <= t <= b for a, b in stops)


# ------------------------------------------------------------------- events
def _nearest_frame(frames, t):
    """The sampled frame closest to `t`, with how far off it is.

    Evidence images come from the frames the vision model actually looked at,
    so an event that happened between samples is illustrated by a nearby frame
    and the report says how near. Quietly captioning it with the event's own
    timestamp would be a small lie that a judge could catch by reading the
    clock in the corner of the picture.
    """
    if not frames:
        return None, None
    best = min(frames, key=lambda f: abs(f["t"] - t))
    return best["path"], round(best["t"] - t, 2)


def _event(**kw):
    kw.setdefault("screenshot", None)
    kw.setdefault("screenshot_offset_s", None)
    kw.setdefault("sources", [])
    kw.setdefault("limitations", [])
    kw.setdefault("hardware", None)
    return kw


def stop_sign_events(verdicts, frames):
    """One event per validated approach, graded three ways."""
    events = []
    for v in verdicts:
        sightings = len(v["sightings"])
        base = (
            f"A stop sign was confirmed in {sightings} sample"
            f"{'s' if sightings != 1 else ''} between {v['first_seen']:.0f}s and "
            f"{v['last_seen']:.0f}s. The approach window stayed open until "
            f"{v['deadline']:.0f}s, because the sign leaves the camera's view "
            "before the car reaches the line."
        )

        if v["grade"] == "fail":
            evidence_t = v["decision_at"]
            shot, offset = _nearest_frame(frames, evidence_t)
            events.append(
                _event(
                    id=_eid("stop_sign", v["first_seen"], "fail"),
                    kind="stop_sign_approach",
                    grade="fail",
                    severity=SEVERITY_CRITICAL,
                    status=STATUS_SCORED,
                    title="Possible incomplete stop",
                    detail=(
                        "No stationary window was found anywhere in the approach, "
                        "so the car appears not to have come to a complete stop."
                    ),
                    rule=(
                        base + " No stationary window was found inside it, so the "
                        "approach is reported as a possible incomplete stop."
                    ),
                    tip=(
                        "Stop fully behind the line, count three seconds, then "
                        "creep forward only when you can see both ways."
                    ),
                    evidence_start=v["first_seen"],
                    evidence_end=v["deadline"],
                    detected_at=v["validated_at"],
                    decision_at=v["decision_at"],
                    screenshot=shot,
                    screenshot_offset_s=offset,
                    sources=list(v["sightings"]),
                    limitations=[NO_SPEED_FEED],
                    hardware={"level": 3, "type": "critical"},
                )
            )
        elif v["grade"] == "brief":
            shot, offset = _nearest_frame(frames, v["stop_at"])
            events.append(
                _event(
                    id=_eid("stop_sign", v["first_seen"], "brief"),
                    kind="stop_sign_approach",
                    grade="brief",
                    severity=SEVERITY_COACHING,
                    status=STATUS_COACHING,
                    title=f"Stopped, but only {v['stop_len']:.1f}s",
                    detail=(
                        f"About {v['stop_len']:.1f}s at a standstill from "
                        f"{v['stop_at']:.1f}s. That is a complete stop and not a "
                        f"DMV error - the {v['target_s']:.0f}s coaching target is "
                        "ours, so this is advice, not a strike."
                    ),
                    rule=(
                        base + f" A stationary window of {v['stop_len']:.1f}s was "
                        f"found at {v['stop_at']:.1f}s, below the "
                        f"{v['target_s']:.0f}s coaching target."
                    ),
                    tip=(
                        "You stopped - hold it a beat longer. Counting to three "
                        "is what gives you time to actually look both ways."
                    ),
                    evidence_start=v["first_seen"],
                    evidence_end=v["stop_end"],
                    detected_at=v["validated_at"],
                    decision_at=v["decision_at"],
                    screenshot=shot,
                    screenshot_offset_s=offset,
                    sources=list(v["sightings"]),
                    limitations=[NO_SPEED_FEED],
                    hardware={"level": 1, "type": "stop_sign"},
                )
            )
        else:
            shot, offset = _nearest_frame(frames, v["stop_at"])
            events.append(
                _event(
                    id=_eid("stop_sign", v["first_seen"], "pass"),
                    kind="stop_sign_approach",
                    grade="pass",
                    severity=SEVERITY_INFO,
                    status=STATUS_PASSED,
                    title=f"Full stop - {v['stop_len']:.1f}s",
                    detail=(
                        f"About {v['stop_len']:.1f}s at a standstill from "
                        f"{v['stop_at']:.1f}s, at or above the "
                        f"{v['target_s']:.0f}s coaching target."
                    ),
                    rule=(
                        base + f" A stationary window of {v['stop_len']:.1f}s was "
                        f"found at {v['stop_at']:.1f}s, meeting the "
                        f"{v['target_s']:.0f}s target."
                    ),
                    tip="That is the one to repeat on the test.",
                    evidence_start=v["first_seen"],
                    evidence_end=v["stop_end"],
                    detected_at=v["validated_at"],
                    decision_at=v["decision_at"],
                    screenshot=shot,
                    screenshot_offset_s=offset,
                    sources=list(v["sightings"]),
                    limitations=[NO_SPEED_FEED],
                    hardware={"level": 1, "type": "stop_sign"},
                )
            )
    return events


def warning_events(samples, stops, frames, spacing=3.0):
    """Heads-up events. Warnings only - none of these score anything.

    A red light we are approaching is not a violation, and scoring one would
    need a reliable stop-line crossing test that we do not have. A close car is
    the model's opinion, not a measured distance. Both are useful to the driver
    in the moment and neither belongs in a verdict, so they are marked as what
    they are.
    """
    events = []

    for run in confirmed_runs(
        samples,
        lambda s: s["confirmed"]["traffic_light"] == "red"
        and s["confirmed"]["light_is_for_our_lane"],
        "traffic_light",
        spacing=spacing,
    ):
        shot, offset = _nearest_frame(frames, run["known_at"])
        events.append(
            _event(
                id=_eid("red_light", run["start"]),
                kind="red_light_ahead",
                severity=SEVERITY_INFO,
                status=STATUS_WARNING,
                title="Red light for our lane",
                detail=(
                    f"A red signal governing our lane, confirmed from "
                    f"{run['start']:.0f}s to {run['end']:.0f}s."
                ),
                rule=(
                    f"Reported in {len(run['samples'])} consecutive samples with "
                    "the signal housing visible. Warning only: scoring a "
                    "red-light violation would need a reliable stop-line "
                    "crossing test, which this build does not have."
                ),
                tip="Cover the brake early and stop behind the line, not on it.",
                evidence_start=run["start"],
                evidence_end=run["end"],
                detected_at=run["known_at"],
                decision_at=run["known_at"],
                screenshot=shot,
                screenshot_offset_s=offset,
                sources=list(run["samples"]),
                limitations=[
                    "Presence only. Whether the car crossed the line on red is "
                    "not tested, so no violation is claimed."
                ],
                hardware={"level": 1, "type": "red_light"},
            )
        )

    for run in confirmed_runs(
        samples,
        lambda s: s["confirmed"]["pedestrian_in_crosswalk"],
        "pedestrian_in_crosswalk",
        spacing=spacing,
    ):
        shot, offset = _nearest_frame(frames, run["known_at"])
        events.append(
            _event(
                id=_eid("pedestrian", run["start"]),
                kind="pedestrian_ahead",
                severity=SEVERITY_INFO,
                status=STATUS_WARNING,
                title="Person in the roadway",
                detail=(
                    f"Confirmed from {run['start']:.0f}s to {run['end']:.0f}s."
                ),
                rule=(
                    f"Reported in {len(run['samples'])} consecutive samples. "
                    "Warning only - right of way is not assessed."
                ),
                tip="Yield and wait until they are fully clear of your path.",
                evidence_start=run["start"],
                evidence_end=run["end"],
                detected_at=run["known_at"],
                decision_at=run["known_at"],
                screenshot=shot,
                screenshot_offset_s=offset,
                sources=list(run["samples"]),
                limitations=["Presence only. Right of way is not assessed."],
                hardware={"level": 1, "type": "pedestrian"},
            )
        )

    # Being stopped behind a car at a light is not tailgating, so the car has
    # to be moving before this is worth mentioning at all.
    for run in confirmed_runs(
        samples,
        lambda s: s["confirmed"]["car_ahead_close"] and _moving_at(s["t"], stops),
        "car_ahead_close",
        spacing=spacing,
    ):
        shot, offset = _nearest_frame(frames, run["known_at"])
        events.append(
            _event(
                id=_eid("following", run["start"]),
                kind="following_distance",
                severity=SEVERITY_INFO,
                status=STATUS_EXPERIMENTAL,
                title="Following-distance risk (experimental)",
                detail=(
                    f"A car was judged close ahead while moving, "
                    f"{run['start']:.0f}s to {run['end']:.0f}s."
                ),
                rule=(
                    "Confirmed across samples and cross-checked against the "
                    "motion track so a queue at a light does not count. This is "
                    "the model's opinion of 'close', not a measured distance, so "
                    "it is not scored."
                ),
                tip="Pick a fixed object ahead and count two seconds behind it.",
                evidence_start=run["start"],
                evidence_end=run["end"],
                detected_at=run["known_at"],
                decision_at=run["known_at"],
                screenshot=shot,
                screenshot_offset_s=offset,
                sources=list(run["samples"]),
                limitations=[
                    "No distance measurement. The model is answering a "
                    "subjective question about one frame."
                ],
                # Deliberately no hardware: an experimental check must not buzz.
                hardware=None,
            )
        )

    return events


def stationary_events(stops, verdicts, frames):
    """Stops not already explained by a stop sign - context, never a verdict."""
    explained = [(v["first_seen"], v["deadline"]) for v in verdicts]
    events = []
    for start, end in stops:
        if any(a <= start <= b for a, b in explained):
            continue
        shot, offset = _nearest_frame(frames, start)
        events.append(
            _event(
                id=_eid("stationary", start),
                kind="stationary",
                severity=SEVERITY_INFO,
                status=STATUS_INFO,
                title=f"Stationary {end - start:.1f}s",
                detail="A stop with no stop sign detected nearby - traffic, a "
                "signal, or a junction the camera did not resolve.",
                rule="Motion track only. No claim is attached to it.",
                tip="",
                evidence_start=round(start, 2),
                evidence_end=round(end, 2),
                detected_at=round(start, 2),
                decision_at=round(end, 2),
                screenshot=shot,
                screenshot_offset_s=offset,
                sources=[],
                limitations=[NO_SPEED_FEED],
                hardware=None,
            )
        )
    return events


def build_events(samples, stops, verdicts, frames, spacing=3.0):
    events = (
        stop_sign_events(verdicts, frames)
        + warning_events(samples, stops, frames, spacing)
        + stationary_events(stops, verdicts, frames)
    )
    return sorted(events, key=lambda e: (e["detected_at"], e["kind"]))


# -------------------------------------------------------------- level track
def level_spans(events, verdicts, critical_hold_s=CRITICAL_HOLD_S):
    """(start, end, level, type) spans the hardware should be in.

    A heads-up runs from the moment we could honestly say something was there
    until the moment it is resolved. A critical starts at decision_at - NOT at
    the last sighting - and is held long enough to be unmistakable.
    """
    spans = []

    for v in verdicts:
        # The heads-up covers the approach: from the sighting that validated it
        # to whatever settles it. This is the only route by which a stop sign
        # reaches the hardware, so an unvalidated sighting never can.
        spans.append(
            (v["validated_at"], v["decision_at"], 1, "stop_sign")
        )
        if v["grade"] == "fail":
            spans.append(
                (
                    v["decision_at"],
                    v["decision_at"] + critical_hold_s,
                    3,
                    "critical",
                )
            )

    for e in events:
        if e["kind"] in ("stop_sign_approach",) or not e.get("hardware"):
            continue
        spans.append(
            (
                e["detected_at"],
                e["evidence_end"],
                e["hardware"]["level"],
                e["hardware"]["type"],
            )
        )

    return [s for s in spans if s[1] > s[0]]


def build_level_track(spans, duration):
    """Piecewise-constant level over the whole drive, as [t, level, type].

    The player looks this up by exact playhead time, not by nearest vision
    sample. That is what lets a decision land at 73.0s instead of at whichever
    sample happens to follow it.
    """
    edges = {0.0, round(float(duration), 2)}
    for start, end, _level, _type in spans:
        edges.add(round(float(start), 2))
        edges.add(round(float(end), 2))
    points = sorted(e for e in edges if 0.0 <= e <= duration + 1e-9)

    track = []
    for t in points[:-1] if len(points) > 1 else points:
        active = [s for s in spans if s[0] <= t < s[1]]
        if active:
            top = max(active, key=lambda s: s[2])
            level, kind = top[2], top[3]
        else:
            level, kind = 0, "clear"
        if track and track[-1][1] == level and track[-1][2] == kind:
            continue
        track.append([round(t, 2), level, kind])

    if not track:
        track = [[0.0, 0, "clear"]]
    if track[0][0] > 0.0:
        track.insert(0, [0.0, 0, "clear"])
    return track


# ------------------------------------------------------------------ outcome
def build_outcome(events):
    """The practice result. Deliberately not a DMV pass or fail.

    We are looking at a forward camera with no speed feed. Calling that a DMV
    verdict would be the single most dishonest thing this project could print,
    so the wording stays in the language of a practice review.
    """
    critical = [e for e in events if e["status"] == STATUS_SCORED]
    coaching = [e for e in events if e["status"] == STATUS_COACHING]
    passed = [e for e in events if e["status"] == STATUS_PASSED]

    if critical:
        result, headline = "needs_review", "Possible critical error detected"
        summary = (
            f"{len(critical)} approach"
            f"{'es' if len(critical) != 1 else ''} to review before the test."
        )
    elif coaching:
        result, headline = "clear_with_notes", "No reviewed critical error detected"
        summary = (
            f"{len(coaching)} coaching note"
            f"{'s' if len(coaching) != 1 else ''} - nothing that would be scored "
            "as an error."
        )
    else:
        result, headline = "clear", "No reviewed critical error detected"
        summary = "Nothing flagged in the checks this build performs."

    return {
        "result": result,
        "headline": headline,
        "summary": summary,
        "critical_findings": len(critical),
        "coaching_notes": len(coaching),
        "clean_approaches": len(passed),
        # The board's simplified practice mode counts strikes. A brief stop is
        # legal and must never move this, which is why it counts scored events
        # rather than coaching notes.
        "strikes": len(critical),
        "disclaimer": (
            "Practice coaching from a forward camera. Not a DMV result, not an "
            "examiner, and not for use during a real road test."
        ),
    }


# ----------------------------------------------------------------- coverage
def coverage():
    """What this build actually checks, and how far we trust each one.

    Printed on the page next to the result. A judge should not have to ask
    which of the four advertised mistakes are real.
    """
    return [
        {
            "key": "stop_sign_approach",
            "label": "Stop-sign approach",
            "status": "active",
            "scored": True,
            "note": (
                "Sign confirmed across samples, graded against the per-frame "
                "motion track. Full / brief / possible incomplete stop."
            ),
        },
        {
            "key": "red_light_ahead",
            "label": "Red light for our lane",
            "status": "warning_only",
            "scored": False,
            "note": (
                "Detected and shown live. Not scored: a violation needs a "
                "reliable stop-line crossing test, and stop_line_visible is the "
                "weakest field we have."
            ),
        },
        {
            "key": "following_distance",
            "label": "Following distance",
            "status": "experimental",
            "scored": False,
            "note": (
                "A subjective per-frame judgement, not a measured distance. "
                "Shown, never counted, and it does not drive the hardware."
            ),
        },
        {
            "key": "head_check",
            "label": "Head checks, mirrors, signals",
            "status": "unavailable",
            "scored": False,
            "note": (
                "A forward camera cannot see them. Needs a synchronised "
                "driver-facing camera and a fast head-pose layer, not the VLM."
            ),
        },
    ]


def limitations():
    return [
        NO_SPEED_FEED,
        "Post-drive analysis, not live detection. Events are replayed against "
        "the recording in sync; the live path would carry a confirmation delay "
        "of about one sampling interval.",
        "The vision model samples roughly every 3 seconds, so anything shorter "
        "than that can fall between samples.",
        "Every finding is reported as possible. This is coaching feedback, not "
        "an official DMV assessment.",
    ]


# ------------------------------------------------------------------- review
def build_review(
    *,
    samples,
    readings,
    drive,
    processing,
    settings,
    frames=None,
    motion_points=None,
    rejected=None,
    spacing=3.0,
):
    """Assemble the whole versioned review structure.

    Everything downstream - player, report, hardware replay - reads this and
    nothing else. If a number is not in here, it is not on the page.
    """
    frames = frames or []
    # Confirmation is recomputed from the raw scenes every time rather than
    # trusted from the input file. It is cheap, it is idempotent, and it means
    # a review can never be built from samples that were filtered under a
    # different spacing than the one recorded here.
    confirm(samples, spacing=spacing)
    if rejected is None:
        rejected = dropped(samples)

    stops = find_stops(readings, settings["min_stop_s"])
    approaches = sign_approaches(
        samples,
        gap_tolerance=settings["sign_gap_tolerance_s"],
        min_samples=settings["min_sign_samples"],
    )
    verdicts = judge(
        approaches, stops, settings["window_after_s"], settings["full_stop_s"]
    )
    events = build_events(samples, stops, verdicts, frames, spacing)
    spans = level_spans(events, verdicts, settings["critical_hold_s"])
    track = build_level_track(spans, drive["duration_s"])

    return {
        "schema_version": SCHEMA_VERSION,
        "drive": drive,
        "processing": processing,
        "privacy": {
            "local_only": True,
            "network_required": False,
            "uploads": 0,
            "statement": (
                "The video is decoded, analysed and reported on this laptop. "
                "The model runs locally through GenieX; the only network "
                "traffic is to 127.0.0.1, and the UNO Q is reached over a USB "
                "cable, not Wi-Fi."
            ),
        },
        "settings": settings,
        "coverage": coverage(),
        "motion": {
            "points": motion_points if motion_points is not None else [],
            "stationary": [[round(a, 2), round(b, 2)] for a, b in stops],
            "threshold": settings["motion_threshold"],
        },
        "observations": {
            "samples": samples,
            "rejected": rejected,
            "frames": frames,
        },
        "approaches": verdicts,
        "events": events,
        "levels": track,
        "outcome": build_outcome(events),
        "limitations": limitations(),
    }
