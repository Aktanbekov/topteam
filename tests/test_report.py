"""The report: safe embedding, deterministic prose, and the narrative guard."""

import json

import fixtures
import narrate
import pytest
from report import bake, summarise_drive, write_report


# ------------------------------------------------------------------ embedding
def test_bake_cannot_close_the_script_block():
    """A drive filename comes from the user and lands inside <script>.

    json.dumps does not escape "</script>", so without this a file named
    `</script><img onerror=...>.mp4` would break out of the block and put its
    contents into the document as markup.
    """
    baked = bake({"file": "evil</script><img src=x onerror=alert(1)>.mp4"})
    assert "</script>" not in baked
    assert "<\\/script>" in baked
    assert json.loads(baked)["file"].endswith(".mp4")


def test_bake_escapes_the_line_separators_that_break_javascript():
    assert " " not in bake({"x": "a b"})
    assert " " not in bake({"x": "a b"})


def test_bake_round_trips():
    review = fixtures.review("fail")
    assert json.loads(bake(review))["outcome"] == review["outcome"]


# -------------------------------------------------------------------- writing
@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_report_writes_and_contains_the_outcome(tmp_path, grade):
    review = fixtures.review(grade)
    path = write_report(review, tmp_path / "report.html")
    html = path.read_text(encoding="utf-8")
    assert "/*__REVIEW_DATA__*/null" not in html  # the data was substituted
    assert review["outcome"]["headline"] in html


def test_report_works_without_a_narrative(tmp_path):
    """The optional text model is not pulled on this laptop. That must be fine."""
    review = fixtures.review("brief")
    html = write_report(review, tmp_path / "report.html").read_text(encoding="utf-8")
    assert '"narrative": null' in html
    # The deterministic summary is embedded, so the page has prose either way.
    assert json.dumps(summarise_drive(review))[1:-1] in html


def test_the_deterministic_summary_mentions_the_grades():
    assert "stopped briefly" in summarise_drive(fixtures.review("brief"))
    assert "no stop at all" in summarise_drive(fixtures.review("fail"))
    assert "met the full-stop target" in summarise_drive(fixtures.review("pass"))


def test_the_summary_survives_an_empty_drive():
    from drive_review import build_review

    review = build_review(
        samples=[],
        readings=[],
        drive=dict(fixtures.DRIVE_META, duration_s=0.0),
        processing=dict(fixtures.PROCESSING),
        settings=dict(fixtures.SETTINGS),
        frames=[],
    )
    assert "No stop-sign approach was confirmed" in summarise_drive(review)


# ------------------------------------------------------------------ narrative
def test_a_narrative_that_claims_a_violation_is_rejected():
    """The failure mode this guard exists for.

    Asked to summarise "possible incomplete stop", a text model will reach for
    "you ran a stop sign". That sentence must never reach the page.
    """
    review = fixtures.review("fail")
    assert narrate.check("You ran a stop sign at 25 seconds.", review) is None
    assert narrate.check("This was a violation of the law.", review) is None
    assert narrate.check("You failed this practice drive.", review) is None


def test_a_narrative_that_invents_a_number_is_rejected():
    review = fixtures.review("brief")
    assert narrate.check(
        "You held the stop for 7.4 seconds, which is plenty.", review
    ) is None


def test_a_good_narrative_is_kept():
    review = fixtures.review("brief")
    text = (
        "You spotted the sign and came to a genuine stop, which is the hard "
        "part. Next time hold it a beat longer so you have time to look both "
        "ways properly."
    )
    assert narrate.check(text, review) == text


def test_an_empty_or_tiny_narrative_is_rejected():
    review = fixtures.review("pass")
    assert narrate.check("", review) is None
    assert narrate.check(None, review) is None
    assert narrate.check("Nice.", review) is None


def test_the_model_is_only_shown_settled_facts():
    """It never sees raw samples, so it cannot re-decide anything."""
    text = narrate.facts(fixtures.review("fail"))
    assert "Practice outcome" in text
    assert "motion_score" not in text
    assert "traffic_light" not in text
    assert "stop_sign" not in text


def test_narrate_returns_none_when_the_model_is_unreachable(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise narrate.requests.ConnectionError("no such model")

    monkeypatch.setattr(narrate.requests, "post", refuse)
    assert narrate.narrate(fixtures.review("pass")) is None
