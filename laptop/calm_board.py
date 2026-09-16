"""Put the UNO Q back to level 0. The "make it stop" button.

    python laptop/calm_board.py

The sketch holds whatever level it was last told, forever - it has no idea the
laptop has gone away. So a drive that ended on a critical error leaves the strip
flashing red and the matrix showing a big X until something says otherwise, and
if the next thing you run does not use --unoq, nothing ever does. The board sits
there strobing and the review page appears to have no effect on it, because it
genuinely has none: there is no relay connected.

This is the one-line fix for that, and ./run.sh calls it for you whenever it
finds a tunnel it is not going to use.

It exits 0 whether or not a board was found. Nothing in this project should
fail because hardware is absent.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unoq_mcp import UnoQ  # noqa: E402


def calm(url=None, quiet=False):
    """Send level 0 and clear the strike count. True if the board answered."""
    unoq = UnoQ(url=url)
    if not unoq.connect():
        if not quiet:
            print("no UNO Q reachable - nothing to calm")
            for line in (unoq.last_error or "").splitlines():
                print(f"  {line}")
        return False

    try:
        # Order matters. set_level(0) stops the animation immediately;
        # reset_drive also clears the strike counter so the next drive does not
        # start part lit. Doing only the reset would work too, but a level that
        # is already 0 makes the reset a no-op on the strip and it is worth
        # being explicit about which one does what.
        level_ok = unoq.set_level(0)
        reset_ok = unoq.reset_drive()
    finally:
        unoq.close()

    if not quiet:
        if level_ok and reset_ok:
            print("board calm: green strip, calm bar, strike count cleared")
        else:
            print(f"board reachable but did not accept the calm: {unoq.last_error}")
    return level_ok and reset_ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="MCP server URL")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="say nothing unless something went wrong",
    )
    args = parser.parse_args()
    calm(url=args.url, quiet=args.quiet)


if __name__ == "__main__":
    main()
