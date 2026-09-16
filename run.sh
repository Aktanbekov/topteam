#!/usr/bin/env bash
#
# Analyse a drive and open the review player. One command, start to finish.
#
#   ./run.sh                     analyse the video in the project root
#   ./run.sh clips/drive2.mov    analyse a specific file
#   ./run.sh --reuse             skip the vision pass, just rebuild the player
#   ./run.sh --unoq              also drive the UNO Q hardware as the video plays
#   ./run.sh --every 5           sample the vision model every 5s instead of 3
#   ./run.sh --compute cpu       ask GenieX for a different compute unit
#   ./run.sh --demo fail         a short clip that ends in a critical error
#   ./run.sh --demo pass --unoq  the clean run, with the board attached
#   ./run.sh --calm              stop the board flashing and exit
#   ./run.sh --no-serve          open the page from disk instead of serving it
#   ./run.sh --out output/9831   write this run somewhere other than output/
#
#   ./run.sh --console           THE DEMO CONSOLE - everything from one page
#   ./run.sh --live              analyse the LAPTOP CAMERA as it happens
#   ./run.sh --live --unoq       ...and signal the board while you drive
#   ./run.sh --live --max-seconds 60    end the drive on its own after a minute
#
# While a live drive runs, WATCH THE CAMERA at http://127.0.0.1:8008 - the page
# shows the frames, the motion score and the current level. Without it a live
# drive draws nothing anywhere and a working run looks identical to a broken one.
#
# Live mode records what it sees to output/live/drive.mp4, so the same player
# and report come out at the end. It is genuinely live, and genuinely behind:
# a confirmed warning arrives about 6.8s after the event, because a vision call
# takes ~3.5s and a second sample has to agree before we act. The page says so.
#
# Demo grades: pass (full stop), brief (short but legal), fail (no stop at all).
# They use synthetic clips, analysed for real by the local model, because we
# have no footage of a driver running a stop sign and we are not going to break
# a traffic law to get some. Everything says "synthetic" on the page.
#
# On Windows, run this from Git Bash (installed with Git for Windows).
#
# The vision pass is the slow part - one model call per sample at ~3.4s each.
# Use --reuse when you are only changing the player, the report or a threshold,
# which are all instant.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

GENIEX_PORT=18181
VISION_EVERY=3.0
COMPUTE=npu
REUSE=0
UNOQ=0
CALM=0
NO_SERVE=0
DEMO=""
VIDEO=""
FULL_STOP=""
WINDOW_AFTER=""
OUT_OVERRIDE=""
LIVE=0
CONSOLE=0
MAX_SECONDS=""
MCP_PORT=3001
PLAYER_PORT=8000
BOARD_DIR=/home/arduino/topteam

# Only set when THIS script started the server, so we know what compute unit was
# actually asked for. If GenieX was already up we cannot know, and the review
# page says nothing rather than guessing.
STARTED_GENIEX=0

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse) REUSE=1; shift ;;
    --unoq) UNOQ=1; shift ;;
    --calm) CALM=1; shift ;;
    --no-serve) NO_SERVE=1; shift ;;
    --out) OUT_OVERRIDE="$2"; shift 2 ;;
    --live) LIVE=1; shift ;;
    --console) CONSOLE=1; shift ;;
    --max-seconds) MAX_SECONDS="$2"; shift 2 ;;
    --every) VISION_EVERY="$2"; shift 2 ;;
    --compute) COMPUTE="$2"; shift 2 ;;
    --demo) DEMO="$2"; shift 2 ;;
    --full-stop) FULL_STOP="$2"; shift 2 ;;
    --window-after) WINDOW_AFTER="$2"; shift 2 ;;
    -h|--help) sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 1 ;;
    *) VIDEO="$1"; shift ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die() { printf '\n\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

case "$COMPUTE" in
  npu|cpu|gpu|hybrid) ;;
  *) die "--compute must be npu, cpu, gpu or hybrid (got: $COMPUTE)" ;;
esac

# ------------------------------------------------------------------- python
# Windows ships a Microsoft Store stub called python.exe that is NOT Python -
# it prints an advert and exits non-zero. `command -v` finds it happily, so we
# have to actually run something to tell the difference.
PY=""
try_python() {
  { [ -x "$1" ] || command -v "$1" >/dev/null 2>&1; } || return 1
  "$1" -c 'import sys' >/dev/null 2>&1 || return 1
  PY="$1"
}

