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

DEFAULT_URL = "http://127.0.0.1:3001/mcp"
CONNECT_TIMEOUT_S = 10.0
CALL_TIMEOUT_S = 5.0


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

    def ping(self):
        """True if the sketch itself answered - not just the server."""
        data = self._call("mcu_ping")
        return bool(data and data.get("ok"))

    def reset_drive(self):
        return self._call("reset_drive") is not None

    def set_level(self, level):
        return self._call("set_alert_level", level=int(level)) is not None


class LevelSender:
    """Sends a level only when it changes.

    The analysis produces a level for every sample, most of them identical. The
    sketch is edge triggered - it counts transitions into a level, not the
    level itself - so resending 2 would be harmless on the counter but still
    costs a USB round trip every few seconds for nothing.

    Wrapping that here rather than in each caller means the player and the
    frame loop cannot disagree about it.
    """

    def __init__(self, unoq):
        self.unoq = unoq
        self.previous = None
        self.sent = 0

    def reset(self):
        self.previous = None
        return self.unoq.reset_drive()

    def send(self, level):
        """Returns True if this call actually went to the board."""
        if level == self.previous:
            return False
        if not self.unoq.set_level(level):
            return False
        self.previous = level
        self.sent += 1
        return True
