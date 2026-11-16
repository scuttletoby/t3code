#!/usr/bin/env bash
# Tests for fork-run-alert.sh against a fake gh; nothing reaches GitHub.
#   bash .github/scripts/fork-run-alert.test.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# The fake gh keeps the open issues in $STUB_STATE/issues.json (number, title, author as gh's JSON
# shows it; the bot is app/github-actions). `issue list` filters by --author as GitHub's list does
# (the bot is github-actions[bot] there) and applies --jq; list.fail / create.fail / close.fail
# make that call fail. Every call is logged to calls, each issue body to bodies.
. "$here/fork-test-gh.sh"
mkdir -p "$root/bin" "$root/state"
fork_test_gh "$root/bin" <<'GH'
set -uo pipefail
s="${STUB_STATE:?}"
echo "gh $*" >> "$s/calls"
case "$1 ${2:-}" in
  "issue list")
    [[ -e "$s/list.fail" ]] && exit 1
    author="" q="."
    while (( $# )); do
      case "$1" in --author) author="$2"; shift ;; --jq) q="$2"; shift ;; esac
      shift
    done
    [[ "$author" == "github-actions[bot]" ]] && author=app/github-actions
    jq -r --arg a "$author" "[.[] | select(\$a == \"\" or .author.login == \$a)] | $q" "$s/issues.json" 2> /dev/null || true
    ;;
  "issue create")
    [[ -e "$s/create.fail" ]] && exit 1
    t="" prev=""
    for a in "$@"; do
      [[ "$prev" == --title ]] && t="$a"
      [[ "$prev" == --body-file ]] && cat "$a" >> "$s/bodies"
      prev="$a"
    done
    [[ -e "$s/issues.json" ]] || echo '[]' > "$s/issues.json"
    jq --arg t "$t" '. + [{number: 7, title: $t, author: {login: "app/github-actions", is_bot: true}}]' \
      "$s/issues.json" > "$s/issues.tmp" && mv "$s/issues.tmp" "$s/issues.json"
    echo "https://github.example.invalid/o/r/issues/7"
    ;;
  "issue comment")
    prev=""
    for a in "$@"; do [[ "$prev" == --body-file ]] && cat "$a" >> "$s/bodies"; prev="$a"; done
    ;;
  "issue close")
    [[ -e "$s/close.fail" ]] && exit 1
    jq --argjson n "$3" 'map(select(.number != $n))' "$s/issues.json" > "$s/issues.tmp" && mv "$s/issues.tmp" "$s/issues.json"
    ;;
  *) echo "fake gh: unexpected call: gh $*" >&2; exit 97 ;;
esac
GH

script="$here/fork-run-alert.sh"
export STUB_STATE="$root/state" GITHUB_REPOSITORY=owner/repo RUN_URL=https://github.example.invalid/o/r/actions/runs/1
unset GITHUB_STEP_SUMMARY
TITLE="Secret sweep failed"
fails=0

reset() { rm -rf "${root:?}/state"; mkdir -p "$root/state"; }
run() { # run RESULT [ARGS...]: as in a GitHub Actions run; sets rc and out
  local result="$1"
  shift
  set +e
  out="$(GITHUB_ACTIONS=true bash "$script" --title "$TITLE" --result "$result" "$@" 2>&1)"
  rc=$?
  set -e
}
stranger_issue() { # an open issue someone else opened, with the very same title
  jq -n --arg t "$TITLE" '[{number: 5, title: $t, author: {login: "stranger", is_bot: false}}]' > "$root/state/issues.json"
}
calls() { cat "$root/state/calls" 2> /dev/null || true; }
expect() { # expect LABEL 'CONDITION' (shell, evaluated)
  if eval "$2"; then
    echo "ok   $1"
  else
    echo "FAIL $1 (rc=$rc)"
    printf '     %s\n' "$out"
    fails=$((fails + 1))
  fi
}
rc_is() { [[ "$rc" == "$1" ]]; }
says() { grep -qF -- "$1" <<< "$out"; }
called() { grep -qF -- "$1" <<< "$(calls)"; }
not_called() { ! grep -qF -- "$1" <<< "$(calls)"; }
issues() { jq length "$root/state/issues.json" 2> /dev/null || echo 0; }

reset; run failure --message "The daily secret sweep failed"
expect "a failed job opens an issue and stays red" \
  'rc_is 1 && called "gh issue create --repo owner/repo --title Secret sweep failed" && says "Opened https://github.example.invalid/o/r/issues/7"'
expect "the issue says what failed, the result and the run" \
  'grep -qF "The daily secret sweep failed" "$root/state/bodies" && grep -qF "failure" "$root/state/bodies" && grep -qF "$RUN_URL" "$root/state/bodies"'
expect "it lists the bot's issues, never through search" 'called "--author github-actions[bot]" && not_called "search"'
run failure
expect "a second failure comments on the open issue, never a second one" \
  'rc_is 1 && called "gh issue comment 7" && [[ "$(calls | grep -c "issue create")" -eq 1 ]] && [[ "$(issues)" -eq 1 ]]'
run success
expect "a success closes the issue" 'rc_is 0 && called "gh issue close 7" && [[ "$(issues)" -eq 0 ]]'
run success
expect "a success with no issue does nothing more" 'rc_is 0 && [[ "$(calls | grep -c "issue close")" -eq 1 ]]'
for result in cancelled skipped ""; do
  reset; run "$result"
  expect "a job result '${result:-(empty)}' is a failure: an issue" 'rc_is 1 && called "issue create"'
done

reset; stranger_issue; run failure
expect "a stranger's issue with the same title is never commented on" \
  'rc_is 1 && not_called "issue comment 5" && called "issue create"'
run success
expect "a success closes its own issue and never the stranger's" \
  'rc_is 0 && called "issue close 7" && not_called "issue close 5" && [[ "$(issues)" -eq 1 ]]'
reset; stranger_issue; run success
expect "a success with only a stranger's issue closes nothing" 'rc_is 0 && not_called "issue close"'

reset; touch "$root/state/list.fail"; run success
expect "an issue list that fails fails the run, even on a success" 'rc_is 1 && says "Could not list the issues"'
reset; touch "$root/state/create.fail"; run failure
expect "an issue that cannot be opened: still red, and says so" 'rc_is 1 && says "Could not open an issue"'
reset; run failure; touch "$root/state/close.fail"; run success
expect "a close that fails fails the run with that reason" 'rc_is 1 && says "Could not close issue #7"'

reset
set +e
out="$(GITHUB_ACTIONS=true bash "$script" --title "$TITLE" 2>&1)"; rc=$?
set -e
expect "no --result: refused, nothing called" 'rc_is 2 && [[ -z "$(calls)" ]]'
set +e
out="$(GITHUB_ACTIONS=true GITHUB_REPOSITORY="" bash "$script" --title "$TITLE" --result failure 2>&1)"; rc=$?
set -e
expect "no repository: refused, nothing called" 'rc_is 2 && [[ -z "$(calls)" ]]'
set +e
out="$(env -u GITHUB_ACTIONS FORK_TEST_MODE= bash "$script" --title "$TITLE" --result failure 2>&1)"; rc=$?
set -e
expect "outside GitHub Actions and outside a test: the gh guard refuses before any call" 'rc_is 1 && [[ -z "$(calls)" ]]'

if [[ $fails -gt 0 ]]; then
  echo "$fails failed"
  exit 1
fi
echo "all passed"
