"""Measure the vision call on whatever compute unit GenieX is currently serving.

The same fixed frames, the same prompt, one compute unit per run - because the
server picks its unit at startup and cannot be told to change mid-flight.

    geniex serve -c npu                      (terminal 1)
    python tools/benchmark_compute.py --label npu

    geniex serve -c cpu                      (restart it)
    python tools/benchmark_compute.py --label cpu

    python tools/benchmark_compute.py --compare

Each run writes output/benchmark/<label>.json. --compare prints every result
side by side. Nothing is hard-coded: if you have not run a comparison, the
table has one row and says so, rather than quoting a speedup from a slide.

**On this laptop there is no CPU number to get, and that is the finding.**
Starting the server with -c cpu makes the plugin say, in its own log:

    qairt plugin only supports NPU inference; ignoring device='cpu' and
    running on NPU

So the run goes to the NPU anyway and the timings come out identical. Publishing
that as "NPU is 1.0x faster than CPU" would be nonsense. This tool reads the
GenieX log, notices the line, records it, and refuses to print a speedup.

That refusal is worth more than the benchmark would have been. The
OpenAI-compatible API never says which unit served a request - but a plugin that
refuses to run anywhere except the NPU, and says so, is the closest thing to
proof we have that inference is on the NPU.

The label is what YOU started the server with; `plugin_evidence` in the output
is what the server said about it.
"""

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))

from config import COMPUTE_UNITS, GENIEX_URL, VISION_MODEL  # noqa: E402
from scene_vision import SceneVision  # noqa: E402
from video_source import VideoSource  # noqa: E402

OUT_DIR = Path("output/benchmark")
LOG_GLOB = "geniex*.log"

# The QAIRT plugin announces when it overrides the requested device. This is the
# only place in the whole stack that says out loud where inference ran.
QAIRT_OVERRIDE = re.compile(
    r"only supports (\w+) inference; ignoring device='(\w+)'", re.IGNORECASE
)


