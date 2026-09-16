# Archive — the HTTP-over-Wi-Fi transport

Nothing in this folder runs, nothing imports it, and neither script is on any
path the product uses. They are kept because the design is sound and because
deleting them would lose the reason the current design exists.

| file | was | replaced by |
|---|---|---|
| `alert_client.py` | laptop → board over HTTP on the local network | [`laptop/unoq_mcp.py`](../laptop/unoq_mcp.py) |
| `alert_listener.py` | board-side HTTP listener | [`unoq/mcp_server.py`](../unoq/mcp_server.py) |

**Why they were replaced.** `wlan0` on the UNO Q was down out of the box, so
`hostname -I` returned only docker's `172.17.0.1` — there was no address for the
laptop to POST to. Bringing Wi-Fi up is possible (the board now has an address)
but a hotspot is one more thing to fail on stage, so the link moved to MCP over
a USB/ADB tunnel and stayed there.

They also depended on `pip3 install msgpack`, which that board cannot do: no
pip, no internet, no cached `.deb`, and sudo wants a password we do not have.
[`unoq/arduino_bridge.py`](../unoq/arduino_bridge.py) now encodes the three
MessagePack type tags the router RPC needs, in about twenty lines, and needs
nothing installed.

If you ever need to drive the board from a machine with no USB access, start
here rather than from scratch — but bring `arduino_bridge.py` with you and do
not reintroduce the msgpack dependency.
