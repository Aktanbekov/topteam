# UNO Q side - driver signals

The three outputs the driver actually sees, driven by one alert level from the
laptop.

```
laptop  --HTTP-->  alert_listener.py  --Bridge.notify-->  alert_sketch.ino
       (network)      (Linux side)        (Unix socket)       (MCU side)
                                                                   |
                                          built-in 8x13 matrix ----+
                                          Modulino Pixels (0x6C) --+
                                          Modulino Vibro  (0x70) --+
```

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
3. In the library manager, install **Modulino** and **Arduino_LED_Matrix**.
4. Paste in [`alert_sketch/alert_sketch.ino`](alert_sketch/alert_sketch.ino).
5. Deploy it to the board.

The sketch registers two functions, `alert(int level)` and `reset()`:

| level | meaning | strip (8 RGB) | vibration | matrix |
|---|---|---|---|---|
| 0 | driving fine | green | — | calm bar |
| 1 | heads up | amber | — | calm bar |
| 2 | minor mistake | +1 red | one buzz | count for 2s |
| 3 | critical mistake | all flash red | three buzzes | big X |

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

Open a terminal on the UNO Q (App Lab has one, or SSH in), then:

```bash
pip3 install msgpack --break-system-packages
```

Copy `arduino_bridge.py` and `alert_listener.py` onto the board, into the same
folder, then start the listener:

```bash
python3 alert_listener.py
```

You should see `connected to arduino-router` and `listening on http://0.0.0.0:8080`.

## 3. Find the board's IP address

On the UNO Q:

```bash
hostname -I
```

Note the address — the laptop needs it.

## 4. Test from the laptop

```bash
python laptop\test_signals.py --host <unoq-ip>
```

It resets the counter, then walks levels 0 to 3, pausing 3 seconds on each and
printing what each one should look like on all three outputs.

To send a single level:

```bash
python laptop\alert_client.py 3 --host <unoq-ip>
```

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

**The matrix stays dark** — this is the most likely thing to need adjusting. The
sketch uses `matrix.draw(frame)` with a `uint8_t frame[104]` laid out as
`frame[row * 13 + col]`. If your library version wants something else, that call
and `setPixel()` are the only two places to change. The library also ships
`add_to_frame(char, pos)` for text if you would rather not keep the 3x5 font in
the sketch.

**Laptop can't reach the board** — both devices must be on the same network.
Check with `ping <unoq-ip>` from the laptop. The listener binds `0.0.0.0`, so it
accepts connections from anywhere on the network, not just localhost.
