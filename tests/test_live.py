"""The live path, tested with no camera, no model and no board.

Live mode reuses the whole decision layer - it feeds build_review the growing
prefix of a drive instead of a finished timeline - so the rules themselves are
already covered by the other files here. What is new, and what these tests pin
down, is everything around that reuse:

  * the invariants still hold when the drive is decided a piece at a time,
  * a live run says it is live and says how far behind it is,
  * and the parts that exist only because a camera is not a file - refusing to
    queue a vision call, dropping recorded frames rather than blocking, keeping
    a ring of stills that can no longer be seeked for - behave under load.

The one thing not tested here is the camera itself. Opening a webcam in a unit
test would make the suite depend on hardware, a driver and a privacy setting,
and the point of this suite is that it runs in a second with the board in a
drawer.
"""

import time

import fixtures
import pytest
from drive_review import build_review, limitations
from live_drive import EvidenceRing, VisionWorker, level_now

LIVE_PROCESSING = {**fixtures.PROCESSING, "mode": "live", "median_s": 3.47}


def live_review(grade, **over):
    """A review built the way live mode builds one."""
    samples, readings = fixtures.drive(grade)
    return build_review(
        samples=samples,
        readings=readings,
        drive={**fixtures.DRIVE_META, "source": "live"},
        processing={**LIVE_PROCESSING, **over.pop("processing", {})},
        settings=fixtures.SETTINGS,
        frames=[],
        motion_points=[[t, s] for t, s, _ in readings[::3]],
        spacing=3.0,
        **over,
    )


# ------------------------------------------------------------ the invariants
def test_a_live_run_never_signals_a_failure_before_its_deadline():
    """Invariant 1, applied one sample at a time.

    This is the live version of the rule that matters most. Deciding the drive
    incrementally is exactly the situation where a level could leak out early -
    the sign has gone, the car has not stopped, and it is tempting to call it.
    Nothing may reach level 3 before decision_at, no matter how the samples are
    fed in.
    """
    samples, readings = fixtures.drive("fail")

    deadline = None
    for n in range(1, len(samples) + 1):
        prefix = samples[:n]
        now = prefix[-1]["t"]
        upto = [r for r in readings if r[0] <= now] or readings[:1]

        review = build_review(
            samples=prefix,
            readings=upto,
            drive={**fixtures.DRIVE_META, "source": "live"},
            processing=LIVE_PROCESSING,
            settings=fixtures.SETTINGS,
            motion_points=[],
            spacing=3.0,
        )
        critical = [e for e in review["events"] if e["severity"] == "critical"]
        if critical:
            deadline = min(e["decision_at"] for e in critical)

        _index, level = level_now(review["levels"], now)
        if deadline is None or now < deadline:
            assert level < 3, (
                f"level {level} at {now}s, before anything was decided"
            )

    assert deadline is not None, "the fail fixture should reach a critical verdict"


def test_a_live_run_is_labelled_live():
    """The converse of 'a replay is never shown as live'.

    A page that cannot tell the two apart is a page that can overstate either
    direction, and this is the direction a judge would catch with a stopwatch.
    """
    review = live_review("fail")
    assert review["drive"]["source"] == "live"
    assert review["processing"]["mode"] == "live"


def test_a_live_run_states_how_far_behind_it_is():
    notes = " ".join(limitations(LIVE_PROCESSING, fixtures.SETTINGS)).lower()
    assert "behind the event" in notes
    # It must not inherit the recorded-drive wording, which would be a plain
    # contradiction on a live page.
    assert "post-drive analysis, not live detection" not in notes


def test_a_live_run_does_not_promise_a_timely_red_light_warning():
    """The lag is survivable for stop signs and not for red lights.

    Saying so is the difference between a scoped claim and an overstated one,
    and it is the first thing that falls apart if someone later 'simplifies'
    the limitation list.
    """
    notes = " ".join(limitations(LIVE_PROCESSING, fixtures.SETTINGS)).lower()
    assert "red light" in notes
    assert "never presented as a warning" in notes


def test_a_recorded_run_still_says_it_is_post_drive():
    """Adding live mode must not quietly reword the file path."""
    notes = " ".join(limitations(fixtures.PROCESSING, fixtures.SETTINGS)).lower()
    assert "post-drive analysis, not live detection" in notes
    assert "behind the event" not in notes


