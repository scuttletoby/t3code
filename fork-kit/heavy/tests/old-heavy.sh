#!/bin/sh
# The wrapper before this kit, kept only for the red check in heavy.test.py: the scope outlives the command, so
# whatever the command leaves behind keeps running, and nothing is printed.
[ $# -gt 0 ] || { echo "usage: heavy <command> [args...]" >&2; exit 2; }
exec systemd-run --user --scope --quiet --slice="${HEAVY_SLICE:-heavy.slice}" --collect -- "$@"
