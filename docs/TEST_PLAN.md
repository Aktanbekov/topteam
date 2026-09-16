# Test plan

Three layers, and the split matters: everything that decides whether a driver
made a mistake is testable in under a second with no model, no video and no
board. Only the things that genuinely need hardware need hardware.

```bash
python -m pytest
```

117 tests, well under a second, no GenieX, no UNO Q, no video file.

---

## 1. Automated — `tests/`

| file | what it pins down |
|---|---|
| `test_stop_sign_rules.py` | approaches, the three grades, and **when** a verdict is due |
| `test_scene_filter.py` | two-sample confirmation, `known_at`, JSON repair |
| `test_review_schema.py` | the shape everything downstream reads, and the promises it makes |
| `test_hardware_protocol.py` | hand-rolled MessagePack, and seek-safe replay |
| `test_report.py` | script-safe embedding, deterministic prose, narrative guard |
| `test_serve_api.py` | relay input validation, and the Windows port trap |

### The tests that exist because something was wrong

- **`test_a_failure_is_not_decided_before_its_deadline`** — the old build
  returned level 3 from the moment the sign left the frame, ten seconds early.
  The test probes the level track at 0, 9, 15, 20 and 24.9s and asserts it is
  below 3 at every one, then exactly 3 at 25.0s.
- **`test_an_unvalidated_sighting_never_reaches_the_hardware`** — `stop_sign` is
  deliberately excluded from `scene_filter` confirmation, so anything reading
  `confirmed["stop_sign"]` is reading an unconfirmed claim. One sighting must
  produce no approach, no event, and a level track that never leaves 0.
- **`test_a_brief_stop_is_a_coaching_note_and_never_a_strike`** — California
  puts no number on "complete stop". The three-second target is ours, so
  counting it as an error would be inventing a rule and failing someone against it.
- **`test_seeking_back_does_not_buzz_or_strike_twice`** — the sketch is edge
  triggered on the level, so a reviewer scrubbing back over a critical event
  would otherwise inflate their own score.
- **`test_no_official_dmv_claim_anywhere`** — greps the whole serialised review
  for "dmv pass", "you failed" and friends. The one sentence this product must
  never print.
- **`test_bake_cannot_close_the_script_block`** — the drive filename comes from
  the user and lands inside `<script>`. `json.dumps` does not escape `</script>`.
- **`test_a_narrative_that_claims_a_violation_is_rejected`** — asked to summarise
  "possible incomplete stop", a text model reaches for "you ran a stop sign".
- **`test_a_confirmed_red_light_actually_reaches_the_driver`** — both red-light
  runs on the real clip were corroborated only by their own last sample, so the
  level span was zero-length and a genuine detection produced no warning at all.
- **`test_port_in_use_sees_a_listening_socket`** — Windows lets a second socket
  bind an address another is already listening on, so three stale servers ended
  up sharing port 8000 and one of them answered `/api/status` with a 404. The
  page concluded there was no hardware while the board sat there connected.

### Fixtures

`tests/fixtures.py` builds three deterministic drives — PASS (4.0s stop), BRIEF
(1.07s, the real measurement from IMG_9830) and FAIL (no stop). Motion scores use
the bands measured on real footage (0.18 stopped, 0.9 moving) rather than 0 and
100, so a test cannot pass on a margin the real signal does not have.

They are labelled fixtures in the file, in the review (`source: "fixture"`), and
in an amber banner on the page. They are never shown as model output.

---

## 2. Pipeline — needs GenieX, no board

```bash
./run.sh --demo pass
./run.sh --demo brief
./run.sh --demo fail
```

| check | expected |
|---|---|
| grade | `[FULL ]`, `[SHORT]`, `[FLAG ]` respectively |
| outcome | "No reviewed critical error detected" ×2, then "Possible critical error detected" |
| decision time on fail | 16s, ten seconds after the sign was last seen at 6s |
| banner | amber "synthetic footage" on all three |
| metrics | non-zero NPU calls, a warm median around 3.4s |

Prompt regression, against the hand-labelled frames:

