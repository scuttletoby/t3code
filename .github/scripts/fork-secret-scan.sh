#!/usr/bin/env bash
# Fork only: the Secret scan job of fork-tests.yml. Picks the commits a pull request or a push adds
# and runs gitleaks over them. Tests: fork-secret-scan.test.sh.
#
# Environment:
#   GITLEAKS     gitleaks binary
#   EVENT_NAME   pull_request or push
#   BASE_SHA     pull_request: the base branch commit the PR is compared with
#   HEAD_SHA     pull_request: the PR head
#   BEFORE       push: the branch tip before the push (all zeros when the branch is new)
#   AFTER        push: the branch tip after the push
#
# Ranges:
#   pull_request   BASE_SHA..HEAD_SHA: every commit the PR adds, merges of the base branch
#                  included, whatever their dates. A squash merge never brings the PR's own commits
#                  to main, but they stay public under refs/pull/N/head, so they are scanned here.
#   push           BEFORE..AFTER. After a force push that is every commit the new tip has and the
#                  old one had not; commits both share were scanned when they were first pushed.
#   push, no BEFORE  a new branch (BEFORE all zeros) or an old tip that can no longer be fetched:
#                  only AFTER itself, with a warning. Everything before it is history this fork
#                  already had (upstream's, or scanned by earlier runs), and there is no older tip
#                  to tell new commits from old ones.
# Merge commits are diffed with --remerge-diff: only what the merge itself changed beyond an
# automatic merge of its parents (a conflict resolution, or a file added in the merge). Without it
# git log shows merges with no diff at all, so gitleaks would never read them.
set -euo pipefail

have() { git cat-file -e "$1^{commit}" 2>/dev/null; }
# Commits only, file contents on demand: the checkout is a blob-less clone.
fetch() {
  have "$1" || git fetch --no-tags --quiet --filter=blob:none origin "$1" 2>/dev/null || true
  have "$1"
}
zero() { [[ -z "$1" || "$1" =~ ^0+$ ]]; }
summary() { if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then printf '%s\n' "$@" >> "$GITHUB_STEP_SUMMARY"; fi; }

warning=""
case "$EVENT_NAME" in
  pull_request)
    for sha in "$BASE_SHA" "$HEAD_SHA"; do
      if ! fetch "$sha"; then
        echo "::error::Secret scan: cannot fetch commit $sha" >&2
        exit 2
      fi
    done
    range="$BASE_SHA..$HEAD_SHA"
    opts="--remerge-diff $range"
    what="the pull request's $(git rev-list --count "$range") commit(s) after base ${BASE_SHA:0:12}, merges included"
    ;;
  push)
    if ! zero "$BEFORE" && fetch "$BEFORE"; then
      range="$BEFORE..$AFTER"
      opts="--remerge-diff $range"
      if git merge-base --is-ancestor "$BEFORE" "$AFTER"; then
        what="the $(git rev-list --count "$range") commit(s) pushed after ${BEFORE:0:12}"
      else
        what="force push: the $(git rev-list --count "$range") commit(s) ${AFTER:0:12} has that the replaced tip ${BEFORE:0:12} had not"
      fi
    else
      range="--max-count=1 $AFTER"
      opts="--remerge-diff $range"
      if zero "$BEFORE"; then
        warning="a new branch has no earlier tip, so only its newest commit ${AFTER:0:12} is scanned"
      else
        warning="the earlier tip ${BEFORE:0:12} cannot be fetched (force push?), so only the newest commit ${AFTER:0:12} is scanned"
      fi
      what="the pushed commit ${AFTER:0:12}"
    fi
    ;;
  *)
    echo "::error::Secret scan: unexpected event $EVENT_NAME" >&2
    exit 2
    ;;
esac

echo "Scanning $what ($range)"
if [[ -n "$warning" ]]; then echo "::warning title=Secret scan::$warning"; fi
set +e
"$GITLEAKS" git --no-banner --redact --verbose --log-opts="$opts" .
status=$?
set -e
summary "### Secret scan" ""
if [[ -n "$warning" ]]; then summary "Warning: $warning." ""; fi
if [[ $status -eq 0 ]]; then
  summary "No secrets found in $what."
else
  summary "gitleaks found possible secrets in $what (values redacted; see the job log). Rotate any real secret: it is already public."
fi
exit "$status"
