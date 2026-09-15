"""Send alert levels from the laptop to the UNO Q.

Used as a module by the detection code later, and as a CLI for testing now.

    python alert_client.py 2
    python alert_client.py 2 --host 192.168.1.42

The UNO Q address comes from --host, else the UNOQ_HOST environment variable.
Set it once per terminal with:

    $env:UNOQ_HOST = "192.168.1.42"
"""

import argparse
import os
import sys

import requests

DEFAULT_PORT = 8080
TIMEOUT_S = 2.0

LEVEL_MEANING = {
    0: "driving fine",
    1: "heads up",
    2: "minor mistake",
    3: "critical mistake",
}


class AlertClient:
    """Posts alert levels to the listener running on the UNO Q."""

    def __init__(self, host=None, port=DEFAULT_PORT, timeout=TIMEOUT_S):
        self.host = host or os.environ.get("UNOQ_HOST")
        if not self.host:
            raise ValueError(
                "No UNO Q address. Pass --host <ip>, or set UNOQ_HOST first:\n"
                '    $env:UNOQ_HOST = "192.168.1.42"'
            )
        self.base_url = f"http://{self.host}:{port}"
        self.timeout = timeout

    def health(self):
        """True if the listener answers. Never raises, so callers can poll it."""
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=self.timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def send(self, level):
        """Send one alert level. Returns the parsed JSON reply."""
        if level not in LEVEL_MEANING:
            raise ValueError(f"level must be 0-3, got {level!r}")

        resp = requests.post(
            f"{self.base_url}/alert",
            json={"level": level},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Send one alert level to the UNO Q.")
    parser.add_argument("level", type=int, choices=sorted(LEVEL_MEANING))
    parser.add_argument("--host", default=None, help="UNO Q IP address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    try:
        client = AlertClient(host=args.host, port=args.port)
    except ValueError as exc:
        sys.exit(str(exc))

    meaning = LEVEL_MEANING[args.level]
    try:
        client.send(args.level)
    except requests.RequestException as exc:
        sys.exit(
            f"Could not reach the UNO Q at {client.base_url}\n"
            f"  {exc}\n\n"
            "Check that alert_listener.py is running on the UNO Q and that both\n"
            "devices are on the same network."
        )

    print(f"sent level {args.level} ({meaning}) to {client.base_url}")


if __name__ == "__main__":
    main()
