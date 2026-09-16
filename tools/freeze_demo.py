"""Freeze a real analysis run so the demo survives GenieX being down.

    python tools/freeze_demo.py --grade fail --timeline output/demo_rolling.json

The demo's primary path is a genuine local run: the synthetic clip goes through
Qwen3-VL on the NPU like any other footage, and that is what should be shown.
This exists for the one situation that would otherwise end the demonstration -
the server will not start, five minutes before we are on.

What gets frozen is only the VISION layer: what the model said, sample by
sample, on a run that really happened. The motion track is still recomputed
from the video every time, because that needs no model and takes two seconds.

The frozen file is marked `replayed`, and the review page says so in an amber
banner. Presenting a recorded run as a live one would be exactly the kind of
thing this whole project is built not to do.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))

GRADES = {"pass": "clip_full.mp4", "brief": "clip_stop.mp4", "fail": "clip_rolling.mp4"}


def freeze(timeline, grade):
    frozen = dict(timeline)
    frozen["drive"] = dict(frozen.get("drive", {}), source="synthetic")
    processing = dict(frozen.get("processing", {}))
    processing["replayed"] = True
    processing["replay_note"] = (
        "Scene labels replayed from a recorded local run of "
        f"{processing.get('model') or 'the vision model'}. GenieX was not "
        "contacted for this build. The motion track was recomputed from the "
        "video just now."
    )
    # The timings and the call count belonged to the recorded run, not to this
    # one. Keeping them would let the page report an inference latency - and a
    # number of NPU calls - for calls this build never made, directly next to a
    # banner saying GenieX was not contacted.
    for key in ("analysis_s", "real_time_ratio", "first_call_s", "median_s",
                "p95_s", "min_s", "max_s", "calls", "failed"):
        processing[key] = None
    frozen["processing"] = processing
    frozen["grade_expected"] = grade
    return frozen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grade", required=True, choices=sorted(GRADES))
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("demo"))
    args = parser.parse_args()

    if not args.timeline.is_file():
        sys.exit(f"no timeline at {args.timeline}")

    timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
    if isinstance(timeline, list):
        sys.exit(
            f"{args.timeline} is an old bare-list timeline with no metadata.\n"
            "Re-run laptop/analyze_video.py to produce one that records the model."
        )

    out = args.out_dir / f"scene_{args.grade}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(freeze(timeline, args.grade), indent=2), encoding="utf-8")

    samples = timeline.get("samples", [])
    signs = [s["t"] for s in samples if s["scene"]["stop_sign"]]
    print(f"froze {len(samples)} samples -> {out}")
    print(f"  clip: {GRADES[args.grade]}")
    print(f"  stop sign reported at: {', '.join(f'{t:.1f}s' for t in signs) or 'nowhere'}")


if __name__ == "__main__":
    main()
