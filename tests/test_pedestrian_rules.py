"""Yielding to a person in the crosswalk: when it is scored and when it is not.

The stop-sign rules ask "did the car stop somewhere in this window". This asks
the same question about a different window - the stretch where a person was
confirmed in the road - and it is the second scored check in the product, so it
gets the same scrutiny.

The shape is deliberately identical to the stop sign:

    confirmed across 2+ samples   or it is a hallucination, not a person
    a stop overlapping the run    -> yielded, no error
    no stop at all                -> possible failure to yield, critical
    decided when the run ends     -> never before, the driver may still stop

What it does NOT know, and must never imply: how far away they were, whether
they were in our lane, or who had right of way. A forward camera with no depth
gives presence and nothing else, so the wording is "possible" throughout.
"""

import json

import fixtures
import pytest
from drive_review import build_review


def drive(marks, stopped_spans=(), duration=30.0):
    """Build a review over a scripted drive."""
    samples = fixtures.samples(marks)
    readings = fixtures.readings(duration, stopped_spans)
    return build_review(
        samples=samples,
        readings=readings,
        drive=fixtures.DRIVE_META,
        processing=fixtures.PROCESSING,
        settings=fixtures.SETTINGS,
        motion_points=[],
        spacing=3.0,
    )


def events_of(review, kind):
    return [e for e in review["events"] if e["kind"] == kind]


WALKING = {"pedestrian_in_crosswalk": True}


# ------------------------------------------------------------------- scoring
def test_driving_through_a_confirmed_crossing_is_a_critical_error():
    """The rule the whole check exists for: do not drive at a person.

    Three consecutive samples with somebody in the road, and the car never
    stops. That is the one case where presence alone is enough to say something
    went wrong, because the correct action - stopping - would have shown up in
    the motion track and did not.
    """
    review = drive([{}, WALKING, WALKING, WALKING, {}, {}, {}, {}])
    (event,) = events_of(review, "pedestrian_crossing")

    assert event["severity"] == "critical"
    assert event["status"] == "scored"
    assert "possible" in event["title"].lower()
    assert event["hardware"]["level"] == 3


def test_stopping_for_them_is_not_an_error():
    """Yielding is the whole point. Doing it right must not be scored."""
    review = drive(
        [{}, WALKING, WALKING, WALKING, {}, {}, {}, {}],
        stopped_spans=((4.0, 10.0),),
    )
    scored = [e for e in review["events"] if e["status"] == "scored"]
    assert not scored, f"yielding was scored as an error: {scored}"

    (event,) = events_of(review, "pedestrian_crossing")
    assert event["severity"] == "info"
    assert event["hardware"]["level"] == 1


def test_a_single_sighting_never_reaches_the_hardware():
    """The pedestrian version of the unvalidated-sighting rule.

    The model reported a person on 5 empty frames out of 38 with the old
    prompt. One frame must never be able to produce a critical error, or that
    becomes 5 fabricated accusations on a clean drive.
    """
    review = drive([{}, WALKING, {}, {}, {}, {}])
    assert not events_of(review, "pedestrian_crossing")
    assert max(row[1] for row in review["levels"]) == 0


def test_a_stop_that_ended_before_they_stepped_out_does_not_count():
    """Stopping earlier in the drive is not yielding to this person."""
    review = drive(
        [{}, {}, {}, {}, WALKING, WALKING, {}, {}, {}, {}],
        stopped_spans=((0.0, 2.0),),
    )
    (event,) = events_of(review, "pedestrian_crossing")
    assert event["severity"] == "critical"


# ------------------------------------------------------- when it is decided
def test_the_failure_is_not_decided_before_the_crossing_ends():
    """Invariant 1, for this check.

    The driver can still stop while the person is mid-road, so nothing may be
    settled until the run is over. Deciding earlier would announce the mistake
    before it had finished being made.
    """
    review = drive([{}, WALKING, WALKING, WALKING, {}, {}, {}, {}])
    (event,) = events_of(review, "pedestrian_crossing")

    assert event["decision_at"] >= event["evidence_end"]
    assert event["detected_at"] < event["decision_at"]

    # And the hardware must not be told before that moment either.
    for t, level, _why in review["levels"]:
        if level == 3:
            assert t >= event["decision_at"]


