"""Show what the camera sees while a live drive runs.

    http://127.0.0.1:8008

Without this a live drive is invisible: the frames go camera -> motion -> model
-> board and are never drawn anywhere, so from the outside a working run and a
broken one look identical. That is fine for a script and useless on a stage.

It is deliberately an MJPEG stream over HTTP rather than a desktop window:

  * the browser is already how this project shows everything else, so the
    preview can sit beside the player and the report on one screen;
  * a Tk window would want the main thread, which belongs to the capture loop;
  * and the laptop camera can only be opened once. A page using getUserMedia
    would be a SECOND consumer of the same device and would fight the analysis
    for it. This streams the frames we have already decoded, so there is only
    ever one camera reader.

NOTHING HERE TOUCHES THE CAPTURE THREAD. LiveDrive drops the newest frame into
a slot - a reference assignment, nanoseconds - and this server encodes JPEG on
its own thread whenever a browser asks. A slow or absent viewer cannot cost the
drive a single frame, which is the whole reason the analysis does not do the
encoding itself.

What the page shows is what the review will say, not a second opinion: the
status it renders comes from the same level track that is being sent to the
board.
"""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

PREVIEW_PORT = 8008

# How often the stream sends a frame. The camera runs at 30fps but a preview
# does not need to - this is a person watching, not a measurement, and every
# frame sent is a JPEG encode we did not have to do.
STREAM_FPS = 10

# Smaller than the captured frame. The preview is for a human glance, and a
# 1280x720 JPEG ten times a second is a lot of encoding for no extra meaning.
PREVIEW_W = 640

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Live drive - camera</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#111; color:#eee;
         font:14px/1.5 ui-sans-serif,system-ui,sans-serif; }
  .wrap { max-width:720px; margin:0 auto; padding:16px; }
  h1 { font-size:15px; font-weight:600; margin:0 0 12px; color:#9aa; }
  img { width:100%; border-radius:8px; display:block; background:#000; }
  .row { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }
  .card { flex:1 1 120px; background:#1b1b1f; border-radius:8px; padding:10px 12px; }
  .k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:#889; }
  .v { font-size:19px; font-weight:600; margin-top:2px; }
  .lvl0{color:#4ade80} .lvl1{color:#fbbf24} .lvl2{color:#fb923c} .lvl3{color:#f87171}
  .note { margin-top:14px; font-size:12px; color:#889; border-top:1px solid #2a2a30;
          padding-top:10px; }
</style>
<div class="wrap">
  <h1>Live drive &mdash; what the camera sees</h1>
  <img src="/stream" alt="camera">
  <div class="row">
    <div class="card"><div class="k">Time</div><div class="v" id="t">-</div></div>
    <div class="card"><div class="k">Motion</div><div class="v" id="m">-</div></div>
    <div class="card"><div class="k">Level</div><div class="v" id="l">-</div></div>
  </div>
  <div class="row">
    <div class="card"><div class="k">Last scene</div><div class="v" id="s"
         style="font-size:14px">waiting for the first sample</div></div>
  </div>
  <p class="note" id="lag">A confirmed warning lands about 6.5s after the event:
     one vision call, plus one sampling interval for a second frame to agree.
     This preview is the raw camera, so it is ahead of the coaching.</p>
</div>
<script>
const NAMES = ["driving fine","heads up","minor mistake","critical"];
async function tick() {
  try {
    const r = await fetch("/status", {cache:"no-store"});
    const d = await r.json();
    document.getElementById("t").textContent = d.t.toFixed(1) + "s";
    document.getElementById("m").textContent =
      d.motion === null ? "-" : d.motion.toFixed(2) + (d.stopped ? " stopped" : " moving");
    const l = document.getElementById("l");
    l.textContent = d.level + " \\u00b7 " + (NAMES[d.level] || "");
    l.className = "v lvl" + d.level;
    document.getElementById("s").textContent = d.scene || "nothing flagged";
  } catch (e) { /* the drive ended; leave the last reading on screen */ }
}
setInterval(tick, 500); tick();
</script>
"""


class LiveState:
    """The one frame and one status the preview reads, written by the drive.

    Assignment under a lock and nothing else: the capture thread must never do
    work here. Encoding, scaling and HTTP all happen on the server's threads.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self.t = 0.0
        self.motion = None
        self.stopped = None
        self.scene = ""
        self.level = 0

    def put_frame(self, rgb):
        with self._lock:
            self._frame = rgb

    def get_frame(self):
        with self._lock:
            return self._frame

    def put_status(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)

    def as_json(self):
        return {
            "t": round(self.t, 2),
            "motion": None if self.motion is None else round(self.motion, 3),
            "stopped": self.stopped,
            "scene": self.scene,
            "level": self.level,
        }


def _encode(rgb, width=PREVIEW_W, quality=70):
    image = Image.fromarray(rgb)
    if image.width > width:
        height = round(image.height * width / image.width)
        image = image.resize((width, height))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class _ExclusiveServer(ThreadingHTTPServer):
    """A server that refuses a port somebody else already has.

    HTTPServer sets SO_REUSEADDR, and on Windows that does not mean "reuse a
    port in TIME_WAIT" as it does on Unix - it means a second process can bind
    a port another process is actively listening on, and the two then fight over
    arriving connections. Since the whole point of catching the bind error is to
    notice that a live drive already owns 8008, we have to turn it off.
    """

    allow_reuse_address = False


def _handler_for(state, stop_event):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass  # the drive's own output is the interesting one

        def do_GET(self):
            if self.path.startswith("/stream"):
                return self._stream()
            if self.path.startswith("/status"):
                return self._json(state.as_json())
            return self._page()

        def _page(self):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while not stop_event.is_set():
                    rgb = state.get_frame()
                    if rgb is not None:
                        jpeg = _encode(rgb)
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(
                            f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                        )
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                    stop_event.wait(1.0 / STREAM_FPS)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the viewer closed the tab, which is not our problem

    return Handler


class PreviewServer:
    """Serves the preview until close(). Never blocks the caller."""

    def __init__(self, state, port=PREVIEW_PORT):
        self.state = state
        self.stop_event = threading.Event()
        self.port = port
        self._server = None
        self._thread = None

    def start(self):
        """Returns the URL, or None if the port was taken - never raises.

        A preview that cannot bind is a missing convenience, not a reason to
        refuse to analyse a drive.
        """
        try:
            self._server = _ExclusiveServer(
                ("127.0.0.1", self.port), _handler_for(self.state, self.stop_event)
            )
        except OSError:
            return None
        # Port 0 means "any free port", which is how the tests bind without
        # fighting a real drive for 8008. Read back what we actually got.
        self.port = self._server.server_address[1]
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="preview"
        )
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.stop_event.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
