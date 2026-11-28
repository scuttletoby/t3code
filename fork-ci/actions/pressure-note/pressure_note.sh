#!/usr/bin/env bash
# pressure-note (action.yml): runs the release's fork-ci-pressure.py for $PHASE under $TIME_LIMIT seconds. Every
# failure is a warning and exit 0: the numbers are facts for the re-run decision, never a reason to fail the job.
script="$GITHUB_ACTION_PATH/../../../.github/scripts/fork-ci-pressure.py"
case "$PHASE" in
  start | end) ;;
  *) echo "::warning::pressure-note: phase must be start or end, not '$PHASE'; no numbers"; exit 0 ;;
esac
case "$TIME_LIMIT" in
  '' | *[!0-9]* | 0) echo "::warning::pressure-note: time-limit '$TIME_LIMIT' is not a whole number of seconds; 30 used"; TIME_LIMIT=30 ;;
esac
if [ ! -f "$script" ]; then
  echo "::warning::pressure-note: fork-ci-pressure.py is not in this release; no numbers"
  exit 0
fi
export JOB_STATUS
timeout -k 5 "$TIME_LIMIT" python3 -I "$script" "$PHASE" || echo "::warning::pressure-note: $PHASE stopped (exit $?), no numbers; the job goes on"
exit 0
