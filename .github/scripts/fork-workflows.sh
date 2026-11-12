#!/usr/bin/env bash
# Keeps upstream's workflows off in this fork (tests: fork-follow.test.sh). Disables every active
# workflow under .github/workflows that is not one of the fork's own, then cancels the unfinished
# runs that such workflows started for HEAD_SHA.
#
# GitHub lists a workflow only once an event has reached its file, so a workflow that arrives with a
# rebase can only be disabled after the push that brings it. fork-follow.yml therefore runs this
# right after each of its pushes and again at the end of the run, and cancels what already started.
#
# Environment: REPOSITORY (owner/name; GITHUB_REPOSITORY in a run), OWN_WORKFLOWS (','-separated
# paths the fork's own commits add; required, nothing is guessed), HEAD_SHA (optional), REPORT
# (receives the reason on failure). Output: disabled= and cancelled= (','-separated paths and run
# ids) to $GITHUB_OUTPUT, else stdout; a summary line on stderr.
set -euo pipefail

REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:?REPOSITORY or GITHUB_REPOSITORY}}"
REPORT="${REPORT:-fork-workflows-report.md}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"

die() {
  printf '%s\n' "$@" > "$REPORT"
  printf '::error::%s\n' "$*" >&2
  exit 1
}

[[ -n "${OWN_WORKFLOWS:-}" ]] || die "The list of the fork's own workflows is empty, so no workflow was disabled (refusing to guess)."
own() { [[ ",$OWN_WORKFLOWS," == *",$1,"* ]]; }
upstream() { [[ "$1" == .github/workflows/* ]] && ! own "$1"; }

active="$(gh api "repos/$REPOSITORY/actions/workflows" --paginate \
  --jq '.workflows[] | select(.state == "active") | "\(.id) \(.path)"')" \
  || die "Could not list the workflows of $REPOSITORY, so upstream workflows may still be enabled."

disabled=()
while read -r id path; do
  [[ -n "${id:-}" ]] && upstream "$path" || continue
  gh api -X PUT "repos/$REPOSITORY/actions/workflows/$id/disable" > /dev/null \
    || die "Could not disable the upstream workflow $path ($id)."
  disabled+=("$path")
done <<< "$active"

cancelled=()
if [[ -n "${HEAD_SHA:-}" ]]; then
  runs="$(gh api "repos/$REPOSITORY/actions/runs?head_sha=$HEAD_SHA&per_page=100" --paginate \
    --jq '.workflow_runs[] | select(.status != "completed") | "\(.id) \(.path)"')" \
    || die "Could not list the runs for ${HEAD_SHA:0:12}, so runs of upstream workflows may still be going."
  while read -r id path; do
    path="${path%%@*}"
    [[ -n "${id:-}" ]] && upstream "$path" || continue
    if ! gh api -X POST "repos/$REPOSITORY/actions/runs/$id/cancel" > /dev/null; then
      status="$(gh api "repos/$REPOSITORY/actions/runs/$id" --jq .status)" \
        || die "Could not cancel run $id of $path, nor read its state."
      [[ "$status" == completed ]] || die "Could not cancel run $id of $path (state: $status)."
      continue
    fi
    cancelled+=("$id ($path)")
  done <<< "$runs"
fi

join() { local IFS=,; echo "$*"; }
{
  echo "disabled=$(join "${disabled[@]}")"
  echo "cancelled=$(join "${cancelled[@]}")"
} >> "$OUTPUT"
echo "Upstream workflows disabled: ${#disabled[@]}; their runs cancelled: ${#cancelled[@]}." >&2
