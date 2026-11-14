#!/usr/bin/env bash
# The pushes of fork-follow.yml, all made with FORK_PUSH_TOKEN, one subcommand each (tests:
# fork-follow.test.sh). Every refusal writes its reason to $REPORT, prints a ::error:: line and
# exits non-zero; nothing falls back to another token.
#
#   check            Fails unless FORK_PUSH_TOKEN is set, accepted by GitHub and may push here.
#   candidate SHA    Force-pushes SHA as the branch fork-follow/candidate.
#   advance PREV SHA Moves SERIES_BRANCH from PREV to SHA with a lease. First keeps PREV as the tag
#                    fork-follow/pre-<BASE_TAG>-<PREV, 12 hex>, so the old series stays reachable,
#                    then tags SHA as fork-follow/synced/<SHA>: the push run of a tagged commit never
#                    uses the fleet (upstream code runs GitHub-hosted only; tag_prefix in
#                    .github/fork-fleet.json, which must say the same). Already at SHA (a re-run) tags
#                    it and is success; moved elsewhere is a refusal.
#   cleanup          Deletes the branch fork-follow/candidate; a failure is only a warning.
#
# Environment: FORK_PUSH_TOKEN, FORK_PUSH_ENVIRONMENT (the GitHub Environment that should hold the
# token; named when it is missing), REPOSITORY (owner/name; GITHUB_REPOSITORY in a run), PUSH_URL
# (default https://github.com/$REPOSITORY.git), SERIES_BRANCH (default main), BASE_TAG (advance),
# REPORT. Output (candidate_pushed, moved, backup_tag) goes to $GITHUB_OUTPUT, else stdout.
#
# The token reaches git through GIT_CONFIG_* variables as an HTTP header: never in a URL, a command
# line or .git/config, so git's error messages (copied into reports) cannot contain it.
set -euo pipefail

REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:?REPOSITORY or GITHUB_REPOSITORY}}"
PUSH_URL="${PUSH_URL:-https://github.com/$REPOSITORY.git}"
SERIES_BRANCH="${SERIES_BRANCH:-main}"
REPORT="${REPORT:-fork-follow-report.md}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"
CANDIDATE_BRANCH=fork-follow/candidate
SYNCED_TAG_PREFIX=fork-follow/synced/
err="$(mktemp)"
trap 'rm -f "$err"' EXIT

