"""Run the fast ego-motion layer over a video and show where the car stopped.

No model, no NPU, no UNO Q - just decoding and frame differencing, so it runs
in a couple of seconds and tells you whether the motion threshold is sane for
your footage.

    python test_video.py ..\\test_clip_stop.mp4
    python test_video.py clip.mp4 --threshold 3.5

Look at the printed scores before trusting the moving/stopped column: the right
threshold sits in the gap between the two clusters.
"""

import argparse
import sys
import time

from ego_motion import MOTION_THRESHOLD, EgoMotion
from video_source import VideoSource

# Ignore blips shorter than this when reporting stationary windows - a single
# noisy frame is not a stop.
MIN_STOP_S = 0.4


def find_stops(readings, min_stop_s=MIN_STOP_S):
    """Collapse per-frame stopped flags into (start, end) windows."""
    windows = []
    start = None

    for t, _score, stopped in readings:
        if stopped and start is None:
            start = t
        elif not stopped and start is not None:
            if t - start >= min_stop_s:
                windows.append((start, t))
            start = None

    if start is not None and readings[-1][0] - start >= min_stop_s:
        windows.append((start, readings[-1][0]))

    return windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD)
    parser.add_argument(
        "--every",
        type=float,
        default=0.5,
        help="seconds between printed rows (default: 0.5)",
    )
    args = parser.parse_args()

    try:
        source = VideoSource(args.video)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(str(exc))

    with source:
        w, h = source.size
        print(f"{args.video}")
        print(f"  {w}x{h} @ {source.fps:.1f}fps, {source.duration_s:.1f}s")
        print(f"  motion threshold: {args.threshold}\n")

        motion = EgoMotion(threshold=args.threshold)
        readings = []
        next_print = 0.0
        started = time.perf_counter()

        print("   time   score  state")
        for frame in source.frames():
            score = motion.update(frame.gray)
            if score is None or not motion.ready:
                continue

            readings.append((frame.t, score, motion.is_stopped))

            if frame.t >= next_print:
                next_print = frame.t + args.every
                bar = "#" * min(40, int(score * 2))
                state = "STOPPED" if motion.is_stopped else "moving "
                print(f"  {frame.t:5.1f}s  {score:6.2f}  {state} {bar}")

        elapsed = time.perf_counter() - started

    if not readings:
        sys.exit("\nNo usable frames - is the video empty?")

    scores = [s for _, s, _ in readings]
    print(f"\n  decoded {len(readings)} frames in {elapsed:.1f}s")
    print(f"  score range: {min(scores):.2f} to {max(scores):.2f}")

    windows = find_stops(readings)
    if windows:
        print("\n  stationary windows:")
        for start, end in windows:
            print(f"    {start:5.1f}s - {end:5.1f}s  ({end - start:.1f}s)")
    else:
        print("\n  stationary windows: none - the car never stopped")


if __name__ == "__main__":
    main()