@pytest.mark.parametrize("grade", ["pass", "brief", "fail"])
def test_no_official_dmv_claim_in_a_live_review(grade):
    """Invariant 4 holds on the live path too.

    Same phrase list as test_no_official_dmv_claim_anywhere, deliberately: the
    point is that going live does not get to relax the rule. It also checks the
    disclaimer survives, since that sentence is the one carrying the denial.
    """
    import json

    text = json.dumps(live_review(grade)).lower()
    for phrase in ("dmv pass", "dmv fail", "you passed", "you failed", "test result"):
        assert phrase not in text, f"found {phrase!r} in a live review"
    assert "not a dmv result" in text


# ------------------------------------------------------- the live-only parts
def test_the_level_track_is_read_at_the_right_moment():
    levels = [[0.0, 0, "clear"], [3.0, 1, "stop_sign"], [16.0, 3, "critical"]]
    assert level_now(levels, -1.0) == (-1, 0)  # before anything, stay calm
    assert level_now(levels, 0.0) == (0, 0)
    assert level_now(levels, 2.9) == (0, 0)
    assert level_now(levels, 3.0) == (1, 1)
    assert level_now(levels, 15.9) == (1, 1)
    assert level_now(levels, 99.0) == (2, 3)


def test_a_vision_call_is_refused_rather_than_queued():
    """Queueing would label an old frame and present it as current.

    That is the one failure mode a live path must not have: the answer would be
    right about a moment that has already gone, and wrong about now.
    """

    class SlowVision:
        def describe(self, rgb):
            time.sleep(0.3)
            return {"stop_sign": False}, None

    worker = VisionWorker(SlowVision())
    assert worker.submit(0.0, None) is True
    assert worker.submit(0.1, None) is False, "a second call must be refused"
    assert worker.submit(0.2, None) is False
    assert worker.refused == 2

    deadline = time.time() + 5
    while worker.busy and time.time() < deadline:
        time.sleep(0.01)

    done = worker.poll()
    assert len(done) == 1, "only the accepted frame should produce an answer"
    assert done[0][0] == 0.0


def test_a_failed_vision_call_does_not_end_the_drive():
    class BrokenVision:
        def describe(self, rgb):
            raise ConnectionError("geniex went away")

    worker = VisionWorker(BrokenVision())
    worker.submit(1.0, None)
    deadline = time.time() + 5
    while worker.busy and time.time() < deadline:
        time.sleep(0.01)

    (t, scene, error) = worker.poll()[0]
    assert t == 1.0
    assert scene is None
    assert "geniex went away" in error


def test_the_evidence_ring_keeps_a_bounded_window():
    """A camera cannot be seeked, so the still has to have been kept.

    An event is decided up to ten seconds after its first sighting, so the ring
    has to still hold that moment - and must not grow without limit while it
    does.
    """
    import numpy as np

    ring = EvidenceRing(seconds=5.0, every=0.5, quality=40)
    tiny = np.zeros((16, 16, 3), dtype=np.uint8)

    for i in range(60):  # 30 seconds at 2Hz
        ring.offer(i * 0.5, tiny)

    assert len(ring._items) <= 12, "the ring grew past its window"
    oldest = ring._items[0][0]
    assert oldest >= 29.5 - 5.0, "the ring kept less than its window"

    found = ring.nearest(27.2)
    assert found is not None
    assert abs(found[0] - 27.2) <= 0.5


def test_the_evidence_ring_thins_to_its_interval():
    """Every frame offered must not become a stored JPEG."""
    import numpy as np

    ring = EvidenceRing(seconds=100.0, every=0.5, quality=40)
    tiny = np.zeros((16, 16, 3), dtype=np.uint8)

    for i in range(300):  # 10 seconds at 30fps
        ring.offer(i / 30.0, tiny)

    assert len(ring._items) == 20, "expected one still every 0.5s over 10s"


def test_the_confirmation_window_follows_the_real_cadence():
    """Measured, not requested - otherwise live detection fails silently.

    A vision call takes ~3.5s, so asking for a sample every 3s yields samples
    about 3.9s apart. scene_filter only calls two samples neighbours within
    spacing * 1.6, so feeding it the requested 3.0s (window 4.8s) leaves almost
    no headroom: one slow call and every detection is rejected as unconfirmed,
    which reads on the page as a drive where nothing happened.
    """
    from live_drive import achieved_spacing

    # What a real run looks like: asked for 3s, got 3.9s.
    real = [{"t": t} for t in (0.2, 4.1, 7.9, 11.7, 15.7, 19.6, 23.5)]
    assert achieved_spacing(real, 3.0) == pytest.approx(3.9, abs=0.05)

    # Fast samples must never tighten the window below what was asked for.
    fast = [{"t": t} for t in (0.0, 1.0, 2.0, 3.0)]
    assert achieved_spacing(fast, 3.0) == 3.0

    # Degenerate inputs fall back rather than dividing by nothing.
    assert achieved_spacing([], 3.0) == 3.0
    assert achieved_spacing([{"t": 1.0}], 3.0) == 3.0


