"""Serve the player over localhost, for when file:// will not play the video.

Opening output/player.html straight from disk usually works. Some browsers
refuse to load a video from a file:// page, and then you need this.

    python tools/serve_player.py
    python tools/serve_player.py --port 8000 --open

Python's built-in server does not support HTTP range requests, which means the
browser cannot seek in the video - the scrubber jumps back to the start. This
handler adds range support, so seeking works.

With --unoq it is also the relay to the board. The page cannot speak MCP and
cannot see the USB tunnel, so it posts to its own origin and we forward:

    POST /api/reset            start a fresh drive, clear the strike counter
    POST /api/alert {level, seq}   apply one transition from the level track
    GET  /api/status           what the hardware panel shows

`seq` is the transition's index in the drive's level track. A transition is
applied only if it is further through the drive than anything applied so far,
so scrubbing backwards over a critical event cannot buzz the motor twice or
count a second strike.

Everything stays on the laptop; the socket binds 127.0.0.1 and nothing is
exposed beyond localhost.
"""

import argparse
import json
import os
import re
import signal
import socket
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
REPLAY = None
BOARD = None


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
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Normal when the viewer seeks away mid-download: the browser
                # drops the range request and opens a new one. Windows raises
                # ConnectionAbortedError (WinError 10053) here rather than the
                # reset the other platforms give, and without it every seek
                # prints a twenty-line traceback over the useful output.
                break
            remaining -= len(chunk)

    # ------------------------------------------------------- hardware relay
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Everything is served from and posted to 127.0.0.1. Saying so stops a
        # stray page on another origin from driving the board.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit=4096):
        """Read a small JSON body. Returns {} for anything we cannot parse."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > limit:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            self._json(200, board_status())
            return
        super().do_GET()

    def do_POST(self):
        route = self.path.split("?")[0]
        if route not in ("/api/alert", "/api/reset"):
            self._json(404, {"error": "POST /api/alert or /api/reset"})
            return

        if REPLAY is None:
            # Not an error: the player works perfectly well with no board, and
            # the page should carry on rather than pop up a failure.
            self._json(200, {"ok": True, "hardware": False})
            return

        if route == "/api/reset":
            ok = REPLAY.reset()
            print("  drive reset - board cleared", flush=True)
            self._json(200, {"ok": ok, "hardware": True, **REPLAY.status()})
            return

        body = self._body()
        try:
            level = int(body.get("level"))
            seq = int(body.get("seq", 0))
        except (TypeError, ValueError):
            self._json(400, {"error": "level and seq must be integers"})
            return
        if level not in (0, 1, 2, 3):
            self._json(400, {"error": f"level must be 0-3, got {level}"})
            return
        if seq < 0:
            self._json(400, {"error": f"seq must not be negative, got {seq}"})
            return

        # The page hands us its position in the level track. EventReplay applies
        # a transition only if it is further through the drive than anything
        # applied so far, so scrubbing backwards cannot buzz twice or tick the
        # strike counter again. See laptop/unoq_mcp.py.
        result = REPLAY.apply(level, seq)
        if result == "sent":
            print(f"  level {level} -> board  (event {seq})", flush=True)
        self._json(
            200,
            {"ok": result != "failed", "hardware": True, "result": result,
             "level": level, **REPLAY.status()},
        )

    def log_message(self, fmt, *args):
        pass


def port_in_use(port, host="127.0.0.1", timeout=0.4):
    """True if something is already listening. See the note in main()."""
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def board_status():
    """What the page's hardware panel shows. Always answers, board or no board."""
    if REPLAY is None or BOARD is None:
        return {
            "connected": False,
            "url": None,
            "sketch_ok": False,
            "events_sent": 0,
            "events_skipped": 0,
            "error": "serving without --unoq, so nothing is being sent to a board",
        }
    return {**BOARD.status(), **REPLAY.status()}


def connect_board(url):
    """Open the MCP session. Returns (UnoQ, EventReplay), or (None, None)."""
    from unoq_mcp import EventReplay, UnoQ

    unoq = UnoQ(url=url) if url else UnoQ()
    if not unoq.connect():
        print("\n  no UNO Q - the player still works, the hardware just sits idle")
        for line in (unoq.last_error or "unknown error").splitlines():
            print(f"    {line}")
        return None, None

    replay = EventReplay(unoq)
    replay.reset()
    print(f"  UNO Q connected at {unoq.url}")
    # mcu_ping is the only call that waits for the sketch to answer. The others
    # only prove the router took our bytes.
    print(f"  sketch answering mcu_ping: {unoq.ping()}")
    return unoq, replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="directory to serve (default: the project root)",
    )
    parser.add_argument(
        "--page",
        default="output/player.html",
        help="which built page to open, relative to --root. A demo build lives "
        "under output/demo/<grade>/player.html.",
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

    global REPLAY, BOARD
    if args.unoq is not None:
        BOARD, REPLAY = connect_board(args.unoq or None)

    handler = partial(RangeHandler, directory=str(args.root))

    # A leftover server from an earlier run holds the port, so step to the next
    # free one and say so rather than dying with a bare traceback.
    #
    # Binding is NOT enough to detect that on Windows. ThreadingHTTPServer sets
    # allow_reuse_address, which is SO_REUSEADDR, and Windows lets a second
    # socket bind an address another socket is already listening on - so the
    # bind succeeds, three servers end up on port 8000, and requests are shared
    # out between them at random. That is how a stale server from an earlier
    # session came to answer /api/status with a 404 while the new one sat there
    # working perfectly: the page decided there was no hardware and the board
    # never moved.
    #
    # So ask first whether anything is already listening, and only then bind.
    server = None
    for port in range(args.port, args.port + 10):
        if port_in_use(port):
            continue
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        if port != args.port:
            print(f"  port {args.port} is in use - serving on {port} instead")
        break

    if server is None:
        sys.exit(
            f"ports {args.port}-{args.port + 9} are all in use.\n"
            "Something is probably still running from an earlier session:\n"
            f"  netstat -ano | grep :{args.port}"
        )

    page = args.page.replace("\\", "/").lstrip("/")
    report = (page.rsplit("/", 1)[0] + "/report.html") if "/" in page else "report.html"
    url = f"http://127.0.0.1:{port}/{page}"
    print(f"serving {args.root}")
    print(f"open {url}   (Ctrl-C to stop)")
    print(f"report at http://127.0.0.1:{port}/{report}")
    if args.open:
        webbrowser.open(url)

    # Ctrl-C is not the only way this ends. A `pkill`, a closed terminal or a
    # stop from a launcher all arrive as SIGTERM, and without a handler Python
    # dies where it stands - leaving the board holding whatever level the drive
    # ended on. For a drive that ended on a critical error that is a strip
    # flashing red until somebody runs ./run.sh --calm.
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, stop)
            except (ValueError, OSError):
                pass  # not available on this platform or not the main thread

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        # Leave the board calm rather than strobing after we exit. The sketch
        # holds its last level forever and has no way to tell that we have gone.
        if REPLAY is not None:
            REPLAY.apply(0, REPLAY.max_seq + 1)
            REPLAY.reset()
            print("  board returned to level 0")
        if BOARD is not None:
            BOARD.close()


if __name__ == "__main__":
    main()
