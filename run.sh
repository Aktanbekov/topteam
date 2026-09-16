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
DEMO=""
VIDEO=""
FULL_STOP=""
WINDOW_AFTER=""
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
    --every) VISION_EVERY="$2"; shift 2 ;;
    --compute) COMPUTE="$2"; shift 2 ;;
    --demo) DEMO="$2"; shift 2 ;;
    --full-stop) FULL_STOP="$2"; shift 2 ;;
    --window-after) WINDOW_AFTER="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# -------------------------------------------------------------------- video
if [ -z "$VIDEO" ]; then
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
[ -f "$VIDEO" ] || die "no such file: $VIDEO"

mkdir -p "$OUT_DIR"
TIMELINE="$OUT_DIR/timeline.json"
PLAYER="$OUT_DIR/player.html"

say "Drive: $VIDEO"
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
  "$geniex" serve -c "$COMPUTE" >output/geniex.log 2>&1 &
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

# ------------------------------------------------------------------ analyse
COMPUTE_ARG=()
[ "$STARTED_GENIEX" = "1" ] && COMPUTE_ARG=(--compute "$COMPUTE")

if [ "$REUSE" = "1" ] && [ -f "$TIMELINE" ]; then
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

# -------------------------------------------------------------------- uno q
# Everything reaches the board over USB. Wi-Fi is deliberately not used: wlan0
# was down out of the box, and a hotspot is one more thing to fail on stage.
if [ "$UNOQ" = "1" ]; then
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
fi

# --------------------------------------------------------------------- open
if [ "$UNOQ" = "1" ]; then
  # The page has to be SERVED, not opened from disk: it posts level changes
  # back to its own origin and serve_player relays them to the board. A
  # file:// page has no origin to post to, so the hardware would sit idle.
  say "Serving the player and driving the hardware"
  echo "    http://127.0.0.1:$PLAYER_PORT/$PLAYER"
  echo "    Ctrl-C to stop"
  echo
  exec "$PY" -u tools/serve_player.py --port "$PLAYER_PORT" --page "$PLAYER" --open --unoq
fi

say "Opening $PLAYER"

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
echo "  report:  $OUT_DIR/report.html      (printable, Ctrl-P to save as PDF)"
echo "  data:    $OUT_DIR/review.json      (every fact behind the page)"
echo
echo "If the video will not play, serve it over localhost instead:"
echo "    $PY tools/serve_player.py --page $PLAYER --open"
echo "To drive the UNO Q as it plays:  ./run.sh --reuse --unoq"
