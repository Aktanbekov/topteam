"""One page that runs the whole demo, so nobody types commands on stage.

    ./demo.sh          (or: python tools/demo_console.py --unoq --open)

Everything the project can do, from one browser tab:

    recorded drives    analyse a clip, then open its review with the board live
    simulations        the bad-driving runs - a rolled stop sign, a crossing
                       driven through - against the real model and the real board
    live               the laptop camera, analysed as it runs

WHY IT IS A SUPERSET OF serve_player RATHER THAN A NEW SERVER. The review pages
post their level changes to their own origin, and serve_player is what turns
those posts into MCP calls to the UNO Q. Serving the console from a different
process would mean either a second board connection fighting the first, or
players that cannot reach the hardware at all. So this imports that handler and
adds routes to it: one port, one board connection, and every page it serves can
drive the strip without knowing anything about how.

JOBS ARE SUBPROCESSES, one at a time. Analysis takes a minute and a live drive
runs until you stop it, so neither can happen on the request thread. The console
starts a child process, streams its output to a file, and the page polls. That
also means a job that crashes cannot take the console down with it - on a stage
that matters more than elegance.

THE BOARD IS SHARED, and the console gets out of the way. A live drive and a
simulation make their OWN board connection (they are separate processes with
their own EventReplay), so while one runs the console stops relaying: two
writers sending levels to one strip would interleave and neither would make
sense. The page says which one currently owns the board.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))

import serve_player  # noqa: E402
from serve_player import RangeHandler, connect_board, port_in_use  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONSOLE_HTML = ROOT / "report" / "console.html"
LOG_PATH = ROOT / "output" / "console_job.log"

VIDEO_SUFFIXES = (".mov", ".mp4", ".mkv", ".avi")

# A drive we have shipped a description for. Anything else found in the project
# root still appears, just without the commentary.
KNOWN = {
    "IMG_9830.MOV": (
        "Real stop-sign approach with a 1.1s stop - legal, but under the three "
        "seconds every instructor teaches. Demos the amber 'brief stop' grade."
    ),
    "IMG_9831.MOV": (
        "A genuine rolling stop. The sign is confirmed three times, the car "
        "never stops, and the board goes red at the deadline."
    ),
    "IMG_9840.MOV": (
        "A man walks across the lane and the driver stops for him. The build "
        "says so - a warning, nothing scored. This is the honest-negative one."
    ),
}

SIMULATIONS = {
    "sim-stop": {
        "title": "Rolling a stop sign",
        "blurb": "A real stop sign, driven through. Amber, then red with the X "
                 "and three buzzes when the window closes with no stop.",
        "image": "output/9831/frames/frame_006_07s.jpg",
        "clear": "output/9831/frames/frame_015_10s.jpg",
    },
    "sim-person": {
        "title": "Driving through a crossing",
        "blurb": "The same man from IMG_9840, but this time the car never "
                 "stops. Same model, same board - only the driving differs.",
        "image": "output/9840/frames/frame_006_03s.jpg",
        "clear": "output/9840/frames/frame_015_03s.jpg",
    },
}


class Job:
    """One child process, its log, and whether it is still going."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.kind = None
        self.label = None
        self.started = None
        self.owns_board = False
        self.stopped_by_user = False
        self.log = LOG_PATH

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, kind, label, argv, owns_board=False):
        with self.lock:
            if self.running:
                return False, f"{self.label} is still running"
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%H:%M:%S")
            with open(LOG_PATH, "w", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {label}\n{' '.join(argv)}\n\n")
            handle = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
            # New process group so stopping a live drive actually stops it
            # rather than only detaching our pipe from it.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            env = {**os.environ, "DEMO_CONSOLE": "1"}
            self.proc = subprocess.Popen(
                argv, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
                creationflags=flags, env=env,
            )
            self.kind, self.label = kind, label
            self.started = time.time()
            self.stopped_by_user = False
            self.owns_board = owns_board
            return True, None

    def stop(self):
        with self.lock:
            if not self.running:
                return False
            self.stopped_by_user = True
            try:
                if os.name == "nt":
                    # CTRL_BREAK reaches the child's own handler, so a live
                    # drive writes its timeline out instead of being killed
                    # mid-sentence.
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=12)
            except Exception:  # noqa: BLE001
                self.proc.kill()
            return True

    def tail(self, limit=7000):
        try:
            text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]

    def status(self):
        running = self.running
        code = None if running or self.proc is None else self.proc.returncode
        # A Ctrl-Break exit is 0xC000013A on Windows, which shown raw looks like
        # a crash. Pressing Stop is not a failure and the page must not say it is.
        return {
            "running": running,
            "stopped": bool(self.stopped_by_user and not running),
            "kind": self.kind,
            "label": self.label,
            "seconds": round(time.time() - self.started, 1) if self.started else 0,
            "exit_code": code,
            "owns_board": running and self.owns_board,
            "log": self.tail(),
        }


