"""Optional: have a local text model rewrite the finished report in plain prose.

    python laptop/narrate.py output/review.json

The report is complete without this. The deterministic summary in report.py is
written from the numbers and always renders; what a model can add is a couple
of sentences that sound like a person rather than a spreadsheet.

Two rules, and they are not negotiable:

  1. The model is handed the FINISHED facts. It never sees raw samples, never
     decides a grade, and never counts anything. A text model asked to
     summarise findings will happily round "possible incomplete stop" up to
     "you ran a stop sign", and once that has been said on stage there is no
     unsaying it.
  2. Anything it returns is checked before it is used. If it invents a number
     that is not in the review, or uses verdict language we do not claim, the
     narrative is dropped and the deterministic summary stands.

qualcomm/Qwen3-4B is not pulled on this laptop yet. That is fine, and it is
the reason nothing depends on this: the function returns None and the report
is unchanged.

    geniex pull qualcomm/Qwen3-4B
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests

from config import GENIEX_URL, REPORT_MODEL

TIMEOUT_S = 120

# Words that assert a verdict we are not entitled to assert. If the model
# reaches for any of them, we throw the whole narrative away rather than try to
# edit it - a rewrite that needs correcting is not one to trust.
FORBIDDEN = re.compile(
    r"\b(dmv\s+(pass|fail)|you\s+(passed|failed)|violation|illegal|ticket|"
    r"ran\s+(a|the)\s+(stop|red)|broke\s+the\s+law|officially)\b",
    re.IGNORECASE,
)

SYSTEM = (
    "You write two or three plain sentences for a learner driver, from facts "
    "you are given. You are a practice coach, not an examiner.\n"
    "Rules:\n"
    "- Use only the facts given. Do not add numbers, times or events.\n"
    "- Every finding is POSSIBLE. Never say the driver committed a violation, "
    "broke a law, passed or failed anything.\n"
    "- A short complete stop is legal. Call it something to work on, never an "
    "error.\n"
    "- Be warm and brief. No headings, no bullet points, no preamble."
)


def facts(review):
    """The only thing the model is allowed to see."""
    outcome = review["outcome"]
    lines = [
        f"Drive length: {review['drive']['duration_s']} seconds.",
        f"Practice outcome: {outcome['headline']}.",
        f"Critical findings: {outcome['critical_findings']}.",
        f"Coaching notes: {outcome['coaching_notes']}.",
        f"Clean stop-sign approaches: {outcome['clean_approaches']}.",
    ]
    for event in review["events"]:
        if event["status"] in ("scored", "coaching_note", "passed"):
            lines.append(
                f"- At {event['decision_at']:.0f}s: {event['title']}. "
                f"{event['detail']} Advice: {event['tip']}"
            )
    rejected = review["observations"]["rejected"]
    if rejected:
        lines.append(
            f"{len(rejected)} single-frame model claims were rejected for lack "
            "of corroboration and did not affect the result."
        )
    return "\n".join(lines)


def check(text, review):
    """Reject a narrative that oversteps. Returns the text, or None."""
    if not text or len(text) < 20:
        return None
    if FORBIDDEN.search(text):
        return None
    if len(text) > 900:
        return None

    # Any number the model states must be one we gave it. This catches the
    # common failure where a summary invents a plausible-looking duration.
    known = set(re.findall(r"\d+(?:\.\d+)?", facts(review)))
    known |= {"1", "2", "3"}  # counts and the three-second rule
    for number in re.findall(r"\d+(?:\.\d+)?", text):
        if number not in known:
            return None
    return text.strip()


def narrate(review, base_url=GENIEX_URL, model=REPORT_MODEL):
    """Prose for the report, or None. Never raises.

    None is a perfectly good answer: the caller already has a complete report.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": facts(review)},
        ],
        "max_tokens": 220,
        "temperature": 0.3,
    }
    try:
        resp = requests.post(
            f"{base_url}/chat/completions", json=payload, timeout=TIMEOUT_S
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None

    # Qwen3 can emit a reasoning block before the answer. Keep what follows it.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return check(text, review)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path, nargs="?", default=Path("output/review.json"))
    parser.add_argument("--model", default=REPORT_MODEL)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rebuild report.html next to the review with the narrative in it",
    )
    args = parser.parse_args()

    if not args.review.is_file():
        sys.exit(f"no review at {args.review} - run ./run.sh first")

    review = json.loads(args.review.read_text(encoding="utf-8"))
    text = narrate(review, model=args.model)

    if text is None:
        print(f"no narrative from {args.model}.")
        print("  Either the model is not pulled, or its answer failed the checks.")
        print(f"  Pull it with:  geniex pull {args.model}")
        print("  The report is already complete without it.")
        return

    print(text)
    if args.write:
        from report import write_report

        out = write_report(review, args.review.parent / "report.html", narrative=text)
        print(f"\nrewrote {out}")


if __name__ == "__main__":
    main()
