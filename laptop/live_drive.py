"""Analyse a drive from the camera as it happens, and drive the board live.

    geniex serve -c npu                 (in another terminal, or use ./run.sh)
    python laptop/live_drive.py --out output/live
    python laptop/live_drive.py --out output/live --unoq --max-seconds 120

Ctrl-C ends the drive. What comes out is the same timeline.json that
analyze_video.py writes, plus the recording made while it ran - so the ordinary
build path turns it into the same player and report as any other drive:

    python tools/make_player.py --video output/live/drive.mp4 \
                               --timeline output/live/timeline.json

WHY THIS IS A SEPARATE SCRIPT, not a flag on analyze_video.py. That script is a
single loop: decode a frame, and every 3s stop everything and wait ~3.5s for the
model. Over a file that is merely slow. Over a camera it is fatal - the motion
layer would miss every frame during the call, and the motion layer is the half
that has to see EVERY frame to tell a stop from a crawl. Live needs the two
layers on different threads, and that is a different program, not a branch.

HOW LATE A LIVE WARNING IS, and why we say so on the page:

    vision call                     ~3.5s   (measured, warm)
    + one interval for confirmation  3.0s   (scene_filter needs a neighbour)
    = a confirmed detection is      ~6.5s old

That is not a bug to be tuned away - it is what it costs to not act on a single
frame, and acting on a single frame is what produces hallucinated red lights.
So the honest scope is:

    stop-sign approaches   fine live. The deadline is already forward-looking:
                           the sign is seen at t and the verdict is due at
                           t+10s, so 6.5s of lag fits inside the window.
    red lights             NOT fine live. A red-light warning 6.5s late is not
                           a warning. It is detected and shown, never presented
                           as something that could have helped in time.

Nothing here decides anything. Exactly as in the file path, this collects
observations and hands them to drive_review.build_review, which is pure, takes
0.18ms over a whole drive, and is unit tested with no camera in sight. Live mode
simply calls it again on the growing prefix every time a sample lands.
"""

import argparse
import io
import json
import queue
import signal
import statistics
import sys
import threading
import time
import webbrowser
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image

from analyze_video import summarise
from camera_source import DEFAULT_FPS, CameraLost, CameraSource, list_cameras
from config import (
    GENIEX_URL,
    MOTION_THRESHOLD,
    VISION_INTERVAL_S,
    VISION_MODEL,
    Settings,
    compute_note,
)
from drive_review import SCHEMA_VERSION, build_review
from ego_motion import EgoMotion
from live_preview import LiveState, PreviewServer
from scene_vision import EMPTY_SCENE, SceneVision
from video_source import MOTION_H, MOTION_W  # noqa: F401  (documents the gray size)

# How deep the evidence ring goes. An event is decided up to WINDOW_AFTER_S
# after its first sighting, so the still that illustrates it is older than the
# newest frame by that much. 25s covers the longest window with room spare.
EVIDENCE_RING_S = 25.0

# How often a frame enters the evidence ring. The vision sampler saves its own
# frames every 3s; this fills the gaps so an event decided between samples is
# illustrated within half a second rather than within three.
EVIDENCE_EVERY_S = 0.5


class VisionWorker:
    """Runs one SceneVision.describe at a time, off the capture thread.

    submit() never blocks and never queues: if the model is still busy, the
    frame is refused and the caller keeps the older schedule. Queueing would
    mean labelling a frame from several seconds ago while calling it current,
    which is the one thing a live path must not do.
    """

    def __init__(self, vision):
        self.vision = vision
        self._busy = threading.Event()
        self._done = queue.Queue()
        self._thread = None
        self.refused = 0

    @property
    def busy(self):
        return self._busy.is_set()

    def submit(self, t, rgb):
        """Start labelling this frame. False if a call is still in flight."""
        if self._busy.is_set():
            self.refused += 1
            return False
        self._busy.set()
        self._thread = threading.Thread(
            target=self._run, args=(t, rgb), daemon=True, name="vision"
        )
        self._thread.start()
        return True

    def _run(self, t, rgb):
        try:
            scene, error = self.vision.describe(rgb)
        except Exception as exc:  # noqa: BLE001 - a dropped call must not end the drive
            scene, error = None, f"{type(exc).__name__}: {exc}"
        finally:
            self._busy.clear()
        self._done.put((t, scene, error))

    def poll(self):
        """Every finished call since the last poll. Never blocks."""
        out = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out


