"""Generate a synthetic dashcam clip with a known motion profile.

Real footage is the goal, but this gives us something repeatable to develop
against - and the ground truth is exact, so we can tell whether the detector is
right rather than guessing.

    python tools/make_test_clip.py --profile full     -> a 4s stop  (PASS)
    python tools/make_test_clip.py --profile stop     -> a 1s stop  (BRIEF)
    python tools/make_test_clip.py --profile rolling  -> never stops (FAIL)

The rolling clip is the one that matters: a correct detector must flag it and
must NOT flag the other two. These are clearly labelled as synthetic wherever
they are shown - a rendered road is not a drive, and nobody should have to
squint at the page to work that out.
"""

import argparse
import math
import random
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image, ImageDraw

W, H = 640, 480
FPS = 20
HORIZON = 240
FOCAL = 500.0
CAM_HEIGHT = 1.5  # metres

SIGN_Z = 90.0  # how far down the road the stop sign sits, in metres
SIGN_X = 3.0
SIGN_HEIGHT = 2.2
SIGN_RADIUS = 0.5

SKY = (135, 175, 210)
GROUND = (110, 130, 95)
ROAD = (85, 85, 90)
PAINT = (235, 235, 225)
SIGN_RED = (185, 30, 35)
POLE = (105, 100, 95)

# World-locked speckle: asphalt grain and roadside scrub, pinned to world
# positions so it slides past as we drive. Real footage is full of detail, and a
# motion detector tuned on a bare synthetic road will not survive real video.
_rng = random.Random(7)
ROAD_SPECKLE = [
    (_rng.uniform(-3.4, 3.4), _rng.uniform(2, 400), _rng.randint(-22, 22))
    for _ in range(1400)
]
VERGE_SPECKLE = [
    (
        _rng.choice([-1, 1]) * _rng.uniform(4.2, 14.0),
        _rng.uniform(2, 400),
        _rng.randint(-30, 30),
    )
    for _ in range(900)
]


def shaded(base, delta):
    return tuple(max(0, min(255, c + delta)) for c in base)


# (until_t, speed_at_that_time) - speed ramps linearly between points, m/s.
#
# The three profiles line up with the three grades the coach can award, so the
# demo can show all of them. We have real footage of a brief stop and none at
# all of a driver running a sign, and we are not going to go and create some.
#
#   full     stops for 4.0s  -> meets the 3s coaching target   -> PASS
#   stop     stops for 1.0s  -> legal but short                -> BRIEF
#   rolling  never stops     -> possible incomplete stop       -> FAIL
#
# Each ramps from 12 m/s and arrives at the sign, 90 m down the road, at about
# the moment the speed reaches its low point.
PROFILES = {
    "full": [(0, 12), (6, 12), (9, 0), (13, 0), (18, 12), (22, 12)],
    "stop": [(0, 12), (6, 12), (9, 0), (10, 0), (15, 12), (20, 12)],
    "rolling": [(0, 12), (6, 12), (8.5, 3), (10, 3), (14, 12), (20, 12)],
}


def speed_at(profile, t):
    points = PROFILES[profile]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def project(x, height, z):
    """World point to screen pixel. z is distance ahead, must be > 0."""
    sx = W / 2 + FOCAL * x / z
    sy = HORIZON + FOCAL * (CAM_HEIGHT - height) / z
    return sx, sy


