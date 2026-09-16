"""Walk the UNO Q through every alert level, so you can watch all three outputs.

This is the end-to-end hardware check with no video and no model involved:

    laptop -> MCP -> USB/ADB -> UNO Q Linux -> Router Bridge -> sketch -> LEDs

Use it when the review player shows nothing on the board and you need to know
which half is at fault. It is also the only way to see level 2: no check in the
product produces it, because a brief stop is legal and following distance is
experimental. For level 3 on a real run, use  ./run.sh --demo fail --unoq

    python laptop/test_signals.py
    python laptop/test_signals.py --hold 4

Needs the tunnel and the board's MCP server, which ./run.sh --unoq sets up:

    adb forward tcp:3001 tcp:3001
    adb shell 'cd /home/arduino/topteam && python3 mcp_server.py'
"""

import argparse
import sys
import time

from unoq_mcp import UnoQ

LEVEL_MEANING = {
    0: "driving fine",
    1: "heads up",
    2: "minor mistake",
    3: "critical mistake",
}

# What each level should look like, so you check the board rather than trust it.
# Levels are told apart by INTENSITY, not by presence: an earlier build kept
# level 1 silent so a buzz could only ever mean a mistake, and on real footage
# the entire heads-up was one small LED changing colour - the first live run
# looked like the hardware was dead.
EXPECTED = {
    0: "green on the strip, calm bar on the matrix, no buzz",
    1: "whole strip amber and breathing, octagon on the matrix, ONE gentle tap",
    2: "ONE firm pulse, strip gains a red LED, matrix shows the count",
    3: "THREE ramping buzzes, whole strip flashes red, matrix shows a big X",
}

# Level 2 is implemented on the board but no check currently produces it: a
# brief stop is legal and must not be counted as an error, and following
# distance is experimental and unscored. This script is the way to see it.
UNUSED_BY_SCORING = {2}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="MCP server URL")
    parser.add_argument(
        "--hold",
        type=float,
        default=3.0,
        help="seconds to stay on each level (default: 3)",
    )
    args = parser.parse_args()

    unoq = UnoQ(url=args.url)
    if not unoq.connect():
        sys.exit(f"{unoq.last_error}")

    print(f"UNO Q: {unoq.url}")

    # mcu_ping is the only call that waits for the sketch to answer, so it is
    # the one thing that separates "the router took our bytes" from "the sketch
    # is actually running". Not fatal - an older sketch has no mcu_ping but
    # still handles alerts perfectly well.
    if unoq.ping():
        print("sketch: answering\n")
    else:
        print("sketch: no answer to mcu_ping - alerts should still work.")
        print("        Re-deploy alert_sketch.ino to get the health check.\n")

    # The count lives on the microcontroller and survives between runs.
    unoq.reset_drive()

    try:
        for level in sorted(LEVEL_MEANING):
            unused = "  (no scored check produces this)" if level in UNUSED_BY_SCORING else ""
            print(f"  level {level}  {LEVEL_MEANING[level]}{unused}")
            print(f"           expect: {EXPECTED[level]}")
            if not unoq.set_level(level):
                sys.exit(f"\nFailed sending level {level}: {unoq.last_error}")
            time.sleep(args.hold)
    finally:
        # Leave the board calm rather than strobing after we exit.
        unoq.set_level(0)
        unoq.reset_drive()
        unoq.close()

    print("\ndone - back to level 0, counter cleared")


if __name__ == "__main__":
    main()
