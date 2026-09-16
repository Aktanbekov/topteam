"""Score a scene prompt against hand-labelled frames.

The vision model is the least predictable part of the pipeline, so before
changing SCENE_PROMPT we measure the change instead of eyeballing one clip.

    python tools/eval_scene_prompt.py                 # score every variant
    python tools/eval_scene_prompt.py --variant strict
    python tools/eval_scene_prompt.py --repeat 2      # check run-to-run drift

Labels live in tools/labels/<video>.json and were written by looking at the
frames. A label of "any" means the frame is genuinely borderline - a car far
down the road, a person beside a crosswalk - and is not scored either way, so
the score only reflects clear-cut mistakes.

What matters most here is the FALSE ALARM column. A missed stop sign costs us
one detection; a phantom red light costs us a fabricated critical error in
front of the judges.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from scene_vision import STRICT_PROMPT, TERSE_PROMPT, SceneVision  # noqa: E402

FIELDS = ["stop_sign", "traffic_light", "pedestrian_in_crosswalk", "car_ahead_close"]

# ---------------------------------------------------------------- variants
# "terse" is the prompt we shipped first, kept so the table always shows a
# candidate against the thing it replaced.

# Same rules as STRICT_PROMPT, but the model must name what it sees before
# answering. Costs tokens and therefore time - the eval prints seconds per call
# so the trade-off is visible.
EVIDENCE_PROMPT = STRICT_PROMPT.replace(
    "Reply with JSON only, no explanation and no markdown:\n",
    "Reply with JSON only, no explanation and no markdown. Fill in evidence\n"
    "first: name the objects you actually see in the frame. Every field you\n"
    "answer true must correspond to something you named there.\n",
).replace('{"stop_sign"', '{"evidence": "<what you see, under 20 words>", "stop_sign"')

VARIANTS = {
    "terse": TERSE_PROMPT,
    "strict": STRICT_PROMPT,
    "evidence": EVIDENCE_PROMPT,
}


def load_labels(path):
    data = json.loads(Path(path).read_text())
    return data["video"], {int(k): v for k, v in data["frames"].items()}


def frame_files(frames_dir, labels):
    """Match output/frames/frame_045_03s.jpg to the label for t=45."""
    found = {}
    for f in sorted(Path(frames_dir).glob("*.jpg")):
        try:
            secs = int(f.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if secs in labels:
            found[secs] = f
    missing = sorted(set(labels) - set(found))
    if missing:
        print(f"  no frame on disk for: {missing}")
    return found


def compare(field, got, want):
    """Returns 'ok', 'false_alarm', 'miss', or None when the frame is not scored."""
    if want == "any":
        return None
    if field == "traffic_light":
        if got == want:
            return "ok"
        # Saying "red" where there is no light is the dangerous direction.
        return "miss" if want != "none" else "false_alarm"
    if got == want:
        return "ok"
    return "false_alarm" if got else "miss"


def run(vision, prompt, files, labels):
    tally = {f: {"ok": 0, "false_alarm": 0, "miss": 0} for f in FIELDS}
    wrong = []
    times = []

    for secs in sorted(files):
        rgb = np.asarray(Image.open(files[secs]).convert("RGB"))
        started = time.perf_counter()
        scene, error = vision.describe(rgb, prompt=prompt)
        times.append(time.perf_counter() - started)
        if error:
            print(f"    {secs:4d}s  [{error}]")
            continue

        for field in FIELDS:
            result = compare(field, scene[field], labels[secs][field])
            if result is None:
                continue
            tally[field][result] += 1
            if result != "ok":
                wrong.append((secs, field, scene[field], labels[secs][field], result))

    return tally, wrong, times


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", default="tools/labels/IMG_9830.json")
    parser.add_argument("--frames", default="output/frames")
    parser.add_argument("--variant", action="append", choices=sorted(VARIANTS))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    video, labels = load_labels(args.labels)
    files = frame_files(args.frames, labels)
    if not files:
        # output/ is gitignored, so a fresh clone has the labels but not the
        # frames they describe. One analysis pass writes them back.
        sys.exit(
            f"no labelled frames in {args.frames}\n"
            f"Generate them first:  ./run.sh {video}"
        )

    vision = SceneVision()
    if not vision.health():
        sys.exit("GenieX is not answering - start it with:  geniex serve")

    names = args.variant or sorted(VARIANTS)
    results = {}

    for name in names:
        for attempt in range(args.repeat):
            tag = name if args.repeat == 1 else f"{name} (run {attempt + 1})"
            print(f"\n=== {tag} - {len(files)} frames from {video}")
            tally, wrong, times = run(vision, VARIANTS[name], files, labels)

            header = f"  {'field':<24} {'right':>6} {'FALSE ALARM':>12} {'missed':>7}"
            print(header)
            for field in FIELDS:
                t = tally[field]
                print(
                    f"  {field:<24} {t['ok']:>6} {t['false_alarm']:>12} {t['miss']:>7}"
                )
            alarms = sum(t["false_alarm"] for t in tally.values())
            misses = sum(t["miss"] for t in tally.values())
            right = sum(t["ok"] for t in tally.values())
            print(f"  {'TOTAL':<24} {right:>6} {alarms:>12} {misses:>7}")
            print(f"  {sum(times) / len(times):.2f}s per call")

            if wrong:
                print("  mistakes:")
                for secs, field, got, want, kind in wrong:
                    label = "FALSE ALARM" if kind == "false_alarm" else "missed"
                    print(
                        f"    {secs:4d}s  {field:<24} said {got!r:<8} "
                        f"actually {want!r:<8} {label}"
                    )

            results[tag] = {
                "tally": tally,
                "false_alarms": alarms,
                "misses": misses,
                "right": right,
                "seconds_per_call": round(sum(times) / len(times), 2),
                "wrong": wrong,
            }

    if len(results) > 1:
        print(f"\n=== summary  ({len(files)} frames)")
        print(
            f"  {'variant':<22} {'right':>6} {'FALSE ALARM':>12} "
            f"{'missed':>7} {'s/call':>7}"
        )
        for tag, r in results.items():
            print(
                f"  {tag:<22} {r['right']:>6} {r['false_alarms']:>12} "
                f"{r['misses']:>7} {r['seconds_per_call']:>7.2f}"
            )

    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2, default=str))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
