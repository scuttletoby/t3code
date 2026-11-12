#!/usr/bin/env bash
# Tests for fork-secret-scan.sh against local git repositories; no network.
#   bash .github/scripts/fork-secret-scan.test.sh
# GITLEAKS_BIN=path runs the real gitleaks; without it a stand-in greps the scanned commits' diffs
# for an AWS access key id. FORK_SECRET_SCAN_SH picks another script.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# No script tested here calls gh. The stub (fork-test-gh.sh: no token, a .invalid GH_HOST) refuses
# every call, so a scan that starts calling gh fails here instead of reaching GitHub.
. "$here/fork-test-gh.sh"
mkdir -p "$root/bin"
fork_test_gh "$root/bin" <<'GH'
echo "fake gh: fork-secret-scan.test.sh expects no gh call (gh $*)" >&2
exit 97
GH

script="$(realpath "${FORK_SECRET_SCAN_SH:-$here/fork-secret-scan.sh}")"

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$root/gitconfig"
git config --global user.name test
git config --global user.email test@example.invalid
git config --global init.defaultBranch main
git config --global advice.detachedHead false
unset GITHUB_STEP_SUMMARY GITHUB_OUTPUT

if [[ -n "${GITLEAKS_BIN:-}" ]]; then
  gitleaks="$(realpath "$GITLEAKS_BIN")"
else
  gitleaks="$root/fake-gitleaks"
  export TMPDIR="$root"
  cat > "$gitleaks" <<'FAKE'
#!/usr/bin/env bash
# Stand-in: `gitleaks git ... --log-opts=OPTS .` fails when the diffs of `git log -p -U0 OPTS` add a
# key id, and logs how many commits it scanned. Like gitleaks 8.30.1, a git error (a file content
# fetch that fails) stops it with "0 commits scanned" and exit 0.
set -uo pipefail
opts=""
for arg in "$@"; do [[ "$arg" == --log-opts=* ]] && opts="${arg#--log-opts=}"; done
# Into a file first: `grep -q` stops reading early, and pipefail would read git's SIGPIPE as no match.
# shellcheck disable=SC2086
if ! git log -p -U0 $opts > "$TMPDIR/fake-gitleaks.log" 2> "$TMPDIR/fake-gitleaks.err"; then
  echo "ERR [git] $(head -n 1 "$TMPDIR/fake-gitleaks.err")"
  echo "INF 0 commits scanned."
  echo "no leaks found"
  exit 0
fi
# Like gitleaks 8.30.1: any stderr line from git is logged as an error, and the scan goes on.
if [[ -s "$TMPDIR/fake-gitleaks.err" ]]; then
  echo "ERR [git] $(head -n 1 "$TMPDIR/fake-gitleaks.err")"
  echo 'ERR error="stderr is not empty"'
fi
# A commit counts when a file it does not delete has a hunk, as in gitleaks.
scanned="$(awk '/^commit / { c = $2 } /^diff --git / { d = 0 } /^deleted file mode / { d = 1 }
  /^@@ / && !d && !(c in s) { s[c] = 1; n++ } END { print n + 0 }' "$TMPDIR/fake-gitleaks.log")"
echo "INF $scanned commits scanned."
if grep -qE '^\+.*(AKIA)[A-Z2-7]{16}' "$TMPDIR/fake-gitleaks.log"; then echo "leaks found: 1"; exit 1; fi
echo "no leaks found"
FAKE
  chmod +x "$gitleaks"
fi

# A made-up AWS access key id, built at run time so this file holds no key.
key="AK""IA""QX7WMB3KZLRT5NHP"
date=1700000000
commit() { # commit FILE CONTENT MESSAGE (each commit one minute newer)
  date=$((date + 60))
  printf '%s\n' "$2" > "$1"
  git add -A
  GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "$3"
}
remove() {
  date=$((date + 60))
  git rm -q "$1"
  GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "$2"
}
merge() {
  date=$((date + 60))
  GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git merge -q --no-ff -m "$2" "$1"
}

