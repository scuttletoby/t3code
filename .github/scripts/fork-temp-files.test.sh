#!/usr/bin/env bash
# Every fork shell script removes the temporary files it makes, however it ends: passing, refused,
# stopped half-way, or ended by a signal bash can catch (TERM, INT, HUP; only KILL leaves them).
# One rule for all of them (a script that calls mktemp has an EXIT trap), then the scripts one by
# one, counting the entries of TMPDIR before and after a call. Local git repositories and a fake
# `gh`; no network.
#   bash .github/scripts/fork-temp-files.test.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# Fake gh: every call ends the process whose id is in $KILL_PID_FILE (TERM), the way a cancelled
# run ends a script half-way. fork_test_gh (fork-test-gh.sh) installs it first on PATH, with no
# token, a .invalid GH_HOST and FORK_TEST_MODE, so the scripts refuse if any other gh would answer.
. "$here/fork-test-gh.sh"
mkdir -p "$root/bin"
fork_test_gh "$root/bin" << 'GH'
kill -TERM "$(cat "${KILL_PID_FILE:?the gh stub needs KILL_PID_FILE}")"
exit 1
GH

# Stand-ins for git and tar that end the calling script the same way.
mkdir -p "$root/killbin"
printf '%s\n' '#!/usr/bin/env bash' 'kill -TERM "$(cat "${KILL_PID_FILE:?}")"' 'exit 1' > "$root/killbin/git"
cp "$root/killbin/git" "$root/killbin/tar"
chmod +x "$root/killbin/git" "$root/killbin/tar"

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$root/gitconfig"
git config --global user.name test
git config --global user.email test@example.invalid
git config --global init.defaultBranch main
git config --global advice.detachedHead false
git config --global url."$root/gh/".insteadOf https://github.com/
export REPOSITORY=me/t3code GITHUB_REPOSITORY=me/t3code UPSTREAM=up/t3code FORK_FOLLOW_CHANNEL=stable
unset GITHUB_OUTPUT GITHUB_STEP_SUMMARY GITHUB_ACTIONS UPSTREAM_URL ORIGIN RUN_SHA SERIES_BRANCH \
  RUNNER_TEMP REPORT REPORTS CANDIDATE
export KILL_PID_FILE="$root/pid"
# The scripts' temporary files go to a folder of this test, so each one is seen.
export TMPDIR="$root/tmp"
mkdir -p "$TMPDIR"

failures=0
pass() { echo "ok   $1"; }
fail() {
  echo "FAIL $1"
  sed 's/^/     | /' "$root/out" 2> /dev/null || true
  failures=$((failures + 1))
}
check() { # check NAME CONDITION...: passes when the condition (a command) succeeds
  local name="$1"
  shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}
has() { grep -q -- "$1" "$2" 2> /dev/null; }
tmp_entries() { find "$TMPDIR" -mindepth 1 -maxdepth 1 | wc -l; }

# run DIR COMMAND...: COMMAND in DIR, as the process whose id $KILL_PID_FILE holds (`exec` keeps the
# id), so a stand-in above can end exactly that script. Output in $root/out; the braces take this
# shell's own "Terminated" line about an ended script.
run() {
  { (cd "$1" && shift && echo "$BASHPID" > "$KILL_PID_FILE" && exec "$@") > "$root/out" 2>&1; } 2> /dev/null
}
# clean NAME WANT RUN-ARGS...: the call ends with status WANT (a number, or `refused` for 1 to 127:
# it stopped by itself) and TMPDIR has no entry it did not have before.
clean() {
  local name="$1" want="$2" before status=0 left
  shift 2
  before="$(tmp_entries)"
  run "$@" || status=$?
  left=$(($(tmp_entries) - before))
  if [[ "$want" == refused ]] && ((status > 0 && status < 128)); then want="$status"; fi
  if [[ "$status" == "$want" ]] && ((left == 0)); then
    pass "$name"
  else
    { echo "exit $status (wanted $want); $left new in TMPDIR:"; ls -A "$TMPDIR"; } >> "$root/out"
    fail "$name"
  fi
}

