#!/usr/bin/env bash
#
# The whole demo, in one command.
#
#   ./demo.sh
#
# Starts GenieX, brings up the UNO Q over USB if it is plugged in, and opens a
# page with everything on it: the recorded drives, the bad-driving simulations
# and the live camera. No more typing during a presentation.
#
# It is a one-line alias for ./run.sh --console, which is where all the setup
# actually lives - the same Python detection, the same GenieX supervision and
# the same adb tunnel every other mode uses. Written separately only because
# "./demo.sh" is the thing to remember with a room watching.
#
# Ctrl-C stops it and returns the board to level 0.

exec "$(dirname "${BASH_SOURCE[0]}")/run.sh" --console "$@"
