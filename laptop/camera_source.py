"""Read live frames from a camera, with the same shape as a video file.

    from camera_source import CameraSource, list_cameras
    with CameraSource() as cam:
        for frame in cam.frames():
            ...   # frame.t, frame.rgb, frame.gray - exactly like VideoSource

This is the sibling of video_source.VideoSource, not a replacement for it. A
recorded drive and a live one produce the same Frame, so every layer above -
ego motion, the vision sampler, the state machine - is untouched by which one
it is reading.

No OpenCV here either. PyAV's ffmpeg has the DirectShow input compiled in
(verified on this laptop: `dshow YES`), so `av.open(..., format="dshow")` opens
the webcam through the same API that opens an .mp4.

TWO THINGS DIFFER FROM A FILE, and both are the reason this is a separate class
rather than a flag on the old one:

1. **Time is wall-clock, not container timestamps.** A file has an authoritative
   presentation time for every frame. A camera does not: if we fall behind, the
   honest reading is that real seconds passed while we were not looking, not
   that the drive was shorter. `t` is therefore measured from the first frame
   with perf_counter, so a stall shows up as a gap in the samples - which is
   true - rather than silently compressing the timeline.

2. **Frames are dropped, never queued.** A live source that buffers is just a
   slow recording with extra latency, and the latency grows without bound. We
   keep ffmpeg's real-time buffer small so the driver drops old frames at the
   source, and we report the fps we actually achieved rather than the one we
   asked for.
"""

import time

import av
import av.logging
import numpy as np

from video_source import MOTION_H, MOTION_W, Frame

# What we ask the camera for. 720p rather than the sensor's maximum on purpose:
# every extra pixel is paid for three times over - decode, the JPEG we send the
# vision model, and the optional recording - and the model sees a 640x480-ish
# frame regardless. Cameras negotiate, so the size we get back may differ; the
# achieved size is read from the stream, never assumed.
DEFAULT_SIZE = (1280, 720)
DEFAULT_FPS = 30

# ffmpeg's capture buffer. Small on purpose - see the note above about dropping
# rather than queueing. Big enough for a few frames of jitter, too small to
# accumulate a backlog we would then be presenting as "live".
RTBUFSIZE = "16M"


class CameraLost(RuntimeError):
    """The camera went away mid-drive, almost always because it was taken.

    Its own type because the caller wants to tell it apart from a camera that
    could never be opened: one means "close the other window", the other means
    "check your privacy settings".
    """


def list_cameras():
    """Names of the DirectShow video devices, in the order ffmpeg reports them.

    ffmpeg has no clean enumeration API - it prints the list while failing to
    open a device called "dummy" - so that is what this does, and it parses the
    log rather than the exception.
    """
    av.logging.set_level(av.logging.INFO)
    lines = []
    with av.logging.Capture(local=False) as logs:
        try:
            av.open("dummy", format="dshow", options={"list_devices": "true"})
        except Exception:  # noqa: BLE001 - failing IS how the list is produced
            pass
    for _level, _name, message in logs:
        lines.append(message.rstrip())

    names = []
    text = "\n".join(lines)
    # Device lines look like:  "QC Front Camera"\n (video\n)
    # and are followed by an indented "Alternative name" line we do not want.
    for block in text.split('"')[1::2]:
        if block.startswith("@device"):
            continue
        after = text.split(f'"{block}"', 1)[1][:40]
        if "video" in after:
            names.append(block)
    # Keep the first occurrence of each, order preserved.
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


