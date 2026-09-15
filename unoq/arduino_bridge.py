"""Minimal client for the arduino-router daemon on the UNO Q.

The router listens on a Unix socket and speaks MessagePack RPC. We only need
fire-and-forget calls (NOTIFY), so this stays small: no response tracking, no
background thread.

Protocol (from the Arduino Router RPC docs):
    REQUEST  [0, msgid, "method", [args...]]
    RESPONSE [1, msgid, error, result]
    NOTIFY   [2, "method", [args...]]
"""

import socket

import msgpack

ROUTER_SOCKET_PATH = "/var/run/arduino-router.sock"


class BridgeError(RuntimeError):
    """Raised when the router socket is unavailable or a send fails."""


class ArduinoBridge:
    """Talks to the MCU sketch through the arduino-router daemon."""

    def __init__(self, socket_path=ROUTER_SOCKET_PATH):
        self.socket_path = socket_path
        self.sock = None

    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(self.socket_path)
        except OSError as exc:
            self.sock = None
            raise BridgeError(
                f"could not connect to {self.socket_path}: {exc}. "
                "Is arduino-router running? Check with: systemctl status arduino-router"
            ) from exc

    def notify(self, method, *args):
        """Send a fire-and-forget call to a function the sketch provides."""
        if self.sock is None:
            self.connect()

        message = [2, method, list(args)]
        try:
            self.sock.sendall(msgpack.packb(message))
        except OSError as exc:
            # The router restarts independently of us, so drop the dead socket
            # and let the next notify() reconnect.
            self.close()
            raise BridgeError(f"send failed for {method!r}: {exc}") from exc

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc_info):
        self.close()
