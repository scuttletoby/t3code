#!/usr/bin/env bash
# Report job of fork-follow.yml (tests: fork-follow.test.sh). Writes the run's summary and exits
# non-zero when the run needs a person, so trouble shows as a failed run even without issues.
#
# What was pushed and published is read back from the repository (branches, tags, releases), not
# inferred from job results, so the summary never claims a state it did not see. When the
# repository has Issues enabled it also opens or comments on one issue, "Fork cannot follow
# upstream", and closes it once a run succeeds; with Issues disabled it says so and opens none.
#
# Environment (from the run): REPOSITORY, SERIES_BRANCH, RUN_URL, ACTION, GUARD_RESULT,
# GUARD_ATTEMPT (the run attempt the guard passed in), RUN_ATTEMPT, PLAN_RESULT, RELEASE_RESULT,
# ADVANCE_RESULT, BASE_TAG, PREVIOUS_TAG, PREVIOUS_HEAD, CANDIDATE, RELEASED_TAG, DROPPED, DISABLED,
# CANCELLED, FINISHED, UNREGISTERED, UNREGISTERED_OLD (from the workflow sweeps), SWEEP_RESULT,
# REPORTS (directory of the jobs' reason files), ORIGIN (git URL of this fork, default GitHub).
# Summary to $GITHUB_STEP_SUMMARY, else stdout.
set -uo pipefail
# Refuses outside a GitHub Actions run unless a test runs it under the gh stub (fork-gh-guard.sh).
. "$(dirname "${BASH_SOURCE[0]}")/fork-gh-guard.sh"

REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:?REPOSITORY or GITHUB_REPOSITORY}}"
ORIGIN="${ORIGIN:-https://github.com/$REPOSITORY.git}"
SERIES_BRANCH="${SERIES_BRANCH:-main}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
ACTION="${ACTION:-}"
REPORTS="${REPORTS:-}"
title="Fork cannot follow upstream"
short() { [[ -n "$1" ]] && echo "\`${1:0:12}\`" || echo "(none)"; }

failed=false
for result in "${GUARD_RESULT:-success}" "${PLAN_RESULT:-}" "${RELEASE_RESULT:-skipped}" "${ADVANCE_RESULT:-skipped}"; do
  [[ "$result" == success || "$result" == skipped ]] || failed=true
done
[[ "${PLAN_RESULT:-}" == success ]] || failed=true
[[ "$ACTION" != stage ]] || failed=true
[[ "${SWEEP_RESULT:-success}" != failure ]] || failed=true

# A re-run of failed jobs (or of one job) keeps the guard's result from an earlier attempt; the jobs
# that get FORK_PUSH_TOKEN skip themselves then. Say so whenever one of them was skipped for it.
stale_guard=false
if [[ "${GUARD_RESULT:-success}" == success && -n "${GUARD_ATTEMPT:-}" && -n "${RUN_ATTEMPT:-}" \
  && "$GUARD_ATTEMPT" != "$RUN_ATTEMPT" ]]; then
  if [[ "${PLAN_RESULT:-}" == skipped ]] \
    || [[ "${ADVANCE_RESULT:-skipped}" == skipped && ( "$ACTION" == advance || "$ACTION" == rebase ) ]]; then
    stale_guard=true
    failed=true
  fi
fi

# The sweeps' lists arrive joined with ','; drop the empty parts and repeats.
tidy() { tr ',' '\n' <<< "$1" | sed '/^$/d' | awk '!seen[$0]++' | paste -sd, -; }
DISABLED="$(tidy "${DISABLED:-}")"
CANCELLED="$(tidy "${CANCELLED:-}")"
FINISHED="$(tidy "${FINISHED:-}")"
UNREGISTERED="$(tidy "${UNREGISTERED:-}")"
UNREGISTERED_OLD="$(tidy "${UNREGISTERED_OLD:-}")"

state=()
row() { state+=("| $1 | $2 |"); }

