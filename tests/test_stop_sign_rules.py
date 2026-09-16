"""The stop-sign state machine: approaches, grades, and when a verdict is due.

These are the rules that decide whether the product accuses a driver of a
critical error, so they get the most attention of anything in the suite.
"""

import fixtures
import pytest
from drive_review import (
    build_level_track,
    find_stops,
    judge,
    level_spans,
    sign_approaches,
)


# ----------------------------------------------------------------- approaches
def test_single_sighting_is_not_an_approach():
    """One frame of stop sign is a misreading, not a junction.

    This is the guard that keeps a hallucinated sign from becoming a
    hallucinated critical error at a junction that was never there.
    """
    samples = fixtures.samples([{}, {"stop_sign": True}, {}, {}])
    assert sign_approaches(samples) == []


def test_two_sightings_make_an_approach_validated_at_the_second():
    samples = fixtures.samples([{}, {"stop_sign": True}, {"stop_sign": True}, {}])
    (approach,) = sign_approaches(samples)
    assert approach["first_seen"] == 3.0
    assert approach["last_seen"] == 6.0
    # Not 3.0: at 3.0s we had one sighting and no grounds to act.
    assert approach["validated_at"] == 6.0


def test_a_gap_does_not_end_an_approach():
    """A van or a tree can hide the sign for a sample and then it reappears."""
    samples = fixtures.samples(
        [{"stop_sign": True}, {}, {"stop_sign": True}, {}, {}, {}]
    )
    (approach,) = sign_approaches(samples)
    assert approach["first_seen"] == 0.0
    assert approach["last_seen"] == 6.0
    assert approach["sightings"] == [0.0, 6.0]


def test_two_junctions_in_one_drive_are_two_approaches():
    marks = [{"stop_sign": True}, {"stop_sign": True}] + [{}] * 6
    marks += [{"stop_sign": True}, {"stop_sign": True}] + [{}] * 3
    approaches = sign_approaches(fixtures.samples(marks))
    assert len(approaches) == 2
    assert approaches[0]["first_seen"] == 0.0
    assert approaches[1]["first_seen"] == 24.0


# --------------------------------------------------------------------- grades
@pytest.mark.parametrize(
    "grade,expected_len",
    [("pass", 4.0), ("brief", 1.07), ("fail", None)],
)
def test_the_three_grades(grade, expected_len):
    samples, readings = fixtures.drive(grade)
    stops = find_stops(readings, 0.4)
    (verdict,) = judge(sign_approaches(samples), stops, 10.0, 3.0)
    assert verdict["grade"] == grade
    if expected_len is None:
        assert verdict["stop_len"] is None
    else:
        assert verdict["stop_len"] == pytest.approx(expected_len, abs=0.12)


def test_a_stop_after_the_sign_left_the_frame_still_counts():
    """The single most important thing the real footage taught us.

    On IMG_9830 the sign was gone by 63s and the car stopped at 64-65s, so
    stop_sign and stopped never co-occur in one frame. A detector that wants
    both at once fails every correct stop.
    """
    samples, readings = fixtures.drive("pass")
    stops = find_stops(readings, 0.4)
    (verdict,) = judge(sign_approaches(samples), stops, 10.0, 3.0)
    assert verdict["last_seen"] == 15.0
    assert verdict["stop_at"] > verdict["last_seen"]
    assert verdict["grade"] == "pass"


def test_the_longest_stop_in_the_window_wins():
    """An approach often dips under the threshold before the real stop."""
    samples = fixtures.samples([{"stop_sign": True}, {"stop_sign": True}] + [{}] * 8)
    readings = fixtures.readings(30.0, ((8.0, 8.6), (12.0, 16.0)))
    (verdict,) = judge(sign_approaches(samples), find_stops(readings, 0.4), 10.0, 3.0)
    assert verdict["stop_at"] == pytest.approx(12.0, abs=0.1)
    assert verdict["grade"] == "pass"


def test_a_stop_after_the_deadline_does_not_rescue_the_approach():
    samples = fixtures.samples([{"stop_sign": True}, {"stop_sign": True}] + [{}] * 10)
    # Sign last seen at 3.0s, so the window closes at 13.0s.
    readings = fixtures.readings(40.0, ((20.0, 26.0),))
    (verdict,) = judge(sign_approaches(samples), find_stops(readings, 0.4), 10.0, 3.0)
    assert verdict["grade"] == "fail"
    assert verdict["decision_at"] == 13.0


# ------------------------------------------------------- when a verdict is due
def test_a_failure_is_not_decided_before_its_deadline():
    """The look-ahead bug, pinned down.

    The previous build returned level 3 from the moment the sign left the
    frame, which is ten seconds before there is anything to decide. A coach
    that announces the mistake before the driver has had a chance to make it is
    worse than no coach.
    """
    samples, readings = fixtures.drive("fail")
    stops = find_stops(readings, 0.4)
    verdicts = judge(sign_approaches(samples), stops, 10.0, 3.0)
    track = build_level_track(level_spans([], verdicts), 30.0)

    deadline = verdicts[0]["decision_at"]
    assert deadline == 25.0

    def level_at(t):
        current = 0
        for at, level, _kind in track:
            if at <= t:
                current = level
        return current

    for t in (0.0, 9.0, 15.0, 20.0, 24.9):
        assert level_at(t) < 3, f"critical signalled at {t}s, before the {deadline}s deadline"
    assert level_at(25.0) == 3
    assert level_at(28.9) == 3
    assert level_at(29.5) == 0  # released after the hold


def test_the_heads_up_starts_at_validation_not_at_first_sight():
    samples, readings = fixtures.drive("fail")
    verdicts = judge(sign_approaches(samples), find_stops(readings, 0.4), 10.0, 3.0)
    track = build_level_track(level_spans([], verdicts), 30.0)
    first_nonzero = next(row for row in track if row[1] > 0)
    # Sightings at 9, 12, 15 - validated by the second one.
    assert first_nonzero[0] == 12.0
    assert first_nonzero[2] == "stop_sign"


def test_an_unvalidated_sighting_never_reaches_the_hardware():
    """The second bug: confirmed["stop_sign"] is the raw reading, always.

    stop_sign is excluded from scene_filter confirmation on purpose, so
    anything reading that field directly is reading an unconfirmed claim. The
    only route to the board is through a validated approach.
    """
    samples = fixtures.samples([{}, {"stop_sign": True}, {}, {}, {}])
    readings = fixtures.readings(15.0)
    verdicts = judge(sign_approaches(samples), find_stops(readings, 0.4), 10.0, 3.0)
    assert verdicts == []
    track = build_level_track(level_spans([], verdicts), 15.0)
    assert {row[1] for row in track} == {0}
