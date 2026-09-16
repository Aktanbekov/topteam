"""Run both detection layers over a dashcam video and print a timeline.

This is build step 1: video in, scene labels and motion state out. No mistake
rules yet, no UNO Q, no report - just proof that both layers see the drive.

    geniex serve                          (in another terminal)
    python analyze_video.py clip.mp4
    python analyze_video.py clip.mp4 --vision-every 2 --save-frames out\\

Every vision call costs ~2.7s, so a 60s clip sampled every 3s takes about a
minute. Use --vision-every 5 for a quick pass over long footage.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image

from ego_motion import MOTION_THRESHOLD, EgoMotion
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument(
        "--vision-every",
        type=float,
        default=3.0,
        help="seconds between vision calls (default: 3, the warm call time)",
    )
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD)
    parser.add_argument(
        "--save-frames",
        type=Path,
        default=None,
        help="directory to write the sampled frames to, for the report later",
    )
    parser.add_argument(
        "--rotate",
        type=int,
        default=None,
        choices=[0, 90, 180, 270],
        help="rotate frames if phone footage comes out sideways",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    vision = SceneVision()
    if not vision.health():
        sys.exit(
            "GenieX is not answering at http://127.0.0.1:18181/v1\n"
            "Start it in another terminal with:  geniex serve"
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
    started = time.perf_counter()

    with source:
        w, h = source.size
        print(f"{args.video}")
        print(f"  {w}x{h} @ {source.fps:.1f}fps, {source.duration_s:.1f}s")
        print(f"  vision call every {args.vision_every}s\n")

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

            if args.save_frames:
                name = f"frame_{frame.t:06.2f}s.jpg".replace(".", "_", 1)
                Image.fromarray(frame.rgb).save(args.save_frames / name, quality=88)

            timeline.append(
                {
                    "t": round(frame.t, 2),
                    "motion_score": round(score, 3),
                    "stopped": motion.is_stopped if motion.ready else None,
                    "scene": scene,
                    "error": error,
                }
            )

    elapsed = time.perf_counter() - started
    print(f"\n  {len(timeline)} samples in {elapsed:.1f}s")
    print(f"  vision calls: {vision.calls}, failed: {vision.failures}")

    stop_sign_seen = [s["t"] for s in timeline if s["scene"]["stop_sign"]]
    if stop_sign_seen:
        print(
            f"  stop sign visible at: "
            f"{', '.join(f'{t:.1f}s' for t in stop_sign_seen)}"
        )
        stopped_any = any(s["stopped"] for s in timeline if s["stopped"] is not None)
        print(f"  car reached a stopped state at some point: {stopped_any}")
    else:
        print("  no stop sign seen in the sampled frames")

    if args.json_out:
        args.json_out.write_text(json.dumps(timeline, indent=2))
        print(f"  wrote {args.json_out}")


if __name__ == "__main__":
    main()
