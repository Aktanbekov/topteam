# UNO Q side - alert signal test

Smoke test for the signal path only. No wiring needed: it uses the board's
built-in LED. Once this works, we swap in the real LEDs and vibration motor.

```
laptop  --HTTP-->  alert_listener.py  --Bridge.notify-->  alert_sketch.ino  -->  LED
       (network)      (Linux side)        (Unix socket)       (MCU side)
```

## 1. Flash the sketch

1. Connect the UNO Q to the laptop with the USB-C cable.
2. Open **Arduino App Lab** and create a new app.
3. Paste in [`alert_sketch/alert_sketch.ino`](alert_sketch/alert_sketch.ino).
4. Deploy it to the board.

The sketch registers one function, `alert(int level)`, and blinks the built-in
LED at a different rate per level:

| level | meaning | LED |
|---|---|---|
| 0 | driving fine | off |
| 1 | heads up | slow blink |
| 2 | minor mistake | fast blink |
| 3 | critical mistake | rapid strobe |

The built-in LED is **active-low** — `LOW` turns it on. That trips people up.

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

It walks levels 0 to 3, pausing 3 seconds on each. Watch the LED change pattern.

To send a single level:

```bash
python laptop\alert_client.py 3 --host <unoq-ip>
```

## Troubleshooting

**`could not connect to /var/run/arduino-router.sock`** — the router daemon
isn't running. On the UNO Q: `systemctl status arduino-router`, and restart it
with `sudo systemctl restart arduino-router`.

**Listener starts but the LED never moves** — the sketch isn't deployed, or the
function name doesn't match. It must be exactly `alert` on both sides. Check the
App Lab monitor for the `alert level -> N` lines the sketch prints.

**Laptop can't reach the board** — both devices must be on the same network.
Check with `ping <unoq-ip>` from the laptop. The listener binds `0.0.0.0`, so it
accepts connections from anywhere on the network, not just localhost.
