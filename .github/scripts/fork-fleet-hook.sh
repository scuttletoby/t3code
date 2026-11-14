#!/usr/bin/env bash
# Fork only: every fleet runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED, which fork-fleet-launch.sh sets
# from a root-only file). Installed root-owned next to root-owned copies of main's fork-fleet.py and fork-fleet.json;
# the runner user can change none of them. `gate` kills the job's Runner.Worker when the job may not run here.
exec /usr/bin/python3 -I "$(dirname -- "$0")/fork-fleet.py" --config "$(dirname -- "$0")/fork-fleet.json" gate