def test_the_heads_up_comes_before_the_verdict():
    """A driver gets warned while it can still help, then scored afterwards."""
    review = drive([{}, WALKING, WALKING, WALKING, {}, {}, {}, {}])
    levels = {level: t for t, level, _ in review["levels"]}
    assert 1 in levels and 3 in levels
    assert levels[1] < levels[3], "the strip went critical with no warning first"


# ------------------------------------------------------------- what we claim
def test_it_never_claims_to_know_distance_or_right_of_way():
    """A forward camera gives presence. Anything more would be invented."""
    review = drive([{}, WALKING, WALKING, WALKING, {}, {}, {}, {}])
    (event,) = events_of(review, "pedestrian_crossing")

    notes = " ".join(event["limitations"]).lower()
    assert "right of way" in notes
    assert "distance" in notes

    text = json.dumps(event).lower()
    for claim in ("metres", "meters", "feet away", "right of way was"):
        assert claim not in text, f"{claim!r} implies a measurement we do not have"


def test_no_official_dmv_claim():
    review = drive([{}, WALKING, WALKING, WALKING, {}, {}, {}, {}])
    text = json.dumps(review).lower()
    for phrase in ("dmv pass", "dmv fail", "you passed", "you failed", "test result"):
        assert phrase not in text


def test_the_coverage_panel_lists_it():
    """A judge should not have to ask whether this check is real or aspirational."""
    review = drive([{}, WALKING, WALKING, {}, {}, {}])
    coverage = {c["key"]: c for c in review["coverage"]}
    assert "pedestrian_crossing" in coverage
    assert coverage["pedestrian_crossing"]["status"] == "active"
    assert coverage["pedestrian_crossing"]["scored"] is True


@pytest.mark.parametrize("stopped", [True, False])
def test_the_outcome_counts_it(stopped):
    """A scored failure has to show up in the headline, not just the timeline."""
    review = drive(
        [{}, WALKING, WALKING, WALKING, {}, {}, {}, {}],
        stopped_spans=((4.0, 10.0),) if stopped else (),
    )
    expected = 0 if stopped else 1
    assert review["outcome"]["critical_findings"] == expected


def test_a_stop_in_the_gap_after_the_last_sighting_still_counts_as_yielding():
    """We only look every 3s, so we do not know when they actually cleared.

    Found on IMG_9840, and it was a false accusation. A man walked across the
    lane; the model confirmed him at 3s and 6s and missed him at 9s, where he
    was at the edge of the frame half-hidden by parked cars. The driver braked
    and came to rest at 8.43s - and the video at 8.5s shows him still mid-stride
    directly in front of the car. The run had "ended" at 6s, the stop was
    outside it, and a driver who yielded correctly was told "possible failure to
    yield".

    The end of a crossing is uncertain by up to one sampling interval, so that
    is how much slack the stop gets. Not the stop sign's ten seconds - that
    window exists because a sign leaves the frame long before the line, which is
    a different thing entirely - just our own resolution, stated honestly.
    """
    review = drive(
        [{}, WALKING, WALKING, {}, {}, {}, {}],   # confirmed 3s-6s
        stopped_spans=((8.43, 10.1),),            # stopped 2.4s after, inside the gap
    )
    (event,) = events_of(review, "pedestrian_crossing")
    assert event["severity"] == "info", "a driver who stopped was called critical"
    assert event["hardware"]["level"] == 1


def test_a_stop_well_after_they_are_gone_is_not_yielding():
    """The slack is one interval, not an amnesty.

    Stopping fifteen seconds after somebody has crossed has nothing to do with
    them, and must not launder driving straight through.
    """
    review = drive(
        [{}, WALKING, WALKING, {}, {}, {}, {}, {}, {}, {}],
        stopped_spans=((21.0, 25.0),),
        duration=40.0,
    )
    (event,) = events_of(review, "pedestrian_crossing")
    assert event["severity"] == "critical"