def test_a_real_cadence_keeps_neighbouring_samples_adjacent():
    """The point of the above, stated as the thing that would actually break."""
    from scene_filter import _adjacent

    a, b = {"t": 0.2}, {"t": 4.1}
    assert not _adjacent(a, b, 2.0), "a too-tight window should reject these"
    assert _adjacent(a, b, 3.9), "the measured cadence must keep them adjacent"


# ------------------------------------------------------------------ preview
def test_the_preview_serves_the_page_the_status_and_the_stream():
    """Without this a live drive is invisible and looks broken.

    It is an MJPEG stream of frames we have ALREADY decoded rather than a page
    calling getUserMedia, because the camera can only be opened once - a second
    reader in the browser would fight the analysis for the device.
    """
    import json
    import urllib.request

    import numpy as np
    from live_preview import LiveState, PreviewServer

    state = LiveState()
    server = PreviewServer(state, port=0)
    url = server.start()
    assert url, "the preview should bind on a free port"
    try:
        state.put_frame(np.zeros((48, 64, 3), dtype=np.uint8))
        state.put_status(t=4.1, motion=0.05, stopped=True, scene="STOP SIGN", level=1)

        with urllib.request.urlopen(url + "/", timeout=5) as r:
            assert r.status == 200
            assert b"<img src=\"/stream\"" in r.read()

        with urllib.request.urlopen(url + "/status", timeout=5) as r:
            status = json.loads(r.read())
        assert status == {
            "t": 4.1, "motion": 0.05, "stopped": True,
            "scene": "STOP SIGN", "level": 1,
        }

        stream = urllib.request.urlopen(url + "/stream", timeout=5)
        try:
            chunk = stream.read(2000)
            assert b"--frame" in chunk
            assert b"\xff\xd8\xff" in chunk, "no JPEG in the stream"
        finally:
            stream.close()
    finally:
        server.close()


def test_a_busy_preview_port_does_not_stop_the_drive():
    """A missing convenience must never cost you the analysis."""
    from live_preview import LiveState, PreviewServer

    first = PreviewServer(LiveState(), port=0)
    url = first.start()
    assert url
    try:
        clash = PreviewServer(LiveState(), port=first.port)
        assert clash.start() is None, "a taken port should return None, not raise"
    finally:
        first.close()


def test_the_capture_thread_only_ever_assigns():
    """The drive hands over a reference; the encode happens on the server.

    If the capture thread did the JPEG work, a browser watching the preview
    would cost the motion layer frames - and the motion layer is the evidence.
    """
    import numpy as np
    from live_preview import LiveState

    state = LiveState()
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    state.put_frame(img)
    assert state.get_frame() is img, "the frame should be stored, not copied"


def test_a_lost_camera_names_the_likely_cause():
    """The error a second live run actually produces, caught by name.

    Regression guard: CameraLost was raised correctly but not imported into
    live_drive, so the handler for it crashed with a NameError instead of
    printing the message. Found by running two live drives at once.
    """
    import live_drive
    from camera_source import CameraLost

    assert live_drive.CameraLost is CameraLost
    assert issubclass(CameraLost, RuntimeError)


def test_every_cli_flag_reaches_the_constructor():
    """Caught a real crash twice: main() passed a kwarg LiveDrive did not take.

    Nothing else notices - the parser accepts the flag, the drive warms the
    model, opens the camera, and only then dies with a TypeError. By that point
    you have spent twenty seconds and turned the webcam on. A signature check
    costs nothing and fails in milliseconds.
    """
    import inspect

    from live_drive import LiveDrive

    accepted = set(inspect.signature(LiveDrive.__init__).parameters)
    for name in (
        "out_dir", "device", "vision_every", "threshold", "rotate",
        "max_seconds", "record", "replay", "preview", "open_preview", "compute",
    ):
        assert name in accepted, f"LiveDrive does not accept {name!r}"


