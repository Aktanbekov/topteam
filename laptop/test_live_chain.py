"""Prove the whole live chain fires, using a real stop sign and no car.

    python laptop/test_live_chain.py --unoq

    real stop-sign frame -> Qwen3-VL on the NPU -> scene_filter -> state machine
        -> level track -> MCP over USB -> the sketch -> LEDs, matrix, buzz

test_signals.py walks the board through levels 0-3 directly, which proves the
HARDWARE works and nothing else. This proves the part in between: that the model
really recognises a stop sign, that two sightings really validate an approach,
and that the level that comes out really reaches the strip. When the board stays
dark on a live drive, this is the script that says which half is at fault.

It replays a still from IMG_9831 in place of the camera, AT REAL TIME, so the
model gets the same seconds it would get from a webcam. Everything downstream is
the genuine article - this is not a simulation of the pipeline, it IS the
pipeline, with one frame source swapped.

TWO RUNS, because the interesting thing is that they differ:

    python laptop/test_live_chain.py --unoq
        A still IS a stationary car, so the approach grades as a stop.
        Levels: 0 -> 1. The board goes amber and stops there. Correct.

    python laptop/test_live_chain.py --unoq --moving
        The image pans, so the motion layer reads "moving" and the window
        closes with nobody having stopped.
        Levels: 0 -> 1 -> 3 -> 0. Amber, then red with the X and three buzzes.

Measured, in the moving run:

    0.5s    green            nothing validated yet
    4.5s    AMBER + a tap    two sightings agreed - the approach is real
    16.5s   RED, X, 3 buzzes the window expired with no stop
    20.5s   green            the critical hold ran out

The four seconds before amber are not lag to be tuned away - they are two
vision calls plus the agreement between them, which is exactly what stops one
hallucinated frame from lighting the strip. A real sign at a real camera behaves
the same way, which is why this is worth watching once before a demo.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from live_drive import LiveDrive
from video_source import MOTION_H, MOTION_W, Frame

ROOT = Path(__file__).resolve().parent.parent

# A real stop sign, and a real frame from the same drive with no sign in it.
# Using our own footage rather than a drawing matters: a rendered octagon is an
# easier question than a sign on a mast at the side of a sunlit road.
SIGN_FRAME = ROOT / "output" / "9831" / "frames" / "frame_006_07s.jpg"
CLEAR_FRAME = ROOT / "output" / "9831" / "frames" / "frame_015_10s.jpg"


class StillReplaySource:
    """A camera that shows one still, then another. Paced to real time.

    Real time matters. A fake source free-running as fast as the CPU allows
    would push the drive clock to 30s in under a second, and every vision slot
    would be skipped because the model was still busy with the first. Sleeping
    between frames makes the model get the same seconds it gets from a webcam.
    """

    device = "still replay (not a camera)"
    looks_portrait = False

    def __init__(self, sign_image, clear_image, sign_for=9.0, total=30.0, fps=15.0,
                 pan_px=0):
        self.sign = sign_image
        self.clear = clear_image
        self.sign_for = sign_for
        self.total = total
        self._fps = fps
        # Pixels the scene shifts per frame. A still reads as a STOPPED car, and
        # correctly so - which means it can only ever demo a clean approach.
        # Panning the image makes the motion layer read "moving", so the window
        # closes with no stop and the drive reaches the critical level.
        self.pan_px = pan_px
        self.frames_seen = 0
        self.started_at = None

    @property
    def size(self):
        return (self.sign.shape[1], self.sign.shape[0])

    @property
    def fps(self):
        return self._fps

    @property
    def achieved_fps(self):
        if not self.started_at or self.frames_seen < 2:
            return 0.0
        elapsed = time.perf_counter() - self.started_at
        return (self.frames_seen - 1) / elapsed if elapsed else 0.0

    def frames(self, stride_s=0.0):
        self.started_at = time.perf_counter()
        step = 1.0 / self._fps
        while True:
            t = time.perf_counter() - self.started_at
            if t > self.total:
                return
            self.frames_seen += 1

            rgb = self.sign if t < self.sign_for else self.clear
            if self.pan_px:
                rgb = np.roll(rgb, int(self.frames_seen * self.pan_px) % rgb.shape[1], axis=1)
            # A little noise so the motion layer has something to measure and
            # does not read a frozen still as a car parked at the line.
            rgb = np.clip(
                rgb.astype(np.int16)
                + np.random.randint(-3, 4, rgb.shape, dtype=np.int16),
                0,
                255,
            ).astype(np.uint8)

            gray = np.asarray(
                Image.fromarray(rgb).convert("L").resize((MOTION_W, MOTION_H)),
                dtype=np.uint8,
            )
            yield Frame(t=t, rgb=rgb, gray=gray)
            time.sleep(step)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Narrating:
    """Wraps the board so every level change is printed as it is sent."""

    def __init__(self, inner):
        self.inner = inner
        self.seen = []

    def reset(self):
        return self.inner.reset() if self.inner else True

    def apply(self, level, seq):
        result = self.inner.apply(level, seq) if self.inner else "no board"
        if result == "sent":
            self.seen.append(level)
            names = {0: "green", 1: "AMBER + tap", 2: "red + pulse", 3: "RED, X, 3 buzzes"}
            print(f"    -> board: level {level}  ({names.get(level, '?')})")
        return result


def _build_player(out_dir):
    """Turn the simulation into the same review page as any other drive."""
    import subprocess

    video = Path(out_dir) / "drive.mp4"
    timeline = Path(out_dir) / "timeline.json"
    if not video.is_file():
        print("  (no recording, so no player)")
        return

    root = Path(__file__).resolve().parent.parent
    print()
    print("  building the review page...")
    result = subprocess.run(
        [sys.executable, str(root / "tools" / "make_player.py"),
         "--video", str(video), "--timeline", str(timeline),
         "--out", str(Path(out_dir) / "player.html")],
        cwd=str(root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("  could not build the player:")
        print(result.stdout[-800:] or result.stderr[-800:])
        return
    print(f"  player: {Path(out_dir) / 'player.html'}")
    print(f"  report: {Path(out_dir) / 'report.html'}")
    if os.environ.get("DEMO_CONSOLE"):
        # The console already serves this page and has just grown an Open
        # button for it. Telling the presenter to run a command would be the
        # one thing this whole console exists to avoid.
        print()
        print("  the card on the console now has an Open button.")
    else:
        print()
        print("  serve it with:")
        print(f"    .venv/Scripts/python.exe tools/serve_player.py "
              f"--page {Path(out_dir) / 'player.html'} --open")


def _kinds(drive):
    """Which checks fired, so the summary names them rather than just a level."""
    try:
        found = sorted({e["kind"] for e in drive._review()["events"]})
        return ", ".join(found) if found else "none"
    except Exception:  # noqa: BLE001 - a summary line must never fail the run
        return "unknown"


def load(path, width=640):
    if not path.is_file():
        sys.exit(
            f"missing {path}\n"
            "Run a drive over IMG_9831 first so the frames exist:\n"
            "  ./run.sh IMG_9831.MOV --out output/9831"
        )
    image = Image.open(path).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)))
    # h264 in yuv420p needs even dimensions on both axes, and a 1920x1072 frame
    # scales to 640x357 - odd, and the encoder refuses it. Trimming a row is
    # invisible and keeps the simulation recordable, which is what gives it a
    # player to watch instead of a wall of terminal output.
    w, h = image.width - image.width % 2, image.height - image.height % 2
    if (w, h) != image.size:
        image = image.crop((0, 0, w, h))
    return np.asarray(image, dtype=np.uint8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unoq", action="store_true", help="drive the real board")
    parser.add_argument("--sign-for", type=float, default=9.0)
    parser.add_argument("--total", type=float, default=30.0)
    parser.add_argument("--vision-every", type=float, default=3.0)
    parser.add_argument(
        "--moving",
        action="store_true",
        help="pan the image so the motion layer reads 'moving'. Without it the "
        "still is a stationary car, the approach grades as a stop, and the board "
        "never goes past level 1 - which is the correct answer, not a fault.",
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=None,
        help="the hazard frame to replay instead of the built-in stop sign. Use "
        "a photo of somebody crossing the road to exercise the yield check.",
    )
    parser.add_argument(
        "--clear-image",
        type=Path,
        default=None,
        help="the frame shown once the hazard has passed",
    )
    parser.add_argument(
        "--no-player",
        action="store_true",
        help="skip the recording and the review page, terminal output only",
    )
    parser.add_argument("--out", type=Path, default=Path("output/chain_check"))
    args = parser.parse_args()

    sign = load(args.image or SIGN_FRAME)
    clear = load(args.clear_image or CLEAR_FRAME)

    replay = None
    if args.unoq:
        from unoq_mcp import EventReplay, UnoQ

        board = UnoQ()
        if not board.connect():
            sys.exit(
                f"could not reach the board: {board.last_error}\n"
                "Bring the tunnel up first:  ./run.sh --live --unoq  (or --calm)"
            )
        replay = EventReplay(board)
        replay.reset()
        print("board connected and reset\n")
    else:
        print("no --unoq: running the chain without the board\n")

    narrator = Narrating(replay)

    drive = LiveDrive(
        out_dir=args.out,
        vision_every=args.vision_every,
        # Record it. A simulation you can only read in a terminal is much harder
        # to trust than one you can watch with the decisions drawn on it, and
        # recording means the ordinary make_player path turns this into exactly
        # the same page as a real drive.
        record=not args.no_player,
        replay=narrator,
        preview=False,
        quiet=False,
    )
    drive.open_source = lambda: StillReplaySource(
        sign, clear, sign_for=args.sign_for, total=args.total,
        pan_px=3 if args.moving else 0,
    )

    print(f"stop sign visible for the first {args.sign_for:g}s of a {args.total:g}s drive")
    print("watch the strip, the matrix and the motor\n")
    timeline, path = drive.run()

    print(f"\n  events found: {_kinds(drive)}")
    print(f"  levels actually sent to the board: {narrator.seen or 'none'}")
    ok = 1 in narrator.seen
    if ok:
        # Deliberately not "saw the sign" any more: with --image this replays
        # whatever hazard you point it at, and a summary that names the wrong
        # one is how a passing test starts meaning nothing.
        print("  PASS - the model saw the hazard and the board was told")
    else:
        print("  FAIL - level 1 never reached the board")
    if 3 in narrator.seen:
        print("  and it went unanswered, so it reached level 3 - the board should")
        print("  be flashing red with the X and three buzzes")
    elif ok:
        print("  (no level 3: the car stopped, or --total is too short for the")
        print("   window to close. Add --moving to drive through it.)")
    if not args.no_player:
        _build_player(args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
