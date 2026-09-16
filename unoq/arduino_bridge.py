"""Talk to the Arduino Router from the UNO Q's Linux side. No dependencies.

The router speaks MessagePack-RPC over a Unix socket:

    REQUEST  [0, msgid, "method", [args...]]
    RESPONSE [1, msgid, error, result]
    NOTIFY   [2, "method", [args...]]

This used to import `msgpack`, which is not installed on the board and cannot
be installed: there is no pip, no pipx, wlan0 is down so there is no route to
PyPI, no cached .deb to apt from, and sudo wants a password we do not have.

Rather than fight that, note how little MessagePack we actually need. Every
message we send is an array of at most four items holding small integers, a
short method name, and an array of small integers. That is three type tags:

    positive fixint   0x00-0x7f   integers 0..127, encoded as themselves
    fixstr            0xa0 | len  strings shorter than 32 bytes
    fixarray          0x90 | len  arrays shorter than 16 items

So the encoder below is about twenty lines and the board needs nothing
installed. The wider formats (uint8/16/32, str8, negative ints) are here too
because they cost three lines each and save a baffling failure later if a
method name grows past 31 characters.
"""

import socket
import struct

ROUTER_SOCKET_PATH = "/var/run/arduino-router.sock"

REQUEST = 0
RESPONSE = 1
NOTIFY = 2


class BridgeError(RuntimeError):
    """The router socket is missing, closed, or said something we cannot read."""


# ------------------------------------------------------------------ encoding
def pack(obj):
    """MessagePack-encode the subset of types the router RPC uses."""
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"

    if isinstance(obj, int):
        if 0 <= obj <= 0x7F:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([0xE0 | (obj + 32)])
        if 0 <= obj <= 0xFF:
            return b"\xcc" + bytes([obj])
        if 0 <= obj <= 0xFFFF:
            return b"\xcd" + struct.pack(">H", obj)
        if 0 <= obj <= 0xFFFFFFFF:
            return b"\xce" + struct.pack(">I", obj)
        if -128 <= obj <= 127:
            return b"\xd0" + struct.pack(">b", obj)
        if -32768 <= obj <= 32767:
            return b"\xd1" + struct.pack(">h", obj)
        return b"\xd2" + struct.pack(">i", obj)

    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        if len(raw) < 32:
            return bytes([0xA0 | len(raw)]) + raw
        if len(raw) <= 0xFF:
            return b"\xd9" + bytes([len(raw)]) + raw
        return b"\xda" + struct.pack(">H", len(raw)) + raw

    if isinstance(obj, (list, tuple)):
        if len(obj) < 16:
            head = bytes([0x90 | len(obj)])
        elif len(obj) <= 0xFFFF:
            head = b"\xdc" + struct.pack(">H", len(obj))
        else:
            head = b"\xdd" + struct.pack(">I", len(obj))
        return head + b"".join(pack(item) for item in obj)

    raise BridgeError(f"cannot encode {type(obj).__name__} for the router")


# ------------------------------------------------------------------ decoding
def unpack(data, index=0):
    """Decode one value. Returns (value, next_index).

    Only the types a router response can contain. Anything else raises rather
    than guessing, because a silently wrong decode is worse than a clear error.
    """
    if index >= len(data):
        raise BridgeError("truncated reply from the router")

    tag = data[index]
    index += 1

    if tag <= 0x7F:
        return tag, index
    if tag >= 0xE0:
        return tag - 256, index
    if 0x80 <= tag <= 0x8F:  # fixmap
        return _unpack_map(data, index, tag & 0x0F)
    if 0x90 <= tag <= 0x9F:  # fixarray
        return _unpack_array(data, index, tag & 0x0F)
    if 0xA0 <= tag <= 0xBF:  # fixstr
        return _unpack_str(data, index, tag & 0x1F)

    if tag == 0xC0:
        return None, index
    if tag == 0xC2:
        return False, index
    if tag == 0xC3:
        return True, index

    if tag == 0xCC:
        return data[index], index + 1
    if tag == 0xCD:
        return struct.unpack_from(">H", data, index)[0], index + 2
    if tag == 0xCE:
        return struct.unpack_from(">I", data, index)[0], index + 4
    if tag == 0xD0:
        return struct.unpack_from(">b", data, index)[0], index + 1
    if tag == 0xD1:
        return struct.unpack_from(">h", data, index)[0], index + 2
    if tag == 0xD2:
        return struct.unpack_from(">i", data, index)[0], index + 4

    if tag == 0xD9:
        return _unpack_str(data, index + 1, data[index])
    if tag == 0xDA:
        return _unpack_str(data, index + 2, struct.unpack_from(">H", data, index)[0])
    if tag == 0xDC:
        return _unpack_array(data, index + 2, struct.unpack_from(">H", data, index)[0])

    raise BridgeError(f"unsupported MessagePack tag 0x{tag:02x} in reply")


def _unpack_str(data, index, length):
    end = index + length
    if end > len(data):
        raise BridgeError("truncated string in reply")
    return data[index:end].decode("utf-8", "replace"), end


def _unpack_array(data, index, count):
    out = []
    for _ in range(count):
        value, index = unpack(data, index)
        out.append(value)
    return out, index


def _unpack_map(data, index, count):
    out = {}
    for _ in range(count):
        key, index = unpack(data, index)
        value, index = unpack(data, index)
        out[key] = value
    return out, index


# -------------------------------------------------------------------- bridge
class ArduinoBridge:
    """A connection to the Arduino Router's Unix socket."""

    def __init__(self, socket_path=ROUTER_SOCKET_PATH, timeout=5.0):
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock = None
        self._msgid = 0

    def connect(self):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except OSError as exc:
            raise BridgeError(
                f"could not connect to {self.socket_path}: {exc}\n"
                "Is the router running?  systemctl status arduino-router"
            ) from exc
        self._sock = sock
        return self

    def _send(self, message):
        if self._sock is None:
            self.connect()
        try:
            self._sock.sendall(pack(message))
        except OSError as exc:
            raise BridgeError(f"failed sending to the router: {exc}") from exc

    def notify(self, method, *args):
        """Fire and forget. The sketch acts on it; nothing comes back."""
        self._send([NOTIFY, method, list(args)])

    def call(self, method, *args):
        """Send a request and wait for the sketch's reply.

        Only use this for functions the sketch actually returns a value from.
        A `void` handler will never answer and this will time out.
        """
        self._msgid = (self._msgid + 1) % 0xFFFF
        msgid = self._msgid
        self._send([REQUEST, msgid, method, list(args)])

        try:
            data = self._sock.recv(4096)
        except OSError as exc:
            raise BridgeError(f"no reply from {method}: {exc}") from exc
        if not data:
            raise BridgeError(f"router closed the connection during {method}")

        reply, _ = unpack(data)
        if not isinstance(reply, list) or len(reply) < 4 or reply[0] != RESPONSE:
            raise BridgeError(f"unexpected reply to {method}: {reply!r}")
        if reply[2] is not None:
            raise BridgeError(f"{method} failed: {reply[2]!r}")
        return reply[3]

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc_info):
        self.close()
