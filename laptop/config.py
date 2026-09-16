"""One place for every number and name the pipeline shares.

Before this existed the vision interval was a default in analyze_video.py, the
motion threshold a constant in ego_motion.py, the post-sign window a constant in
make_player.py, and the model id was written out in three files. Changing one of
them meant remembering which of the others also had to move, and the review
page had no way to record what the run was actually configured with.

Nothing here changes the behaviour we measured: every default is the value the
project was already using. What changes is that there is now a single answer to
"what settings produced this review", and it travels in the review file.
"""

from dataclasses import asdict, dataclass

# ------------------------------------------------------------------- GenieX
GENIEX_URL = "http://127.0.0.1:18181/v1"
GENIEX_PORT = 18181

# Note the qualcomm/ prefix and the :W4A16 precision suffix - the API rejects
# the bare name.
VISION_MODEL = "qualcomm/Qwen3-VL-4B-Instruct:W4A16"

# Optional. Only used to rewrite the deterministic report in friendlier prose,
# never to decide anything. Not pulled on this laptop yet, which is exactly why
# nothing is allowed to depend on it.
REPORT_MODEL = "qualcomm/Qwen3-4B"

# What we ask geniex serve to run on. "npu" is the demo default; the server
# does not report back which unit it actually used, so the review file records
# this as *requested* and never claims it was verified. See compute_note().
DEFAULT_COMPUTE = "npu"
COMPUTE_UNITS = ("npu", "cpu", "gpu", "hybrid")

# ------------------------------------------------------------------- timing
# A warm vision call measured 2.7-3.8s on this laptop, so sampling faster than
# this just queues work up behind itself.
VISION_INTERVAL_S = 3.0

# ------------------------------------------------------------------- motion
# Shift-in-pixels-per-frame below which we call the picture still. Calibrated
# narrowly on IMG_9830: stopped tops out at 0.24, moving starts at 0.31. Do not
# tighten it without re-running tools/eval_motion.py on real footage.
MOTION_THRESHOLD = 0.25

# Ignore stationary blips shorter than this - one noisy frame is not a stop.
MIN_STOP_S = 0.4

# --------------------------------------------------------------- stop signs
# How long after a stop sign leaves the frame we keep looking for the stop.
# THE SIGN LEAVES THE FRAME BEFORE THE CAR REACHES THE LINE: on IMG_9830 the
# sign was gone by 63s and the stop happened at 64-65s. A detector that wants
# both in one frame flags every correct stop as a violation.
WINDOW_AFTER_S = 10.0

# How long a stop should last to count as a good one. California asks for a
# complete stop and puts no number on it; every instructor teaches "count to
# three". A shorter complete stop is legal, so it is coaching feedback and
# never a scored error.
FULL_STOP_S = 3.0

# Below this many sightings a stop sign is noise, not a junction. One frame is
# not enough to start accusing the driver of running a sign.
MIN_SIGN_SAMPLES = 2

# A sample or two without the sign does not end an approach - a tree or a van
# can hide it and then it reappears.
SIGN_GAP_TOLERANCE_S = 6.0

# How long the board holds the critical signal after a failed approach. Long
# enough to be unmistakable, short enough that the strip is green again before
# the next junction.
CRITICAL_HOLD_S = 4.0

# ------------------------------------------------------------------- ports
MCP_PORT = 3001
PLAYER_PORT = 8000
UNOQ_MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"

# ------------------------------------------------------------------ levels
# The laptop -> UNO Q protocol. Level 2 is defined and implemented on the board
# but no check currently produces it: a brief stop is coaching feedback and must
# not count as a DMV error, and following distance is experimental and unscored.
# laptop/test_signals.py is the way to exercise it. Saying that out loud beats
# inventing a strike so the counter moves on stage.
LEVEL_NAMES = {
    0: "driving fine",
    1: "heads up",
    2: "minor mistake",
    3: "critical mistake",
}


@dataclass(frozen=True)
class Settings:
    """The knobs one analysis run was configured with.

    Carried into review.json so a report can always say what produced it, and
    so a threshold change shows up as a different review rather than a mystery.
    """

    vision_interval_s: float = VISION_INTERVAL_S
    motion_threshold: float = MOTION_THRESHOLD
    min_stop_s: float = MIN_STOP_S
    window_after_s: float = WINDOW_AFTER_S
    full_stop_s: float = FULL_STOP_S
    min_sign_samples: int = MIN_SIGN_SAMPLES
    sign_gap_tolerance_s: float = SIGN_GAP_TOLERANCE_S
    critical_hold_s: float = CRITICAL_HOLD_S

    def as_dict(self):
        return asdict(self)


def compute_note(requested):
    """Honest wording for the compute target on the review page.

    geniex serve takes -c npu|cpu|gpu|hybrid but the OpenAI-compatible API does
    not report which unit a request actually ran on, so there is nothing to
    read back. We say what we asked for and say that we asked - never that we
    checked. A judge who spots an unverifiable "running on NPU" badge has found
    the one claim on the page we could not stand behind.
    """
    if not requested:
        return "server default (no compute unit requested)"
    return f"requested {requested} via geniex serve -c {requested}; not independently verified"