def plugin_evidence(log_dir=Path("output")):
    """What the server said about where it ran, from its own log.

    Returns None when there is nothing to report - an absent log or a run that
    never asked for anything the plugin had to override. Absence of evidence is
    reported as absence, not as confirmation.
    """
    logs = sorted(log_dir.glob(LOG_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = QAIRT_OVERRIDE.search(text)
        if match:
            runs_on, ignored = match.group(1).upper(), match.group(2)
            return {
                "source": str(log),
                "runs_on": runs_on,
                "ignored_device": ignored,
                "quote": match.group(0),
                "means": (
                    f"The plugin refused device='{ignored}' and ran on {runs_on} "
                    "anyway, so this is not a measurement of that device."
                ),
            }
    return None


def pick_frames(video, count):
    """`count` frames spread evenly through the clip, decoded once."""
    with VideoSource(video) as source:
        duration = source.duration_s or 1.0
        wanted = [duration * (i + 0.5) / count for i in range(count)]
        frames, next_i = [], 0
        for frame in source.frames():
            if next_i >= len(wanted):
                break
            if frame.t >= wanted[next_i]:
                frames.append(frame.rgb)
                next_i += 1
    return frames


def run(video, count, label):
    frames = pick_frames(video, count)
    if not frames:
        sys.exit(f"no frames decoded from {video}")

    vision = SceneVision()
    if not vision.health():
        sys.exit(
            f"GenieX is not answering at {GENIEX_URL}\n"
            f"Start it with:  geniex serve -c {label}"
        )

    print(f"{len(frames)} frames from {video}")
    print(f"  model   {VISION_MODEL}")
    print(f"  label   {label} (what the server was asked for - not verified)\n")

    evidence = plugin_evidence()
    latencies, failures = [], 0
    for i, rgb in enumerate(frames, 1):
        started = time.perf_counter()
        _scene, error = vision.describe(rgb)
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        if error:
            failures += 1
        note = " COLD (includes model load)" if i == 1 else ""
        flag = "  FAILED" if error else ""
        print(f"  frame {i}/{len(frames)}  {elapsed:6.2f}s{note}{flag}")

    warm = latencies[1:]
    result = {
        "label": label,
        "model": VISION_MODEL,
        "endpoint": GENIEX_URL,
        "video": str(video),
        "frames": len(frames),
        "failed": failures,
        "first_call_s": round(latencies[0], 2),
        "warm_median_s": round(statistics.median(warm), 2) if warm else None,
        "warm_min_s": round(min(warm), 2) if warm else None,
        "warm_max_s": round(max(warm), 2) if warm else None,
        "verified": False,
        "plugin_evidence": evidence,
        "measures_requested_device": evidence is None,
        "note": (
            f"Server started with -c {label}. The OpenAI-compatible API does not "
            "report which compute unit served a request, so this records what "
            "was requested."
        ),
        "measured_at": time.strftime("%Y-%m-%d %H:%M"),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{label}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n  first call {result['first_call_s']}s (model load)")
    if result["warm_median_s"]:
        print(f"  warm median {result['warm_median_s']}s over {len(warm)} calls")
    if failures:
        print(f"  {failures} call(s) came back unparseable and were still timed")
    if evidence:
        print(f"\n  NOTE, from {evidence['source']}:")
        print(f"    \"{evidence['quote']}\"")
        print(f"    {evidence['means']}")
    print(f"  wrote {path}")
    return result


def compare():
    results = []
    for path in sorted(OUT_DIR.glob("*.json")):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            print(f"  could not read {path}")

    if not results:
        sys.exit(
            f"no benchmark results in {OUT_DIR}.\n"
            "Run one first:  python tools/benchmark_compute.py --label npu"
        )

    print(f"{'requested':<12}{'warm median':>13}{'first call':>13}{'frames':>9}")
    print("-" * 47)
    for r in results:
        warm = f"{r['warm_median_s']}s" if r["warm_median_s"] else "-"
        print(
            f"{r['label']:<12}{warm:>13}{r['first_call_s']:>12.2f}s"
            f"{r['frames']:>9}"
        )

    overridden = [r for r in results if r.get("plugin_evidence")]
    if overridden:
        ev = overridden[0]["plugin_evidence"]
        print("\n  These rows are NOT a comparison of different compute units.")
        print(f"  The plugin said: \"{ev['quote']}\"")
        print(f"  Every row above ran on {ev['runs_on']} whatever its label says,")
        print("  which is why the numbers match. No speedup is reported.")
        print("\n  It is also the strongest evidence we have that inference really")
        print("  is on the NPU: the plugin will not run anywhere else, and says so.")
        return

    warm = {r["label"]: r["warm_median_s"] for r in results if r["warm_median_s"]}
    if "npu" in warm and "cpu" in warm:
        print(f"\n  npu is {warm['cpu'] / warm['npu']:.1f}x faster than cpu, warm,")
        print("  on these frames, with each server started with that -c flag.")
    elif len(results) == 1:
        print(
            f"\n  Only '{results[0]['label']}' has been measured, so there is "
            "nothing to compare it against yet."
        )
    print("\n  Neither figure is independently verified - see the note in each file.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label",
        choices=list(COMPUTE_UNITS),
        help="the compute unit the running server was started with",
    )
    parser.add_argument("--video", type=Path, default=Path("demo/clip_rolling.mp4"))
    parser.add_argument(
        "--frames",
        type=int,
        default=6,
        help="how many frames to time. Use 3 on CPU - it is slow.",
    )
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()

    if args.compare and not args.label:
        compare()
        return
    if not args.label:
        parser.error("give --label npu|cpu|gpu|hybrid, or --compare")
    if not args.video.is_file():
        sys.exit(
            f"no video at {args.video}\n"
            "Generate the demo clips:  ./run.sh --demo fail"
        )

    run(args.video, max(2, args.frames), args.label)
    if args.compare:
        print()
        compare()


if __name__ == "__main__":
    main()