failures=0
check() { # check NAME EXPECTED_STATUS [GREP_PATTERN] -- runs the scan in $clone with the env given
  local name="$1" expected="$2" pattern="${3:-}" out status
  set +e
  out="$(cd "$clone" && GITLEAKS="$gitleaks" GITHUB_STEP_SUMMARY="$root/summary" "$script" 2>&1)"
  status=$?
  set -e
  if [[ "$status" -ne "$expected" ]]; then
    echo "FAIL $name: exit $status, expected $expected"
    printf '%s\n' "$out" | sed 's/^/    /'
    failures=$((failures + 1))
  elif [[ -n "$pattern" ]] && ! grep -qE "$pattern" <<< "$out"; then
    echo "FAIL $name: output lacks /$pattern/"
    printf '%s\n' "$out" | sed 's/^/    /'
    failures=$((failures + 1))
  else
    echo "ok   $name"
  fi
  rm -f "$root/summary"
}

# origin: main with an old key on it (already public, not this PR's business), then two PR
# branches that each merge three newer main commits.
git init -q --bare "$root/origin.git"
git -C "$root/origin.git" config uploadpack.allowFilter true
git -C "$root/origin.git" config uploadpack.allowAnySHA1InWant true
git init -q "$root/work"
cd "$root/work"
git remote add origin "$root/origin.git"
commit app.txt "app v1" "main 1"
commit old.txt "old=$key" "main 2: an old key"
remove old.txt "main 3: drop the old key"
fork_point=$(git rev-parse HEAD)

git checkout -q -b leaky
commit config.txt "aws_access_key_id=$key" "PR A: add a key"
remove config.txt "PR B: delete it again"
git checkout -q -b clean "$fork_point"
commit notes.txt "notes" "PR A': no key"

git checkout -q main
for n in 4 5 6; do commit "app$n.txt" "app v$n" "main $n"; done
base=$(git rev-parse HEAD)

for branch in leaky clean; do
  git checkout -q "$branch"
  merge main "Merge main into $branch"
  commit "$branch-tail.txt" "tail" "PR C: after the merge"
done
# A key that only the merge commit adds (on top of an automatic merge of main).
git checkout -q -b evil "$fork_point"
commit notes2.txt "notes" "PR A'': no key"
git merge -q --no-ff --no-commit main >/dev/null 2>&1
printf '%s\n' "token=$key" > merged.txt
git add merged.txt
date=$((date + 60))
GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "Merge main into evil"
git push -q origin main leaky clean evil
leaky=$(git rev-parse leaky)
clean=$(git rev-parse clean)
evil=$(git rev-parse evil)

# The workflow's checkout: blob-less, only the PR head's history (no main ref).
clone="$root/clone"
git clone -q --filter=blob:none --no-checkout --single-branch --branch leaky "file://$root/origin.git" "$clone"

export EVENT_NAME=pull_request BASE_SHA="$base" BEFORE="" AFTER=""
# The count GitHub reports for the PR, for scripts that use it.
COMMITS=$(git rev-list --count "$base..$leaky")
export COMMITS
HEAD_SHA="$leaky" check "pull request: a key added then deleted before a merge of main is found" 1
HEAD_SHA="$clean" check "pull request: main's own old key is not the PR's" 0
HEAD_SHA="$leaky" check "pull request: the range is base..head" 1 "\\.\\.$leaky"
HEAD_SHA="$evil" check "pull request: a key added only in a merge commit is found" 1

export EVENT_NAME=push BASE_SHA="" HEAD_SHA=""
BEFORE="$fork_point" AFTER="$leaky" check "push: the pushed commits are scanned" 1 "pushed after"
BEFORE="$(git rev-parse leaky~1)" AFTER="$leaky" check "push: only commits after the old tip" 0
BEFORE="$clean" AFTER="$leaky" check "force push: commits the old tip had not" 1 "force push"
BEFORE="$fork_point" AFTER="$evil" check "push: a key added only in a merge commit is found" 1
BEFORE="0000000000000000000000000000000000000000" AFTER="$leaky" \
  check "push of a new branch: only its newest commit, with a warning" 0 "::warning.*new branch"