# --- the rule: mktemp only with an EXIT trap -----------------------------------------------------
# A removal at the end of the script is skipped by every early exit; the trap is not.
makers=() untrapped=()
for file in "$here"/*.sh; do
  grep -qE '^[^#]*\<mktemp\>' "$file" || continue
  makers+=("${file##*/}")
  grep -qE '^[[:space:]]*trap .*\<EXIT\>' "$file" || untrapped+=("${file##*/}")
done
printf '%s\n' "scripts that call mktemp: ${makers[*]}" "without an EXIT trap: ${untrapped[*]:-none}" > "$root/out"
check "rule: every fork shell script that calls mktemp has an EXIT trap" \
  eval '[[ " ${makers[*]} " == *" fork-follow.sh "* && " ${makers[*]} " == *" fork-release.sh "* && ${#untrapped[@]} == 0 ]]'

# --- the plan: fork-follow.sh --------------------------------------------------------------------
# Upstream up/t3code with v0.1.9 and v0.1.10; the fork me/t3code is v0.1.10 plus one commit, in
# $root/work. The plan's answer for it is `release`.
mkdir -p "$root/gh/up" "$root/gh/me"
git init -q --bare "$root/gh/up/t3code.git"
git init -q --bare "$root/gh/me/t3code.git"
(
  git init -q "$root/upwork" && cd "$root/upwork"
  mkdir -p .github/workflows
  echo "on: workflow_call" > .github/workflows/release-desktop.yml
  echo u1 > u1.txt && git add -A && git commit -q -m u1 && git tag v0.1.9
  echo u2 > u2.txt && git add -A && git commit -q -m u2 && git tag v0.1.10
  git push -q https://github.com/up/t3code.git main --tags
) > /dev/null 2>&1
git clone -q https://github.com/up/t3code.git "$root/work" 2> /dev/null
(
  cd "$root/work"
  git remote set-url origin https://github.com/me/t3code.git
  git checkout -q -B main v0.1.10
  mkdir -p .github/scripts
  cp "$here/fork-schema-check.cjs" "$here/fork-seam-check.cjs" .github/scripts/
  echo '{"files":{}}' > .github/fork-seams.json
  printf 'jobs:\n  desktop:\n    uses: ./.github/workflows/release-desktop.yml\n' > .github/workflows/fork-release.yml
  echo "# upstream workflow files the fork deletes" > .github/fork-removed-workflows.txt
  echo feature > feature.txt
  git add -A && git commit -q -m "fork: own change"
  git push -q --force origin main
) > /dev/null 2>&1

plan() { # plan NAME WANT [ENV...]: the plan in $root/work; its output in $root/plan.out, its reason in $root/report.md
  local name="$1" want="$2"
  shift 2
  rm -f "$root/report.md" "$root/plan.out"
  clean "$name" "$want" "$root/work" env REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" "$@" bash "$here/fork-follow.sh"
}
plan "plan: one that passes leaves nothing in TMPDIR" 0
check "plan: and it did plan (release)" has "^action=release$" "$root/plan.out"
plan "plan: run again, it leaves nothing either" 0
plan "plan: a refused one (upstream cannot be fetched) leaves nothing" refused UPSTREAM_URL="$root/gh/none.git"
check "plan: and it said why" has "Could not fetch the tags and default branch" "$root/report.md"
plan "plan: one ended by TERM at its first git call leaves nothing" 143 PATH="$root/killbin:$PATH"
# In a run nothing changes: the logs are in RUNNER_TEMP, which is the runner's and stays.
mkdir -p "$root/runner-temp"
echo keep > "$root/runner-temp/other-step.txt"
plan "plan: with RUNNER_TEMP it makes no folder of its own" 0 RUNNER_TEMP="$root/runner-temp"
check "plan: RUNNER_TEMP holds the logs and is left as it was otherwise" \
  eval '[[ -f "$root/runner-temp/fetch.log" && "$(cat "$root/runner-temp/other-step.txt")" == keep ]]'