JOB = Job()


# ----------------------------------------------------------------- catalogue
def built(out_dir):
    return (ROOT / out_dir / "player.html").is_file()


def out_dir_for(name):
    """Where a clip's build goes. IMG_9831.MOV -> output/9831."""
    stem = Path(name).stem
    digits = "".join(ch for ch in stem if ch.isdigit())
    return f"output/{digits[-4:]}" if digits else f"output/{stem.lower()}"


def catalogue():
    drives = []
    for path in sorted(ROOT.glob("*")):
        if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
            continue
        out = out_dir_for(path.name)
        entry = {
            "id": path.name,
            "name": path.name,
            "note": KNOWN.get(path.name, "Recorded footage found in the project root."),
            "out": out,
            "built": built(out),
            "player": f"/{out}/player.html",
            "report": f"/{out}/report.html",
            "size_mb": round(path.stat().st_size / 1e6, 1),
        }
        if entry["built"]:
            entry.update(summary_of(out))
        drives.append(entry)

    sims = []
    for key, sim in SIMULATIONS.items():
        out = f"output/{key}"
        entry = {"id": key, "name": sim["title"], "note": sim["blurb"],
                 "out": out, "built": built(out), "empty": "not run yet",
                 "player": f"/{out}/player.html", "report": f"/{out}/report.html",
                 "ready": (ROOT / sim["image"]).is_file()}
        if entry["built"]:
            entry.update(summary_of(out))
        sims.append(entry)

    live_out = "output/live"
    return {
        "drives": drives,
        "sims": sims,
        "live": {"built": built(live_out), "out": live_out,
                 "player": f"/{live_out}/player.html",
                 "report": f"/{live_out}/report.html",
                 **(summary_of(live_out) if built(live_out) else {})},
    }


def summary_of(out_dir):
    """Headline numbers from a built review, for the card."""
    path = ROOT / out_dir / "review.json"
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    outcome = review.get("outcome") or {}
    return {
        "headline": outcome.get("headline"),
        "critical": outcome.get("critical_findings", 0),
        "notes": outcome.get("coaching_notes", 0),
        "duration": (review.get("drive") or {}).get("duration_s"),
        "events": [
            {"title": e["title"], "severity": e["severity"],
             "level": (e.get("hardware") or {}).get("level")}
            for e in review.get("events", [])
        ],
    }