class Recorder:
    """Write the drive to an MP4 on a worker thread, dropping when behind.

    The recording exists so a live drive produces the same artefacts as a file
    one - the player needs something to scrub. It is deliberately the FIRST
    thing to suffer under load: a dropped frame costs a slightly choppier
    replay, whereas a blocked capture thread costs the motion track, and the
    motion track is evidence.

    Presentation times are the real capture times, not a frame counter. If we
    drop frames the video must still be 60 seconds long after 60 seconds, or
    every event timestamp in the player would point at the wrong moment.
    """

    QUEUE_DEPTH = 48

    def __init__(self, path, size, fps=DEFAULT_FPS):
        self.path = Path(path)
        self.dropped = 0
        self.written = 0
        self._q = queue.Queue(maxsize=self.QUEUE_DEPTH)
        self._error = None

        self._container = av.open(str(self.path), mode="w")
        self._stream = self._container.add_stream("libx264", rate=fps)
        self._stream.width, self._stream.height = size
        self._stream.pix_fmt = "yuv420p"
        # Millisecond timebase: pts is the capture time, so the file's clock and
        # the timeline's clock are the same clock. It has to be set on the codec
        # context as well as the stream - setting only the stream leaves the
        # encoder on its own default, and the pts we hand it are then read in
        # the wrong units.
        self._stream.time_base = Fraction(1, 1000)
        self._stream.codec_context.time_base = Fraction(1, 1000)
        self._stream.options = {"preset": "ultrafast", "crf": "23", "tune": "zerolatency"}
        self._last_pts = None

        self._thread = threading.Thread(target=self._run, daemon=True, name="recorder")
        self._thread.start()

    def write(self, t, rgb):
        try:
            self._q.put_nowait((t, rgb))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                break
            t, rgb = item
            try:
                pts = int(round(t * 1000))
                # The muxer rejects a frame whose timestamp does not advance, and
                # two captures can land in the same millisecond. Nudging the
                # duplicate forward by one keeps the stream strictly increasing
                # and moves the picture by 1ms, which nothing can see.
                if self._last_pts is not None and pts <= self._last_pts:
                    pts = self._last_pts + 1
                self._last_pts = pts

                frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
                frame.pts = pts
                frame.time_base = Fraction(1, 1000)
                for packet in self._stream.encode(frame):
                    self._container.mux(packet)
                self.written += 1
            except Exception as exc:  # noqa: BLE001 - never kill the drive over the recording
                self._error = f"{type(exc).__name__}: {exc}"

    def close(self):
        self._q.put(None)
        self._thread.join(timeout=10)
        try:
            for packet in self._stream.encode():
                self._container.mux(packet)
        except Exception:  # noqa: BLE001
            pass
        self._container.close()
        return self._error


class EvidenceRing:
    """The last EVIDENCE_RING_S of frames, as JPEG bytes.

    A file can be seeked when an event is decided, so the exact still is always
    available. A camera cannot: by the time a stop-sign window expires, the
    moment that illustrates it is ten seconds gone. This keeps it.

    JPEG rather than raw: 25s of 720p RGB is 1.6GB, the same as JPEGs is ~10MB.
    """

    def __init__(self, seconds=EVIDENCE_RING_S, every=EVIDENCE_EVERY_S, quality=88):
        self.seconds = seconds
        self.every = every
        self.quality = quality
        self._items = []  # [(t, jpeg_bytes)]
        self._next_t = 0.0

    def offer(self, t, rgb):
        if t < self._next_t:
            return
        self._next_t = t + self.every

        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=self.quality)
        self._items.append((t, buf.getvalue()))

        cutoff = t - self.seconds
        while self._items and self._items[0][0] < cutoff:
            self._items.pop(0)

    def nearest(self, t):
        """(time, jpeg) closest to t, or None if the ring has rolled past it."""
        if not self._items:
            return None
        return min(self._items, key=lambda item: abs(item[0] - t))