for candidate in python python3 py; do
  try_python "$candidate" && break
done

if [ -z "$PY" ]; then
  # A terminal opened before Python was installed carries a stale PATH. Look in
  # the standard per-user install location before giving up.
  for p in "$HOME"/AppData/Local/Programs/Python/Python3*/python.exe; do
    try_python "$p" && break
  done
fi

[ -n "$PY" ] || die "no working Python found.
    If you installed it recently, open a NEW terminal - Windows only gives the
    updated PATH to newly started programs."

# --------------------------------------------------------------------- calm
# The sketch holds its last level forever - it cannot tell that the laptop has
# gone away. So a drive that ended on a critical error leaves the strip flashing
# until something says otherwise.
mcp_port_open() {
  "$PY" - "$MCP_PORT" <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
}

if [ "$CALM" = "1" ]; then
  say "Calming the UNO Q"
  "$PY" laptop/calm_board.py
  exit 0
fi

# --------------------------------------------------------------------- demo
# The demo grades pair a synthetic clip with the outcome it is meant to show.
OUT_DIR=output
FROZEN=""
SOURCE_LABEL=real

if [ -n "$DEMO" ]; then
  case "$DEMO" in
    pass)  CLIP=demo/clip_full.mp4;    PROFILE=full ;;
    brief) CLIP=demo/clip_stop.mp4;    PROFILE=stop ;;
    fail)  CLIP=demo/clip_rolling.mp4; PROFILE=rolling ;;
    *) die "--demo must be pass, brief or fail (got: $DEMO)" ;;
  esac

  [ -z "$VIDEO" ] || die "pass either a video or --demo, not both"

  if [ ! -f "$CLIP" ]; then
    say "Generating the $DEMO demo clip"
    "$PY" tools/make_test_clip.py --profile "$PROFILE" --out "$CLIP"
  fi

  VIDEO="$CLIP"
  OUT_DIR="output/demo/$DEMO"
  FROZEN="demo/scene_$DEMO.json"
  SOURCE_LABEL=synthetic
fi

# An explicit output directory lets two runs sit side by side - the canonical
# drive in output/ and a new clip in output/9831 - instead of the second one
# quietly overwriting the first. output/ is gitignored, so an overwrite there
# cannot be recovered; only the source video can.
if [ -n "$OUT_OVERRIDE" ]; then
  [ -z "$DEMO" ] || die "pass either --demo or --out, not both"
  OUT_DIR="$OUT_OVERRIDE"
fi

# --------------------------------------------------------------------- live
# Live mode has no input file to find: the camera IS the input. It writes the
# drive to OUT_DIR/drive.mp4 as it goes, and everything after the drive - the
# player, the report, the serving - is the ordinary path over that recording.
if [ "$LIVE" = "1" ]; then
  [ -z "$DEMO" ] || die "pass either --demo or --live, not both"
  [ -z "$VIDEO" ] || die "--live reads the camera; do not also pass a video file"
  [ "$REUSE" = "0" ] || die "--reuse rebuilds a finished drive; --live records a new one"
  [ -n "$OUT_OVERRIDE" ] || OUT_DIR=output/live
  VIDEO="$OUT_DIR/drive.mp4"
fi

# -------------------------------------------------------------------- video
if [ "$LIVE" = "0" ] && [ "$CONSOLE" = "0" ] && [ -z "$VIDEO" ]; then
  # Pick the only video in the project root, or complain if it is ambiguous.
  mapfile -t found < <(find . -maxdepth 1 -type f \
    \( -iname '*.mov' -o -iname '*.mp4' -o -iname '*.avi' -o -iname '*.mkv' \) | sort)
  case ${#found[@]} in
    0) die "no video found. Put one in $(pwd), or pass a path: ./run.sh myclip.mp4
    No footage to hand? Try a demo:  ./run.sh --demo fail" ;;
    1) VIDEO="${found[0]}" ;;
    *) printf 'more than one video here - pick one:\n'
       printf '  %s\n' "${found[@]}"
       die "pass the one you want: ./run.sh <file>" ;;
  esac
fi
[ "$LIVE" = "1" ] || [ "$CONSOLE" = "1" ] || [ -f "$VIDEO" ] || die "no such file: $VIDEO"

mkdir -p "$OUT_DIR"
TIMELINE="$OUT_DIR/timeline.json"
PLAYER="$OUT_DIR/player.html"

