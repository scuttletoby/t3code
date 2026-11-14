#!/usr/bin/env bash
# Tests for fork-interaction-limit.sh against a fake gh; nothing reaches GitHub.
#   bash .github/scripts/fork-interaction-limit.test.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# The fake gh keeps the repository's state as files in $STUB_STATE: limit.json (what GET answers;
# absent = {}), get.fail / put.fail / put.noop / list.fail (make that call fail or not take),
# has_issues (default true), issue (the open issue's number). Every call is logged to calls.
. "$here/fork-test-gh.sh"
mkdir -p "$root/bin" "$root/state"
fork_test_gh "$root/bin" <<'GH'
set -uo pipefail
s="${STUB_STATE:?}"
echo "gh $*" >> "$s/calls"
case "$1 ${2:-}" in
  "api repos/"*/interaction-limits)
    if [[ -e "$s/get.fail" ]]; then
      [[ -e "$s/get.fail.after-put" && ! -e "$s/put.done" ]] || { echo "gh: Resource not accessible by integration (HTTP 403)" >&2; exit 1; }
    fi
    [[ -e "$s/limit.json" ]] && cat "$s/limit.json" || echo '{}'
    ;;
  "api -X")
    [[ -e "$s/put.fail" ]] && { echo "gh: Must have admin rights to Repository. (HTTP 403)" >&2; exit 1; }
    touch "$s/put.done"
    [[ -e "$s/put.noop" ]] && { echo '{}'; exit 0; }
    limit=""
    for a in "$@"; do [[ "$a" == limit=* ]] && limit="${a#limit=}"; done
    jq -n --arg l "$limit" --argjson t "$(( FORK_TEST_NOW + 182 * 86400 ))" \
      '{limit: $l, origin: "repository", expires_at: ($t | todate)}' > "$s/limit.json"
    cat "$s/limit.json"
    ;;
  "api repos/"*)
    cat "$s/has_issues" 2> /dev/null || echo true
    ;;
  "issue list")
    [[ -e "$s/list.fail" ]] && exit 1
    cat "$s/issue" 2> /dev/null || true
    ;;
  "issue create")
    echo 7 > "$s/issue"
    echo "https://github.example.invalid/o/r/issues/7"
    ;;
  "issue comment") ;;
  "issue close") rm -f "$s/issue" ;;
  *) echo "fake gh: unexpected call: gh $*" >&2; exit 97 ;;
esac
GH

script="$here/fork-interaction-limit.sh"
export STUB_STATE="$root/state" FORK_TEST_NOW=1800000000 GITHUB_REPOSITORY=owner/repo
unset REPOSITORY GITHUB_STEP_SUMMARY RUN_URL
fails=0
day=86400

reset() { rm -rf "${root:?}/state"; mkdir -p "$root/state"; }
limit() { # limit LIMIT DAYS_LEFT: the repository's stored limit
  jq -n --arg l "$1" --argjson t "$(( FORK_TEST_NOW + $2 * day ))" \
    '{limit: $l, origin: "repository", expires_at: ($t | todate)}' > "$root/state/limit.json"
}
run() { # run ARGS...: sets rc and out (stdout and stderr)
  set +e
  out="$(bash "$script" "$@" 2>&1)"
  rc=$?
  set -e
}
calls() { cat "$root/state/calls" 2> /dev/null || true; }
expect() { # expect LABEL 'CONDITION' (shell, evaluated)
  local label="$1"
  if eval "$2"; then
    echo "ok   $label"
  else
    echo "FAIL $label (rc=$rc)"
    printf '     %s\n' "$out"
    fails=$((fails + 1))
  fi
}
rc_is() { [[ "$rc" == "$1" ]]; }
says() { grep -qF -- "$1" <<< "$out"; }
called() { calls | grep -qF -- "$1"; }
not_called() { ! calls | grep -qF -- "$1"; }

# --- check -------------------------------------------------------------------------------------
reset; limit collaborators_only 180; run check
expect "check: the limit with 180 days left passes" 'rc_is 0 && says "ok: owner/repo"'
expect "check: it only reads" 'not_called "-X PUT"'

