#!/usr/bin/env bash
# Keeps upstream's workflows off in this fork (tests: fork-follow.test.sh). Disables every active
# workflow under .github/workflows that is not one of the fork's own (OWN_WORKFLOWS, and every
# .github/workflows/fork-*.y[a]ml), cancels every unfinished run of such a workflow (whatever event
# started it: push, pull_request, schedule ...), and names what it could not stop:
#   finished      runs of upstream workflows that completed since SINCE (they ran before a sweep
#                 could stop them), with their conclusion
#   unregistered  upstream workflow files in the tree at TREE_REF that GitHub does not list yet
#                 and that are new or changed on this upstream release (NEW_WORKFLOWS). GitHub
#                 lists a workflow only once an event has reached its file, so these can be neither
#                 disabled nor cancelled until then; the next sweep after that event does.
#   unregistered_old  the other unlisted upstream files, from earlier releases. Most of them run
#                 only on events the fork never sends (workflow_call, workflow_dispatch,
#                 workflow_run), so they stay unlisted; they are named, but not as a warning.
#
# fork-follow.yml runs this right after each of its pushes, right before it moves the series
# branch (REQUIRE_NEW_OFF=true), and again at the end of the run.
#
# Environment: REPOSITORY (owner/name; GITHUB_REPOSITORY in a run), OWN_WORKFLOWS (','-separated
# paths the fork's own commits add; required, nothing is guessed), NEW_WORKFLOWS (','-separated
# upstream files added or changed since the series' old base; optional), REQUIRE_NEW_OFF (true: fail when one
# of NEW_WORKFLOWS is in the tree and not listed, because then it cannot be disabled before the
# series branch has it), TREE_REF (commit or branch whose workflow files are checked; default
# HEAD_SHA, else SERIES_BRANCH, else main), HEAD_SHA, SERIES_BRANCH, PREVIOUS_HEAD (the series head
# the move starts from, for the advice), SINCE (ISO 8601 time; optional), REPORT (receives the
# reason on failure). Output: disabled=, cancelled=, finished=, unregistered= and unregistered_old=
# (','-separated) to $GITHUB_OUTPUT, else stdout; a summary line on stderr, and a ::warning:: line
# for finished runs and new unregistered files.
set -euo pipefail
# Refuses outside a GitHub Actions run unless a test runs it under the gh stub (fork-gh-guard.sh).
. "$(dirname "${BASH_SOURCE[0]}")/fork-gh-guard.sh"

REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:?REPOSITORY or GITHUB_REPOSITORY}}"
REPORT="${REPORT:-fork-workflows-report.md}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"
TREE_REF="${TREE_REF:-${HEAD_SHA:-${SERIES_BRANCH:-main}}}"

die() {
  printf '%s\n' "$@" > "$REPORT"
  printf '::error::%s\n' "$1" >&2
  [[ $# -lt 2 ]] || printf '%s\n' "${@:2}" >&2
  exit 1
}

[[ -n "${OWN_WORKFLOWS:-}" ]] || die "The list of the fork's own workflows is empty, so no workflow was disabled (refusing to guess)."
# A fork-*.yml or fork-*.yaml file is the fork's own even when the series does not have it yet (a
# fork pull request adding it runs it from its branch): it is never disabled, nor its runs
# cancelled. The plan refuses an upstream release that has such a file (fork-follow.sh).
own() { [[ ",$OWN_WORKFLOWS," == *",$1,"* || "$1" =~ ^\.github/workflows/fork-[^/]*\.ya?ml$ ]]; }
upstream() { [[ "$1" == .github/workflows/* ]] && ! own "$1"; }
new() { [[ ",${NEW_WORKFLOWS:-}," == *",$1,"* ]]; }

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
unregistered_old=()
files="$(gh api "repos/$REPOSITORY/contents/.github/workflows?ref=$TREE_REF" \
  --jq '.[] | select(.type == "file") | .path')" \
  || die "Could not list the workflow files of $REPOSITORY at $TREE_REF, so unregistered upstream workflows are not known."
while read -r path; do
  [[ "$path" == *.yml || "$path" == *.yaml ]] && upstream "$path" || continue
  awk -v p="$path" '$3 == p { found = 1 } END { exit !found }' <<< "$listed" && continue
  if new "$path"; then unregistered+=("$path"); else unregistered_old+=("$path"); fi
done <<< "$files"

join() { local IFS=,; echo "$*"; }
{
  echo "disabled=$(join "${disabled[@]}")"
  echo "cancelled=$(join "${cancelled[@]}")"
  echo "finished=$(join "${finished[@]}")"
  echo "unregistered=$(join "${unregistered[@]}")"
  echo "unregistered_old=$(join "${unregistered_old[@]}")"
} >> "$OUTPUT"
((${#finished[@]} == 0)) \
  || echo "::warning::Upstream workflow runs finished before they could be stopped: $(join "${finished[@]}")" >&2
((${#unregistered[@]} == 0)) \
  || echo "::warning::Upstream workflow files new or changed on this release that GitHub does not list yet, so not disabled: $(join "${unregistered[@]}")" >&2
echo "Upstream workflows disabled: ${#disabled[@]}; runs cancelled: ${#cancelled[@]}; finished before a sweep: ${#finished[@]}; new and not registered yet: ${#unregistered[@]}; older and not registered: ${#unregistered_old[@]}." >&2

# Before the series branch moves: any workflow file on it may ask for the Environment that holds
# FORK_PUSH_TOKEN, and the push starts its push workflows before a later sweep can stop them. A
# new upstream file that GitHub does not list cannot be disabled first, so a person decides.
if [[ "${REQUIRE_NEW_OFF:-}" == true ]] && ((${#unregistered[@]})); then
  branch="${SERIES_BRANCH:-main}"
  die "$branch was not moved: upstream workflow files that are new or changed on this release are not listed by GitHub yet, so they could not be disabled before $branch gets them:" \
    "$(printf -- '- `%s`\n' "${unregistered[@]}")" "" \
    "On $branch, a push starts their push workflows before a sweep can stop them, and any workflow file there may ask for the Environment that holds FORK_PUSH_TOKEN." \
    "A person decides. Read them in the candidate (\`git show ${TREE_REF:0:12}:<file>\`). If they are harmless, move $branch to $TREE_REF yourself" \
    "(\`git push --force-with-lease=refs/heads/$branch:${PREVIOUS_HEAD:-<its head now>} https://github.com/$REPOSITORY.git $TREE_REF:refs/heads/$branch\`)" \
    "and right after start the follow workflow once (\`gh workflow run fork-follow.yml --repo $REPOSITORY\`): it disables them and cancels their runs." \
    "Otherwise add a fork patch that deletes them; the next run then moves $branch."
fi
