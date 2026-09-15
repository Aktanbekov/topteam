"""Fast layer: is the car moving, or stopped?

Runs on every frame. Plain frame differencing on a small greyscale image - no
model, no NPU, microseconds per frame.

The score is the frame-to-frame difference divided by the frame's own spatial
detail. That ratio matters: a raw difference depends on how textured the scene
is, so a bland scene (empty road, overcast sky) scores low even at speed and
gets misread as stopped. Dividing by the spatial gradient cancels that out, and
leaves a number that approximates how many pixels the scene shifted - which is
what we actually want to know.

What this does NOT do: distinguish "our car is stopped" from "our car is moving
and the scene happens to be blank". Nor does it give real speed. It answers one
question - has the picture gone still - and that is exactly what the rolling-stop
state machine needs.

MOTION_THRESHOLD needs calibrating against real footage; the default is tuned on
the synthetic test clip. Run test_video.py on a real clip and look at where the
scores actually sit before trusting it.
"""

from collections import deque

import numpy as np

# Below this shift-in-pixels-per-frame, we call the picture still.
MOTION_THRESHOLD = 0.25

# Guards against dividing by zero on a completely flat frame.
MIN_TEXTURE = 0.5

# Smoothing window. Single frames are noisy (compression, a passing car), so we
# judge on a short rolling median instead of one reading.
SMOOTH_FRAMES = 5


class EgoMotion:
    """Tracks whether the camera - and so the car - is moving."""

    def __init__(self, threshold=MOTION_THRESHOLD, smooth=SMOOTH_FRAMES):
        self.threshold = threshold
        self._recent = deque(maxlen=smooth)
        self._prev = None

    def update(self, gray):
        """Feed the next small greyscale frame. Returns the smoothed score."""
        if self._prev is None:
            self._prev = gray.astype(np.int16)
            return None

        current = gray.astype(np.int16)

        temporal = float(np.abs(current - self._prev).mean())
        # Horizontal gradient stands in for "how much detail is in this frame".
        spatial = float(np.abs(np.diff(current, axis=1)).mean())
        score = temporal / max(spatial, MIN_TEXTURE)

        self._prev = current

        self._recent.append(score)
        return self.score

    @property
    def score(self):
        """Smoothed motion score, or None before we have any readings."""
        if not self._recent:
            return None
        return float(np.median(self._recent))

    @property
    def is_stopped(self):
        """True when the picture has gone still. None until warmed up."""
        score = self.score
        if score is None:
            return None
        return score < self.threshold

    @property
    def ready(self):
        """True once the smoothing window is full, so readings can be trusted."""
        return len(self._recent) == self._recent.maxlen
