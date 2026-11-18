#!/usr/bin/env bash
# Every fork shell script removes the temporary files it makes, however it ends: passing, refused,
# stopped half-way, or ended by a signal bash can catch (TERM, INT, HUP; only KILL leaves them).
# One rule for all of them (each mktemp has the EXIT trap that removes what it made), then the
# scripts one by one: TMPDIR lists the same entries before and after a call, and while a call runs
# its file or folder carries the script's name. Local git repositories and a fake `gh`; no network.
#   bash .github/scripts/fork-temp-files.test.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# Fake gh: every call ends the process whose id is in $KILL_PID_FILE (TERM), the way a cancelled
# run ends a script half-way. Before that it lists TMPDIR in $SEEN_FILE: what the script holds
# there while it runs. fork_test_gh (fork-test-gh.sh) installs it first on PATH, with no token, a
# .invalid GH_HOST and FORK_TEST_MODE, so the scripts refuse if any other gh would answer.
. "$here/fork-test-gh.sh"
mkdir -p "$root/bin"
fork_test_gh "$root/bin" << 'GH'
LC_ALL=C ls -A "${TMPDIR:?}" > "${SEEN_FILE:?the gh stub needs SEEN_FILE}"
kill -TERM "$(cat "${KILL_PID_FILE:?the gh stub needs KILL_PID_FILE}")"
exit 1
GH

# Stand-ins for git and tar that list TMPDIR and end the calling script the same way.
mkdir -p "$root/killbin"
printf '%s\n' '#!/usr/bin/env bash' 'LC_ALL=C ls -A "${TMPDIR:?}" > "${SEEN_FILE:?}"' \
  'kill -TERM "$(cat "${KILL_PID_FILE:?}")"' 'exit 1' > "$root/killbin/git"
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
export KILL_PID_FILE="$root/pid" SEEN_FILE="$root/seen"
# The scripts' temporary files go to a folder of this test, so each one is seen. It starts with
# what a run ended by KILL leaves, and what another run of the same script holds right now: one
# entry under each script's own name. They are not this call's, so every call leaves them alone.
export TMPDIR="$root/tmp"
mkdir -p "$TMPDIR/fork-follow.earlier" "$TMPDIR/fork-release.earlier/unpacked" "$TMPDIR/fork-interaction-limit.earlier"
echo "another run's report" > "$TMPDIR/fork-follow-report.earlier"

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
tmp_listing() { LC_ALL=C ls -A "$TMPDIR"; }

# run DIR COMMAND...: COMMAND in DIR, as the process whose id $KILL_PID_FILE holds (`exec` keeps the
# id), so a stand-in above can end exactly that script. Output in $root/out; the braces take this
# shell's own "Terminated" line about an ended script.
run() {
  { (cd "$1" && shift && echo "$BASHPID" > "$KILL_PID_FILE" && exec "$@") > "$root/out" 2>&1; } 2> /dev/null
}
# cleanly WANT RUN-ARGS...: the call ends with status WANT (a number, or `refused` for 1 to 127: it
# stopped by itself) and TMPDIR lists exactly the entries it listed before, by name: one entry gone
# and another one new is not "nothing left". The listings stay in $root/before and $root/after;
# $root/seen is what a stand-in listed while the call ran (absent when none was called).
cleanly() {
  local want="$1" status=0
  shift
  rm -f "$SEEN_FILE"
  tmp_listing > "$root/before"
  run "$@" || status=$?
  tmp_listing > "$root/after"
  if [[ "$want" == refused ]] && ((status > 0 && status < 128)); then want="$status"; fi
  [[ "$status" == "$want" ]] && cmp -s "$root/before" "$root/after" && return 0
  {
    echo "exit $status (wanted $want); TMPDIR before (<) and after (>) the call:"
    diff "$root/before" "$root/after"
  } >> "$root/out"
  return 1
}
clean() { # clean NAME WANT RUN-ARGS...
  local name="$1"
  shift
  check "$name" cleanly "$@"
}
# held NAME GLOB: while the last call ran, TMPDIR had exactly one entry more than before it, named
# GLOB. A run ended by KILL leaves that entry, and its name is all that says whose it was.
held() {
  local new
  new="$(LC_ALL=C comm -13 "$root/before" "$SEEN_FILE" 2> /dev/null)"
  # shellcheck disable=SC2053 # $2 is a glob
  if [[ -f "$SEEN_FILE" && -n "$new" && "$new" != *$'\n'* && "$new" == $2 ]]; then
    pass "$1"
  else
    echo "new in TMPDIR while the call ran (wanted one entry, $2): ${new:-none}" >> "$root/out"
    fail "$1"
  fi
}

