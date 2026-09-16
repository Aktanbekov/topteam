"""Make the project's modules importable without installing anything.

The laptop code is a folder of scripts rather than a package - that is
deliberate, it keeps `python laptop/analyze_video.py clip.mp4` working with no
setup - so the test run puts those folders on the path itself.

Nothing in these tests needs GenieX, a video file, or the UNO Q. That is the
point: the rules that decide whether a driver made a mistake have to be
checkable in a second, on any machine, with the board in a drawer.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for folder in ("laptop", "unoq", "tools"):
    path = str(ROOT / folder)
    if path not in sys.path:
        sys.path.insert(0, path)
