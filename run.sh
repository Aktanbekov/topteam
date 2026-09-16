#!/usr/bin/env bash
#
# Analyse a drive and open the review player.
#
#   ./run.sh                     analyse the video in the project root
#   ./run.sh clips/drive2.mov    analyse a specific file
#   ./run.sh --reuse             skip the vision pass, just rebuild the player
#   ./run.sh --every 5           sample the vision model every 5s instead of 3
#
# On Windows, run this from Git Bash (installed with Git for Windows).
#
# The vision pass is the slow part - roughly one model call per sample at ~2.7s
# each, so a 2 minute clip takes about 2 minutes. Use --reuse when you are only
# changing the player or the motion threshold, which are both instant.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

GENIEX_PORT=18181
VISION_EVERY=3.0
REUSE=0
VIDEO=""

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse) REUSE=1; shift ;;
    --every) VISION_EVERY="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 1 ;;
    *) VIDEO="$1"; shift ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

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

# -------------------------------------------------------------------- video
if [ -z "$VIDEO" ]; then
  # Pick the only video in the project root, or complain if it is ambiguous.
  mapfile -t found < <(find . -maxdepth 1 -type f \
    \( -iname '*.mov' -o -iname '*.mp4' -o -iname '*.avi' -o -iname '*.mkv' \) | sort)
  case ${#found[@]} in
    0) die "no video found. Put one in $(pwd), or pass a path: ./run.sh myclip.mp4" ;;
    1) VIDEO="${found[0]}" ;;
    *) printf 'more than one video here - pick one:\n'
       printf '  %s\n' "${found[@]}"
       die "pass the one you want: ./run.sh <file>" ;;
  esac
fi
[ -f "$VIDEO" ] || die "no such file: $VIDEO"

say "Drive: $VIDEO"

# ------------------------------------------------------------------- geniex
port_open() {
  "$PY" - "$GENIEX_PORT" <<'EOF'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
}

if port_open; then
  say "GenieX already serving on port $GENIEX_PORT"
else
  # Same stale-PATH problem as Python, so fall back to the install location.
  GENIEX=""
  if command -v geniex >/dev/null 2>&1; then
    GENIEX="geniex"
  elif [ -x "$HOME/AppData/Local/GenieX CLI/geniex.exe" ]; then
    GENIEX="$HOME/AppData/Local/GenieX CLI/geniex.exe"
  else
    die "geniex not found, and nothing is serving on port $GENIEX_PORT"
  fi

  say "Starting GenieX (first model load takes ~15s)"
  mkdir -p output
  "$GENIEX" serve >output/geniex.log 2>&1 &
  GENIEX_PID=$!

  for _ in $(seq 1 40); do
    port_open && break
    kill -0 "$GENIEX_PID" 2>/dev/null || break   # it died; stop waiting
    sleep 1
  done

  if ! port_open; then
    echo "--- last lines of output/geniex.log ---" >&2
    tail -n 15 output/geniex.log >&2 || true
    die "GenieX did not come up on port $GENIEX_PORT"
  fi

  echo "    running as PID $GENIEX_PID - it stays up after this script exits"
  echo "    stop it later with:  kill $GENIEX_PID"
fi

# ------------------------------------------------------------------ analyse
mkdir -p output

if [ "$REUSE" = "1" ] && [ -f output/timeline.json ]; then
  say "Reusing output/timeline.json (skipping the vision pass)"
elif [ "$REUSE" = "1" ]; then
  die "--reuse given but output/timeline.json does not exist yet"
else
  say "Analysing (vision call every ${VISION_EVERY}s - this is the slow part)"
  "$PY" laptop/analyze_video.py "$VIDEO" \
    --vision-every "$VISION_EVERY" \
    --save-frames output/frames \
    --json-out output/timeline.json
fi

# ------------------------------------------------------------------- player
say "Building the player"
"$PY" tools/make_player.py \
  --video "$VIDEO" \
  --timeline output/timeline.json \
  --out output/player.html

# --------------------------------------------------------------------- open
PLAYER="output/player.html"
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
echo "If the video will not play, serve it over localhost instead:"
echo "    $PY tools/serve_player.py --open"