class CameraSource:
    """Live frames from a webcam, yielded like VideoSource yields a file.

    `device` is a DirectShow name from list_cameras(); None picks the first.
    `max_seconds` stops the generator on its own, which is what keeps an
    unattended demo from filling a disk.
    """

    def __init__(
        self,
        device=None,
        size=DEFAULT_SIZE,
        fps=DEFAULT_FPS,
        rotate=None,
        max_seconds=None,
    ):
        if device is None:
            found = list_cameras()
            if not found:
                raise RuntimeError(
                    "no camera found. Check Settings > Privacy & security > "
                    "Camera, and that nothing else is using it."
                )
            device = found[0]

        self.device = device
        self.max_seconds = max_seconds
        self.rotate = 0 if rotate is None else rotate % 360
        if self.rotate % 90:
            raise ValueError(f"rotate must be a multiple of 90, got {rotate}")

        options = {
            "video_size": f"{size[0]}x{size[1]}",
            "framerate": str(fps),
            "rtbufsize": RTBUFSIZE,
        }
        try:
            self._container = av.open(f"video={device}", format="dshow", options=options)
        except av.FFmpegError as exc:
            raise RuntimeError(
                f"could not open the camera {device!r}: {exc}\n"
                "Another application may be holding it, or camera access is "
                "blocked in Windows privacy settings."
            ) from exc

        try:
            self.stream = self._container.streams.video[0]
        except IndexError:
            self._container.close()
            raise RuntimeError(f"{device!r} exposes no video stream") from None

        self.stream.thread_type = "AUTO"

        self.started_at = None
        self.frames_seen = 0

    # ------------------------------------------------------------- metadata
    # Named to match VideoSource so callers do not have to know which they hold.
    @property
    def size(self):
        return (self.stream.codec_context.width, self.stream.codec_context.height)

    @property
    def fps(self):
        """The rate the camera negotiated, which may not be the one we asked for."""
        rate = self.stream.average_rate or self.stream.guessed_rate
        return float(rate) if rate else 0.0

    @property
    def achieved_fps(self):
        """Frames per second we actually got through, measured.

        This is the number worth reporting: `fps` is what the camera claims it
        is sending, this is what survived our loop.
        """
        if not self.started_at or self.frames_seen < 2:
            return 0.0
        elapsed = time.perf_counter() - self.started_at
        return (self.frames_seen - 1) / elapsed if elapsed > 0 else 0.0

    @property
    def elapsed_s(self):
        """Seconds since the first frame. A live drive has no known duration."""
        if not self.started_at:
            return 0.0
        return time.perf_counter() - self.started_at

    @property
    def looks_portrait(self):
        w, h = self.size
        return h > w

    # --------------------------------------------------------------- frames
    def frames(self, stride_s=0.0):
        """Yield Frames until the camera stops or max_seconds is reached.

        `stride_s` is accepted for parity with VideoSource. Skipping frames here
        would break the motion layer, which needs consecutive frames to difference
        - so a stride only ever thins what is yielded, never what is decoded.
        """
        next_t = 0.0

        # The decode has to be stepped by hand rather than driven by a for-loop.
        # Only ONE process can hold the camera, so a second ./run.sh --live - or
        # Teams, or the Camera app - opens the device happily and then fails on
        # the first read. An I/O error at that point is not a bug in the drive,
        # it is somebody else holding the webcam, and the message should say so
        # instead of printing a PyAV traceback.
        decoder = self._container.decode(self.stream)
        while True:
            try:
                av_frame = next(decoder)
            except StopIteration:
                return
            except av.FFmpegError as exc:
                raise CameraLost(
                    f"lost the camera {self.device!r}: {exc}. Another program is "
                    "almost certainly holding it - a second ./run.sh --live, "
                    "Teams, or the Camera app. Only one can read it at a time."
                ) from exc

            now = time.perf_counter()
            if self.started_at is None:
                self.started_at = now
            t = now - self.started_at
            self.frames_seen += 1

            if self.max_seconds is not None and t > self.max_seconds:
                return

            if stride_s and t < next_t:
                continue
            next_t = t + stride_s

            gray = av_frame.reformat(
                width=MOTION_W, height=MOTION_H, format="gray"
            ).to_ndarray()

            rgb = av_frame.to_ndarray(format="rgb24")
            if self.rotate:
                rgb = np.rot90(rgb, k=self.rotate // 90)

            yield Frame(t=t, rgb=rgb, gray=gray)

    def close(self):
        self._container.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