reset; limit collaborators_only 31; run check
expect "check: 31 days left passes the default 30" 'rc_is 0'
reset; limit collaborators_only 30; run check
expect "check: 30 days left fails the default 30" 'rc_is 1 && says "renew it"'
reset; limit collaborators_only 100; run check --days 120
expect "check: --days sets the margin" 'rc_is 1'
reset; limit collaborators_only -2; run check
expect "check: an ended limit fails" 'rc_is 1'

reset; run check
expect "check: no limit ({}) fails and says so" 'rc_is 1 && says "has no interaction limit"'
reset; : > "$root/state/limit.json"; run check
expect "check: an empty answer counts as no limit" 'rc_is 1 && says "has no interaction limit"'

reset; limit contributors_only 180; run check
expect "check: another limit fails" 'rc_is 1 && says "wanted collaborators_only"'
reset; limit contributors_only 180; run check --limit contributors_only
expect "check: --limit picks the wanted limit" 'rc_is 0'
reset; limit collaborators_only 180; run check --limit contributors_only
expect "check: a stricter limit than configured still fails (exact match)" 'rc_is 1'

reset; touch "$root/state/get.fail"; run check
expect "check: a read the token may not make is an error (2), not a pass" 'rc_is 2 && says "could not read"'
reset; echo '[1,2]' > "$root/state/limit.json"; run check
expect "check: an answer of the wrong shape is an error" 'rc_is 2'
reset; echo '{"limit":"collaborators_only","expires_at":"soon"}' > "$root/state/limit.json"; run check
expect "check: an unreadable end date is an error" 'rc_is 2'

reset; limit collaborators_only 180; run check --out "$root/result"
expect "check --out: ok result" 'rc_is 0 && [[ "$(head -n1 "$root/result")" == ok ]]'
reset; run check --out "$root/result"
expect "check --out: attention result" '[[ "$(head -n1 "$root/result")" == attention ]]'
reset; touch "$root/state/get.fail"; run check --out "$root/result"
expect "check --out: error result" '[[ "$(head -n1 "$root/result")" == error ]]'

reset; limit collaborators_only 180; run check --repo other/name
expect "check: --repo picks the repository" 'called "gh api repos/other/name/interaction-limits"'
reset; run check --limit everyone
expect "check: an unknown limit is refused before any call" 'rc_is 2 && not_called "gh "'
reset; run check --days soon
expect "check: --days must be a number" 'rc_is 2'
reset; GITHUB_REPOSITORY="" run check
expect "check: no repository is refused" 'rc_is 2 && says "no repository"'
reset; run frobnicate
expect "an unknown command shows the usage" 'rc_is 2 && says "check  [--repo"'

# --- renew -------------------------------------------------------------------------------------
reset; run renew
expect "renew: sets the limit for six months" 'rc_is 0 && called "-X PUT repos/owner/repo/interaction-limits -f limit=collaborators_only -f expiry=six_months"'
expect "renew: reads it back" 'says "renewed: owner/repo: the limit is collaborators_only" && [[ "$(calls | grep -c interaction-limits)" -eq 2 ]]'
run renew
expect "renew: running it again just renews again" 'rc_is 0 && [[ "$(calls | grep -c -- "-X PUT")" -eq 2 ]]'

reset; limit collaborators_only 100; run renew --if-under 60
expect "renew --if-under: more days left reads only" 'rc_is 0 && not_called "-X PUT"'
expect "renew --if-under: and says so" 'says "no renewal needed"'
reset; limit collaborators_only 59; run renew --if-under 60
expect "renew --if-under: fewer days left renews" 'rc_is 0 && called "-X PUT"'
reset; limit contributors_only 100; run renew --if-under 60
expect "renew --if-under: another limit renews to the configured one" 'rc_is 0 && called "limit=collaborators_only"'
reset; run renew --if-under 60
expect "renew --if-under: no limit renews" 'rc_is 0 && called "-X PUT"'
reset; touch "$root/state/get.fail"; run renew --if-under 60
expect "renew --if-under: an unreadable limit sets nothing" 'rc_is 2 && not_called "-X PUT"'

