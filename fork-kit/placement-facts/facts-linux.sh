#!/bin/sh
# Example placement factsCommand for Linux (fork-kit/placement-facts/README.md). Copy it to a path
# you own, edit the settings below, `chmod 755` it and put its absolute path in Settings, placement,
# factsCommand. Exit 0: free for work, each line printed one more label. Exit 1: not available, the
# first line of the error output says why. T3 stops it after 5 seconds and reads that as "not available".
set -u

# ---- settings -------------------------------------------------------------------------------------
# Programs that mean "in use, place no work here": exact process names, as `pgrep -x` matches them.
# Linux keeps only the first 15 characters of a process name, so a longer name is matched by those.
BUSY_PROCESSES=""
# While this file exists the machine is not available; its first line is the reason.
BUSY_FILE="${HOME:-/nonexistent}/.config/t3-placement/busy"
# 1: not available while running on battery (a laptop away from power may sleep). 0: ignore power.
BUSY_ON_BATTERY=1
# Labels printed every time (the same as Settings' placement labels; either place works).
EXTRA_LABELS=""
# Where the kernel lists power supplies.
POWER_SUPPLY_DIR=/sys/class/power_supply
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
  # `pgrep -x` matches the kernel's 15-character process name.
  short=$(printf '%.15s' "$name")
  if pgrep -x "$short" >/dev/null 2>&1; then busy "busy: $name is running"; fi
done

if [ "$BUSY_ON_BATTERY" = 1 ]; then
  has_battery="" on_power=""
  for supply in "$POWER_SUPPLY_DIR"/*; do
    [ -r "$supply/type" ] || continue
    # A wireless mouse's, keyboard's or controller's battery (scope Device) is not this machine's.
    [ "$(cat "$supply/scope" 2>/dev/null)" = Device ] && continue
    case $(cat "$supply/type" 2>/dev/null) in
      Battery) has_battery=1 ;;
      Mains | USB*) [ "$(cat "$supply/online" 2>/dev/null)" = 1 ] && on_power=1 ;;
    esac
  done
  if [ -n "$has_battery" ] && [ -z "$on_power" ]; then busy "busy: on battery"; fi
fi

for label in $EXTRA_LABELS; do printf '%s\n' "$label"; done

# An NVIDIA GPU the driver can see: `gpu` and `cuda`.
# Its own exit code counts too, so its output is kept first rather than piped.
if command -v nvidia-smi >/dev/null 2>&1 && gpus=$(timeout 3 nvidia-smi -L 2>/dev/null) &&
  printf '%s\n' "$gpus" | grep -q '^GPU '; then
  printf 'gpu\ncuda\n'
fi

# A GitHub Actions self-hosted runner running here.
if pgrep -f 'Runner.Listener' >/dev/null 2>&1; then printf 'ci-runner\n'; fi

exit 0
