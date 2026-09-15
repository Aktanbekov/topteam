"""Walk the UNO Q through every alert level, so you can watch the LED react.

This is the end-to-end signal check: laptop -> network -> UNO Q Linux ->
Bridge -> sketch -> LED. No video, no model, nothing else involved.

    python test_signals.py
    python test_signals.py --host 192.168.1.42 --hold 3
"""

import argparse
import sys
import time

import requests

from alert_client import LEVEL_MEANING, AlertClient

EXPECTED_LED = {
    0: "LED off",
    1: "slow blink",
    2: "fast blink",
    3: "rapid strobe",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="UNO Q IP address")
    parser.add_argument(
        "--hold",
        type=float,
        default=3.0,
        help="seconds to stay on each level (default: 3)",
    )
    args = parser.parse_args()

    try:
        client = AlertClient(host=args.host)
    except ValueError as exc:
        sys.exit(str(exc))

    print(f"UNO Q: {client.base_url}")

    if not client.health():
        sys.exit(
            f"No answer from {client.base_url}/health\n\n"
            "On the UNO Q, run:  python3 alert_listener.py\n"
            "Then check both devices are on the same network."
        )
    print("listener is up\n")

    for level in sorted(LEVEL_MEANING):
        meaning = LEVEL_MEANING[level]
        print(f"  level {level}  {meaning:<18} expect: {EXPECTED_LED[level]}")
        try:
            client.send(level)
        except requests.RequestException as exc:
            sys.exit(f"\nFailed sending level {level}: {exc}")
        time.sleep(args.hold)

    # Leave the board in the calm state rather than strobing after we exit.
    client.send(0)
    print("\ndone - back to level 0")


if __name__ == "__main__":
    main()
