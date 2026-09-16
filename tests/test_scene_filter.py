"""Confirmation across samples, and the JSON repair that feeds it."""

import fixtures
from scene_filter import confirm, dropped
from scene_vision import EMPTY_SCENE, extract_json, tidy


# ---------------------------------------------------------------- model reply
def test_extract_json_survives_prose_and_fences():
    assert extract_json('```json\n{"stop_sign": true}\n```') == {"stop_sign": True}
    assert extract_json('Sure! {"stop_sign": false} hope that helps') == {
        "stop_sign": False
    }
    assert extract_json("no json here") is None
    assert extract_json('{"broken": ') is None


def test_tidy_repairs_a_lane_claim_with_no_signal():
    """The model returned this three times in one pass over IMG_9830.

    A lane-relevant signal with no signal is not a fact we can act on, so the
    field goes back to false rather than being passed downstream.
    """
    fixed = tidy({"traffic_light": "none", "light_is_for_our_lane": True})
    assert fixed["light_is_for_our_lane"] is False

    fixed = tidy({"traffic_light": "purple", "light_is_for_our_lane": True})
    assert fixed["traffic_light"] == "none"
    assert fixed["light_is_for_our_lane"] is False


def test_tidy_leaves_a_real_signal_alone():
    fixed = tidy({"traffic_light": "red", "light_is_for_our_lane": True})
    assert fixed["traffic_light"] == "red"
    assert fixed["light_is_for_our_lane"] is True


# --------------------------------------------------------------- confirmation
def test_a_lone_claim_is_rejected():
    samples = fixtures.samples([{}, {"traffic_light": "red"}, {}])
    confirm(samples, spacing=3.0)
    assert samples[1]["confirmed"]["traffic_light"] == "none"
    assert samples[1]["scene"]["traffic_light"] == "red"  # raw is untouched


def test_two_neighbours_confirm():
    samples = fixtures.samples(
        [{}, {"traffic_light": "red"}, {"traffic_light": "red"}, {}]
    )
    confirm(samples, spacing=3.0)
    assert samples[1]["confirmed"]["traffic_light"] == "red"
    assert samples[2]["confirmed"]["traffic_light"] == "red"


def test_disagreeing_colours_do_not_confirm_each_other():
    samples = fixtures.samples(
        [{}, {"traffic_light": "red"}, {"traffic_light": "green"}, {}]
    )
    confirm(samples, spacing=3.0)
    assert samples[1]["confirmed"]["traffic_light"] == "none"
    assert samples[2]["confirmed"]["traffic_light"] == "none"


def test_lane_relevance_needs_both_samples_to_agree():
    samples = fixtures.samples(
        [
            {"traffic_light": "red", "light_is_for_our_lane": True},
            {"traffic_light": "red", "light_is_for_our_lane": False},
        ]
    )
    confirm(samples, spacing=3.0)
    assert samples[0]["confirmed"]["traffic_light"] == "red"
    assert samples[0]["confirmed"]["light_is_for_our_lane"] is False


def test_a_gap_in_time_means_they_are_not_neighbours():
    """Samples either side of a long gap cannot corroborate each other."""
    samples = fixtures.samples([{"traffic_light": "red"}])
    samples += fixtures.samples([{"traffic_light": "red"}], start=60.0)
    confirm(samples, spacing=3.0)
    assert all(s["confirmed"]["traffic_light"] == "none" for s in samples)


def test_known_at_does_not_look_into_the_future():
    """A claim backed up only by the NEXT sample is not known until it lands.

    Warning at the earlier timestamp would be reading the future. The one
    sampling interval of latency is real and we write it down rather than
    quietly pretending it away.
    """
    samples = fixtures.samples(
        [{}, {"traffic_light": "red"}, {"traffic_light": "red"}, {}]
    )
    confirm(samples, spacing=3.0)
    # Sample at 3.0s was only corroborated by the one at 6.0s.
    assert samples[1]["known_at"]["traffic_light"] == 6.0
    # Sample at 6.0s had the one before it, so it is known on arrival.
    assert samples[2]["known_at"]["traffic_light"] == 6.0


def test_stop_sign_is_deliberately_not_confirmed_here():
    """It is validated by the approach window instead - see drive_review.

    If this ever starts passing the other way, every consumer that reads
    confirmed["stop_sign"] needs revisiting.
    """
    samples = fixtures.samples([{}, {"stop_sign": True}, {}])
    confirm(samples, spacing=3.0)
    assert samples[1]["confirmed"]["stop_sign"] is True


def test_dropped_reports_what_was_thrown_away():
    samples = fixtures.samples(
        [{}, {"traffic_light": "red", "car_ahead_close": True}, {}]
    )
    confirm(samples, spacing=3.0)
    out = dropped(samples)
    fields = {r["field"] for r in out}
    assert fields == {"traffic_light", "car_ahead_close"}
    assert all(r["reason"] for r in out)
    assert all(r["t"] == 3.0 for r in out)


def test_a_clean_drive_drops_nothing():
    samples = fixtures.samples([{}, {}, {}])
    confirm(samples, spacing=3.0)
    assert dropped(samples) == []


def test_every_scene_field_is_covered_by_the_empty_scene():
    """Guards against a new field being added to the prompt and nowhere else."""
    samples = fixtures.samples([{}])
    assert set(samples[0]["scene"]) == set(EMPTY_SCENE)
