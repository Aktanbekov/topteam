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
import os
import re
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


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

    def log_message(self, fmt, *args):
        pass


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
    args = parser.parse_args()

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