BEFORE="1111111111111111111111111111111111111111" AFTER="$leaky" \
  check "push: an old tip that cannot be fetched warns" 0 "::warning.*cannot be fetched"

EVENT_NAME=workflow_dispatch check "an unknown event fails" 2

export EVENT_NAME=pull_request BEFORE="" AFTER=""
BASE_SHA="1111111111111111111111111111111111111111" HEAD_SHA="$leaky" \
  check "pull request: a base commit that cannot be fetched fails" 2 "cannot fetch commit 1111"
BASE_SHA="$base" HEAD_SHA="2222222222222222222222222222222222222222" \
  check "pull request: a head commit that cannot be fetched fails" 2 "cannot fetch commit 2222"

# gitleaks stopping early (a git error part-way) without failing: it scans only the newest commit.
real_gitleaks="$gitleaks"
gitleaks="$root/early-stop-gitleaks"
cat > "$gitleaks" <<STOP
#!/usr/bin/env bash
args=()
for arg in "\$@"; do
  if [[ "\$arg" == --log-opts=* ]]; then arg="--log-opts=--max-count=1 \${arg#--log-opts=}"; fi
  args+=("\$arg")
done
"$real_gitleaks" "\${args[@]}" || true
exit 0
STOP
chmod +x "$gitleaks"
BASE_SHA="$base" HEAD_SHA="$leaky" \
  check "pull request: gitleaks scanning fewer commits than the range has fails" 2 "scanned 1 commit"
gitleaks="$real_gitleaks"

# The file contents cannot be fetched part-way: a fresh blob-less clone (no contents yet) whose
# origin is gone afterwards. gitleaks would log "0 commits scanned" and exit 0.
clone="$root/clone-offline"
git clone -q --filter=blob:none --no-checkout --single-branch --branch leaky "file://$root/origin.git" "$clone"
mv "$root/origin.git" "$root/origin-gone.git"
BASE_SHA="$base" HEAD_SHA="$leaky" \
  check "pull request: contents that cannot be fetched after the clone fail" 2 "cannot read the commits"
mv "$root/origin-gone.git" "$root/origin.git"

# git writes a stray line to stderr while gitleaks reads the log (in gitleaks 8.30.1 that is
# `ERR error="stderr is not empty"`, possibly with diffs dropped, and still exit 0): the scan fails.
mkdir -p "$root/straygit"
cat > "$root/straygit/git" <<STRAY
#!/usr/bin/env bash
"$(command -v git)" "\$@"
status=\$?
[[ " \$* " != *" log "* ]] || echo "warning: a stray line on stderr" >&2
exit \$status
STRAY
chmod +x "$root/straygit/git"
clone="$root/clone"
PATH="$root/straygit:$PATH" BASE_SHA="$base" HEAD_SHA="$clean" \
  check "pull request: gitleaks logging a git error (stderr not empty) and passing fails" 2 "gitleaks logged an error|gitleaks scanned"
BASE_SHA="$base" HEAD_SHA="$clean" check "pull request: without the stray line the same scan passes" 0
# The case the count misses (gitleaks 8.30.1): one commit adds 3,000 files and a key in the last
# one; the stray line stops gitleaks before that file, with the commit already counted. The real
# gitleaks then exits 0, so the scan must fail on its ERR lines (2); the stand-in reads every file
# and finds the key (1).
cd "$root/work"
git checkout -q -b wide "$base"
mkdir -p wide && for n in $(seq -w 1 3000); do echo "line $n" > "wide/f$n.txt"; done
printf '%s\n' "aws_access_key_id=$key" > wide/zzz.txt
date=$((date + 60))
git add -A && GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "PR W: 3,000 files, a key last"
git push -q origin wide
wide=$(git rev-parse wide)
git -C "$root/clone" fetch -q origin wide
cd "$root"
wide_status=1; [[ -z "${GITLEAKS_BIN:-}" ]] || wide_status=2
PATH="$root/straygit:$PATH" BASE_SHA="$base" HEAD_SHA="$wide" \
  check "pull request: a stray git stderr line that hides a key in a wide commit fails" "$wide_status"

if [[ "$failures" -ne 0 ]]; then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