def draw_scene(distance):
    """Render one frame, given how far we have driven from the start."""
    img = Image.new("RGB", (W, H), SKY)
    d = ImageDraw.Draw(img)
    d.rectangle([0, HORIZON, W, H], fill=GROUND)

    # Road surface: a quad from just in front of the car out to the horizon.
    near, far = 3.0, 200.0
    road = [
        project(-3.5, 0, near),
        project(3.5, 0, near),
        project(3.5, 0, far),
        project(-3.5, 0, far),
    ]
    d.polygon(road, fill=ROAD)

    # Asphalt grain and roadside scrub, drawn far to near.
    for speckle, base, spread in (
        (VERGE_SPECKLE, GROUND, 0.30),
        (ROAD_SPECKLE, ROAD, 0.09),
    ):
        for x, zw, shade in speckle:
            z = zw - distance
            if z < near or z > 220:
                continue
            sx, sy = project(x, 0, z)
            r = max(0.5, FOCAL * spread / z)
            d.ellipse([sx - r, sy - r * 0.5, sx + r, sy + r * 0.5], fill=shaded(base, shade))

    # Centre-line dashes every 8 m. Drawing far to near keeps the overlap right.
    spacing = 8.0
    first = math.floor(distance / spacing) * spacing
    for i in range(40, -1, -1):
        z0 = first + i * spacing - distance
        z1 = z0 + 3.0
        if z1 < near:
            continue
        z0 = max(z0, near)
        quad = [
            project(-0.15, 0, z0),
            project(0.15, 0, z0),
            project(0.15, 0, z1),
            project(-0.15, 0, z1),
        ]
        d.polygon(quad, fill=PAINT)

    # Roadside poles every 20 m, so there is texture moving past on the right.
    for i in range(30, -1, -1):
        z = math.floor(distance / 20) * 20 + i * 20 - distance
        if z < near:
            continue
        bx, by = project(5.0, 0, z)
        _, ty = project(5.0, 4.0, z)
        width = max(1, int(FOCAL * 0.12 / z))
        d.rectangle([bx - width, ty, bx + width, by], fill=POLE)

    # Stop line and stop sign.
    z_sign = SIGN_Z - distance
    if z_sign > near:
        line = [
            project(-3.5, 0, z_sign),
            project(3.5, 0, z_sign),
            project(3.5, 0, z_sign + 0.5),
            project(-3.5, 0, z_sign + 0.5),
        ]
        d.polygon(line, fill=PAINT)

        cx, cy = project(SIGN_X, SIGN_HEIGHT, z_sign)
        r = FOCAL * SIGN_RADIUS / z_sign
        if r >= 2:
            px, py = project(SIGN_X, 0, z_sign)
            pw = max(1, int(FOCAL * 0.05 / z_sign))
            d.rectangle([px - pw, cy, px + pw, py], fill=POLE)

            pts = [
                (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
                for a in range(22, 382, 45)
            ]
            d.polygon(pts, fill=SIGN_RED, outline=(255, 255, 255))
            if r > 14:
                d.text((cx - 11, cy - 4), "STOP", fill=(255, 255, 255))

    return img


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="stop")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out = args.out or Path(f"test_clip_{args.profile}.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    total_t = PROFILES[args.profile][-1][0]
    n_frames = int(total_t * FPS)

    container = av.open(str(out), mode="w")
    stream = container.add_stream("libx264", rate=Fraction(FPS, 1))
    stream.width, stream.height = W, H
    stream.pix_fmt = "yuv420p"

    distance = 0.0
    dt = 1.0 / FPS
    stopped_from = stopped_until = None

    for i in range(n_frames):
        t = i * dt
        v = speed_at(args.profile, t)

        # Record the true stationary window so we can check the detector.
        if v < 0.1:
            if stopped_from is None:
                stopped_from = t
            stopped_until = t

        frame = av.VideoFrame.from_image(draw_scene(distance))
        for packet in stream.encode(frame):
            container.mux(packet)

        distance += v * dt

    for packet in stream.encode():
        container.mux(packet)
    container.close()

    print(f"wrote {out}  ({n_frames} frames, {total_t:.0f}s, {W}x{H} @ {FPS}fps)")
    print(f"profile: {args.profile}")
    if stopped_from is None:
        print("ground truth: never fully stopped  <- detector SHOULD flag this")
    else:
        print(
            f"ground truth: stopped {stopped_from:.1f}s - {stopped_until:.1f}s"
            "  <- detector should NOT flag this"
        )
    print(f"stop sign reached at ~{SIGN_Z:.0f}m down the road")


if __name__ == "__main__":
    main()