def test_the_preview_defaults_to_on():
    """A live drive that draws nothing looks broken, so watching is the default.

    --no-preview and --no-open exist for the unattended case; neither should be
    something you have to know about to see yourself on screen.
    """
    import inspect

    from live_drive import LiveDrive

    params = inspect.signature(LiveDrive.__init__).parameters
    assert params["preview"].default is True
    assert params["open_preview"].default is True


# ------------------------------------------------- the whole drive, no camera
class FakeCamera:
    """A scripted stand-in for CameraSource, same surface, no webcam."""

    device = "fake camera"
    size = (64, 48)
    fps = 30.0
    achieved_fps = 30.0
    looks_portrait = False

    def __init__(self, seconds=6.0, fps=30.0):
        self.seconds = seconds
        self._fps = fps

    def frames(self, stride_s=0.0):
        import numpy as np

        from video_source import Frame

        n = int(self.seconds * self._fps)
        for i in range(n):
            t = i / self._fps
            # Changing pixels, so the motion layer has something to difference.
            rgb = np.full((48, 64, 3), (i * 7) % 255, dtype=np.uint8)
            gray = np.full((120, 160), (i * 7) % 255, dtype=np.uint8)
            # The frame carries its own timestamp in one pixel, so FakeVision can
            # answer differently over the drive without sharing mutable state
            # with the capture thread - which would be a race, and a flaky test.
            rgb[0, 0, 0] = min(255, int(t))
            yield Frame(t=t, rgb=rgb, gray=gray)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeVision:
    """SceneVision's surface, answering instantly.

    By default it sees a stop sign in every frame. Real approaches do not work
    like that - the sign LEAVES the frame before the car reaches the line, which
    is what opens the ten-second window the verdict is due at the end of. Pass a
    `sign_until` to model that, or the approach never closes and no failure is
    ever decided.
    """

    def __init__(self, sign_until=None):
        self.calls = 0
        self.sign_until = sign_until

    def health(self):
        return True

    def describe(self, rgb):
        from scene_vision import EMPTY_SCENE

        self.calls += 1
        t = int(rgb[0, 0, 0])  # stamped by FakeCamera
        sign = self.sign_until is None or t < self.sign_until
        return {**EMPTY_SCENE, "stop_sign": sign}, None

    def stats(self):
        return {
            "calls": self.calls, "failed": 0, "first_call_s": 0.1,
            "median_s": 0.1, "p95_s": 0.1, "min_s": 0.1, "max_s": 0.1,
        }

    def reset_stats(self):
        self.calls = 0


def test_a_whole_live_drive_runs_with_no_camera_and_no_model(tmp_path):
    """Execute run() end to end. This is the test that was missing.

    Three bugs shipped in a row - `webbrowser` never imported, `open_preview`
    missing from the constructor, `CameraLost` caught by a name that was not
    imported - and every one of them was invisible until run() had warmed the
    model and switched the webcam on. None of them could survive this test,
    which takes under a second and touches no hardware.
    """
    import live_drive
    from live_drive import LiveDrive

    # Port 0, not the real 8008: a live drive running on this machine must not
    # be able to fail the suite. That is exactly what happened the first time.
    drive = LiveDrive(out_dir=tmp_path, vision_every=1.0, record=False,
                      quiet=True, preview_port=0)
    drive.open_source = lambda: FakeCamera(seconds=6.0)
    drive.open_vision = FakeVision

    # Leave open_preview ON and stub the browser call instead. Disabling it here
    # would skip the very line whose missing import started all this - a test
    # that routes around the bug it was written for is worse than no test.
    opened = []
    real_open = live_drive.webbrowser.open
    live_drive.webbrowser.open = opened.append
    try:
        timeline, path = drive.run()
    finally:
        live_drive.webbrowser.open = real_open

    assert opened, "the drive should have opened the preview in a browser"
    assert opened[0].startswith("http://127.0.0.1:")

    assert path.is_file(), "the drive should write its timeline"
    assert timeline["drive"]["source"] == "live"
    assert timeline["processing"]["mode"] == "live"
    assert timeline["samples"], "no vision samples landed"
    assert timeline["readings"], "no motion readings recorded"
    # A stop sign in every sample must produce a validated approach downstream.
    assert all(s["scene"]["stop_sign"] for s in timeline["samples"])


