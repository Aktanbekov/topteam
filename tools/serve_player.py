"""Serve the player over localhost, for when file:// will not play the video.

Opening output/player.html straight from disk usually works. Some browsers
refuse to load a video from a file:// page, and then you need this.

    python tools/serve_player.py
    python tools/serve_player.py --port 8000 --open

Python's built-in server does not support HTTP range requests, which means the
browser cannot seek in the video - the scrubber jumps back to the start. This
handler adds range support, so seeking works.

Everything stays on the laptop; nothing is exposed beyond localhost.
"""

import argparse
import json
import os
import re
import sys
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

# Set by main() when --unoq is given. The page posts here as the video plays,
# and we forward to the board's MCP server over the USB tunnel. The browser
# cannot reach the board itself - it has no way to speak MCP, and the tunnel
# lives on this machine - so the page talks to its own origin and we relay.
SENDER = None


class RangeHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler plus the byte-range support video needs."""

    def send_head(self):
        range_header = self.headers.get("Range")
        if not range_header:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(f.fileno()).st_size
        match = RANGE_RE.match(range_header.strip())
        if not match:
            f.close()
            self.send_error(400, "Malformed Range header")
            return None

        start_s, end_s = match.groups()
        try:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
        except ValueError:
            f.close()
            self.send_error(400, "Malformed Range header")
            return None

        end = min(end, size - 1)
        if start > end:
            f.close()
            self.send_error(416, "Requested range not satisfiable")
            self.send_header("Content-Range", f"bytes */{size}")
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        f.seek(start)
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        """Send only the requested slice when a range was asked for."""
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)

        self._remaining = None
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            try:
                outputfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Normal when the viewer seeks away mid-download.
                break
            remaining -= len(chunk)

    # ------------------------------------------------------- hardware relay
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        route = self.path.split("?")[0]
        if route not in ("/api/alert", "/api/reset"):
            self._json(404, {"error": "POST /api/alert or /api/reset"})
            return

        if SENDER is None:
            # Not an error: the player works perfectly well with no board, and
            # the page should carry on rather than pop up a failure.
            self._json(200, {"ok": True, "hardware": False})
            return

        if route == "/api/reset":
            self._json(200, {"ok": SENDER.reset(), "hardware": True})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            level = json.loads(self.rfile.read(length) or b"{}").get("level")
            level = int(level)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": f"bad level: {exc}"})
            return
        if level not in (0, 1, 2, 3):
            self._json(400, {"error": f"level must be 0-3, got {level}"})
            return

        sent = SENDER.send(level)
        if sent:
            print(f"  level {level} -> board", flush=True)
        self._json(200, {"ok": True, "hardware": True, "sent": sent, "level": level})

    def log_message(self, fmt, *args):
        pass


def connect_board(url):
    """Open the MCP session. Returns a LevelSender, or None if unreachable."""
    from unoq_mcp import LevelSender, UnoQ

    unoq = UnoQ(url=url) if url else UnoQ()
    if not unoq.connect():
        print("\n  no UNO Q - the player still works, the hardware just sits idle")
        for line in unoq.last_error.splitlines():
            print(f"    {line}")
        return None

    sender = LevelSender(unoq)
    sender.reset()
    print(f"  UNO Q connected at {unoq.url}")
    print(f"  sketch answering: {unoq.ping()}")
    return sender


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="directory to serve (default: the project root)",
    )
    parser.add_argument("--open", action="store_true", help="open a browser")
    parser.add_argument(
        "--unoq",
        nargs="?",
        const="",
        default=None,
        metavar="URL",
        help="drive the UNO Q as the video plays. Needs the USB tunnel first: "
        "adb forward tcp:3001 tcp:3001",
    )
    args = parser.parse_args()

    global SENDER
    if args.unoq is not None:
        SENDER = connect_board(args.unoq or None)

    handler = partial(RangeHandler, directory=str(args.root))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)

    url = f"http://127.0.0.1:{args.port}/output/player.html"
    print(f"serving {args.root}")
    print(f"open {url}   (Ctrl-C to stop)")
    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