# If a previous --unoq session left a tunnel up and this run is not going to
# use it, the board is still showing that session's last verdict. Nothing else
# will ever clear it, and the page has no relay to talk to - which looks exactly
# like "the hardware is broken and the player does nothing".
if [ "$UNOQ" = "0" ] && [ "$CONSOLE" = "0" ] && mcp_port_open; then
  "$PY" laptop/calm_board.py --quiet || true
  echo "    (found an idle UNO Q tunnel and returned the board to level 0)"
fi

if [ "$CONSOLE" = "1" ]; then say "Demo console"
elif [ "$LIVE" = "1" ]; then say "Live drive from the camera"
else say "Drive: $VIDEO"; fi
[ -n "$DEMO" ] && echo "    demo grade: $DEMO (synthetic footage, labelled as such)"

# ------------------------------------------------------------------- geniex
port_open() {
  "$PY" - "$GENIEX_PORT" <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
}

start_geniex() {
  # Same stale-PATH problem as Python, so fall back to the install location.
  local geniex=""
  if command -v geniex >/dev/null 2>&1; then
    geniex="geniex"
  elif [ -x "$HOME/AppData/Local/GenieX CLI/geniex.exe" ]; then
    geniex="$HOME/AppData/Local/GenieX CLI/geniex.exe"
  else
    return 1
  fi

  say "Starting GenieX on the $COMPUTE (first model load takes ~15s)"
  mkdir -p output
  # --skip-update because this has to work with the Wi-Fi off. GenieX caches its
  # update check for a day, so most starts touch nothing - but the moment that
  # cache expires it would try to reach the internet, and on a stage with no
  # network that is a hang nobody can explain. The model is already on disk
  # (~/.cache/geniex, 4.1GB) and the server binds loopback only, so with this
  # flag the whole pipeline has no reason to contact anything, ever.
  "$geniex" serve -c "$COMPUTE" --skip-update >output/geniex.log 2>&1 &
  local pid=$!

  for _ in $(seq 1 40); do
    port_open && break
    kill -0 "$pid" 2>/dev/null || break   # it died; stop waiting
    sleep 1
  done

  port_open || return 1
  STARTED_GENIEX=1
  echo "    running as PID $pid - it stays up after this script exits"
  echo "    stop it later with:  kill $pid"
}

HAVE_GENIEX=0
if [ "$REUSE" = "1" ] && [ -f "$TIMELINE" ]; then
  : # nothing to analyse, so the server is not needed at all
elif port_open; then
  say "GenieX already serving on port $GENIEX_PORT"
  warn "It was not started by this script, so we cannot know which compute unit"
  warn "it is using. The review page will say nothing about it rather than guess."
  warn "To record the compute unit, stop it and re-run: ./run.sh --compute $COMPUTE"
  HAVE_GENIEX=1
elif start_geniex; then
  HAVE_GENIEX=1
else
  if [ -n "$FROZEN" ] && [ -f "$FROZEN" ]; then
    warn ""
    warn "GenieX would not start. Falling back to the recorded scene labels in"
    warn "$FROZEN - a real run that happened earlier, replayed. The page will"
    warn "say so. The motion track is still computed from the video just now."
  else
    echo "--- last lines of output/geniex.log ---" >&2
    tail -n 15 output/geniex.log 2>/dev/null >&2 || true
    die "GenieX did not come up on port $GENIEX_PORT.
    For a demo that needs no model at all:  ./run.sh --demo $DEMO"
  fi
fi