def achieved_spacing(samples, requested):
    """The interval the samples ACTUALLY landed at, never below the requested one.

    A vision call takes ~3.5s, so asking for one every 3s produces samples about
    3.9s apart - the sampler waits for the model rather than queueing behind it.
    That matters because scene_filter only treats two samples as neighbours when
    they are within spacing * 1.6 of each other: pass it the requested 3.0s and
    a 4.9s gap stops counting as adjacent, at which point every detection is
    silently rejected as unconfirmed and the drive quietly stops detecting
    anything. Failing safe is right; failing silently is not, so the confirmation
    window is measured from what happened rather than from what we asked for.

    Never goes below `requested`: a burst of fast samples must not tighten the
    window on the slow ones around it.
    """
    times = [s["t"] for s in samples]
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not gaps:
        return requested
    return max(requested, round(statistics.median(gaps), 2))


def level_now(levels, t):
    """(index, level) of the level-track span covering t. (-1, 0) before the first."""
    found = (-1, 0)
    for i, entry in enumerate(levels):
        if entry[0] <= t:
            found = (i, entry[1])
        else:
            break
    return found


class LiveDrive:
    """Capture, analyse and signal, all at once.

    The capture thread does the cheap work on every single frame - motion,
    recording hand-off, evidence ring - and never waits for the model. The model
    runs on its own thread and its answers are collected whenever they arrive.
    """

    def __init__(
        self,
        out_dir,
        device=None,
        vision_every=VISION_INTERVAL_S,
        threshold=MOTION_THRESHOLD,
        rotate=None,
        max_seconds=None,
        record=True,
        replay=None,
        preview=True,
        open_preview=True,
        preview_port=None,
        compute=None,
        quiet=False,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir = self.out_dir / "frames"
        self.frames_dir.mkdir(exist_ok=True)

        self.device = device
        self.vision_every = vision_every
        self.threshold = threshold
        self.rotate = rotate
        self.max_seconds = max_seconds
        self.record = record
        self.replay = replay  # an EventReplay, or None for no board
        self.preview = preview
        self.open_preview = open_preview
        # Tests pass 0 so the OS picks a free port: a real live drive already
        # holding 8008 must not be able to make the suite fail.
        self.preview_port = preview_port
        self.compute = compute
        self.quiet = quiet

        self.samples = []
        self.readings = []
        self.frames = []
        self.stop_requested = False
        self.samples_missed = 0
        self._slot_missed = False
        self._state = None

    def request_stop(self):
        self.stop_requested = True

    # Two seams so the whole drive can be run with no camera and no model. They
    # exist for one reason: three bugs in a row - a missing import, a missing
    # constructor argument, an exception caught by a name that was never
    # imported - were all invisible until run() had warmed the model and turned
    # the webcam on, twenty seconds in. Every one of them would have been caught
    # by executing this method once against a fake. See tests/test_live.py.
    def open_source(self):
        return CameraSource(
            device=self.device, rotate=self.rotate, max_seconds=self.max_seconds
        )

    def open_vision(self):
        return SceneVision()

    def _warm_up(self, vision):
        """One throwaway call, so the model load does not land inside the drive.

        Returns the seconds it took, or None if it failed - a failure here is not
        fatal, it only means the first real sample pays the load instead.
        """
        import numpy as np

        grey = np.full((480, 640, 3), 128, dtype=np.uint8)
        started = time.perf_counter()
        try:
            vision.describe(grey)
        except Exception:  # noqa: BLE001 - the real calls report failures properly
            return None
        # The warm-up is real inference, but it is not part of the drive, so it
        # must not be reported as the drive's cold start.
        vision.reset_stats()
        return time.perf_counter() - started

    # ------------------------------------------------------------------ run
    def run(self):
        vision = self.open_vision()
        if not vision.health():
            raise RuntimeError(
                f"GenieX is not answering at {GENIEX_URL}\n"
                "Start it in another terminal with:  geniex serve -c npu"
            )

        # Load the weights BEFORE the drive clock starts. A cold first call
        # measured 18.3s on this laptop, and geniex unloads the model after its
        # 300s keepalive - so without this a live drive opens with an eighteen
        # second blind spot, which is most of a short demo.
        warm_s = self._warm_up(vision)

        source = self.open_source()
        worker = VisionWorker(vision)
        ring = EvidenceRing()

        # A live drive is otherwise invisible: without this the frames go to the
        # model and the board and are never drawn, so a working run and a broken
        # one look exactly the same from the outside.
        state = LiveState()
        preview = (
            PreviewServer(state, **({} if self.preview_port is None
                                    else {"port": self.preview_port}))
            if self.preview else None
        )
        preview_url = preview.start() if preview else None
        self._state = state

        # Open it rather than printing a URL and hoping. A live drive that draws
        # nothing looks broken, and the first thing anyone wants to see is
        # themselves on screen - proof the camera really is being read.
        if preview_url and self.open_preview:
            webbrowser.open(preview_url)
        motion = EgoMotion(threshold=self.threshold)
        recorder = None
        started = time.perf_counter()

        if not self.quiet:
            if warm_s is not None:
                print(f"model warmed in {warm_s:.1f}s, before the drive clock starts")
            print(f"camera: {source.device}")
            print(f"  {source.size[0]}x{source.size[1]} @ {source.fps:.0f}fps")
            print(f"  vision call every {self.vision_every}s")
            print(f"  model {VISION_MODEL}")
            print(f"  compute: {compute_note(self.compute)}")
            print(f"  board: {'attached' if self.replay else 'not attached'}")
            if preview_url:
                print(f"\n  WATCH THE CAMERA:  {preview_url}")
            elif self.preview:
                print("\n  preview port busy - carrying on without it")
            print("\n  Ctrl-C to end the drive\n")
            print("   time  motion   state    scene")

        next_vision = 0.0
        with source:
            if self.record:
                recorder = Recorder(
                    self.out_dir / "drive.mp4", source.size, fps=DEFAULT_FPS
                )

            for frame in source.frames():
                if self.stop_requested:
                    break

                score = motion.update(frame.gray)
                if score is None or not motion.ready:
                    continue

                self.readings.append((frame.t, score, motion.is_stopped))
                # Reference assignment only - the JPEG encode happens on the
                # preview's own thread, so a browser cannot cost us a frame.
                state.put_frame(frame.rgb)
                state.put_status(t=frame.t, motion=score, stopped=motion.is_stopped)
                ring.offer(frame.t, frame.rgb)
                if recorder:
                    recorder.write(frame.t, frame.rgb)

                if frame.t >= next_vision:
                    if worker.submit(frame.t, frame.rgb):
                        next_vision = frame.t + self.vision_every
                        self._slot_missed = False
                    elif not self._slot_missed:
                        # The model was still busy when this sample fell due.
                        # Retry on the next frame, but count the slot once
                        # rather than thirty times a second.
                        self._slot_missed = True
                        self.samples_missed += 1

                for t, scene, error in worker.poll():
                    self._landed(t, scene, error, ring)

        # A call can still be in flight when the drive ends; give it a moment
        # rather than throwing away the last three seconds of the drive.
        deadline = time.perf_counter() + 6.0
        while worker.busy and time.perf_counter() < deadline:
            time.sleep(0.05)
        for t, scene, error in worker.poll():
            self._landed(t, scene, error, ring)

        elapsed = time.perf_counter() - started
        rec_error = recorder.close() if recorder else None
        if preview:
            preview.close()

        return self._finish(source, vision, worker, recorder, rec_error, elapsed)

    # --------------------------------------------------------------- a sample
    def _landed(self, t, scene, error, ring):
        """One vision answer arrived. Record it, then re-decide the whole drive."""
        if scene is None:
            scene = dict(EMPTY_SCENE)

        saved = None
        shot = ring.nearest(t)
        if shot:
            name = f"frame_{t:06.2f}s.jpg".replace(".", "_", 1)
            (self.frames_dir / name).write_bytes(shot[1])
            saved = f"frames/{name}"
            self.frames.append({"t": round(t, 2), "path": saved})

        stopped_then = next(
            (s for rt, _sc, s in reversed(self.readings) if rt <= t), None
        )
        self.samples.append(
            {
                "t": round(t, 2),
                "motion_score": round(
                    next((sc for rt, sc, _s in reversed(self.readings) if rt <= t), 0.0), 3
                ),
                "stopped": stopped_then,
                "scene": scene,
                "error": error,
                "frame": saved,
            }
        )

        if getattr(self, "_state", None) is not None:
            self._state.put_status(scene=summarise(scene) if error is None else "")

        if not self.quiet:
            state = "STOPPED" if stopped_then else "moving"
            note = summarise(scene) if error is None else f"[{error}]"
            print(
                f"  {t:5.1f}s  {self.samples[-1]['motion_score']:6.2f}  "
                f"{state:<8} {note}"
            )

        self._tick()

    def _tick(self):
        """Re-decide the drive so far and push any level change to the board.

        build_review is pure and costs 0.18ms over a whole drive, so there is no
        incremental state machine here and no need for one - the prefix is
        rebuilt from scratch every time, which means a live verdict and a
        post-drive verdict cannot drift apart.
        """
        if not self.samples:
            return
        review = self._review()
        now = self.readings[-1][0] if self.readings else 0.0
        # The preview shows the level whether or not a board is attached, so the
        # page and the strip can never be telling different stories.
        if getattr(self, "_state", None) is not None:
            self._state.put_status(level=level_now(review["levels"], now)[1])
        if not self.replay:
            return
        index, level = level_now(review["levels"], now)
        if index < 0:
            return

        result = self.replay.apply(level, index)
        # Say what the board was told. Without this a live drive is silent about
        # the hardware, and "the strip did nothing" is unanswerable afterwards:
        # you cannot tell a level that was never due from one that was sent and
        # missed, or from a send that failed. All three look identical.
        if result == "sent":
            names = {0: "green", 1: "AMBER + tap", 2: "red + pulse",
                     3: "RED, X, 3 buzzes"}
            print(f"    -> board: level {level}  ({names.get(level, '?')})",
                  flush=True)
        elif result == "failed":
            error = getattr(self.replay, "last_error", None)
            print(f"    -> board: level {level} FAILED - {error}", flush=True)

    # ----------------------------------------------------------------- output
    def _review(self):
        """The whole drive so far, re-decided from scratch. Used by _tick."""
        duration = round(self.readings[-1][0], 2) if self.readings else 0.0
        drive = {
            "file": self.device or "camera",
            "source": "live",
            "duration_s": duration,
            "fps": 0.0,
            "width": 0,
            "height": 0,
        }
        processing = {
            "model": VISION_MODEL,
            "endpoint": GENIEX_URL,
            "mode": "live",
            "compute_requested": self.compute,
            "compute_verified": None,
            "compute_note": compute_note(self.compute),
            "analysis_s": duration,
            "real_time_ratio": 1.0,
        }
        settings = Settings(
            vision_interval_s=self.vision_every, motion_threshold=self.threshold
        ).as_dict()

        return build_review(
            samples=[dict(s) for s in self.samples],
            readings=self.readings,
            drive=drive,
            processing=processing,
            settings=settings,
            frames=self.frames,
            motion_points=[[round(t, 2), round(sc, 3)] for t, sc, _ in self.readings[::3]],
            rejected=None,
            spacing=achieved_spacing(self.samples, self.vision_every),
        )

    def _finish(self, source, vision, worker, recorder, rec_error, elapsed):
        stats = vision.stats()
        lag = None
        if stats.get("median_s"):
            # What the page has to say out loud: how old a confirmed warning is.
            lag = round(stats["median_s"] + self.vision_every, 1)

        extra = {
            "warning_lag_s": lag,
            "vision_interval_achieved_s": achieved_spacing(
                self.samples, self.vision_every
            ),
            "samples_missed": self.samples_missed,
            "camera_fps": round(source.achieved_fps, 2),
            "recorded_frames": recorder.written if recorder else 0,
            "recording_dropped": recorder.dropped if recorder else 0,
            "recording_error": rec_error,
        }

        timeline = {
            "schema_version": SCHEMA_VERSION,
            "drive": {
                "file": source.device,
                "source": "live",
                "duration_s": round(self.readings[-1][0], 2) if self.readings else 0.0,
                "fps": round(source.achieved_fps, 2),
                "width": source.size[0],
                "height": source.size[1],
            },
            "processing": {
                "model": VISION_MODEL,
                "endpoint": GENIEX_URL,
                "mode": "live",
                "compute_requested": self.compute,
                "compute_verified": None,
                "compute_note": compute_note(self.compute),
                "analysis_s": round(elapsed, 1),
                "real_time_ratio": 1.0,
                **stats,
                **extra,
            },
            "settings": Settings(
                vision_interval_s=self.vision_every, motion_threshold=self.threshold
            ).as_dict(),
            "samples": self.samples,
            "frames": self.frames,
            "rejected": None,
            # The per-frame motion the live run actually measured. The player
            # must render THESE, not a track recomputed from the recording:
            # the board was signalled from this one, and a recording that
            # dropped frames under load would recompute to something slightly
            # different. Two tracks means the page and the hardware can disagree
            # about the same drive, which is the bug review.json exists to stop.
            "readings": [
                [round(t, 3), round(score, 3), bool(stopped)]
                for t, score, stopped in self.readings
            ],
            "motion_points": [
                [round(t, 2), round(sc, 3)] for t, sc, _ in self.readings[::3]
            ],
        }
        path = self.out_dir / "timeline.json"
        path.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
        return timeline, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("output/live"))
    parser.add_argument("--device", default=None, help="camera name; default is the first")
    parser.add_argument("--list-cameras", action="store_true")
    parser.add_argument("--vision-every", type=float, default=VISION_INTERVAL_S)
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD)
    parser.add_argument("--rotate", type=int, default=None, choices=[0, 90, 180, 270])
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="end the drive on its own after this long",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="skip the MP4. The report still builds; the player has nothing to show.",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="do not serve the camera preview page",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="serve the preview but do not open a browser at it",
    )
    parser.add_argument("--unoq", action="store_true", help="signal the board live")
    parser.add_argument("--compute", default=None)
    args = parser.parse_args()

    if args.list_cameras:
        found = list_cameras()
        print("\n".join(found) if found else "no cameras found")
        return 0

    replay = None
    if args.unoq:
        from unoq_mcp import EventReplay, UnoQ

        board = UnoQ()
        if not board.connect():
            print(f"  could not reach the board: {board.last_error}", file=sys.stderr)
            print("  carrying on without it - the analysis does not need it.\n")
        else:
            replay = EventReplay(board)
            replay.reset()

    drive = LiveDrive(
        out_dir=args.out,
        device=args.device,
        vision_every=args.vision_every,
        threshold=args.threshold,
        rotate=args.rotate,
        max_seconds=args.max_seconds,
        record=not args.no_record,
        replay=replay,
        preview=not args.no_preview,
        open_preview=not args.no_open,
        compute=args.compute,
    )

    def on_sigint(_sig, _frame):
        print("\n  ending the drive...")
        drive.request_stop()

    signal.signal(signal.SIGINT, on_sigint)

    try:
        timeline, path = drive.run()
    except CameraLost as exc:
        # Worth its own branch: this is almost always a second live run in
        # another window, and saying so saves a hunt through a PyAV traceback.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except (RuntimeError, KeyboardInterrupt) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    proc = timeline["processing"]
    print(f"\n  {len(timeline['samples'])} samples over {timeline['drive']['duration_s']:.1f}s")
    print(f"  camera delivered {proc['camera_fps']:.1f} fps")
    print(f"  vision calls: {proc.get('calls', 0)}, failed: {proc.get('failed', 0)}")
    if proc.get("warning_lag_s"):
        print(
            f"  a confirmed warning was ~{proc['warning_lag_s']}s behind the event "
            "(one model call + one interval to confirm)"
        )
    if proc.get("samples_missed"):
        print(
            f"  {proc['samples_missed']} sample slot(s) skipped - the model was "
            "still busy when the next one fell due. Try --vision-every 4."
        )
    if proc.get("recording_dropped"):
        print(
            f"  {proc['recording_dropped']} frame(s) dropped from the recording "
            "under load - the analysis saw them, the replay will not."
        )
    print(f"  wrote {path}")

    if not timeline["samples"]:
        print("\n  no vision samples landed - nothing to build a review from.")
        return 1

    video = drive.out_dir / "drive.mp4"
    if video.is_file():
        print("\n  build the player and report:")
        print(
            f"    python tools/make_player.py --video {video} "
            f"--timeline {path} --out {drive.out_dir / 'player.html'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
