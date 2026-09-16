"""Talk to the UNO Q's MCP server from the laptop, over the USB tunnel.

    laptop code  ->  this  ->  127.0.0.1:3001  --adb forward-->  UNO Q
                                                                    |
                                                        mcp_server.py
                                                                    |
                                              Arduino Router -> sketch

Set the tunnel up once per session:

    adb forward tcp:3001 tcp:3001

The FastMCP client is async and the two things that need it - the player's HTTP
handler and the frame loop in analyze_video - are both synchronous. Rather than
make them async, this keeps one event loop on a background thread and hands
work to it. The MCP session stays open for the whole drive, so an alert costs a
single round trip rather than a fresh handshake.

Nothing here raises at the caller. A missing board must never take down a drive
analysis: every call returns True or False and the reason lands in .last_error.

    unoq = UnoQ()
    if unoq.connect():
        unoq.reset_drive()
        unoq.set_level(2)
    unoq.close()
"""

import asyncio
import os
import threading
import time

from config import UNOQ_MCP_URL as DEFAULT_URL

CONNECT_TIMEOUT_S = 10.0
CALL_TIMEOUT_S = 5.0

# mcu_ping costs a USB round trip, so the status panel is allowed to reuse a
# recent answer rather than pinging the board four times a second.
PING_CACHE_S = 5.0


class UnoQ:
    """Synchronous wrapper around the board's MCP tools."""

    def __init__(self, url=None, timeout=CALL_TIMEOUT_S):
        self.url = url or os.environ.get("UNOQ_MCP_URL", DEFAULT_URL)
        self.timeout = timeout
        self.last_error = None
        self.connected = False

        self._loop = None
        self._thread = None
        self._client = None
        self._session = None
        self._ping_ok = None
        self._ping_at = 0.0

    # ------------------------------------------------------------- plumbing
    def _start_loop(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="unoq-mcp", daemon=True
        )
        self._thread.start()

    def _run(self, coro, timeout):
        """Run a coroutine on the background loop and wait for it here."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    # ---------------------------------------------------------------- setup
    def connect(self):
        """Open the MCP session. False if the board is not reachable."""
        if self.connected:
            return True

        try:
            from fastmcp import Client
        except ImportError as exc:
            self.last_error = (
                f"fastmcp is not installed on this laptop: {exc}\n"
                "  pip install --only-binary :all: cryptography && pip install fastmcp"
            )
            return False

        if self._loop is None:
            self._start_loop()

        async def open_session():
            client = Client(self.url, timeout=self.timeout)
            session = await client.__aenter__()
            return client, session

        try:
            self._client, self._session = self._run(open_session(), CONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - any failure means "no board"
            self.last_error = (
                f"no MCP server at {self.url}: {exc}\n"
                "  Is the tunnel up?      adb forward tcp:3001 tcp:3001\n"
                "  Is the board serving?  adb shell 'cd ~/topteam && python3 mcp_server.py'"
            )
            return False

        self.connected = True
        self.last_error = None
        return True

    def close(self):
        if self._client is not None:
            try:
                self._run(self._client.__aexit__(None, None, None), self.timeout)
            except Exception:  # noqa: BLE001 - shutting down, nothing to salvage
                pass
            self._client = self._session = None
        self.connected = False

        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
            self._loop = self._thread = None

    # ---------------------------------------------------------------- tools
    def _call(self, tool, **args):
        if not self.connected:
            self.last_error = "not connected"
            return None
        try:
            result = self._run(self._session.call_tool(tool, args), self.timeout)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{tool} failed: {exc}"
            return None
        self.last_error = None
        return result.data

    def ping(self, max_age_s=PING_CACHE_S):
        """True if the sketch itself answered - not just the server.

        This is the only call that proves the SKETCH is running. The others are
        notifications: they prove the router accepted our bytes and nothing
        more. Cached briefly so a status panel can ask often without flooding
        the USB link.
        """
        now = time.monotonic()
        if self._ping_ok is not None and now - self._ping_at < max_age_s:
            return self._ping_ok
        data = self._call("mcu_ping")
        self._ping_ok = bool(data and data.get("ok"))
        self._ping_at = now
        return self._ping_ok

    def status(self):
        """Everything the hardware panel needs, in one dict."""
        return {
            "connected": self.connected,
            "url": self.url,
            "sketch_ok": self.ping() if self.connected else False,
            "error": self.last_error,
        }

    def reset_drive(self):
        return self._call("reset_drive") is not None

    def set_level(self, level):
        return self._call("set_alert_level", level=int(level)) is not None


class EventReplay:
    """Plays a drive's level transitions to the board, each one exactly once.

    The review player scrubs. Without this, seeking back over a critical event
    and playing forward again would buzz the motor a second time and, for a
    level 2, tick the strike counter again - the sketch is edge triggered on
    the level, so any re-entry looks like a fresh mistake to it.

    Fixing that on the board would mean a protocol change and a re-flash, which
    is not something to do the night before a demo. So the rule lives here and
    is stated in one line: a transition is applied only if it is further
    through the drive than anything applied so far.

        play from the start   every transition fires, in order
        seek back, play on    already-played transitions are skipped, and new
                              ones resume once the playhead passes the furthest
                              point reached
        Replay drive          resets the board and the counter, and it all runs
                              again from scratch

    That is monotone, needs no sketch change, and is one sentence to explain to
    a judge who asks why scrubbing does not inflate the score.
    """

    def __init__(self, unoq):
        self.unoq = unoq
        self.max_seq = -1
        self.level = None
        self.sent = 0
        self.skipped = 0
        self.last_error = None

    def reset(self):
        """Start a fresh drive: clear the board's counter and our own memory."""
        self.max_seq = -1
        self.level = None
        self.sent = 0
        self.skipped = 0
        ok = self.unoq.reset_drive()
        if not ok:
            self.last_error = self.unoq.last_error
        return ok

    def apply(self, level, seq):
        """Send one transition. Returns "sent", "skipped" or "failed"."""
        if seq <= self.max_seq:
            self.skipped += 1
            return "skipped"

        self.max_seq = seq
        if level == self.level:
            # A later transition that happens to land on the same level - the
            # board is already showing it, and re-sending would re-trigger the
            # edge.
            return "skipped"

        if not self.unoq.set_level(level):
            self.last_error = self.unoq.last_error
            return "failed"

        self.level = level
        self.sent += 1
        return "sent"

    def status(self):
        return {
            "level": self.level,
            "events_sent": self.sent,
            "events_skipped": self.skipped,
            "position": self.max_seq,
            "error": self.last_error,
        }


# The old name, kept so an older script or a stale shell does not break. New
# code uses EventReplay, which is the one that knows about seeking.
LevelSender = EventReplay
