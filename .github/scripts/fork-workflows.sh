#!/usr/bin/env bash
# Keeps upstream's workflows off in this fork (tests: fork-follow.test.sh). Disables every active
# workflow under .github/workflows that is not one of the fork's own, cancels every unfinished run
# of such a workflow (whatever event started it: push, pull_request, schedule ...), and names what
# it could not stop:
#   finished      runs of upstream workflows that completed since SINCE (they ran before a sweep
#                 could stop them), with their conclusion
#   unregistered  upstream workflow files in the tree at TREE_REF that GitHub does not list yet.
#                 GitHub lists a workflow only once an event has reached its file, so these can be
#                 neither disabled nor cancelled until then; the next sweep after that event does.
#
# fork-follow.yml runs this right after each of its pushes and again at the end of the run.
#
# Environment: REPOSITORY (owner/name; GITHUB_REPOSITORY in a run), OWN_WORKFLOWS (','-separated
# paths the fork's own commits add; required, nothing is guessed), TREE_REF (commit or branch whose
# workflow files are checked; default HEAD_SHA, else SERIES_BRANCH, else main), HEAD_SHA, SINCE
# (ISO 8601 time; optional), REPORT (receives the reason on failure). Output: disabled=, cancelled=,
# finished= and unregistered= (','-separated) to $GITHUB_OUTPUT, else stdout; a summary line on
# stderr, and a ::warning:: line for finished runs and unregistered files.
set -euo pipefail

REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:?REPOSITORY or GITHUB_REPOSITORY}}"
REPORT="${REPORT:-fork-workflows-report.md}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"
TREE_REF="${TREE_REF:-${HEAD_SHA:-${SERIES_BRANCH:-main}}}"

die() {
  printf '%s\n' "$@" > "$REPORT"
  printf '::error::%s\n' "$*" >&2
  exit 1
}

[[ -n "${OWN_WORKFLOWS:-}" ]] || die "The list of the fork's own workflows is empty, so no workflow was disabled (refusing to guess)."
own() { [[ ",$OWN_WORKFLOWS," == *",$1,"* ]]; }
upstream() { [[ "$1" == .github/workflows/* ]] && ! own "$1"; }

listed="$(gh api "repos/$REPOSITORY/actions/workflows" --paginate \
  --jq '.workflows[] | "\(.id) \(.state) \(.path)"')" \
  || die "Could not list the workflows of $REPOSITORY, so upstream workflows may still be enabled."

disabled=()
while read -r id state path; do
  [[ -n "${id:-}" && "$state" == active ]] && upstream "$path" || continue
  gh api -X PUT "repos/$REPOSITORY/actions/workflows/$id/disable" > /dev/null \
    || die "Could not disable the upstream workflow $path ($id)."
  disabled+=("$path")
done <<< "$listed"

# Unfinished runs, from any event and for any commit; one listing per unfinished status.
cancelled=()
runs=""
for status in requested queued pending waiting in_progress; do
  runs+="$(gh api "repos/$REPOSITORY/actions/runs?status=$status&per_page=100" --paginate \
    --jq '.workflow_runs[] | select(.status != "completed") | "\(.id) \(.path)"')"$'\n' \
    || die "Could not list the $status runs of $REPOSITORY, so runs of upstream workflows may still be going."
done
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
done <<< "$(sort -u <<< "$runs")"

# Runs that finished before a sweep could stop them. Cancelled ones are left out: a sweep stopped
# them (named under cancelled) or a person did.
finished=()
if [[ -n "${SINCE:-}" ]]; then
  done_runs="$(gh api "repos/$REPOSITORY/actions/runs?created=%3E%3D$SINCE&status=completed&per_page=100" --paginate \
    --jq '.workflow_runs[] | select(.status == "completed" and .conclusion != "cancelled") | "\(.id) \(.conclusion) \(.event) \(.path)"')" \
    || die "Could not list the runs of $REPOSITORY since $SINCE, so upstream runs that already finished are not known."
  while read -r id conclusion event path; do
    path="${path%%@*}"
    [[ -n "${id:-}" ]] && upstream "$path" || continue
    finished+=("$id ($path: $conclusion after $event)") # no ',': lists are ','-joined
  done <<< "$(sort -u <<< "$done_runs")"
fi

# Upstream workflow files GitHub has not registered (any state counts as registered).
unregistered=()
files="$(gh api "repos/$REPOSITORY/contents/.github/workflows?ref=$TREE_REF" \
  --jq '.[] | select(.type == "file") | .path')" \
  || die "Could not list the workflow files of $REPOSITORY at $TREE_REF, so unregistered upstream workflows are not known."
while read -r path; do
  [[ "$path" == *.yml || "$path" == *.yaml ]] && upstream "$path" || continue
  awk -v p="$path" '$3 == p { found = 1 } END { exit !found }' <<< "$listed" || unregistered+=("$path")
done <<< "$files"

join() { local IFS=,; echo "$*"; }
{
  echo "disabled=$(join "${disabled[@]}")"
  echo "cancelled=$(join "${cancelled[@]}")"
  echo "finished=$(join "${finished[@]}")"
  echo "unregistered=$(join "${unregistered[@]}")"
} >> "$OUTPUT"
((${#finished[@]} == 0)) \
  || echo "::warning::Upstream workflow runs finished before they could be stopped: $(join "${finished[@]}")" >&2
((${#unregistered[@]} == 0)) \
  || echo "::warning::Upstream workflow files GitHub does not list yet, so not disabled: $(join "${unregistered[@]}")" >&2
echo "Upstream workflows disabled: ${#disabled[@]}; runs cancelled: ${#cancelled[@]}; finished before a sweep: ${#finished[@]}; not registered yet: ${#unregistered[@]}." >&2
