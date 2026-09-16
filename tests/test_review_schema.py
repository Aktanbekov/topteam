"""The review structure everything downstream reads.

If the player, the report and the hardware are going to agree, they have to be
reading the same thing. These tests pin the shape of that thing, and pin the
promises the product makes about it: no official DMV verdict, no coaching note
counted as an error, no scored event without evidence.
"""

import json

import fixtures
import pytest
from drive_review import SCHEMA_VERSION, build_review

REQUIRED_TOP = {
    "schema_version",
    "drive",
    "processing",
    "privacy",
    "settings",
    "coverage",
    "motion",
    "observations",
    "approaches",
    "events",
    "levels",
    "outcome",
    "limitations",
}

REQUIRED_EVENT = {
    "id",
    "kind",
    "severity",
    "status",
    "title",
    "detail",
    "rule",
    "tip",
    "evidence_start",
    "evidence_end",
    "detected_at",
    "decision_at",
    "screenshot",
    "sources",
    "limitations",
    "hardware",
}


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_review_has_every_section(grade):
    review = fixtures.review(grade)
    assert review["schema_version"] == SCHEMA_VERSION
    assert REQUIRED_TOP <= set(review)


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_review_is_json_serialisable(grade):
    """It gets baked into an HTML page, so it has to survive json.dumps."""
    json.dumps(fixtures.review(grade))


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_every_event_has_the_full_shape(grade):
    for event in fixtures.review(grade)["events"]:
        missing = REQUIRED_EVENT - set(event)
        assert not missing, f"{event['kind']} is missing {missing}"


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_event_ids_are_unique(grade):
    ids = [e["id"] for e in fixtures.review(grade)["events"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_every_scored_event_carries_evidence(grade):
    """A finding without a timestamp and a picture is an assertion, not evidence."""
    for event in fixtures.review(grade)["events"]:
        if event["status"] not in ("scored", "coaching_note"):
            continue
        assert event["decision_at"] is not None
        assert event["screenshot"], f"{event['id']} has no evidence image"
        assert event["rule"], f"{event['id']} does not say which rule fired"
        assert event["limitations"], f"{event['id']} claims no limitations"


# --------------------------------------------------------------------- outcome
def test_a_brief_stop_is_a_coaching_note_and_never_a_strike():
    """California asks for a complete stop and puts no number on it.

    The three-second target is ours. Counting it as a DMV error would be
    inventing a rule and then failing someone against it.
    """
    outcome = fixtures.review("brief")["outcome"]
    assert outcome["coaching_notes"] == 1
    assert outcome["critical_findings"] == 0
    assert outcome["strikes"] == 0
    assert outcome["result"] == "clear_with_notes"


def test_a_missed_stop_is_reported_as_possible_and_not_as_a_verdict():
    review = fixtures.review("fail")
    assert review["outcome"]["critical_findings"] == 1
    assert review["outcome"]["strikes"] == 1
    (event,) = [e for e in review["events"] if e["status"] == "scored"]
    assert "possible" in event["title"].lower()


def test_a_clean_stop_scores_nothing():
    outcome = fixtures.review("pass")["outcome"]
    assert outcome["critical_findings"] == 0
    assert outcome["coaching_notes"] == 0
    assert outcome["clean_approaches"] == 1
    assert outcome["result"] == "clear"


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_no_official_dmv_claim_anywhere(grade):
    """The one thing we must never print.

    We are reading a forward camera with no speed feed. A DMV pass or fail from
    that would be the most dishonest sentence this project could produce.
    """
    text = json.dumps(fixtures.review(grade)).lower()
    for phrase in ("dmv pass", "dmv fail", "you passed", "you failed", "test result"):
        assert phrase not in text, f"found {phrase!r} in the review"
    assert "not a dmv result" in text


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_coverage_names_what_is_not_supported(grade):
    coverage = {c["key"]: c for c in fixtures.review(grade)["coverage"]}
    assert coverage["stop_sign_approach"]["status"] == "active"
    assert coverage["stop_sign_approach"]["scored"] is True
    assert coverage["red_light_ahead"]["status"] == "warning_only"
    assert coverage["red_light_ahead"]["scored"] is False
    assert coverage["following_distance"]["status"] == "experimental"
    assert coverage["head_check"]["status"] == "unavailable"


# ------------------------------------------------------------------ hardware
@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_the_level_track_starts_at_zero_and_is_ordered(grade):
    track = fixtures.review(grade)["levels"]
    assert track[0][0] == 0.0
    assert [row[0] for row in track] == sorted(row[0] for row in track)
    assert all(row[1] in (0, 1, 2, 3) for row in track)


def test_only_a_scored_failure_ever_reaches_level_three():
    for grade in ("pass", "brief"):
        levels = {row[1] for row in fixtures.review(grade)["levels"]}
        assert 3 not in levels
    assert 3 in {row[1] for row in fixtures.review("fail")["levels"]}


def test_an_experimental_check_never_drives_the_hardware():
    """Following distance is the model's opinion. Opinions do not buzz."""
    samples = fixtures.samples(
        [
            {"car_ahead_close": True},
            {"car_ahead_close": True},
            {"car_ahead_close": True},
            {},
        ]
    )
    review = build_review(
        samples=samples,
        readings=fixtures.readings(15.0),
        drive=dict(fixtures.DRIVE_META, duration_s=15.0),
        processing=dict(fixtures.PROCESSING),
        settings=dict(fixtures.SETTINGS),
        frames=fixtures.frames([s["t"] for s in samples]),
    )
    (event,) = [e for e in review["events"] if e["kind"] == "following_distance"]
    assert event["status"] == "experimental"
    assert event["hardware"] is None
    assert {row[1] for row in review["levels"]} == {0}


def test_a_queue_at_a_red_light_is_not_tailgating():
    """Stopped behind a car is not following too closely."""
    samples = fixtures.samples([{"car_ahead_close": True}] * 3 + [{}])
    review = build_review(
        samples=samples,
        readings=fixtures.readings(15.0, ((0.0, 9.0),)),
        drive=dict(fixtures.DRIVE_META, duration_s=15.0),
        processing=dict(fixtures.PROCESSING),
        settings=dict(fixtures.SETTINGS),
        frames=[],
    )
    assert not [e for e in review["events"] if e["kind"] == "following_distance"]


def test_a_red_light_is_a_warning_and_never_a_violation():
    samples = fixtures.samples(
        [{"traffic_light": "red", "light_is_for_our_lane": True}] * 3 + [{}]
    )
    review = build_review(
        samples=samples,
        readings=fixtures.readings(15.0),
        drive=dict(fixtures.DRIVE_META, duration_s=15.0),
        processing=dict(fixtures.PROCESSING),
        settings=dict(fixtures.SETTINGS),
        frames=[],
    )
    (event,) = [e for e in review["events"] if e["kind"] == "red_light_ahead"]
    assert event["status"] == "warning_only"
    assert event["hardware"]["level"] == 1
    assert review["outcome"]["critical_findings"] == 0
    assert "violation" not in event["title"].lower()


# ----------------------------------------------------------- degraded inputs
def test_a_drive_with_no_observations_still_produces_a_review():
    """An empty drive is a legitimate answer, not a crash."""
    review = build_review(
        samples=[],
        readings=[],
        drive=dict(fixtures.DRIVE_META, duration_s=0.0),
        processing=dict(fixtures.PROCESSING),
        settings=dict(fixtures.SETTINGS),
        frames=[],
    )
    assert review["events"] == []
    assert review["levels"] == [[0.0, 0, "clear"]]
    assert review["outcome"]["result"] == "clear"


def test_failed_model_calls_leave_a_gap_rather_than_a_detection():
    """A dropped GenieX call must cost a detection, never invent one."""
    samples = fixtures.samples([{}, {}, {}])
    for s in samples:
        s["error"] = "request failed: connection refused"
    review = build_review(
        samples=samples,
        readings=fixtures.readings(10.0),
        drive=dict(fixtures.DRIVE_META, duration_s=10.0),
        processing=dict(fixtures.PROCESSING, failed=3),
        settings=dict(fixtures.SETTINGS),
        frames=[],
    )
    assert review["events"] == []
    assert review["processing"]["failed"] == 3


def test_missing_frames_do_not_break_the_build():
    review = fixtures.review("fail", frames=[])
    (event,) = [e for e in review["events"] if e["status"] == "scored"]
    assert event["screenshot"] is None
