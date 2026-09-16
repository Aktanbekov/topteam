"""Analyse one clip and build its review page, in a single process.

    python tools/build_drive.py IMG_9831.MOV --out output/9831

run.sh does this with two commands and a lot of shell around them. The demo
console needs it as ONE child process it can start and watch, so this is that
same pair of steps with nothing else attached: no GenieX supervision, no board,
no serving, no browser. It prints as it goes, because the console streams this
output straight onto the page and a silent minute looks like a hang.

Existing work is reused rather than repeated: if the timeline is already there
and is newer than the video, the vision pass is skipped and only the page is
rebuilt. That is what makes the console's "Rebuild" instant when all you changed
was a threshold.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(argv, what):
    print(f"\n==> {what}", flush=True)
    result = subprocess.run([sys.executable, "-u", *argv], cwd=str(ROOT))
    if result.returncode != 0:
        sys.exit(f"\n{what} failed with exit code {result.returncode}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--out", required=True)
    parser.add_argument("--vision-every", type=float, default=3.0)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run the vision pass even if a usable timeline is already there",
    )
    args = parser.parse_args()

    video = ROOT / args.video
    if not video.is_file():
        sys.exit(f"no such video: {args.video}")

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    timeline = out / "timeline.json"

    fresh = (
        timeline.is_file()
        and timeline.stat().st_mtime >= video.stat().st_mtime
        and not args.force
    )
    if fresh:
        print(f"==> Reusing {timeline.relative_to(ROOT)}")
        print("    (the vision pass already ran on this video; --force redoes it)")
    else:
        run(
            [str(ROOT / "laptop" / "analyze_video.py"), args.video,
             "--vision-every", str(args.vision_every), "--source", "real",
             "--save-frames", str(out / "frames"), "--json-out", str(timeline)],
            f"Analysing {args.video} - one model call every {args.vision_every:g}s",
        )

    run(
        [str(ROOT / "tools" / "make_player.py"), "--video", args.video,
         "--timeline", str(timeline), "--out", str(out / "player.html")],
        "Building the review page",
    )
    print("\n==> Done. The card on the console now has an Open button.", flush=True)


if __name__ == "__main__":
    main()
