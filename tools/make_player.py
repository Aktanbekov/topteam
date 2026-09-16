"""Turn an analysed drive into the review player, the report, and review.json.

    python tools/make_player.py --video IMG_9830.MOV --timeline output/timeline.json

Three files come out, and the first one is the important one:

    output/review.json    every fact, every decision, every caveat
    output/player.html    the video, synchronised with those decisions
    output/report.html    the same decisions, printable

The player and the report do not reason about the drive. They render
review.json and nothing else, which is the whole reason this build exists: when
the page, the printout and the LED strip disagreed about what happened, it was
because each of them was interpreting the raw samples for itself.

The decision rules live in laptop/drive_review.py and are unit tested without
any of this - no video, no model, no board.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))

from config import (  # noqa: E402
    COMPUTE_UNITS,
    FULL_STOP_S,
    MOTION_THRESHOLD,
    VISION_INTERVAL_S,
    WINDOW_AFTER_S,
    Settings,
    compute_note,
)
from drive_review import build_review  # noqa: E402
from report import bake, write_report  # noqa: E402
from video_prep import dense_motion, ensure_playable, grab_frames, thin  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "report" / "player_template.html"


def load_timeline(path):
    """Read a timeline file, accepting both the current and the older shape.

    analyze_video.py used to write a bare list of samples. It now writes a dict
    with the drive and processing metadata alongside them, which is what lets
    the review page report real measured latencies instead of quoting a number
    from the brief. An old file still loads - it just has less to say.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {"samples": raw, "legacy": True}
    raw.setdefault("samples", [])
    raw["legacy"] = False
    return raw


def evidence_for_events(events, video, out_dir, rotate):
    """Replace nearest-sample screenshots with exact stills where we can.

    An event that happened between vision samples was illustrated by whichever
    sampled frame was closest, up to a sampling interval away. Decoding the
    exact moment is cheap - we are allowed to seek here, unlike the motion
    layer - and an evidence picture of the actual decision is worth far more
    than one from a second and a half earlier.

    If the grab fails for any reason the nearest-sample fallback stays, so the
    report never loses its illustration over a decode hiccup.
    """
    wanted = []
    for event in events:
        at = event.get("decision_at")
        if at is not None:
            wanted.append(at)

    grabbed = grab_frames(video, wanted, out_dir / "evidence", rotate=rotate)
    for event in events:
        at = event.get("decision_at")
        path = grabbed.get(round(float(at), 2)) if at is not None else None
        if path:
            event["screenshot"] = path
            event["screenshot_offset_s"] = 0.0
            event["screenshot_exact"] = True
        else:
            event["screenshot_exact"] = False
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("output/player.html"))
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD)
    parser.add_argument(
        "--window-after",
        type=float,
        default=WINDOW_AFTER_S,
        help="seconds after the sign leaves the frame to keep looking for the "
        f"stop (default: {WINDOW_AFTER_S:g})",
    )
    parser.add_argument(
        "--full-stop",
        type=float,
        default=FULL_STOP_S,
        help="seconds a stop should last before it counts as a good one "
        f"(default: {FULL_STOP_S:g}, the 'count to three' rule)",
    )
    parser.add_argument("--rotate", type=int, default=None, choices=[0, 90, 180, 270])
    parser.add_argument("--compute", default=None, choices=list(COMPUTE_UNITS))
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="skip the second decode pass that grabs exact evidence stills",
    )
    args = parser.parse_args()

    if not args.timeline.is_file():
        sys.exit(
            f"no timeline at {args.timeline}\n"
            "Generate one first:\n"
            "  python laptop/analyze_video.py <video> --json-out output/timeline.json"
        )

    timeline = load_timeline(args.timeline)
    samples = timeline["samples"]
    if not samples:
        sys.exit(f"{args.timeline} contains no samples - was the analysis cut short?")

    out_dir = args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    print("recomputing dense motion track...")
    readings, meta = dense_motion(args.video, args.threshold, args.rotate)

    spacing = timeline.get("settings", {}).get("vision_interval_s") or VISION_INTERVAL_S
    settings = Settings(
        vision_interval_s=spacing,
        motion_threshold=args.threshold,
        window_after_s=args.window_after,
        full_stop_s=args.full_stop,
    ).as_dict()

    drive = timeline.get("drive") or {}
    drive = {
        "file": drive.get("file", args.video.name),
        "source": drive.get("source", "real"),
        **meta,
    }

    processing = timeline.get("processing") or {
        "model": None,
        "endpoint": None,
        "compute_requested": None,
        "compute_verified": None,
        "compute_note": "unknown - this timeline predates the metadata",
        "analysis_s": None,
        "real_time_ratio": None,
        "calls": len(samples),
        "failed": sum(1 for s in samples if s.get("error")),
        "first_call_s": None,
    "median_s": None,
        "p95_s": None,
        "min_s": None,
        "max_s": None,
    }
    if args.compute:
        processing["compute_requested"] = args.compute
        processing["compute_note"] = compute_note(args.compute)

    playable = ensure_playable(args.video, out_dir)

    review = build_review(
        samples=samples,
        readings=readings,
        drive=drive,
        processing=processing,
        settings=settings,
        frames=timeline.get("frames") or [],
        motion_points=thin(readings, meta["fps"]),
        rejected=None,
        spacing=spacing,
    )
    review["drive"]["playback"] = Path(
        os.path.relpath(playable.resolve(), args.out.resolve().parent)
    ).as_posix()

    if not args.no_evidence:
        print("grabbing evidence stills...")
        evidence_for_events(review["events"], args.video, out_dir, args.rotate)

    review_path = out_dir / "review.json"
    review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")

    # bake() rather than json.dumps: the drive filename comes from the user and
    # a "</script>" inside one would end the block early.
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DRIVE_DATA__*/null", bake(review)
    )
    args.out.write_text(html, encoding="utf-8")

    report_path = write_report(review, out_dir / "report.html")

    # ------------------------------------------------------------- summary
    outcome = review["outcome"]
    print(f"\nwrote {review_path}")
    print(f"      {args.out}")
    print(f"      {report_path}")
    print(
        f"\n  {len(review['motion']['points'])} motion points, "
        f"{len(samples)} vision samples, "
        f"{len(review['motion']['stationary'])} stationary window(s)"
    )

    marks = {"pass": "FULL ", "brief": "SHORT", "fail": "FLAG "}
    for v in review["approaches"]:
        detail = f" ({v['stop_len']:.1f}s of {v['target_s']:.0f}s)" if v["stopped"] else ""
        print(
            f"  [{marks[v['grade']]}] sign {v['first_seen']:.0f}-{v['last_seen']:.0f}s"
            f", decided at {v['decision_at']:.0f}s{detail}"
        )

    print(f"\n  {outcome['headline']} - {outcome['summary']}")
    print(
        f"  critical {outcome['critical_findings']}, "
        f"coaching {outcome['coaching_notes']}, "
        f"clean {outcome['clean_approaches']}"
    )

    rejected = review["observations"]["rejected"]
    if rejected:
        print(
            f"\n  {len(rejected)} unconfirmed detection(s) dropped - "
            "no neighbouring sample agreed:"
        )
        for r in rejected:
            print(f"    {r['t']:6.1f}s  {r['field']} = {r['value']!r}")


if __name__ == "__main__":
    main()
