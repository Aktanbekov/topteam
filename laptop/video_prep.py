"""Video work the review build needs: playable copy, motion track, evidence stills.

Kept apart from the scoring rules on purpose. Everything in drive_review.py is
pure and can be unit tested in milliseconds with no ffmpeg, no model and no
video file; everything in here touches real media and is slow. Mixing the two
is how the stop-sign logic ended up untestable in the first place.
"""

from pathlib import Path

import av
import numpy as np
from PIL import Image

from config import MOTION_THRESHOLD
from ego_motion import EgoMotion
from video_source import VideoSource

# What browsers will actually decode. Note .mov is absent deliberately: Chrome
# and Edge routinely refuse a QuickTime file even when the video inside is
# ordinary H.264, which is exactly what IMG_9830.MOV is.
BROWSER_SAFE_CONTAINERS = {".mp4", ".m4v", ".webm"}
BROWSER_SAFE_CODECS = {"h264", "vp8", "vp9", "av1"}


# ------------------------------------------------------------ playable copy
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
    src, out_dir = Path(src), Path(out_dir)
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


# ------------------------------------------------------------- motion track
def dense_motion(video_path, threshold=MOTION_THRESHOLD, rotate=None):
    """Per-frame motion readings [(t, score, stopped), ...] plus drive metadata.

    Decoding stays sequential: seeking lands on keyframes, which makes
    frame-to-frame comparison meaningless.
    """
    readings = []
    with VideoSource(video_path, rotate=rotate) as source:
        motion = EgoMotion(threshold=threshold)
        for frame in source.frames():
            score = motion.update(frame.gray)
            if score is None or not motion.ready:
                continue
            readings.append((frame.t, score, motion.is_stopped))
        meta = {
            "duration_s": round(source.duration_s, 2),
            "fps": round(source.fps, 2),
            "width": source.size[0],
            "height": source.size[1],
        }
    return readings, meta


def thin(readings, fps, per_second=10):
    """One point per ~0.1s. Plenty for a graph and it keeps the HTML small."""
    step = max(1, int(round((fps or 30) / per_second)))
    return [
        [round(t, 2), round(score, 3)]
        for i, (t, score, _stopped) in enumerate(readings)
        if i % step == 0
    ]


# ---------------------------------------------------------- evidence stills
def grab_frames(video_path, times, out_dir, rotate=None, quality=88):
    """Save one still per requested timestamp. Returns {time: relative path}.

    Seeking is fine here in a way it is not for the motion layer: we want a
    picture, not a difference between neighbouring pictures, so landing a few
    frames off a keyframe costs nothing.

    Never raises. Evidence images are a nicety - a report that lost one should
    say so, not fail to build.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = sorted({round(float(t), 2) for t in times})
    found = {}
    if not wanted:
        return found

    try:
        container = av.open(str(video_path))
    except (av.AVError, OSError) as exc:  # pragma: no cover - corrupt input only
        print(f"  could not open {video_path} for evidence stills: {exc}")
        return found

    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        time_base = stream.time_base

        for t in wanted:
            try:
                container.seek(
                    max(0, int((t - 1.0) / time_base)), stream=stream, any_frame=False
                )
                picked = None
                for av_frame in container.decode(stream):
                    if av_frame.pts is None:
                        continue
                    picked = av_frame
                    if float(av_frame.pts * time_base) >= t:
                        break
                if picked is None:
                    continue

                rgb = picked.to_ndarray(format="rgb24")
                if rotate:
                    rgb = np.rot90(rgb, k=rotate // 90)
                name = f"evidence_{t:08.2f}s.jpg".replace(".", "_", 1)
                Image.fromarray(rgb).save(out_dir / name, quality=quality)
                found[t] = f"{out_dir.name}/{name}"
            except (av.AVError, ValueError, OSError) as exc:
                print(f"  no evidence still at {t:.2f}s: {exc}")
    finally:
        container.close()

    return found