reset; touch "$root/state/put.fail"; run renew
expect "renew: a refused set fails" 'rc_is 2 && says "could not set"'
expect "renew: and claims nothing" '! says "renewed:" && [[ "$(calls | grep -c interaction-limits)" -eq 1 ]]'
reset; limit collaborators_only 10; touch "$root/state/put.noop"; run renew
expect "renew: a set that did not take fails on the read back" 'rc_is 2 && says "but read back"'
reset; touch "$root/state/get.fail" "$root/state/get.fail.after-put"; run renew
expect "renew: a read back that fails fails" 'rc_is 2 && says "could not read it back"'
reset; run renew --limit contributors_only
expect "renew: --limit sets that limit" 'called "limit=contributors_only"'

# --- report ------------------------------------------------------------------------------------
reset; printf 'attention\nowner/repo has no interaction limit\n' > "$root/result"; run report "$root/result"
expect "report: a failed check opens an issue" 'rc_is 1 && called "gh issue create --repo owner/repo --title Interaction limit needs attention"'
expect "report: and names it" 'says "Opened https://github.example.invalid/o/r/issues/7"'
run report "$root/result"
expect "report: again comments on the open issue, never a second one" \
  'rc_is 1 && called "gh issue comment 7" && [[ "$(calls | grep -c "issue create")" -eq 1 ]]'
printf 'ok\nok: fine\n' > "$root/result"; run report "$root/result"
expect "report: an ok check closes the issue" 'rc_is 0 && called "gh issue close 7"'
run report "$root/result"
expect "report: an ok check with no issue does nothing more" 'rc_is 0 && [[ "$(calls | grep -c "issue close")" -eq 1 ]]'

reset; run report "$root/missing"
expect "report: a check that left no result is a failure with an issue" 'rc_is 1 && called "issue create"'
reset; printf 'error\ncould not read\n' > "$root/result"; echo false > "$root/state/has_issues"; run report "$root/result"
expect "report: with Issues off it opens nothing but still fails" 'rc_is 1 && not_called "gh issue"'
reset; printf 'attention\nx\n' > "$root/result"; touch "$root/state/list.fail"; run report "$root/result"
expect "report: an issue list that fails opens nothing (no duplicate) and fails" 'rc_is 1 && not_called "issue create"'
reset; printf 'ok\nfine\n' > "$root/result"; touch "$root/state/list.fail"; run report "$root/result"
expect "report: an ok check whose issue list fails still fails" 'rc_is 1'

# --- the gh guard ------------------------------------------------------------------------------
reset; limit collaborators_only 180
set +e
out="$(env -u FORK_TEST_MODE bash "$script" check 2>&1)"; rc=$?
set -e
expect "guard: outside Actions, a test and FORK_GH_LOCAL it refuses before any call" 'rc_is 1 && not_called "gh "'
set +e
out="$(env -u FORK_TEST_MODE FORK_GH_LOCAL=fork-release.sh bash "$script" check 2>&1)"; rc=$?
set -e
expect "guard: FORK_GH_LOCAL naming another script refuses" 'rc_is 1 && not_called "gh "'
# Not test mode: the pinned clock is ignored, so a limit that ended by the pinned time (year 2096)
# still has decades left by the real one. The stub stays first on PATH; GH_HOST is .invalid.
reset; jq -n '{limit: "collaborators_only", origin: "repository", expires_at: "2090-01-01T00:00:00Z"}' > "$root/state/limit.json"
set +e
out="$(env -u FORK_TEST_MODE FORK_GH_LOCAL=fork-interaction-limit.sh FORK_TEST_NOW=4000000000 bash "$script" check 2>&1)"; rc=$?
set -e
expect "guard: FORK_GH_LOCAL naming this script runs it, on the real clock" 'rc_is 0 && called "gh api repos/owner/repo/interaction-limits"'
set +e
out="$(GH_TOKEN=x bash "$script" check 2>&1)"; rc=$?
set -e
expect "guard: a token in test mode refuses" 'rc_is 1 && says "a GitHub token is set"'

echo
if (( fails )); then
  echo "$fails failed"
  exit 1
fi
echo "all passed"
