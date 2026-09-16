"""SUPERSEDED - replaced by unoq/mcp_server.py.

This served alert levels over HTTP on the board's network port. wlan0 was
down out of the box, so the laptop had no address to reach it on, and
everything moved to MCP over the USB/ADB tunnel instead.

Kept as the simpler fallback: it needs no fastmcp, only arduino_bridge.py,
which now has no dependencies at all.

Alert listener - runs on the UNO Q Linux side.

Receives alert levels from the laptop over the local network and forwards them
to the MCU sketch through the Arduino Router Bridge.

    laptop  --HTTP-->  this script  --Bridge.notify-->  alert_sketch.ino

Run it on the UNO Q with:
    python3 alert_listener.py

Then from the laptop:
    curl "http://<unoq-ip>:8080/alert?level=2"
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from arduino_bridge import ArduinoBridge, BridgeError

DEFAULT_PORT = 8080
VALID_LEVELS = (0, 1, 2, 3)

bridge = ArduinoBridge()


def send_to_mcu(method, *args):
    """Forward a call to the sketch, reconnecting once if the socket died."""
    try:
        bridge.notify(method, *args)
    except BridgeError:
        # One retry: the router may have restarted since our last message.
        bridge.connect()
        bridge.notify(method, *args)


class AlertHandler(BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_alert(self, level_raw):
        try:
            level = int(level_raw)
        except (TypeError, ValueError):
            self._reply(400, {"error": f"level must be an integer, got {level_raw!r}"})
            return

        if level not in VALID_LEVELS:
            self._reply(400, {"error": f"level must be one of {list(VALID_LEVELS)}"})
            return

        try:
            send_to_mcu("alert", level)
        except BridgeError as exc:
            self._reply(503, {"error": str(exc)})
            return

        print(f"alert level {level} -> MCU", flush=True)
        self._reply(200, {"ok": True, "level": level})

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._reply(200, {"ok": True, "service": "alert_listener"})
            return

        if parsed.path == "/alert":
            params = parse_qs(parsed.query)
            self._handle_alert(params.get("level", [None])[0])
            return

        if parsed.path == "/reset":
            self._handle_reset()
            return

        self._reply(404, {"error": "try /health, /alert?level=0..3 or /reset"})

    def _handle_reset(self):
        """Clear the strike counter the sketch keeps on the microcontroller."""
        try:
            send_to_mcu("reset")
        except BridgeError as exc:
            self._reply(503, {"error": str(exc)})
            return

        print("reset -> MCU", flush=True)
        self._reply(200, {"ok": True, "reset": True})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/reset":
            self._handle_reset()
            return

        if path != "/alert":
            self._reply(404, {"error": "POST /alert with {\"level\": 0-3}, or /reset"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._reply(400, {"error": f"invalid JSON: {exc}"})
            return

        self._handle_alert(payload.get("level"))

    def log_message(self, fmt, *args):
        # Quieten the default per-request logging; we print our own line.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address; 0.0.0.0 accepts connections from the laptop",
    )
    args = parser.parse_args()

    try:
        bridge.connect()
        print("connected to arduino-router")
    except BridgeError as exc:
        # Keep serving anyway so /health still answers and the error is visible
        # in the HTTP response instead of only in this terminal.
        print(f"WARNING: {exc}")

    server = ThreadingHTTPServer((args.host, args.port), AlertHandler)
    print(f"listening on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        bridge.close()


if __name__ == "__main__":
    main()