# --- what the repository holds now -----------------------------------------------------------
if refs="$(git ls-remote "$ORIGIN" "refs/heads/$SERIES_BRANCH" refs/heads/fork-follow/candidate 'refs/tags/*' 2>&1)"; then
  at() { awk -v ref="$1" '$2 == ref { print $1 }' <<< "$refs"; }
  series_now="$(at "refs/heads/$SERIES_BRANCH")"
  candidate_now="$(at refs/heads/fork-follow/candidate)"
  if [[ -z "${CANDIDATE:-}" ]]; then
    row "Series branch \`$SERIES_BRANCH\`" "at $(short "$series_now")"
  elif [[ "$series_now" == "$CANDIDATE" && "${PREVIOUS_HEAD:-$CANDIDATE}" != "$CANDIDATE" ]]; then
    row "Series branch \`$SERIES_BRANCH\`" "moved to $(short "$CANDIDATE") (was $(short "$PREVIOUS_HEAD"))"
  elif [[ "$series_now" == "$CANDIDATE" ]]; then
    row "Series branch \`$SERIES_BRANCH\`" "at $(short "$series_now"), unchanged"
  else
    row "Series branch \`$SERIES_BRANCH\`" "**not moved**: at $(short "$series_now")"
  fi
  row "Candidate branch \`fork-follow/candidate\`" "$([[ -n "$candidate_now" ]] && echo "at $(short "$candidate_now")" || echo "absent")"
  backups="$(awk '$2 ~ "^refs/tags/fork-follow/pre-" && $2 !~ /\^\{\}$/ { sub("refs/tags/", "", $2); print $2 }' <<< "$refs" | paste -sd, -)"
  [[ -z "$backups" ]] || row "Backups of earlier series (tags)" "${backups//,/, }"
  # Fork release tags that point at the candidate (lightweight, or annotated and peeled).
  tags=""
  if [[ -n "${CANDIDATE:-}" ]]; then
    tags="$(awk -v sha="$CANDIDATE" '$1 == sha { t = $2; sub("refs/tags/", "", t); sub(/\^\{\}$/, "", t); print t }' <<< "$refs" \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+-fork\.[0-9]+$' | sort -uV)"
  fi
  if [[ -z "${CANDIDATE:-}" ]]; then
    row "Release" "none: the plan did not pick a commit"
  elif [[ -z "$tags" ]]; then
    row "Release" "**none published** for $(short "$CANDIDATE")"
  else
    for tag in $tags; do
      if draft="$(gh api "repos/$REPOSITORY/releases/tags/$tag" --jq .draft 2>&1)"; then
        if [[ "$draft" == false ]]; then
          if [[ "$tag" == "${RELEASED_TAG:-}" && "$ACTION" != release ]]; then
            row "Release" "$tag, published earlier"
          else
            row "Release" "$tag **published** at $(short "$CANDIDATE")"
          fi
        else
          row "Release" "$tag tag exists, release is still a draft (not offered to updaters)"
          failed=true
        fi
      elif [[ "$draft" == *"Not Found"* || "$draft" == *"404"* ]]; then
        row "Release" "tag $tag exists without a release"
        failed=true
      else
        row "Release" "unknown: could not read the release of $tag"
        failed=true
      fi
    done
  fi
else
  row "Repository state" "unknown: could not read the branches and tags of $REPOSITORY"
  failed=true
fi
[[ -z "${DISABLED:-}" ]] || row "Upstream workflows disabled" "${DISABLED//,/, }"
[[ -z "${CANCELLED:-}" ]] || row "Their runs cancelled" "${CANCELLED//,/, }"
[[ -z "${FINISHED:-}" ]] || row "**Upstream runs that finished before a sweep could stop them**" "${FINISHED//,/, }"
[[ -z "${UNREGISTERED:-}" ]] \
  || row "**Upstream workflow files new on this release that GitHub does not list yet, so not disabled** (the sweep after an event reaches them disables them; the branch move waits for that)" "${UNREGISTERED//,/, }"
[[ -z "${DROPPED:-}" ]] || row "Patches dropped (empty on $BASE_TAG: upstream has the change)" "${DROPPED//;/, }"

reasons=""
if [[ "$stale_guard" == true ]]; then
  reasons+="This is attempt $RUN_ATTEMPT of the run, but the guard passed in attempt $GUARD_ATTEMPT: \"Re-run failed jobs\" and \"Re-run job\" keep the guard's earlier result without checking again, so the jobs that get FORK_PUSH_TOKEN were skipped. Use \"Re-run all jobs\", or start a new run (\`gh workflow run fork-follow.yml --repo $REPOSITORY\`)."$'\n\n'