die() {
  printf '%s\n' "$@" > "$REPORT"
  printf '::error::%s\n' "$1" >&2
  [[ $# -lt 2 ]] || printf '%s\n' "${@:2}" >&2
  exit 1
}

git_said() { printf '%s\n' '```' "$(tail -n 15 "$err")" '```'; }

with_token() {
  if [[ -z "${FORK_PUSH_TOKEN:-}" && -n "${FORK_PUSH_ENVIRONMENT:-}" ]]; then
    die "The secret FORK_PUSH_TOKEN is missing in the Environment $FORK_PUSH_ENVIRONMENT (or the Environment does not exist yet), so this run cannot push." \
      "Under Settings > Environments, create $FORK_PUSH_ENVIRONMENT (a run that names a missing Environment makes an empty one with no rules)," \
      "limit its deployment branches to $SERIES_BRANCH, and add the secret FORK_PUSH_TOKEN there: a fine-grained token for $REPOSITORY only, with Contents and Workflows read and write." \
      "Keep it out of the repository secrets: a workflow file on any pushed branch can read those, while an Environment limited to $SERIES_BRANCH gives it only to runs from $SERIES_BRANCH." \
      "GITHUB_TOKEN cannot replace it: it may not push commits that change workflow files."
  fi
  [[ -n "${FORK_PUSH_TOKEN:-}" ]] \
    || die "The secret FORK_PUSH_TOKEN is missing, so this run cannot push." \
      "Add it under Settings > Secrets and variables > Actions: a fine-grained token for $REPOSITORY only, with Contents and Workflows read and write." \
      "GITHUB_TOKEN cannot replace it: it may not push commits that change workflow files."
  local header
  header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$FORK_PUSH_TOKEN" | base64 -w0)"
  [[ "${GITHUB_ACTIONS:-}" != true ]] || echo "::add-mask::$header"
  export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.https://github.com/.extraheader GIT_CONFIG_VALUE_0="$header"
  export GIT_TERMINAL_PROMPT=0
}

remote_ref() { # prints the remote's commit for a ref, or nothing; fails if the remote cannot be read
  git ls-remote "$PUSH_URL" "$1" 2> "$err" | cut -f1
}

cmd_check() {
  with_token
  git ls-remote "$PUSH_URL" HEAD > /dev/null 2> "$err" \
    || die "GitHub refused FORK_PUSH_TOKEN: it is expired, revoked, or not valid for $REPOSITORY." \
      "Renew it (a fine-grained token for $REPOSITORY only, Contents and Workflows read and write) and update the secret. git said:" \
      "$(git_said)"
  # A dry run asks for push access (receive-pack) without changing anything.
  git push --dry-run "$PUSH_URL" "$(git rev-parse HEAD):refs/heads/fork-follow/token-check" > /dev/null 2> "$err" \
    || die "FORK_PUSH_TOKEN may not push to $REPOSITORY." \
      "Give the token Contents read and write (and Workflows read and write) on $REPOSITORY. git said:" "$(git_said)"
  echo "FORK_PUSH_TOKEN is accepted and may push to $REPOSITORY."
}

cmd_candidate() {
  local sha="${1:?SHA}"
  with_token
  git push --force "$PUSH_URL" "$sha:refs/heads/$CANDIDATE_BRANCH" > /dev/null 2> "$err" \
    || die "Could not push the candidate ${sha:0:12} to $CANDIDATE_BRANCH." \
      "If the rebase brought workflow file changes, the token also needs Workflows read and write. git said:" "$(git_said)"
  echo "candidate_pushed=true" >> "$OUTPUT"
  echo "Pushed ${sha:0:12} to $CANDIDATE_BRANCH."
}

tag_synced() { # tag_synced SHA OUTCOME: the tag keeping the push run of SHA off the fleet, made before main is SHA
  git push "$PUSH_URL" "$1:refs/tags/$SYNCED_TAG_PREFIX$1" > /dev/null 2> "$err" \
    || die "Could not tag ${1:0:12} as $SYNCED_TAG_PREFIX$1, $2." \
      "Without the tag, the push run of a synced commit may use the fleet; upstream code runs on GitHub-hosted runners only. git said:" "$(git_said)"
}

cmd_advance() {
  local prev="${1:?PREV}" sha="${2:?SHA}" now backup
  : "${BASE_TAG:?BASE_TAG}"
  with_token
  now="$(remote_ref "refs/heads/$SERIES_BRANCH")" \
    || die "Could not read $SERIES_BRANCH of $REPOSITORY, so it was not moved. git said:" "$(git_said)"
  if [[ "$now" == "$sha" ]]; then
    tag_synced "$sha" "so a re-run of its push run could use the fleet" # an earlier run moved it
    echo "moved=already" >> "$OUTPUT"
    echo "$SERIES_BRANCH is already at ${sha:0:12}."
    return 0
  fi
  [[ "$now" == "$prev" ]] \
    || die "$SERIES_BRANCH moved since the plan (now ${now:-missing}, planned from ${prev:0:12}), so it was not moved to ${sha:0:12}." \
      "A fork pull request merged meanwhile, or someone pushed it. The next run rebases again with that change."
  backup="fork-follow/pre-$BASE_TAG-${prev:0:12}"
  git push "$PUSH_URL" "$prev:refs/tags/$backup" > /dev/null 2> "$err" \
    || die "Could not keep the old series ${prev:0:12} as tag $backup, so $SERIES_BRANCH was not moved. git said:" "$(git_said)"
  echo "backup_tag=$backup" >> "$OUTPUT"
  tag_synced "$sha" "so $SERIES_BRANCH was not moved"
  git push --force-with-lease="refs/heads/$SERIES_BRANCH:$prev" "$PUSH_URL" "$sha:refs/heads/$SERIES_BRANCH" > /dev/null 2> "$err" \
    || die "Could not move $SERIES_BRANCH from ${prev:0:12} to ${sha:0:12}." \
      "If a rule protects $SERIES_BRANCH, the token's user must be allowed to bypass it. The old series is kept as tag $backup. git said:" "$(git_said)"
  echo "moved=true" >> "$OUTPUT"
  echo "Moved $SERIES_BRANCH from ${prev:0:12} to ${sha:0:12} (old series kept as tag $backup)."
}

cmd_cleanup() {
  with_token
  local now
  if ! now="$(remote_ref "refs/heads/$CANDIDATE_BRANCH")"; then
    echo "::warning::Could not read $CANDIDATE_BRANCH; it may be left over (the next run force-pushes over it)."
  elif [[ -n "$now" ]] && ! git push "$PUSH_URL" --delete "refs/heads/$CANDIDATE_BRANCH" > /dev/null 2> "$err"; then
    echo "::warning::Could not delete $CANDIDATE_BRANCH (the next run force-pushes over it): $(tail -n 3 "$err" | tr '\n' ' ')"
  fi
  return 0
}

case "${1:-}" in
  check) cmd_check ;;
  candidate) cmd_candidate "${@:2}" ;;
  advance) cmd_advance "${@:2}" ;;
  cleanup) cmd_cleanup ;;
  *) echo "usage: $0 check | candidate SHA | advance PREV SHA | cleanup" >&2; exit 2 ;;
esac