# --- the check itself ----------------------------------------------------------------------------
# A call that removes an entry that was there and leaves one of its own has the same count as
# before; the listing sees it. The stand-ins below are this test's own, not fork scripts.
check "the check: a call that leaves TMPDIR as it was passes" cleanly 0 "$root" true
check "the check: a call that leaves a new entry is seen" eval '! cleanly 0 "$root" touch "$TMPDIR/left"'
rm -f "$TMPDIR/left"
check "the check: a call that swaps an earlier entry for a new one is seen (same count)" \
  eval '! cleanly 0 "$root" mv "$TMPDIR/fork-follow-report.earlier" "$TMPDIR/swapped"'
mv "$TMPDIR/swapped" "$TMPDIR/fork-follow-report.earlier"
check "the check: a call that removes an earlier entry is seen" \
  eval '! cleanly 0 "$root" rmdir "$TMPDIR/fork-follow.earlier"'
mkdir -p "$TMPDIR/fork-follow.earlier"

# --- the rule: each mktemp has the EXIT trap that removes what it made ----------------------------
# A removal at the end of the script is skipped by every early exit; the trap is not. An EXIT trap
# somewhere in the file is not enough: it has to be the one for this call. So every call keeps its
# result in a variable (NAME="$(mktemp ...)"), and one of these holds:
#   next    the next command is `trap ... EXIT` and removes $NAME (only comments in between, so
#           nothing can end the script while the file has no trap)
#   named   an EXIT trap names a function of the file (`trap cleanup EXIT`) whose body removes $NAME
#   inside  the call makes its file inside a folder that another call of the file made and its
#           trap removes ("$tmp/XXXXXX")
# A call is the word at the start of a command or of a $( ) or ` `; in a text, quote the word or
# say it another way. The word is put together here, so that this file's own patterns and
# examples are not calls.
maker="mk""temp"
var_re='[A-Za-z_][A-Za-z0-9_]*'
call_re="(^|[[:space:];&|(\`])$maker([[:space:])]|\$)"
kept_re="($var_re)=\"?\\\$\\($maker([[:space:])]|\$)"
inside_re="\\\$\\($maker[^)]*[[:space:]]\"?\\\$\\{?($var_re)\\}?/"
exit_trap_re='^[[:space:]]*trap[[:space:]].*[[:space:]]EXIT([[:space:]]|$)'
named_trap_re="^[[:space:]]*trap[[:space:]]+($var_re)[[:space:]]+EXIT([[:space:]]|\$)"
removes() { # removes TEXT NAME: TEXT is an rm of $NAME or ${NAME...}
  local rm_re='(^|[^[:alnum:]_])rm[[:space:]]' name_re="\\\$\\{?$2([^[:alnum:]_]|\$)"
  [[ "$1" =~ $rm_re && "$1" =~ $name_re ]]
}
# unguarded FILE: "FILE:LINE: why" for every call in FILE that has none of the three.
unguarded() {
  local file="$1" i j k name indent def_re last covered
  local -a text=() calls=() names=() parents=() cleaners=()
  local -A guarded=()
  mapfile -t text < "$file"
  for i in "${!text[@]}"; do
    [[ "${text[i]}" =~ $named_trap_re ]] && cleaners+=("${BASH_REMATCH[1]}")
  done
  for i in "${!text[@]}"; do
    [[ ! "${text[i]}" =~ ^[[:space:]]*# && "${text[i]}" =~ $call_re ]] || continue
    name="" covered=""
    [[ "${text[i]}" =~ $kept_re ]] && name="${BASH_REMATCH[1]}"
    calls+=("$i") names+=("$name")
    [[ "${text[i]}" =~ $inside_re ]] && parents+=("${BASH_REMATCH[1]}") || parents+=("")
    [[ -n "$name" ]] || continue
    # next: over the rest of this command (lines that end in \), then over comments and empty lines
    j=$i
    while [[ "${text[j]-}" == *\\ ]]; do j=$((j + 1)); done
    j=$((j + 1))
    while [[ "${text[j]-x}" =~ ^[[:space:]]*(#|$) ]]; do j=$((j + 1)); done
    [[ "${text[j]:-}" =~ $exit_trap_re ]] && removes "${text[j]}" "$name" && covered=1
    # named: from the function's first line to its closing brace (the same indent; a one-line
    # function ends where it starts)
    for k in "${cleaners[@]}"; do
      def_re="^([[:space:]]*)(function[[:space:]]+)?$k[[:space:]]*\\(\\)"
      for j in "${!text[@]}"; do
        [[ "${text[j]}" =~ $def_re ]] || continue
        indent="${BASH_REMATCH[1]}"
        [[ "${text[j]}" =~ \}[[:space:]]*$ ]] && last=$j || last=""
        while [[ -n "${text[j]+x}" ]]; do
          removes "${text[j]}" "$name" && covered=1
          [[ "$j" != "$last" && "${text[j]}" != "$indent}" ]] || break
          j=$((j + 1))
        done
        break
      done
    done
    [[ -z "$covered" ]] || guarded[$name]=1
  done
  for k in "${!calls[@]}"; do
    i="${calls[k]}" name="${names[k]}"
    if [[ -z "$name" ]]; then
      echo "${file##*/}:$((i + 1)): the result is not kept in a variable, so no trap can remove it"
    elif [[ -z "${guarded[$name]:-}" && ( -z "${parents[k]}" || -z "${guarded[${parents[k]}]:-}" ) ]]; then
      echo "${file##*/}:$((i + 1)): no EXIT trap removes \$$name: neither the next command, nor a function an EXIT trap names"
    fi
  done
}

# The rule on examples first: what it must let through, and what it must name. `@` is the call.
mkdir -p "$root/rule"
example() { sed "s/@/$maker/g" > "$root/rule/$1.sh"; }
example guarded << 'SH'
one="$(@ "${TMPDIR:-/tmp}/one.XXXXXX")"
# only a comment in between

trap 'rm -f "$one"' EXIT
two="$(@ -d)" \
  || exit 1
# shellcheck disable=SC2064
trap "rm -rf -- $(printf '%q' "$two")" EXIT
inner="$(@ -d "$two/XXXXXX")"
echo "a text may say \"@\" in quotes"
SH
example named << 'SH'
cleanup() {
  [[ -z $dir ]] || rm -rf -- "${dir:?}"
}
trap cleanup EXIT INT
dir=$(@ -d)
SH
example unguarded << 'SH'
trap 'rm -rf "$early"' EXIT
early="$(@ -d)"
echo "the trap above comes before the call"
other="$(@)"
trap 'rm -f "$something_else"' EXIT
late="$(@)"
cd "$late" || exit 1
trap 'rm -rf "$late"' EXIT
@ -d > "$root/name"
cat "$(@)"
forgotten() { rm -rf "$unnamed"; }
unnamed="$(@ -d)"
inner="$(@ -d "$unnamed/XXXXXX")"
kept="$(@)"
trap 'echo "$kept"' EXIT
SH
unguarded "$root/rule/guarded.sh" > "$root/out"
unguarded "$root/rule/named.sh" >> "$root/out"
check "rule: a trap that is the next command, a function an EXIT trap names, a file inside a guarded folder pass" \
  eval '[[ ! -s "$root/out" ]]'
unguarded "$root/rule/unguarded.sh" > "$root/out"
check "rule: a trap before the call, for another variable, after another command, not removing; no variable: all named" \
  eval '[[ "$(cut -d: -f2 "$root/out" | paste -sd, -)" == 2,4,6,9,10,12,13,14 ]]'

# Then on every fork shell script, the tests and this file with them.
makers=()
: > "$root/out"
for file in "$here"/*.sh; do
  grep -qE "^[^#]*$call_re" "$file" || continue
  makers+=("${file##*/}")
  unguarded "$file" >> "$root/out"
done
echo "scripts that call it: ${makers[*]}" >> "$root/out"
check "rule: every call in a fork shell script has the EXIT trap that removes what it made" \
  eval '[[ " ${makers[*]} " == *" fork-follow.sh "* && " ${makers[*]} " == *" fork-release.sh "* && "$(wc -l < "$root/out")" == 1 ]]'

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
held "plan: and while it ran, its folder was fork-follow.XXXXXX" 'fork-follow.??????'
# In a run nothing changes: the logs are in RUNNER_TEMP, which is the runner's and stays.
mkdir -p "$root/runner-temp"
echo keep > "$root/runner-temp/other-step.txt"
plan "plan: with RUNNER_TEMP it makes no folder of its own" 0 RUNNER_TEMP="$root/runner-temp"
check "plan: RUNNER_TEMP holds the logs and is left as it was otherwise" \
  eval '[[ -f "$root/runner-temp/fetch.log" && "$(cat "$root/runner-temp/other-step.txt")" == keep ]]'
plan "plan: one ended by TERM with RUNNER_TEMP set leaves nothing in TMPDIR" 143 RUNNER_TEMP="$root/runner-temp" PATH="$root/killbin:$PATH"
check "plan: and RUNNER_TEMP is still there" eval '[[ "$(cat "$root/runner-temp/other-step.txt")" == keep ]]'
check "plan: and while it ran, TMPDIR held nothing of it" cmp -s "$root/before" "$SEEN_FILE"
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
held "report: and while it ran, its file was fork-follow-report.XXXXXX" 'fork-follow-report.??????'
# No file can be made: it stops there, before it asks GitHub (the stub would end it with TERM) or
# writes a summary without a body, and the run shows why.
clean "report: no file can be made -> it stops, and leaves nothing" 1 "$root/work" \
  env GITHUB_STEP_SUMMARY="$root/summary-none.md" RUN_URL=run REPORTS="$root/reports" ORIGIN="$root/gh/me/t3code.git" \
  ACTION= PLAN_RESULT=failure TMPDIR="$root/no-such-folder" bash "$here/fork-follow-report.sh"
check "report: and it said why, in the run and nowhere else" \
  eval 'has "^::error::Could not make a temporary file in $root/no-such-folder" "$root/out" && [[ ! -e "$root/summary-none.md" ]]'

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
held "check-archives: and while it ran, its folder was fork-release.XXXXXX" 'fork-release.??????'
# No folder can be made: a refusal like the others, with its ::error:: line. First the folder of the
# check itself, then (a stand-in that fails only for a name inside a folder made in TMPDIR) the
# one of an archive.
archives "check-archives: no folder can be made -> refused" refused good TMPDIR="$root/no-such-folder"
check "check-archives: and it said why" \
  has "^::error::Could not make a temporary folder in $root/no-such-folder" "$root/out"
mkdir -p "$root/failbin"
printf '%s\n' '#!/usr/bin/env bash' '[[ "${*: -1}" != "$TMPDIR"/*/* ]] || { echo "stand-in: no space left" >&2; exit 1; }' \
  "PATH=\"\${PATH#*:}\" exec $maker \"\$@\"" > "$root/failbin/$maker"
chmod +x "$root/failbin/$maker"
archives "check-archives: no folder for an archive can be made -> refused, and its own folder is gone" refused good \
  PATH="$root/failbin:$PATH"
check "check-archives: and it said why" has "^::error::Could not make a folder in .* to unpack $stem.tar.gz" "$root/out"

# --- the interaction limit: fork-interaction-limit.sh --------------------------------------------
# Each subcommand keeps gh's answer or its own text in a file while it calls gh.
echo error > "$root/limit-result"
echo "the check could not read the limit" >> "$root/limit-result"
for sub in check renew report; do
  args=("$sub")
  [[ "$sub" != report ]] || args+=("$root/limit-result") # an array: the path may hold a space
  clean "interaction limit: $sub ended by TERM while it calls gh leaves nothing" 143 "$root" \
    bash "$here/fork-interaction-limit.sh" "${args[@]}" --repo me/t3code
  held "interaction limit: and while $sub ran, its folder was fork-interaction-limit.XXXXXX" 'fork-interaction-limit.??????'
done

echo
if ((failures)); then
  echo "$failures failed"
  exit 1
fi
echo "all passed"