fi
if [[ -n "$REPORTS" && -d "$REPORTS" ]]; then
  for file in "$REPORTS"/*.md; do
    [[ -f "$file" ]] && reasons+="$(cat "$file")"$'\n\n'
  done
fi

case "$ACTION" in
  none) what="Nothing to do: the series is released${RELEASED_TAG:+ as $RELEASED_TAG}." ;;
  release) what="Release the series on ${BASE_TAG:-?} as it is." ;;
  rebase) what="Rebase the series from ${PREVIOUS_TAG:-?} onto ${BASE_TAG:-?}, release it, then move \`$SERIES_BRANCH\`." ;;
  advance) what="Move \`$SERIES_BRANCH\` onto ${BASE_TAG:-?}: ${RELEASED_TAG:-?} already has the rebased series." ;;
  stage) what="Push the candidate on ${BASE_TAG:-?}; its release needs its own workflow files." ;;
  *) if [[ "${GUARD_RESULT:-success}" != success ]]; then
       what="None: the guard refused, so no job got FORK_PUSH_TOKEN."
     elif [[ "$stale_guard" == true ]]; then
       what="None: the guard did not run in this attempt, so no job got FORK_PUSH_TOKEN."
     else
       what="The plan did not finish."
     fi ;;
esac

body="$(mktemp)"
{
  if [[ "$failed" == true ]]; then
    echo "## $title"
  else
    echo "## Fork follows upstream"
  fi
  echo
  echo "Plan: $what Run: ${RUN_URL:-?}"
  echo
  if [[ "$failed" == true ]]; then
    if [[ -n "$reasons" ]]; then
      printf '%s' "$reasons"
    else
      echo "A job failed without writing a reason (results: guard ${GUARD_RESULT:-success}, plan ${PLAN_RESULT:-?}, release ${RELEASE_RESULT:-skipped}, advance ${ADVANCE_RESULT:-skipped}); see the run."
      echo
    fi
  fi
  echo "| What | State now |"
  echo "|---|---|"
  printf '%s\n' "${state[@]}"
  if [[ -n "$UNREGISTERED_OLD" ]]; then
    echo
    echo "<details><summary>Upstream workflow files from earlier releases that GitHub does not list (not a warning)</summary>"
    echo
    echo "Most of them run only on events this fork never sends (workflow_call, workflow_dispatch, workflow_run), so they stay unlisted. A sweep disables one once an event reaches it: ${UNREGISTERED_OLD//,/, }"
    echo
    echo "</details>"
  fi
} > "$body"

# --- the issue, when the repository has Issues ---------------------------------------------------
note=""
if ! has_issues="$(gh api "repos/$REPOSITORY" --jq .has_issues 2>&1)"; then
  note="Could not read whether $REPOSITORY has Issues, so no issue was opened or closed."
  failed=true
elif [[ "$has_issues" != true ]]; then
  note="Issues are disabled on $REPOSITORY, so no issue was opened or closed; this summary and the run's status are the report."
elif ! issue="$(gh issue list --repo "$REPOSITORY" --state open --search "\"$title\" in:title" \
  --json number,title --jq ".[] | select(.title == \"$title\") | .number" | head -n1)"; then
  note="Could not list the issues of $REPOSITORY, so no issue was opened or closed."
  failed=true
elif [[ "$failed" == true && -n "$issue" ]]; then
  gh issue comment "$issue" --repo "$REPOSITORY" --body-file "$body" > /dev/null \
    && note="Commented on issue #$issue." || { note="Could not comment on issue #$issue."; }
elif [[ "$failed" == true ]]; then
  if url="$(gh issue create --repo "$REPOSITORY" --title "$title" --body-file "$body")"; then
    note="Opened $url."
  else
    note="Could not open an issue."
  fi
elif [[ -n "$issue" ]]; then
  gh issue close "$issue" --repo "$REPOSITORY" --comment "$(cat "$body")" > /dev/null \
    && note="Closed issue #$issue." || { note="Could not close issue #$issue."; failed=true; }
fi

{
  cat "$body"
  [[ -z "$note" ]] || printf '\n%s\n' "$note"
} >> "$SUMMARY"
rm -f "$body"
[[ "$failed" != true ]]
