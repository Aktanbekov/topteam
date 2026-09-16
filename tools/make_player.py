"""Build a self-contained review player for a analysed drive.

Combines three things into one HTML file you can open straight from disk:
  - the video itself
  - the dense per-frame motion track (recomputed here, it is fast)
  - the vision samples from analyze_video.py --json-out

    python tools/make_player.py --video IMG_9830.MOV --timeline output/timeline.json

The timeline data is baked into the HTML rather than fetched, because browsers
block fetch() from file:// URLs. The video is referenced by relative path, which
does work from file://.

This is also where the stop-sign state machine lives for now: for each stretch
where a stop sign was visible, it looks for a stop during the approach or in the
seconds after the sign leaves the frame, and calls it a stop or a possible
incomplete stop.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import av

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))

from ego_motion import MOTION_THRESHOLD, EgoMotion  # noqa: E402
from scene_filter import confirm, dropped  # noqa: E402
from test_video import find_stops  # noqa: E402
from video_source import VideoSource  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "report" / "player_template.html"

# How long after a stop sign leaves the frame we keep looking for the stop.
# Real footage: sign gone by 63s, stop at 64-65s. 10s gives comfortable margin.
DEFAULT_WINDOW_AFTER = 10.0

# How long a stop should last. California does not put a number on it - the law
# asks for a complete stop, full stop - but every instructor teaches "count to
# three", and a driver who is stationary for a moment has not really looked. So
# a stop shorter than this passes the legal test and still earns a coaching
# note; it is never counted as a DMV error.
DEFAULT_FULL_STOP_S = 3.0

# Below this many samples a stop-sign sighting is treated as noise rather than
# an approach. One frame is not enough to start accusing the driver of running
# a sign - see scene_filter for the same argument applied per field.
MIN_SIGN_SAMPLES = 2

# What browsers will actually decode. Note .mov is absent deliberately: Chrome
# and Edge routinely refuse a QuickTime file even when the video inside is
# ordinary H.264, which is exactly what IMG_9830.MOV is.
BROWSER_SAFE_CONTAINERS = {".mp4", ".m4v", ".webm"}
BROWSER_SAFE_CODECS = {"h264", "vp8", "vp9", "av1"}


def _remux(src, dst):
    """Rewrap the video into MP4 without touching the pixels."""
    with av.open(str(src)) as inp, av.open(str(dst), mode="w") as out:
        in_v = inp.streams.video[0]
        out_v = out.add_stream_from_template(in_v)
        for packet in inp.demux(in_v):
            if packet.dts is None:  # flush packets carry no timestamp
                continue
            packet.stream = out_v
            out.mux(packet)


def _transcode(src, dst):
    """Re-encode to H.264. Slow, but the only option for HEVC sources."""
    with av.open(str(src)) as inp, av.open(str(dst), mode="w") as out:
        in_v = inp.streams.video[0]
        out_v = out.add_stream("libx264", rate=in_v.average_rate or 30)
        out_v.width = in_v.codec_context.width
        out_v.height = in_v.codec_context.height
        out_v.pix_fmt = "yuv420p"
        out_v.options = {"crf": "23", "preset": "veryfast"}

        for frame in inp.decode(in_v):
            for packet in out_v.encode(frame):
                out.mux(packet)
        for packet in out_v.encode():
            out.mux(packet)


def ensure_playable(src, out_dir):
    """Return a video path the browser can decode, converting only if needed.

    The analysis always runs on the original file. This copy exists purely so
    the review page can play it back.
    """
    with av.open(str(src)) as probe:
        codec = probe.streams.video[0].codec_context.name

    if src.suffix.lower() in BROWSER_SAFE_CONTAINERS and codec in BROWSER_SAFE_CODECS:
        return src

    dst = out_dir / f"{src.stem}_web.mp4"
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        print(f"reusing existing {dst.name}")
        return dst

    if codec in BROWSER_SAFE_CODECS:
        print(f"{src.suffix} will not play in most browsers - rewrapping as MP4")
        print(f"  stream copy, no re-encoding, no quality loss -> {dst.name}")
        _remux(src, dst)
    else:
        print(f"{codec} will not play in most browsers - re-encoding to H.264")
        print(f"  this takes a while -> {dst.name}")
        _transcode(src, dst)

    print(f"  done, {dst.stat().st_size / 1e6:.0f} MB")
    return dst


def dense_motion(video_path, threshold, rotate=None):
    """Per-frame motion readings: [(t, score, stopped), ...]."""
    readings = []
    with VideoSource(video_path, rotate=rotate) as source:
        motion = EgoMotion(threshold=threshold)
        for frame in source.frames():
            score = motion.update(frame.gray)
            if score is None or not motion.ready:
                continue
            readings.append((frame.t, score, motion.is_stopped))
        meta = {
            "duration": round(source.duration_s, 2),
            "fps": round(source.fps, 2),
            "width": source.size[0],
            "height": source.size[1],
        }
    return readings, meta


def sample_spacing(samples, default=3.0):
    """Seconds between vision samples, read off the timeline rather than assumed."""
    gaps = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
    if not gaps:
        return default
    return sorted(gaps)[len(gaps) // 2]


def sign_windows(samples, gap_tolerance=6.0, min_samples=MIN_SIGN_SAMPLES):
    """Contiguous stretches where a stop sign was visible.

    A sample or two without the sign does not end the window - the sign can be
    hidden by a tree or a van and then reappear. A window built from fewer than
    `min_samples` sightings is dropped: it is more likely a misreading than a
    junction, and a phantom junction turns into a phantom critical error.
    """
    windows = []
    current = None

    def close(w):
        if w["samples"] >= min_samples:
            windows.append(w)

    for s in samples:
        if s["scene"]["stop_sign"]:
            if current is None:
                current = {"start": s["t"], "end": s["t"], "samples": 1}
            else:
                current["end"] = s["t"]
                current["samples"] += 1
        elif current is not None and s["t"] - current["end"] > gap_tolerance:
            close(current)
            current = None

    if current is not None:
        close(current)
    return windows


def judge(windows, stops, window_after, full_stop_s=DEFAULT_FULL_STOP_S):
    """Decide, for each stop sign, whether the car stopped for it and for how long.

    The sign leaves the forward view before the car reaches the line, so the
    stop almost always lands AFTER the sign was last seen. Looking only inside
    the visible window would fail every legitimate stop.

    Three outcomes, not two. A stop that happened but lasted under full_stop_s
    is legal in California and still worth telling the driver about, so it gets
    its own grade instead of being filed away as a clean pass.
    """
    verdicts = []
    for w in windows:
        deadline = w["end"] + window_after
        inside = [st for st in stops if w["start"] <= st[0] <= deadline]
        # The longest stop in the window, not the first: an approach often dips
        # under the threshold for a moment before the real stop.
        matched = max(inside, key=lambda st: st[1] - st[0], default=None)
        length = round(matched[1] - matched[0], 2) if matched else None

        if matched is None:
            grade, verdict = "fail", "possible incomplete stop"
        elif length < full_stop_s:
            grade, verdict = "brief", "stopped, but only briefly"
        else:
            grade, verdict = "pass", "full stop"

        verdicts.append(
            {
                "start": w["start"],
                "end": w["end"],
                "deadline": round(deadline, 2),
                "stopped": matched is not None,
                "stop_at": round(matched[0], 2) if matched else None,
                "stop_len": length,
                "target_s": full_stop_s,
                "grade": grade,
                "verdict": verdict,
            }
        )
    return verdicts


def alert_level(sample, verdicts):
    """The level we would send to the UNO Q at this moment (see the brief).

    Reads "confirmed", not "scene": nothing the model said only once gets to
    light an LED or buzz a motor.
    """
    scene = sample["confirmed"]
    for v in verdicts:
        if v["grade"] == "fail" and v["end"] <= sample["t"] <= v["deadline"]:
            return 3
    if scene["stop_sign"]:
        return 1
    if scene["traffic_light"] == "red" and scene["light_is_for_our_lane"]:
        return 1
    if scene["pedestrian_in_crosswalk"]:
        return 1
    return 0


def runs(samples, predicate, min_samples=2):
    """Stretches of consecutive samples matching `predicate`, as (start, end)."""
    out = []
    start = last = None
    count = 0
    for s in samples:
        if predicate(s):
            if start is None:
                start, count = s["t"], 0
            last, count = s["t"], count + 1
        else:
            if start is not None and count >= min_samples:
                out.append((start, last))
            start, count = None, 0
    if start is not None and count >= min_samples:
        out.append((start, last))
    return out


def build_events(samples, stops, verdicts):
    events = []
    for v in verdicts:
        events.append(
            {
                "t": v["start"],
                "kind": "sign",
                "text": f"Stop sign ahead (visible to {v['end']:.0f}s)",
            }
        )
        if v["grade"] == "pass":
            events.append(
                {
                    "t": v["stop_at"],
                    "kind": "pass",
                    "text": f"Full stop - {v['stop_len']:.1f}s at a standstill",
                }
            )
        elif v["grade"] == "brief":
            events.append(
                {
                    "t": v["stop_at"],
                    "kind": "brief",
                    "text": (
                        f"Stopped, but only {v['stop_len']:.1f}s - "
                        f"hold it for {v['target_s']:.0f}s"
                    ),
                }
            )
        else:
            events.append(
                {
                    "t": v["deadline"],
                    "kind": "fail",
                    "text": "Possible incomplete stop - no stop detected",
                }
            )

    sign_spans = [(v["start"], v["deadline"]) for v in verdicts]
    for start, end in stops:
        # Stops already explained by a stop sign are reported above.
        if any(a <= start <= b for a, b in sign_spans):
            continue
        events.append(
            {
                "t": start,
                "kind": "stop",
                "text": f"Stationary {end - start:.1f}s",
            }
        )

    # One event per stretch of red light, not one for the whole drive. Only
    # confirmed sightings count, so a single hallucinated frame stays silent.
    for start, end in runs(
        samples,
        lambda s: s["confirmed"]["traffic_light"] == "red"
        and s["confirmed"]["light_is_for_our_lane"],
    ):
        events.append(
            {
                "t": start,
                "kind": "light",
                "text": f"Red light for our lane, still red at {end:.0f}s",
            }
        )

    return sorted(events, key=lambda e: e["t"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("output/player.html"))
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD)
    parser.add_argument("--window-after", type=float, default=DEFAULT_WINDOW_AFTER)
    parser.add_argument(
        "--full-stop",
        type=float,
        default=DEFAULT_FULL_STOP_S,
        help="seconds a stop should last before it counts as a good one "
        f"(default: {DEFAULT_FULL_STOP_S:.0f}, the 'count to three' rule)",
    )
    parser.add_argument("--rotate", type=int, default=None, choices=[0, 90, 180, 270])
    args = parser.parse_args()

    if not args.timeline.is_file():
        sys.exit(
            f"no timeline at {args.timeline}\n"
            "Generate one first:\n"
            "  python laptop/analyze_video.py <video> --json-out output/timeline.json"
        )

    samples = json.loads(args.timeline.read_text())
    samples = confirm(samples, spacing=sample_spacing(samples))
    rejected = dropped(samples)

    print("recomputing dense motion track...")
    readings, meta = dense_motion(args.video, args.threshold, args.rotate)
    stops = find_stops(readings)
    windows = sign_windows(samples)
    verdicts = judge(windows, stops, args.window_after, args.full_stop)

    for s in samples:
        s["level"] = alert_level(s, verdicts)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    playable = ensure_playable(args.video, args.out.parent)

    data = {
        "video": Path(
            os.path.relpath(playable.resolve(), args.out.resolve().parent)
        ).as_posix(),
        "name": args.video.name,
        "threshold": args.threshold,
        "full_stop_s": args.full_stop,
        "meta": meta,
        # Thin the dense track: one point per ~0.1s is plenty for a graph and
        # keeps the HTML small.
        "motion": [
            [round(t, 2), round(score, 3)]
            for i, (t, score, _) in enumerate(readings)
            if i % max(1, int(meta["fps"] / 10)) == 0
        ],
        "samples": samples,
        "stops": [[round(a, 2), round(b, 2)] for a, b in stops],
        "verdicts": verdicts,
        "events": build_events(samples, stops, verdicts),
        "rejected": [[round(t, 2), field, value] for t, field, value in rejected],
    }

    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DRIVE_DATA__*/null", json.dumps(data)
    )
    args.out.write_text(html, encoding="utf-8")

    print(f"\nwrote {args.out}")
    print(f"  {len(data['motion'])} motion points, {len(samples)} vision samples")
    print(f"  {len(stops)} stationary windows, {len(verdicts)} stop-sign approach(es)")
    marks = {"pass": "PASS", "brief": "SHORT", "fail": "FLAG"}
    for v in verdicts:
        detail = f" ({v['stop_len']:.1f}s of {v['target_s']:.0f}s)" if v["stopped"] else ""
        print(
            f"  [{marks[v['grade']]}] sign {v['start']:.0f}-{v['end']:.0f}s -> "
            f"{v['verdict']}{detail}"
        )

    if rejected:
        print(f"\n  {len(rejected)} unconfirmed detection(s) dropped - "
              "no neighbouring sample agreed:")
        for t, field, value in rejected:
            print(f"    {t:6.1f}s  {field} = {value!r}")


if __name__ == "__main__":
    main()
