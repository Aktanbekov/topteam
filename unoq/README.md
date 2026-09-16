# UNO Q side - driver signals

The three outputs the driver actually sees, driven by one alert level from the
laptop.

```
laptop  --MCP over USB-->  mcp_server.py  --Bridge-->  alert_sketch.ino
       (adb forward 3001)    (Linux side)  (Unix sock)     (MCU side)
                                                                  |
                                         built-in 8x13 matrix ----+
                                         Modulino Pixels (0x6C) --+
                                         Modulino Vibro  (0x70) --+
```

**Start at [MCP over USB](#mcp-over-usb-no-network-needed) at the bottom — that
is the path that works.** Sections 2 to 4 below describe an earlier HTTP-over-
Wi-Fi design and are kept only for the level table and the troubleshooting;
`wlan0` on this board is down, so that path cannot work as written.

## 0. Plug in the Modulinos

No wiring, no soldering, no resistors, and **no transistor** — the Vibro has its
own STM32 and MOSFET on board.

1. Qwiic cable from the UNO Q's **QWIIC** connector to either Modulino.
2. Second Qwiic cable from that Modulino to the other one. Order does not matter.

Each Modulino has two Qwiic connectors so they daisy-chain on one I²C bus. Their
default addresses differ (Pixels `0x6C`, Vibro `0x70`), so no address changing is
needed. A small green LED on each board lights when it has power — check that
before blaming the code.

## 1. Flash the sketch

1. Connect the UNO Q to the laptop with the USB-C cable.
2. Open **Arduino App Lab** and create a new app.
3. In the library manager, install **Modulino**. That is the only one you need —
   it covers every Modulino node, Pixels and Vibro included.
4. Paste in [`alert_sketch/alert_sketch.ino`](alert_sketch/alert_sketch.ino).
5. Deploy it to the board.

**Don't look for `Arduino_LED_Matrix` in the library manager — it isn't there.**
It ships inside the Arduino Zephyr Core that the UNO Q runs on, so `#include
<Arduino_LED_Matrix.h>` just works. Either spelling of the class compiles; the
header typedefs `ArduinoLEDMatrix` to `Arduino_LED_Matrix`.

What does *not* carry over from UNO R4 examples is `canvasWidth` /
`canvasHeight` — on this version they are private, and only exist when
ArduinoGraphics is present. Write 13 and 8.

The header is worth reading if the matrix misbehaves:
`C:\Users\<you>\AppData\Local\Arduino15\packages\arduino\hardware\zephyr\1.0.0\libraries\Arduino_LED_Matrix\src\`

## Known-good baseline

[`hardware_test/hardware_test.ino`](hardware_test/hardware_test.ino) exercises
all three outputs with no bridge, no network and no laptop. **Deploy it first
after any wiring change.** If it passes and `alert_sketch.ino` does not, the
problem is our code, not the modules.

It is also the API reference — `alert_sketch.ino` is written to match its calls
exactly. The one thing it does that the alert sketch must never do is use
blocking `delay()`, which would stall the router bridge.

The sketch registers two functions, `alert(int level)` and `reset()`:

| level | meaning | strip (8 RGB) | vibration | matrix |
|---|---|---|---|---|
| 0 | driving fine | green | — | calm bar |
| 1 | heads up | amber | — | calm bar |
| 2 | minor mistake | +1 red | one `MEDIUM` pulse | count for 2s |
| 3 | critical mistake | all flash red | `GENTLE`→`INTENSE`→`MAXIMUM` | big X |

The critical buzz **ramps** rather than hitting three identical times. The
driver should be alerted, not startled — startling a learner at the wheel is a
hazard of its own. The full scale is `STOP, GENTLE, MODERATE, MEDIUM, INTENSE,
POWERFUL, MAXIMUM` if you want to retune it.

**Why level 1 is silent.** The brief has it buzzing at every stop sign. Don't. If
it buzzes at every junction the driver tunes it out within ten minutes, and then
it fails when it matters. A buzz means exactly one thing: *you made a mistake*.
The amber LED carries the heads-up.

**The strike count lives on the microcontroller.** The sketch counts transitions
into level 2 itself, so the laptop protocol did not have to change — but the
count survives between runs. Call `reset()` at the start of each drive.

**Digits only briefly.** The matrix shows a bar while you drive and the actual
number for two seconds after a new strike. Reading digits at speed is a bad
idea; a short confirmation right after the event is not.

**Nothing blocks.** `delay()` would stall the bridge and make the board miss the
next alert, so every animation runs off `millis()` and the buzz is queued.

## 2. Set up the Linux side

> **Superseded.** This section used to say `pip3 install msgpack
> --break-system-packages` and then start `alert_listener.py` on a network
> port. Neither works on this board: there is no pip, and `wlan0` is down so
> there is no address to connect to. `arduino_bridge.py` now encodes
> MessagePack itself and needs nothing installed, and the laptop reaches the
> board over USB. See [MCP over USB](#mcp-over-usb-no-network-needed).

## 3. Find the board's IP address

> **Superseded.** `hostname -I` returns only `172.17.0.1`, which is docker's
> bridge and `linkdown` — not an address the laptop can use. USB instead.

## 4. Test from the laptop

Quickest check that the whole chain is alive, straight over USB:

```bash
adb shell 'cd /home/arduino/topteam && python3 -c "
from arduino_bridge import ArduinoBridge
b = ArduinoBridge().connect()
b.notify(\"reset\"); b.notify(\"alert\", 2)
b.close()"'
```

One buzz, a red LED, and `1` on the matrix. This needs no MCP server and no
Wi-Fi — just the sketch deployed and the USB cable in.

## Troubleshooting

**`could not connect to /var/run/arduino-router.sock`** — the router daemon
isn't running. On the UNO Q: `systemctl status arduino-router`, and restart it
with `sudo systemctl restart arduino-router`.

**Listener starts but nothing moves** — the sketch isn't deployed, or the
function name doesn't match. It must be exactly `alert` on both sides. Check the
App Lab monitor for the `alert level -> N  strikes: N` lines the sketch prints.

**The matrix works but the Modulinos don't** — check the green power LED on each
board first, then the Qwiic cables. If only one responds, the two may have been
set to the same address; the Modulino library ships an `AddressChanger` example.

**The matrix stays dark** — the sketch uses `matrix.draw(frame)` with a
`uint8_t frame[104]` laid out as `frame[row * 13 + col]`, writing `1` for a lit
pixel, exactly as `hardware_test.ino` does. `draw()` feeds
`matrixGrayscaleWrite()`, so in principle the buffer is brightness rather than
on/off — but `1` demonstrably lights a pixel on this board, so leave it alone.
`setGrayscaleBits()` is the knob if you ever want real brightness levels.

Deliberately **not** using the library's `loadPixels()` / `renderBitmap()`
convenience path. It calls `loadPixelsToBuffer()` with a `uint32_t _frameHolder[3]`
(96 bits) and then hands it to `loadFrame()`, which reads four words — fine for
the UNO R4's 12x8 = 96 pixels, one word short for this board's 8x13 = 104.
`draw()` avoids that code entirely.

**Laptop can't reach the board** — check `adb devices` first; it should list one
device, not `unauthorized` or nothing. Then `adb forward --list` should show
`tcp:3001 tcp:3001`. The forward does not survive unplugging the cable, so
re-run it after a reconnect.

**`secure_mkdirs failed: No such file or directory` on `adb push`** — Git Bash
rewrote `/home/arduino/...` into `C:/Program Files/Git/home/...`. Prefix the
command with `MSYS_NO_PATHCONV=1`.

## MCP over USB (no network needed)

`wlan0` on this board is down, so the laptop cannot reach it by IP at all —
`hostname -I` returns only docker's `172.17.0.1`. Everything goes over USB
instead, which is more robust for a demo anyway: no Wi-Fi to fail on stage.

### One-time, on the board

Needs internet on the board just for the install. Bring up Wi-Fi:

```bash
nmcli dev wifi connect "<SSID>" password "<password>"
```

```bash
sudo apt update && sudo apt install -y python3-pip && pip3 install fastmcp --break-system-packages
```

`arduino_bridge.py` needs nothing installed — it encodes MessagePack itself,
precisely because the board could not install `msgpack` either.

### Every session

Push the board-side files and start the MCP server:

```bash
adb push unoq/arduino_bridge.py unoq/mcp_server.py /home/arduino/topteam/
```

```bash
adb shell 'cd /home/arduino/topteam && python3 mcp_server.py'
```

Then on the laptop, open the tunnel:

```bash
adb forward tcp:3001 tcp:3001
```

`adb` ships with App Lab at
`%LOCALAPPDATA%\Arduino15\packages\arduino\tools\adb\32.0.0\adb.exe`. In Git
Bash, prefix commands with `MSYS_NO_PATHCONV=1` or it rewrites `/home/arduino`
into a Windows path and the push fails with `secure_mkdirs failed`.

### Drive the hardware from the review player

```bash
python tools/serve_player.py --open --unoq
```

As the video plays, the page posts each level change to its own origin and the
server relays it over the tunnel. The browser cannot speak MCP or see the
tunnel, so the relay has to live on the laptop side.

Without `--unoq` the player behaves exactly as before. With it but no board,
you get one warning and the page still works — the review page is the whole
product when the hardware is not plugged in.

### The three MCP tools

| tool | what it does |
|---|---|
| `set_alert_level(level)` | 0-3, edge triggered on the board |
| `reset_drive()` | clears the strike count |
| `mcu_ping()` | the only call that waits for a reply, so it proves the **sketch** is running, not just the router |