# -------------------------------------------------------------------- uno q
# Everything reaches the board over USB. Wi-Fi is deliberately not used: wlan0
# was down out of the box, and a hotspot is one more thing to fail on stage.
# A function rather than a straight-line block, because the two modes need the
# board at different moments: a recorded drive can set it up after the analysis,
# but a LIVE drive is signalling it while it runs, so the tunnel has to be open
# before the camera does.
UNOQ_READY=0
setup_unoq() {
  [ "$UNOQ_READY" = "1" ] && return 0
  say "UNO Q over USB"

  # adb ships with App Lab rather than on PATH.
  ADB=""
  if command -v adb >/dev/null 2>&1; then
    ADB="adb"
  else
    for a in "$LOCALAPPDATA"/Arduino15/packages/arduino/tools/adb/*/adb.exe \
             "$HOME"/AppData/Local/Arduino15/packages/arduino/tools/adb/*/adb.exe; do
      [ -x "$a" ] && ADB="$a" && break
    done
  fi
  [ -n "$ADB" ] || die "adb not found. Install Arduino App Lab, or put adb on PATH."

  # Git Bash rewrites /home/arduino into a Windows path and adb push then fails
  # with 'secure_mkdirs failed'. This is the fix, and it must be exported.
  export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

  if ! "$ADB" devices | grep -qw device; then
    die "no board over USB. Check the cable, then:  $ADB devices
    The review works fully without it - drop --unoq and everything else runs."
  fi
  echo "    board connected"

  "$ADB" push unoq/arduino_bridge.py unoq/mcp_server.py "$BOARD_DIR/" >/dev/null 2>&1 \
    || die "could not copy the server to the board"

  if "$ADB" shell "ss -lnt 2>/dev/null | grep -q :$MCP_PORT"; then
    echo "    MCP server already running on the board"
  else
    echo "    starting the MCP server on the board"
    # setsid detaches it, otherwise adb holds the connection open and we hang.
    "$ADB" shell "cd $BOARD_DIR && setsid nohup python3 mcp_server.py > mcp.log 2>&1 < /dev/null &" \
      >/dev/null 2>&1 || true
    for _ in $(seq 1 15); do
      "$ADB" shell "ss -lnt 2>/dev/null | grep -q :$MCP_PORT" && break
      sleep 1
    done
    if ! "$ADB" shell "ss -lnt 2>/dev/null | grep -q :$MCP_PORT"; then
      "$ADB" shell "tail -5 $BOARD_DIR/mcp.log" >&2 || true
      die "the board's MCP server did not start.
    Is fastmcp installed there?  $ADB shell 'pip3 install fastmcp --break-system-packages'"
    fi
  fi

  # The tunnel does not survive unplugging the cable, so always re-establish it.
  "$ADB" forward tcp:$MCP_PORT tcp:$MCP_PORT >/dev/null \
    || die "could not forward tcp:$MCP_PORT"
  echo "    tunnel up on 127.0.0.1:$MCP_PORT"
  UNOQ_READY=1
}


# ----------------------------------------------------------------- console
# Everything above has already happened: a working Python, GenieX serving, and
# the board's tunnel available. The console then runs the analysis, the
# simulations and the live drive as child processes of its own, so the whole
# demo is buttons on a page rather than commands in a terminal.
if [ "$CONSOLE" = "1" ]; then
  # Try the board, but never refuse to start over it. A console that will not
  # come up because a USB cable is loose is the worst possible stage failure.
  # Plain shell on purpose: the same adb lookup setup_unoq does, and no inline
  # script to get its escaping mangled on the way through the heredoc.
  CONSOLE_ARGS=()
  CONSOLE_ADB=""
  if command -v adb >/dev/null 2>&1; then
    CONSOLE_ADB="adb"
  else
    for a in "$LOCALAPPDATA"/Arduino15/packages/arduino/tools/adb/*/adb.exe              "$HOME"/AppData/Local/Arduino15/packages/arduino/tools/adb/*/adb.exe; do
      [ -x "$a" ] && CONSOLE_ADB="$a" && break
    done
  fi

  if [ -n "$CONSOLE_ADB" ] &&      MSYS_NO_PATHCONV=1 "$CONSOLE_ADB" devices 2>/dev/null | grep -qw device; then
    setup_unoq
    CONSOLE_ARGS=(--unoq)
  else
    warn ""
    warn "No UNO Q over USB. The console still runs - every page, every"
    warn "simulation and the live drive all work; only the strip stays dark."
    warn ""
  fi

  say "Opening the demo console"
  echo "    everything runs from that page - no more commands"
  echo "    Ctrl-C here stops it and returns the board to level 0"
  echo
  exec "$PY" -u tools/demo_console.py --open "${CONSOLE_ARGS[@]}"
fi

# ------------------------------------------------------------------ analyse
COMPUTE_ARG=()
[ "$STARTED_GENIEX" = "1" ] && COMPUTE_ARG=(--compute "$COMPUTE")