# -------------------------------------------------------------------- launch
def launch(kind, body):
    py = sys.executable
    if kind == "analyse":
        name = body.get("id")
        if not name or not (ROOT / name).is_file():
            return False, f"no such video: {name}"
        out = out_dir_for(name)
        return JOB.start(
            "analyse", f"Analysing {name}",
            [py, "-u", str(ROOT / "tools" / "build_drive.py"), name, "--out", out],
        )

    if kind == "sim":
        sim = SIMULATIONS.get(body.get("id"))
        if not sim:
            return False, "unknown simulation"
        if not (ROOT / sim["image"]).is_file():
            return False, (f"{sim['image']} is missing - analyse the clip it "
                           "comes from first, so its frames exist")
        argv = [py, "-u", str(ROOT / "laptop" / "test_live_chain.py"),
                "--moving", "--image", sim["image"], "--clear-image", sim["clear"],
                "--sign-for", "12", "--total", "34",
                "--out", f"output/{body['id']}"]
        if body.get("unoq"):
            argv.append("--unoq")
        return JOB.start("sim", sim["title"], argv, owns_board=bool(body.get("unoq")))

    if kind == "live":
        argv = [py, "-u", str(ROOT / "laptop" / "live_drive.py"),
                "--out", "output/live", "--no-open"]
        if body.get("seconds"):
            argv += ["--max-seconds", str(int(body["seconds"]))]
        if body.get("unoq"):
            argv.append("--unoq")
        return JOB.start("live", "Live drive", argv, owns_board=bool(body.get("unoq")))

    if kind == "build_live":
        video = ROOT / "output" / "live" / "drive.mp4"
        if not video.is_file():
            return False, "no live recording yet - run a live drive first"
        return JOB.start(
            "build", "Building the live review",
            [py, "-u", str(ROOT / "tools" / "make_player.py"),
             "--video", "output/live/drive.mp4",
             "--timeline", "output/live/timeline.json",
             "--out", "output/live/player.html"],
        )

    return False, f"unknown action: {kind}"


# ------------------------------------------------------------------ handler
class ConsoleHandler(RangeHandler):
    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html", "/console"):
            return self._console()
        if route == "/api/catalog":
            return self._json(200, catalogue())
        if route == "/api/job":
            return self._json(200, JOB.status())
        if route == "/api/board":
            status = serve_player.board_status()
            status["busy"] = JOB.running and JOB.owns_board
            return self._json(200, status)
        return super().do_GET()

    def do_POST(self):
        route = self.path.split("?")[0]
        if route == "/api/launch":
            body = self._body()
            # While a child process owns the board, the console must not also be
            # sending levels - two writers on one strip is nonsense nobody can
            # debug on a stage.
            ok, err = launch(body.get("action"), body)
            return self._json(200 if ok else 400, {"ok": ok, "error": err})
        if route == "/api/stop":
            return self._json(200, {"ok": JOB.stop()})
        if route == "/api/calm":
            if JOB.running and JOB.owns_board:
                return self._json(200, {"ok": False, "error": "a job owns the board"})
            if serve_player.REPLAY is None:
                return self._json(200, {"ok": False, "error": "no board attached"})
            serve_player.REPLAY.reset()
            return self._json(200, {"ok": True})
        return super().do_POST()

    def _console(self):
        try:
            body = CONSOLE_HTML.read_bytes()
        except OSError:
            body = b"<h1>report/console.html is missing</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--open", action="store_true")
    parser.add_argument(
        "--unoq", nargs="?", const="", default=None, metavar="URL",
        help="connect the UNO Q so every page served can drive it",
    )
    args = parser.parse_args()

    if args.unoq is not None:
        serve_player.BOARD, serve_player.REPLAY = connect_board(args.unoq or None)

    handler = partial(ConsoleHandler, directory=str(ROOT))
    server = None
    for port in range(args.port, args.port + 10):
        if port_in_use(port):
            continue
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"no free port in {args.port}-{args.port + 9}")

    url = f"http://127.0.0.1:{server.server_port}/"
    print()
    print(f"  DEMO CONSOLE   {url}")
    print("  everything runs from that page - no more commands")
    print("  Ctrl-C to stop")
    print()
    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping...")
    finally:
        if JOB.running:
            JOB.stop()
        if serve_player.REPLAY is not None:
            try:
                serve_player.REPLAY.reset()
                print("  board returned to level 0")
            except Exception:  # noqa: BLE001
                pass
        server.server_close()


if __name__ == "__main__":
    main()
