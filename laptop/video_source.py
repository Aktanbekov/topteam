"""Read a dashcam video frame by frame.

Uses PyAV rather than OpenCV: opencv-python has no Windows ARM64 wheel and will
not build on this laptop. PyAV wraps ffmpeg and installs cleanly.

Each frame arrives with two views of the same picture:
  .rgb   full resolution, for the vision model and report screenshots
  .gray  small greyscale, for the fast ego-motion layer

ffmpeg does the downscale and colour conversion, which is much faster than
doing it in numpy afterwards.
"""

from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np

# Small enough that frame differencing is cheap, large enough to still show
# road texture moving past.
MOTION_W, MOTION_H = 160, 120


@dataclass
class Frame:
    t: float  # seconds from the start of the clip
    rgb: np.ndarray  # (H, W, 3) uint8
    gray: np.ndarray  # (MOTION_H, MOTION_W) uint8


class VideoSource:
    """Sequential reader over a video file.

    Container format does not matter: .mp4, .mov and .mkv all work, and both
    H.264 and HEVC decode fine (tested 2026-09-15). What does matter is
    rotation. Phone footage carries a rotation flag that PyAV does NOT apply
    when decoding, so the frames can arrive sideways - which wrecks the vision
    model's reading of the scene. Pass rotate=90/180/270 to correct it.
    """

    def __init__(self, path, rotate=None):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"no such video: {self.path}")

        self._container = av.open(str(self.path))
        try:
            self.stream = self._container.streams.video[0]
        except IndexError:
            self._container.close()
            raise ValueError(f"{self.path} has no video stream") from None

        self.stream.thread_type = "AUTO"  # let ffmpeg use multiple cores
        self.rotate = self._resolve_rotation(rotate)

    def _resolve_rotation(self, rotate):
        """Pick a rotation: explicit argument wins, else the container's flag."""
        if rotate is not None:
            if rotate % 90:
                raise ValueError(f"rotate must be a multiple of 90, got {rotate}")
            return rotate % 360

        # Older QuickTime files expose it here. Newer ones use a display matrix
        # in side data, which PyAV 18 does not give us a getter for - hence the
        # portrait warning below and the manual override.
        tag = self.stream.metadata.get("rotate")
        if tag:
            try:
                return int(tag) % 360
            except ValueError:
                pass
        return 0

    @property
    def looks_portrait(self):
        """True if frames are taller than wide - usually a rotation problem."""
        w, h = self.size
        return h > w

    @property
    def fps(self):
        rate = self.stream.average_rate
        return float(rate) if rate else 0.0

    @property
    def duration_s(self):
        if self.stream.duration and self.stream.time_base:
            return float(self.stream.duration * self.stream.time_base)
        if self._container.duration:
            return self._container.duration / av.time_base
        return 0.0

    @property
    def size(self):
        return (self.stream.codec_context.width, self.stream.codec_context.height)

    def frames(self, stride_s=0.0):
        """Yield Frames, at most one every `stride_s` seconds (0 = every frame).

        Decoding stays sequential even when striding. Seeking would be faster
        on long files, but it lands on keyframes, which makes frame-to-frame
        motion comparisons meaningless.
        """
        next_t = 0.0
        time_base = self.stream.time_base

        for av_frame in self._container.decode(self.stream):
            if av_frame.pts is None:
                continue

            t = float(av_frame.pts * time_base)
            if stride_s and t < next_t:
                continue
            next_t = t + stride_s

            gray = av_frame.reformat(
                width=MOTION_W, height=MOTION_H, format="gray"
            ).to_ndarray()

            rgb = av_frame.to_ndarray(format="rgb24")
            if self.rotate:
                # Only the RGB needs correcting. The motion layer measures
                # texture change, which is orientation-independent, so leaving
                # the grey frame alone saves work and changes nothing.
                rgb = np.rot90(rgb, k=self.rotate // 90)

            yield Frame(t=t, rgb=rgb, gray=gray)

    def close(self):
        self._container.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