plan "plan: one ended by TERM with RUNNER_TEMP set leaves nothing in TMPDIR" 143 RUNNER_TEMP="$root/runner-temp" PATH="$root/killbin:$PATH"
check "plan: and RUNNER_TEMP is still there" eval '[[ "$(cat "$root/runner-temp/other-step.txt")" == keep ]]'
# No folder can be made: the plan stops with a reason, like any other stop outside the planned refusals.
plan "plan: no log folder can be made -> it stops" refused TMPDIR="$root/no-such-folder"
check "plan: and the report names the step" \
  eval 'has "plan stopped at fork-follow.sh line" "$root/report.md" && has "mktemp" "$root/report.md"'

# --- the report: fork-follow-report.sh -----------------------------------------------------------
# Its first gh call (does the repository have Issues) comes after it wrote the body to a file.
mkdir -p "$root/reports"
echo "the plan stopped" > "$root/reports/plan.md"
clean "report: one ended by TERM while it asks GitHub leaves nothing" 143 "$root/work" \
  env GITHUB_STEP_SUMMARY="$root/summary.md" RUN_URL=run REPORTS="$root/reports" ORIGIN="$root/gh/me/t3code.git" \
  ACTION= PLAN_RESULT=failure bash "$here/fork-follow-report.sh"

# --- the release guards: fork-release.sh check-archives ------------------------------------------
# Layout of scripts/build-cli-archive.ts: <stem>/t3, which must name this repository.
stem=t3-0.1.10-fork.1-linux-x64
archive() { # archive NAME [REPO]: $root/assets-NAME/<stem>.tar.gz whose t3 names REPO; no t3 without REPO
  mkdir -p "$root/arch-$1/$stem/client" "$root/assets-$1"
  [[ -z "${2:-}" ]] || printf 'x\0const repo = "%s";\0' "$2" > "$root/arch-$1/$stem/t3"
  tar -czf "$root/assets-$1/$stem.tar.gz" -C "$root/arch-$1" "$stem"
}
archive good me/t3code
archive upstream up/t3code
archive noprogram
mkdir -p "$root/assets-broken" "$root/assets-none"
echo "not an archive" > "$root/assets-broken/$stem.tar.gz"
archives() { # archives NAME WANT ASSETS [ENV...]
  local name="$1" want="$2" assets="$3"
  shift 3
  clean "$name" "$want" "$root" env "$@" bash "$here/fork-release.sh" check-archives "$root/assets-$assets"
}
archives "check-archives: archives that pass leave nothing in TMPDIR" 0 good
archives "check-archives: a refused archive (names upstream) leaves nothing" refused upstream
check "check-archives: and it said why" has "does not name me/t3code" "$root/out"
archives "check-archives: a refused archive (no program) leaves nothing" refused noprogram
check "check-archives: and it said why" has "has no $stem/t3" "$root/out"
archives "check-archives: an archive that cannot be unpacked leaves nothing" refused broken
archives "check-archives: no archive at all leaves nothing" refused none
archives "check-archives: one ended by TERM while it unpacks leaves nothing" 143 good PATH="$root/killbin:$PATH"

# --- the interaction limit: fork-interaction-limit.sh --------------------------------------------
# Each subcommand keeps gh's answer or its own text in a file while it calls gh.
echo error > "$root/limit-result"
echo "the check could not read the limit" >> "$root/limit-result"
for args in "check" "renew" "report $root/limit-result"; do
  # shellcheck disable=SC2086 # the subcommand and its file are two words
  clean "interaction limit: ${args%% *} ended by TERM while it calls gh leaves nothing" 143 "$root" \
    bash "$here/fork-interaction-limit.sh" $args --repo me/t3code
done

echo
if ((failures)); then
  echo "$failures failed"
  exit 1
fi
echo "all passed"