```bash
python tools/eval_scene_prompt.py
```

Watch the **FALSE ALARM** column. `strict` should be at 0 on those 38 frames. A
missed detection costs one finding; a false alarm costs a fabricated error in
front of a judge.

Inference timing, and where it actually runs:

```bash
python tools/benchmark_compute.py --label npu --frames 6
```

```bash
python tools/benchmark_compute.py --compare
```

Measured 2026-09-16: warm median **3.44s** over 5 calls, first call 13.4s
including model load. Asking for `-c cpu` produces an identical number and a
log line saying the plugin ignored the request and ran on the NPU anyway, so
`--compare` refuses to print a speedup. Check that it still refuses.

Motion threshold on new footage:

```bash
python laptop/test_video.py <clip>
```

The right threshold sits in the gap between the two clusters. On IMG_9830 that
gap is 0.24 to 0.31 — only 25% — so do not tighten 0.25 without re-running this.

### Degraded paths

| simulate | how | expected |
|---|---|---|
| GenieX down | stop the server, `./run.sh --demo fail` | falls back to recorded scene labels, amber "replayed labels" banner, no NPU badge |
| failed model calls | stop GenieX mid-run | those samples carry no observations; page says so; **no fabricated detections** |
| no narrative model | it is not pulled | report renders the deterministic summary; no error |
| unplayable video | open `player.html` from `file://` | page still works, notice appears, report unaffected |
| no board | omit `--unoq` | hardware panel says "not connected"; everything else works |

---

## 3. Hardware — needs the UNO Q

```bash
python laptop/test_signals.py
```

Walks all four levels. Check the board, do not trust the script:

| level | strip | matrix | you feel |
|---|---|---|---|
| 0 | green | calm bar | — |
| 1 | all amber, breathing | octagon | one gentle tap |
| 2 | +1 red | the count for 2s | one firm pulse |
| 3 | all flash red | big X | three, building |

Then the real path:

```bash
./run.sh --demo fail --unoq
```

- [ ] Header badge reads **UNO Q: connected**; panel says *answering (mcu_ping)*
- [ ] 6s: amber, octagon, one tap
- [ ] 16s: red flash, X, three ramping buzzes — **not before 16s**
- [ ] **Replay drive from the start**: board resets, everything fires again
- [ ] Scrub back over 16s and play forward: **no second buzz**, and the panel's
      "skipped on rewind" count goes up
- [ ] Ctrl-C: board is left on level 0, not strobing

### Verified on the board (2026-09-16)

Through the full one-command path, `./run.sh --demo fail --unoq`, not a mock:

- MCP over USB/ADB up, `mcu_ping` answering.
- Playing from 0: `reset`, then level 1 at 6s, then level 3 at 16s — three
  transitions, in order, once each.
- Scrubbing back to 5s and playing forward through 16s again: `events_sent`
  stayed at 3 and `events_skipped` went to 3. **The motor did not buzz again
  and the strike counter did not move.**
- The hardware panel showed "3 sent, 3 skipped on rewind" and
  "answering (mcu_ping)" throughout.
- Ctrl-C left the board on level 0.

### Not verified, and why

- **No sketch changes were made or tested in this build.** `alert_sketch.ino` is
  documentation-only edits — the header table now matches `alert()`. Deploying
  needs Arduino App Lab, which is a GUI. If you re-flash, run
  `laptop/test_signals.py` immediately afterwards.
- **NPU execution is requested, not confirmed by the API.** `geniex serve -c npu`
  is what we ask for and the OpenAI-compatible API never reports back, so the
  page says "requested" everywhere.

  It is, however, corroborated out of band: asking for `-c cpu` makes the
  plugin log *"qairt plugin only supports NPU inference; ignoring device='cpu'
  and running on NPU"*. That is the backend saying it has no other option. The
  page does not use this - it is gathered from a log after the fact, not
  something a build can check - but the presenter should.

---

## Before you commit

```bash
python -m compileall -q laptop tools unoq
```

```bash
python -m pytest
```

```bash
git status
```

Only intended changes. No footage, no `output/`, no local paths, no secrets.
