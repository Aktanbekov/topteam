# Submission checklist

Work down it. Anything still unticked an hour before the deadline is something
to cut, not something to rush.

## Blocking — do these first

- [ ] **Take the hero screenshot.** `./run.sh --demo fail`, screenshot the first
      viewport, save as `docs/hero.png`, uncomment the image in `README.md`.
      The repo currently has a placeholder saying exactly this.
- [ ] **Record the backup run.** Screen recording of `./run.sh --demo fail --unoq`
      with the board in shot, from Wi-Fi off to the printed report. Store it
      **outside the repo**. Link it under "Demo video" in the README.
- [ ] **Choose a licence.** There is no `LICENSE` file, so all rights are
      reserved by default — probably not what you want for a hackathon entry.
      MIT or Apache-2.0 takes one minute.
- [ ] **Restart GenieX through `run.sh` at least once** so a build records the
      compute unit. If the server was already up, `run.sh` deliberately records
      nothing and the NPU badge does not appear — it cannot know what an
      already-running server was started with.

## Strongly worth it

- [ ] **Get one clip with a real three-second stop.** IMG_9830 has a genuine
      1.07s stop, so the real footage can only demo the amber "brief" grade.
      A real full stop would let the flagship clip show a green pass.
- [ ] Rehearse [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) twice with a timer. Under
      three minutes, out loud, with the board plugged in.
- [ ] Practise the three "if they ask" answers until they are one sentence each.

## Code and repo

- [ ] `python -m pytest` — 108 pass
- [ ] `python -m compileall -q laptop tools unoq` — clean
- [ ] `git status` — only intended changes
- [ ] No footage, no `output/`, no local machine paths, no secrets committed
- [ ] README quick start matches the actual commands
- [ ] `demo/` clips are present and committed (they are the `.gitignore`
      exception; `./run.sh --demo` on a fresh clone depends on them)
- [ ] `archive/` clearly marks the superseded HTTP transport, and nothing
      imports it

## The claims on the page — check each one is bounded

These are what a judge will test you on. Each should already be worded this way;
verify nothing has drifted.

- [ ] "Zero false alarms" always reads **"on the 38 labelled frames"**
- [ ] The compute badge says **requested**, never verified
- [ ] Every stop finding says **possible**
- [ ] Nowhere does the product print a DMV pass or fail
      (`test_no_official_dmv_claim_anywhere` enforces this)
- [ ] A brief stop is described as coaching, never as an error
- [ ] Synthetic footage is labelled synthetic on every page it appears on
- [ ] A replayed run is labelled replayed and drops the NPU badge
- [ ] Unavailable checks (head checks) are listed as unavailable, not omitted

## Hardware

- [ ] `laptop/test_signals.py` walks all four levels correctly on the board
- [ ] `./run.sh --demo fail --unoq` fires level 1 at 6s and level 3 at **16s**
- [ ] Rewinding does not buzz twice
- [ ] Ctrl-C leaves the board calm
- [ ] Spare USB-C cable in the bag
- [ ] If you re-flash the sketch for any reason, re-run `test_signals.py`
      immediately — do not re-flash on the day without testing

## On the day

- [ ] Laptop charged, and charger packed
- [ ] `./run.sh --demo fail --unoq` already running before you are called
- [ ] Wi-Fi off, visibly, at the start
- [ ] Backup recording on the desktop, one click away
- [ ] Browser zoom at 100% and no other tabs

---

## Known risks, worst first

| risk | mitigation |
|---|---|
| A judge reads "critical error" on synthetic footage as a real driving event | The amber banner is on every page and the demo script says it out loud at 2:40. Say it before they ask. |
| The board does not enumerate on the venue's USB | Drop `--unoq`. The page is the whole product without it and the panel says "not connected" honestly. |
| GenieX will not start under time pressure | `./run.sh --demo fail` falls back to recorded labels and says so. Read the banner aloud. |
| "Is it really on the NPU?" | Answer with the requested/verified distinction. It is a stronger answer than a badge would have been. |
| The strike counter never moves in a live run | True and intentional. Level 2 has no producer because a brief stop is legal. Say so; `test_signals.py` shows the hardware works. |
| Only one real clip | Acknowledged in the README limitations. The fix is footage, not code. |
