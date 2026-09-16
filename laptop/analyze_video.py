"""Run both detection layers over a dashcam video and write the timeline.

    geniex serve -c npu                    (in another terminal, or use ./run.sh)
    python analyze_video.py clip.mp4
    python analyze_video.py clip.mp4 --vision-every 2 --save-frames output/frames

Every vision call costs roughly 3 seconds on the NPU, so a 114s clip sampled
every 3s takes about two minutes. Use --vision-every 5 for a quick pass over
long footage.

The output is observations only - what the model saw and what the motion layer
measured. No verdicts are reached here; that is drive_review.py's job, and
keeping the two apart is what lets the scoring rules be unit tested without a
model or a GPU anywhere near them.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image

from config import (
    COMPUTE_UNITS,
    GENIEX_URL,
    MOTION_THRESHOLD,
    VISION_INTERVAL_S,
    VISION_MODEL,
    Settings,
    compute_note,
)
from drive_review import SCHEMA_VERSION
from ego_motion import EgoMotion
from scene_filter import confirm, dropped
from scene_vision import SceneVision
from video_source import VideoSource


def summarise(scene):
    """Condense a scene dict into a short line, listing only what was seen."""
    seen = []
    if scene["stop_sign"]:
        seen.append("STOP SIGN")
    if scene["traffic_light"] != "none":
        lane = "ours" if scene["light_is_for_our_lane"] else "not ours"
        seen.append(f"light={scene['traffic_light']} ({lane})")
    if scene["stop_line_visible"]:
        seen.append("stop line")
    if scene["pedestrian_in_crosswalk"]:
        seen.append("PEDESTRIAN")
    if scene["car_ahead_close"]:
        seen.append("car ahead close")
    return ", ".join(seen) if seen else "-"


def frame_name(t):
    """Stable filename for a sampled frame, e.g. 64.03s -> frame_064_03s.jpg."""
    return f"frame_{t:06.2f}s.jpg".replace(".", "_", 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument(
        "--vision-every",
        type=float,
        default=VISION_INTERVAL_S,
        help=f"seconds between vision calls (default: {VISION_INTERVAL_S:g})",
    )
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD)
    parser.add_argument(
        "--save-frames",
        type=Path,
        default=None,
        help="directory to write the sampled frames to, used as report evidence",
    )
    parser.add_argument(
        "--rotate",
        type=int,
        default=None,
        choices=[0, 90, 180, 270],
        help="rotate frames if phone footage comes out sideways",
    )
    parser.add_argument(
        "--compute",
        default=None,
        choices=list(COMPUTE_UNITS),
        help="the compute unit geniex serve was asked for, recorded in the "
        "output. This does not change where inference runs - it only records "
        "what was requested, because the API cannot tell us what was used.",
    )
    parser.add_argument(
        "--source",
        default="real",
        choices=["real", "synthetic"],
        help="labels the footage in the review page. Synthetic clips are "
        "marked as such so nobody mistakes a rendered road for a real drive.",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    vision = SceneVision()
    if not vision.health():
        sys.exit(
            f"GenieX is not answering at {GENIEX_URL}\n"
            "Start it in another terminal with:  geniex serve -c npu\n"
            "Or let ./run.sh start it for you."
        )

    try:
        source = VideoSource(args.video, rotate=args.rotate)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(str(exc))

    if source.looks_portrait and not source.rotate:
        print(
            "  NOTE: frames are taller than wide. If the road looks sideways in\n"
            "        --save-frames output, re-run with --rotate 90 or --rotate 270.\n"
        )

    if args.save_frames:
        args.save_frames.mkdir(parents=True, exist_ok=True)

    timeline = []
    frames = []
    started = time.perf_counter()

    with source:
        w, h = source.size
        drive = {
            "file": Path(args.video).name,
            "source": args.source,
            "duration_s": round(source.duration_s, 2),
            "fps": round(source.fps, 2),
            "width": w,
            "height": h,
        }
        print(f"{args.video}")
        print(f"  {w}x{h} @ {source.fps:.1f}fps, {source.duration_s:.1f}s")
        print(f"  vision call every {args.vision_every}s")
        print(f"  model {VISION_MODEL}")
        print(f"  compute: {compute_note(args.compute)}\n")

        motion = EgoMotion(threshold=args.threshold)
        next_vision = 0.0

        print("   time  motion   state    scene")
        for frame in source.frames():
            score = motion.update(frame.gray)
            if score is None:
                continue

            if frame.t < next_vision:
                continue
            next_vision = frame.t + args.vision_every

            # Wait for the smoothing window before reporting a motion state, so
            # the first sample is not judged on one noisy reading.
            state = "?" if not motion.ready else (
                "STOPPED" if motion.is_stopped else "moving"
            )

            scene, error = vision.describe(frame.rgb)
            note = summarise(scene) if error is None else f"[{error}]"
            print(f"  {frame.t:5.1f}s  {score:6.2f}  {state:<8} {note}")

            saved = None
            if args.save_frames:
                name = frame_name(frame.t)
                Image.fromarray(frame.rgb).save(args.save_frames / name, quality=88)
                saved = f"{args.save_frames.name}/{name}"
                frames.append({"t": round(frame.t, 2), "path": saved})

            timeline.append(
                {
                    "t": round(frame.t, 2),
                    "motion_score": round(score, 3),
                    "stopped": motion.is_stopped if motion.ready else None,
                    "scene": scene,
                    "error": error,
                    "frame": saved,
                }
            )

    elapsed = time.perf_counter() - started
    stats = vision.stats()
    print(f"\n  {len(timeline)} samples in {elapsed:.1f}s")
    print(f"  vision calls: {stats['calls']}, failed: {stats['failed']}")
    if stats["first_call_s"] is not None:
        print(f"  first call: {stats['first_call_s']}s (includes model load)")
    if stats["median_s"] is not None:
        print(f"  warm calls: median {stats['median_s']}s, p95 {stats['p95_s']}s")

    if stats["failed"]:
        # Failures leave an all-false scene behind, which can only cause a
        # missed detection - never a fabricated one. Say so rather than letting
        # the gap pass unmentioned.
        print(
            f"  {stats['failed']} call(s) failed. Those samples hold no "
            "observations, so they can only cost a detection, never invent one."
        )

    # The table above is the model's raw answer, one frame at a time. This is
    # what survives being checked against the samples either side of it.
    confirm(timeline, spacing=args.vision_every)
    rejected = dropped(timeline)
    if rejected:
        print(f"  {len(rejected)} unconfirmed detection(s), ignored downstream:")
        for r in rejected:
            print(f"    {r['t']:6.1f}s  {r['field']} = {r['value']!r}")

    stop_sign_seen = [s["t"] for s in timeline if s["scene"]["stop_sign"]]
    if stop_sign_seen:
        print(
            f"  stop sign visible at: "
            f"{', '.join(f'{t:.1f}s' for t in stop_sign_seen)}"
        )
    else:
        print("  no stop sign seen in the sampled frames")

    if args.json_out:
        settings = Settings(
            vision_interval_s=args.vision_every, motion_threshold=args.threshold
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "drive": drive,
            "processing": {
                "model": VISION_MODEL,
                "endpoint": GENIEX_URL,
                "compute_requested": args.compute,
                "compute_verified": None,
                "compute_note": compute_note(args.compute),
                "analysis_s": round(elapsed, 1),
                "real_time_ratio": (
                    round(drive["duration_s"] / elapsed, 2) if elapsed else None
                ),
                **stats,
            },
            "settings": settings.as_dict(),
            "samples": timeline,
            "frames": frames,
            "rejected": rejected,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"  wrote {args.json_out}")


if __name__ == "__main__":
    main()
