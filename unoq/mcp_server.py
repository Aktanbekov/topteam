"""MCP server - runs on the UNO Q's Linux side, reached from the laptop over USB.

    laptop  --MCP/HTTP-->  127.0.0.1:3001  --adb forward--> this server
                                                                  |
                                    Arduino Router (Unix socket)  |
                                                                  v
                                             STM32 sketch: matrix, pixels, vibro

Start it on the board:

    python3 mcp_server.py

Then on the laptop, once per session:

    adb forward tcp:3001 tcp:3001

It binds 127.0.0.1 deliberately. Everything reaches it through the USB tunnel,
so there is no reason to expose the board's LED matrix to the Wi-Fi network -
and at the time of writing wlan0 is down anyway, which is exactly why the USB
path exists.

Set UNOQ_DRY_RUN=1 to log calls instead of touching the router. That lets the
MCP layer be tested on a laptop with no board attached.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastmcp import FastMCP  # noqa: E402

from arduino_bridge import ArduinoBridge, BridgeError  # noqa: E402

DRY_RUN = os.environ.get("UNOQ_DRY_RUN") == "1"

LEVEL_MEANING = {
    0: "driving fine",
    1: "heads up",
    2: "minor mistake",
    3: "critical mistake",
}

mcp = FastMCP("uno-q-driving-coach")


def call_mcu(method, *params, expect_reply=False):
    """One RPC to the sketch, on a fresh connection.

    Per-call connect costs well under a millisecond over a Unix socket and
    means a dropped socket cannot wedge the server between drives, which
    matters more than the saving when alerts arrive every few seconds.

    expect_reply picks REQUEST over NOTIFY. Only use it for sketch functions
    that actually return something - a void handler never answers and the call
    would sit there until it times out.
    """
    if DRY_RUN:
        print(f"[dry run] {method}{params}", flush=True)
        return "pong" if expect_reply else None

    bridge = ArduinoBridge()
    try:
        bridge.connect()
        if expect_reply:
            return bridge.call(method, *params)
        bridge.notify(method, *params)
        return None
    finally:
        bridge.close()


@mcp.tool
def set_alert_level(level: int) -> dict:
    """Set the driving alert level on the coach hardware.

    0 = driving fine, 1 = heads up, 2 = minor mistake, 3 = critical mistake.

    Levels 2 and 3 are edge triggered on the board: the sketch counts the
    transition into a level, not the level itself, so sending 2 repeatedly
    buzzes once rather than once per call.
    """
    if not isinstance(level, int) or isinstance(level, bool):
        raise ValueError(f"level must be an integer, got {type(level).__name__}")
    if level not in LEVEL_MEANING:
        raise ValueError(f"level must be 0, 1, 2 or 3 - got {level}")

    try:
        call_mcu("alert", level)
    except BridgeError as exc:
        raise RuntimeError(f"could not reach the microcontroller: {exc}") from exc

    return {"ok": True, "level": level, "meaning": LEVEL_MEANING[level]}


@mcp.tool
def reset_drive() -> dict:
    """Clear the strike counter and return the hardware to the calm state.

    The count lives on the microcontroller so it survives between runs. Call
    this at the start of every drive or the strip starts part lit.
    """
    try:
        call_mcu("reset")
    except BridgeError as exc:
        raise RuntimeError(f"could not reach the microcontroller: {exc}") from exc

    return {"ok": True, "reset": True}


@mcp.tool
def mcu_ping() -> dict:
    """Check the whole chain: this server, the router, and the sketch.

    Unlike the other two this waits for the sketch to answer, so a reply proves
    the microcontroller is running our sketch and not, say, the hardware test.
    """
    try:
        reply = call_mcu("mcu_ping", expect_reply=True)
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": reply == "pong", "reply": reply}


def main():
    host = os.environ.get("UNOQ_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("UNOQ_MCP_PORT", "3001"))

    if DRY_RUN:
        print("UNOQ_DRY_RUN=1 - logging calls, not touching the router")
    print(f"MCP server on http://{host}:{port}/mcp", flush=True)
    print("On the laptop:  adb forward tcp:3001 tcp:3001", flush=True)

    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
