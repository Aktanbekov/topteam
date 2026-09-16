# Demo script — three minutes

Rehearse it twice. The only things that can go wrong are things you can check
before you stand up, and there is a checklist for that at the bottom.

**One sentence per idea. If you are explaining an idea for more than one
sentence, you are losing the room.**

---

## Before you are called

```bash
./run.sh --demo fail --unoq
```

Leave it running with the page open and paused at 0:00. It has already done the
analysis, the board is already connected, and the first viewport is already
telling the story. If your slot slips, nothing goes stale.

Have a second terminal ready at the repo root.

---

## 0:00 — The problem, and the thing nobody else will say (20s)

> "Learner drivers fail the road test on things nobody told them about. There
> are apps that will coach you — and every one of them wants you to upload
> dashcam footage of your teenager, your street, and everyone else on the road
> who never agreed to be filmed."

> "This one never uploads anything. Watch."

**Turn Wi-Fi off.** Do it visibly. Leave it off for the whole demo.

---

## 0:20 — What is actually running (25s)

Point at the badges in the header: **100% on-device · Qwen3-VL-4B-Instruct:W4A16
· NPU requested**.

> "A four-billion-parameter vision-language model, on the Snapdragon NPU,
> through GenieX. Three and a half seconds a frame warm — which is the number
> that makes the rest of this possible."

Point at the UNO Q badge: **connected**.

> "And the board is on a USB cable. No Wi-Fi anywhere in this demo."

---

## 0:45 — Play it (40s)

Press space. Let it run. Say almost nothing.

- At **6s** the strip goes amber, the matrix shows an octagon, and the motor
  taps once.
  > "It has seen a stop sign. That is a heads-up — the octagon, not a cross,
  > because nothing has gone wrong yet."
- The car rolls through without stopping.
- At **16s** the strip flashes red, the matrix shows a big X, and the motor
  gives three buzzes that build.
  > "And there it is."

Let the buzzing land. Do not talk over it.

---

## 1:25 — The bit that is actually hard (45s)

Scroll to **How the coach decided**. Walk the four steps with your finger.

> "The stop sign leaves the camera's view at six seconds. The car doesn't reach
> the line until ten seconds later. They are *never in the same frame* — so a
> detector that asks 'do I see a sign and a stopped car' fails every correct
> stop on the road."

> "So the window stays open. And notice *when* the alarm fired: sixteen seconds,
> not six. We don't accuse the driver at the moment we see the sign. We wait for
> the window to expire, because until it does, nothing has happened yet."

If you only land one technical point, land that one.

---

## 2:10 — Why you can believe it (30s)

Scroll to **Trust**.

> "The model is asked what is visible in one frame — never 'did the driver
> stop', because it cannot see across time and it would guess. And when it says
> something only once, we throw it away."

Point at the struck-through rows.

> "Those are the model's own claims that didn't survive. We show them. Our first
> prompt produced twenty-two false alarms on thirty-eight labelled frames.
> Rewriting the prompt and requiring two samples to agree took that to zero —
> on those thirty-eight frames."

Say "on those thirty-eight frames" out loud. A judge who hears you bound your
own claim will believe the rest of them.

---

## 2:40 — The thing the parent keeps (20s)

Click **Open the printable report**.

> "Every finding: timestamp, the frame it happened in, the rule that fired, and
> what to do next time. Ctrl-P and it's a PDF for the instructor."

Scroll past **Checks performed**.

> "And this is what we *don't* do. Head checks need a driver-facing camera we
> haven't built. Red lights we detect but won't score, because scoring one needs
> a stop-line crossing test we don't trust yet. We'd rather show you the gap."

---

## 3:00 — Land it

> "It runs on the laptop, on the NPU, offline. The video never leaves the
> device. That's not a feature — for this product it's the only version a parent
> should ever install."

Stop talking.

---

## If they ask

**"Is this a DMV pass or fail?"**
> "No, and it never says so. We have a forward camera and no speed feed, so
> every finding is 'possible'. It's a practice coach, not an examiner."

**"How do you know it's on the NPU?"**
> "The page only claims we *asked* for it, because the API doesn't report back.
> But we tried to benchmark against CPU and the plugin wouldn't let us — it
> logs 'qairt plugin only supports NPU inference; ignoring device=cpu and
> running on NPU'. So there's no CPU number to get, and that refusal is better
> evidence than the benchmark would have been."

*(`python tools/benchmark_compute.py --compare` prints exactly that, and
refuses to quote a speedup. Have it ready in the second terminal.)*

**"That's synthetic footage."**
> "It is, and the page says so in amber. We have real footage of a correct stop
> —" *(run `./run.sh --reuse` on IMG_9830)* "— but we don't have footage of
> someone running a sign, and we're not going to break a traffic law to get it.
> The analysis of the synthetic clip was a real local run either way."

**"Why is the strike counter at zero?"**
> "Because nothing scored a minor. A short-but-complete stop is legal, so it's
> advice, not a strike. Level 2 works — `python laptop/test_signals.py` — no
> check currently produces it, and we'd rather say that than invent one."

**"What's the business?"**
> "Parents of teen drivers on subscription; driving schools per-seat. Both care
> about privacy and neither has to trust a cloud with a child's face."

---

## Pre-flight, ten minutes before

```bash
python -m pytest
```

```bash
./run.sh --demo fail --unoq
```

- [ ] 117 tests pass
- [ ] The header shows **UNO Q: connected** and the sketch panel says *answering*
- [ ] Press play: amber tap at 6s, red + three buzzes at 16s
- [ ] Press **Replay drive from the start** — it all fires again
- [ ] Scrub backwards over 16s: the motor does **not** buzz again (this is a
      feature; be ready to say so if someone notices)
- [ ] The report opens and prints
- [ ] Backup recording is on the desktop and plays

## If it falls over on stage

| what broke | do this |
|---|---|
| GenieX will not start | `./run.sh --demo fail` — falls back to recorded labels, says so on the page. Read the banner out loud; it costs you nothing. |
| The board is not found | Drop `--unoq`. The page is the whole product without it, and the UNO Q panel says "not connected" rather than lying. |
| The browser will not play the video | Everything else on the page still works. Talk through the evidence cards and the report instead. |
| Nothing works at all | Play the backup recording. Say "this is a recording of the run" — do not let anyone find that out for themselves. |
