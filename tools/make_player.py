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
from test_video import find_stops  # noqa: E402
from video_source import VideoSource  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "report" / "player_template.html"

# How long after a stop sign leaves the frame we keep looking for the stop.
# Real footage: sign gone by 63s, stop at 64-65s. 10s gives comfortable margin.
DEFAULT_WINDOW_AFTER = 10.0

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


def sign_windows(samples, gap_tolerance=6.0):
    """Contiguous stretches where a stop sign was visible.

    A sample or two without the sign does not end the window - the sign can be
    hidden by a tree or a van and then reappear.
    """
    windows = []
    current = None

    for s in samples:
        if s["scene"]["stop_sign"]:
            if current is None:
                current = {"start": s["t"], "end": s["t"]}
            else:
                current["end"] = s["t"]
        elif current is not None and s["t"] - current["end"] > gap_tolerance:
            windows.append(current)
            current = None

    if current is not None:
        windows.append(current)
    return windows


def judge(windows, stops, window_after):
    """Decide, for each stop sign, whether the car stopped for it.

    The sign leaves the forward view before the car reaches the line, so the
    stop almost always lands AFTER the sign was last seen. Looking only inside
    the visible window would fail every legitimate stop.
    """
    verdicts = []
    for w in windows:
        deadline = w["end"] + window_after
        matched = next(
            (st for st in stops if w["start"] <= st[0] <= deadline), None
        )
        verdicts.append(
            {
                "start": w["start"],
                "end": w["end"],
                "deadline": round(deadline, 2),
                "stopped": matched is not None,
                "stop_at": round(matched[0], 2) if matched else None,
                "stop_len": round(matched[1] - matched[0], 2) if matched else None,
                "verdict": "full stop" if matched else "possible incomplete stop",
            }
        )
    return verdicts


def alert_level(sample, verdicts):
    """The level we would send to the UNO Q at this moment (see the brief)."""
    scene = sample["scene"]
    for v in verdicts:
        if not v["stopped"] and v["end"] <= sample["t"] <= v["deadline"]:
            return 3
    if scene["stop_sign"]:
        return 1
    if scene["traffic_light"] == "red" and scene["light_is_for_our_lane"]:
        return 1
    if scene["pedestrian_in_crosswalk"]:
        return 1
    return 0


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
        if v["stopped"]:
            events.append(
                {
                    "t": v["stop_at"],
                    "kind": "pass",
                    "text": f"Full stop made - {v['stop_len']:.1f}s stationary",
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

    lit = [s for s in samples if s["scene"]["traffic_light"] == "red"]
    if lit:
        events.append(
            {"t": lit[0]["t"], "kind": "light", "text": "Red light reported"}
        )

    return sorted(events, key=lambda e: e["t"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("output/player.html"))
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD)
    parser.add_argument("--window-after", type=float, default=DEFAULT_WINDOW_AFTER)
    parser.add_argument("--rotate", type=int, default=None, choices=[0, 90, 180, 270])
    args = parser.parse_args()

    if not args.timeline.is_file():
        sys.exit(
            f"no timeline at {args.timeline}\n"
            "Generate one first:\n"
            "  python laptop/analyze_video.py <video> --json-out output/timeline.json"
        )

    samples = json.loads(args.timeline.read_text())

    print("recomputing dense motion track...")
    readings, meta = dense_motion(args.video, args.threshold, args.rotate)
    stops = find_stops(readings)
    windows = sign_windows(samples)
    verdicts = judge(windows, stops, args.window_after)

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
    }

    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DRIVE_DATA__*/null", json.dumps(data)
    )
    args.out.write_text(html, encoding="utf-8")

    print(f"\nwrote {args.out}")
    print(f"  {len(data['motion'])} motion points, {len(samples)} vision samples")
    print(f"  {len(stops)} stationary windows, {len(verdicts)} stop-sign approach(es)")
    for v in verdicts:
        mark = "PASS" if v["stopped"] else "FLAG"
        print(f"  [{mark}] sign {v['start']:.0f}-{v['end']:.0f}s -> {v['verdict']}")


if __name__ == "__main__":
    main()
