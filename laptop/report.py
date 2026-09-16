"""Render the post-drive report from a review, deterministically.

The report is built from review.json and nothing else. No model is consulted
about what happened, what it means, or what the result was - those are settled
before this file is ever called. That is not caution for its own sake: a text
model asked to summarise findings will cheerfully round "possible incomplete
stop" up to "you ran a stop sign", and once it has done that on stage there is
no taking it back.

An optional local text model may rewrite the wording (see narrate.py). It is
handed the finished facts and its output is treated as decoration: if it is
missing, unreachable or says something unusable, the deterministic report is
already complete and simply renders without it.
"""

import html
import json
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "report" / "report_template.html"


def bake(obj):
    """JSON safe to paste inside a <script> block.

    A drive filename comes from the user, and "</script>" in one would end the
    block early and drop the rest of the page into the document as markup.
    json.dumps does not escape that, so we do.
    """
    return (
        json.dumps(obj)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def escape(text):
    """For the few places a value is written into markup rather than via JS."""
    return html.escape(str(text), quote=True)


def summarise_drive(review):
    """A couple of plain sentences about the drive, built from the numbers.

    This is what the optional text model replaces when it is available. Writing
    it deterministically first means the report is never waiting on a model,
    and means there is always something to compare the model's version against.
    """
    drive = review["drive"]
    outcome = review["outcome"]
    approaches = review["approaches"]
    minutes, seconds = divmod(int(drive["duration_s"]), 60)
    length = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

    parts = [f"A {length} practice drive was reviewed on this laptop."]

    if approaches:
        graded = {"pass": 0, "brief": 0, "fail": 0}
        for a in approaches:
            graded[a["grade"]] += 1
        bits = []
        if graded["pass"]:
            bits.append(f"{graded['pass']} met the full-stop target")
        if graded["brief"]:
            bits.append(f"{graded['brief']} stopped briefly")
        if graded["fail"]:
            bits.append(f"{graded['fail']} showed no stop at all")
        parts.append(
            f"{len(approaches)} stop-sign approach"
            f"{'es were' if len(approaches) != 1 else ' was'} found: "
            + ", ".join(bits)
            + "."
        )
    else:
        parts.append("No stop-sign approach was confirmed in the footage.")

    stationary = review["motion"]["stationary"]
    if stationary:
        parts.append(
            f"The motion layer found {len(stationary)} stationary window"
            f"{'s' if len(stationary) != 1 else ''} across the drive."
        )

    rejected = review["observations"]["rejected"]
    if rejected:
        parts.append(
            f"{len(rejected)} observation"
            f"{'s' if len(rejected) != 1 else ''} the model reported only once "
            "did not survive cross-checking and were not acted on."
        )

    parts.append(outcome["summary"])
    return " ".join(parts)


def write_report(review, path, narrative=None):
    """Write report.html next to the player. Returns the path written.

    `narrative` is optional prose from a local text model. It may only replace
    wording - the counts, grades and timestamps rendered on the page come from
    the review either way.
    """
    path = Path(path)
    payload = dict(review)
    payload["report"] = {
        "generated_at": datetime.now(timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M"),
        "summary": summarise_drive(review),
        "narrative": narrative,
    }

    html_text = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__REVIEW_DATA__*/null", bake(payload)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")
    return path
