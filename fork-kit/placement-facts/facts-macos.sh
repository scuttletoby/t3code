#!/bin/sh
# Example placement factsCommand for macOS (fork-kit/placement-facts/README.md). Copy it to a path
# you own, edit the settings below, `chmod 755` it and put its absolute path in Settings, placement,
# factsCommand. Exit 0: free for work, each line printed one more label. Exit 1: not available, the
# first line of the error output says why. T3 stops it after 5 seconds and reads that as "not available".
set -u

# ---- settings -------------------------------------------------------------------------------------
# Programs that mean "in use, place no work here": exact process names, as `pgrep -x` matches them.
BUSY_PROCESSES=""
# While this file exists the machine is not available; its first line is the reason.
BUSY_FILE="${HOME:-/nonexistent}/.config/t3-placement/busy"
# 1: not available while running on battery (a laptop away from power may sleep). 0: ignore power.
BUSY_ON_BATTERY=1
# Labels printed every time (the same as Settings' placement labels; either place works). A Mac's
# GPU is always there, so say `gpu` here if work that needs one may run on it.
EXTRA_LABELS=""
# ----------------------------------------------------------------------------------------------------

busy() {
  printf '%s\n' "$1" >&2
  exit 1
}

if [ -f "$BUSY_FILE" ]; then
  reason=$(head -n 1 "$BUSY_FILE" 2>/dev/null)
  busy "busy: ${reason:-$BUSY_FILE is there}"
fi

# Without pgrep the busy list cannot be checked: not available rather than free.
if [ -n "$BUSY_PROCESSES" ] && ! command -v pgrep >/dev/null 2>&1; then
  busy "busy: pgrep is missing, so BUSY_PROCESSES cannot be checked"
fi
for name in $BUSY_PROCESSES; do
  if pgrep -x "$name" >/dev/null 2>&1; then busy "busy: $name is running"; fi
done

# `pmset -g batt` starts with "Now drawing from 'Battery Power'" on battery.
if [ "$BUSY_ON_BATTERY" = 1 ] && pmset -g batt 2>/dev/null | head -n 1 | grep -q "'Battery Power'"; then
  busy "busy: on battery"
fi

for label in $EXTRA_LABELS; do printf '%s\n' "$label"; done

# A GitHub Actions self-hosted runner running here.
if pgrep -f 'Runner.Listener' >/dev/null 2>&1; then printf 'ci-runner\n'; fi

exit 0