def test_the_live_drive_survives_a_camera_that_dies_mid_drive(tmp_path):
    """A webcam taken by another program must not lose the drive so far."""
    from camera_source import CameraLost
    from live_drive import LiveDrive

    class DyingCamera(FakeCamera):
        def frames(self, stride_s=0.0):
            for i, frame in enumerate(super().frames(stride_s)):
                if i > 60:
                    raise CameraLost("someone else took the camera")
                yield frame

    drive = LiveDrive(out_dir=tmp_path, vision_every=0.5, record=False, quiet=True)
    drive.open_source = lambda: DyingCamera(seconds=10.0)
    drive.open_vision = FakeVision
    drive.open_preview = False

    with pytest.raises(CameraLost):
        drive.run()
    # The readings gathered before it died are still in hand, not thrown away.
    assert drive.readings, "the drive discarded everything it had measured"


def test_a_live_drive_signals_the_board_as_it_goes(tmp_path):
    """The whole point of --unoq: levels reach the board DURING the drive.

    Worth pinning deterministically. Pointing a webcam at a room for 25 seconds
    exercises none of this - the level never leaves 0 - so a broken live-to-board
    path would look exactly like a quiet drive.
    """
    from live_drive import LiveDrive

    class RecordingReplay:
        def __init__(self):
            self.applied = []

        def reset(self):
            self.applied.clear()
            return True

        def apply(self, level, seq):
            self.applied.append((level, seq))
            return "sent"

    board = RecordingReplay()
    drive = LiveDrive(
        out_dir=tmp_path, vision_every=1.0, record=False, quiet=True,
        replay=board, preview=False,
    )
    # A stop sign in every sample, and the car never stops - so the approach is
    # validated and then fails at its deadline, which is the level 3 case.
    # Sign visible for the first 8s, gone after - so the window closes at ~18s
    # and the drive runs past it. A sign that never leaves keeps the approach
    # open forever and no verdict is ever due.
    drive.open_source = lambda: FakeCamera(seconds=30.0)
    drive.open_vision = lambda: FakeVision(sign_until=8)

    drive.run()

    assert board.applied, "nothing was ever sent to the board"
    levels = [level for level, _seq in board.applied]
    assert 1 in levels, f"a confirmed stop sign should raise level 1, got {levels}"
    assert 3 in levels, f"a failed approach should reach level 3, got {levels}"

    # Monotone: a live drive only moves forward, so the sequence numbers must too.
    seqs = [seq for _level, seq in board.applied]
    assert seqs == sorted(seqs), f"sequence went backwards: {seqs}"


def test_the_board_is_never_told_level_3_before_the_deadline(tmp_path):
    """Invariant 1 on the wire, not just in the review.

    The review can be right and the hardware still wrong if the tick reads the
    level track at the wrong moment. This checks what the board was actually
    sent, in order.
    """
    from live_drive import LiveDrive

    sent = []

    class Watcher:
        def reset(self):
            return True

        def apply(self, level, seq):
            sent.append(level)
            return "sent"

    drive = LiveDrive(
        out_dir=tmp_path, vision_every=1.0, record=False, quiet=True,
        replay=Watcher(), preview=False,
    )
    drive.open_source = lambda: FakeCamera(seconds=30.0)
    drive.open_vision = lambda: FakeVision(sign_until=8)
    drive.run()

    assert 3 in sent, f"the drive never reached a critical verdict: {sent}"
    first_three = sent.index(3)
    assert all(level < 3 for level in sent[:first_three])
    # and it must have warned before it accused
    assert 1 in sent[:first_three], "the board went to critical with no heads-up first"


def test_the_drive_says_what_the_board_was_told(tmp_path, capsys):
    """A silent hardware path is unanswerable after the fact.

    "The board did nothing" has three possible causes that look identical from
    the outside: no level was ever due, one was sent and missed, or the send
    failed. Printing every transition tells them apart in the log, which is the
    only record that survives a live drive.
    """
    from live_drive import LiveDrive

    class FlakyBoard:
        def __init__(self):
            self.last_error = "board went away"
            self.calls = 0

        def reset(self):
            return True

        def apply(self, level, seq):
            self.calls += 1
            return "failed" if level == 3 else "sent"

    drive = LiveDrive(out_dir=tmp_path, vision_every=1.0, record=False,
                      quiet=True, replay=FlakyBoard(), preview=False)
    drive.open_source = lambda: FakeCamera(seconds=30.0)
    drive.open_vision = lambda: FakeVision(sign_until=8)
    drive.run()

    out = capsys.readouterr().out
    assert "-> board: level 1" in out, "a sent level was not reported"
    assert "AMBER" in out
    assert "FAILED" in out, "a failed send was not reported"
    assert "board went away" in out, "the reason was not reported"