if [ "$LIVE" = "1" ]; then
  # The board has to be listening before the drive starts, not after it ends.
  [ "$UNOQ" = "1" ] && setup_unoq

  LIVE_ARGS=(--out "$OUT_DIR" --vision-every "$VISION_EVERY" "${COMPUTE_ARG[@]}")
  [ -n "$MAX_SECONDS" ] && LIVE_ARGS+=(--max-seconds "$MAX_SECONDS")
  [ "$UNOQ" = "1" ] && LIVE_ARGS+=(--unoq)

  if [ "$UNOQ" = "0" ]; then
    # Easy to miss otherwise: the drive detects everything correctly, the page
    # shows level 3, and the strip sits there dark because nothing was ever
    # told to send it. That looks like broken hardware and is not.
    warn ""
    warn "The UNO Q will NOT react to this drive - you did not pass --unoq."
    warn "Everything else works; only the board is left out. For the hardware:"
    warn "    ./run.sh --live --unoq"
    warn ""
  fi

  say "Live drive - Ctrl-C to end it"
  "$PY" -u laptop/live_drive.py "${LIVE_ARGS[@]}"

  [ -f "$TIMELINE" ] || die "the live drive produced no timeline"
  [ -f "$VIDEO" ] || die "the live drive produced no recording at $VIDEO"
elif [ "$REUSE" = "1" ] && [ -f "$TIMELINE" ]; then
  say "Reusing $TIMELINE (skipping the vision pass)"
elif [ "$REUSE" = "1" ]; then
  die "--reuse given but $TIMELINE does not exist yet"
elif [ "$HAVE_GENIEX" = "1" ]; then
  say "Analysing (vision call every ${VISION_EVERY}s - this is the slow part)"
  "$PY" laptop/analyze_video.py "$VIDEO" \
    --vision-every "$VISION_EVERY" \
    --source "$SOURCE_LABEL" \
    "${COMPUTE_ARG[@]}" \
    --save-frames "$OUT_DIR/frames" \
    --json-out "$TIMELINE"
else
  say "Replaying recorded scene labels (no model call)"
  cp "$FROZEN" "$TIMELINE"
fi

# ------------------------------------------------------------------- player
say "Building the player and the report"
BUILD_ARGS=(--video "$VIDEO" --timeline "$TIMELINE" --out "$PLAYER")
[ -n "$FULL_STOP" ] && BUILD_ARGS+=(--full-stop "$FULL_STOP")
[ -n "$WINDOW_AFTER" ] && BUILD_ARGS+=(--window-after "$WINDOW_AFTER")
[ "$STARTED_GENIEX" = "1" ] && BUILD_ARGS+=(--compute "$COMPUTE")

"$PY" tools/make_player.py "${BUILD_ARGS[@]}"

if [ "$UNOQ" = "1" ] && [ "$LIVE" = "0" ]; then
  setup_unoq
fi

# --------------------------------------------------------------------- open
# The page is always SERVED and opened over http, whether or not the board is
# attached. It used to be opened from disk unless you asked for --unoq, and that
# split caused more trouble than it saved:
#
#   * a file:// page has no origin to post to, so the hardware sits idle and the
#     hardware panel can never say anything useful;
#   * Chrome and Edge refuse to play a video from file:// often enough that the
#     README needed a footnote telling you to serve it instead;
#   * the report link and the evidence stills are relative paths, which behave
#     differently from disk.
#
# Serving costs nothing and makes all three work, so there is one path now.
echo
echo "  report:  $OUT_DIR/report.html      (printable, Ctrl-P to save as PDF)"
echo "  data:    $OUT_DIR/review.json      (every fact behind the page)"

if [ "$NO_SERVE" = "1" ]; then
  say "Opening $PLAYER from disk (--no-serve)"
  warn "The video may not play and the hardware panel cannot work from file://."
  if command -v cygpath >/dev/null 2>&1; then
    cmd //c start "" "$(cygpath -w "$PLAYER")" || true   # Git Bash on Windows
  elif command -v open >/dev/null 2>&1; then
    open "$PLAYER"                                        # macOS
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$PLAYER"                                    # Linux
  else
    echo "    open it yourself: $(pwd)/$PLAYER"
  fi
  echo
  echo "To serve it properly instead:  ./run.sh --reuse"
  exit 0
fi

SERVE_ARGS=(--port "$PLAYER_PORT" --page "$PLAYER" --open)
if [ "$UNOQ" = "1" ]; then
  say "Serving the player and driving the hardware"
  SERVE_ARGS+=(--unoq)
else
  say "Serving the player"
fi
echo "    Ctrl-C to stop (that is also what returns the board to level 0)"
echo

# exec, so Ctrl-C reaches the server directly and its shutdown actually runs.
# The URL is printed by serve_player itself, once it knows which port it got -
# printing it here would be a guess, and a wrong one whenever the port steps.
exec "$PY" -u tools/serve_player.py "${SERVE_ARGS[@]}"
