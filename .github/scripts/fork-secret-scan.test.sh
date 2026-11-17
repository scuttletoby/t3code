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
unset GITHUB_STEP_SUMMARY GITHUB_OUTPUT UPSTREAM UPSTREAM_URL RUNNER_TEMP GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML OLD_HEADS PUSH_REF
export CONFIG_BRANCH=main

if [[ -n "${GITLEAKS_BIN:-}" ]]; then
  gitleaks="$(realpath "$GITLEAKS_BIN")"
else
  gitleaks="$root/fake-gitleaks"
  export TMPDIR="$root"
  cat > "$gitleaks" <<'FAKE'
#!/usr/bin/env bash
# Stand-in: `gitleaks git ... --log-opts=OPTS SOURCE` fails when the diffs of `git log -p -U0 OPTS`
# add a key id, and logs how many commits it scanned. Like gitleaks 8.30.1, a git error (a file
# content fetch that fails) stops it with "0 commits scanned" and exit 0. Settings, found as gitleaks
# finds them: a config (--config, GITLEAKS_CONFIG, GITLEAKS_CONFIG_TOML, SOURCE/.gitleaks.toml) with
# a line starting [allowlist] allows every key, and one with `[extend]` and a `path =` line fails to load (FTL,
# exit 1) unless the file is there; a .gitleaksignore (--gitleaks-ignore-path, default ".", and
# SOURCE) line `<commit>:...` ignores the keys of that commit, a line `file:aws-access-token:line`
# the key on that line of that file in any commit. `gitleaks stdin`: the same settings, a key id
# on a line of the input fails, with each line in the --report-path JSON as "StartLine".
set -uo pipefail
opts="" config="" ignore_path="." source="." mode=git report=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --log-opts=*) opts="${1#--log-opts=}" ;;
    --config) config="$2"; shift ;;
    --gitleaks-ignore-path) ignore_path="$2"; shift ;;
    --report-path) report="$2"; shift ;;
    --report-format) shift ;;
    -*) ;;
    git | stdin) mode="$1" ;;
    *) source="$1" ;;
  esac
  shift
done
settings=""
if [[ -n "$config" ]]; then settings="$(cat "$config")"
elif [[ -n "${GITLEAKS_CONFIG:-}" ]]; then settings="$(cat "$GITLEAKS_CONFIG")"
elif [[ -n "${GITLEAKS_CONFIG_TOML:-}" ]]; then settings="$GITLEAKS_CONFIG_TOML"
elif [[ -f "$source/.gitleaks.toml" ]]; then settings="$(cat "$source/.gitleaks.toml")"
fi
extend_path="$(sed -nE "s/^[[:space:]]*path[[:space:]]*=[[:space:]]*[\"']([^\"']*)[\"'].*/\1/p" <<< "$settings" | sed -n 1p)"
if [[ "$settings" == *"[extend]"* && -n "$extend_path" && ! -f "$extend_path" ]]; then
  echo "FTL Failed to load config error=\"failed to load extended config, err: open $extend_path: no such file or directory\""
  exit 1
fi
ignored=" "
for file in "$ignore_path" "$ignore_path/.gitleaksignore" "$source/.gitleaksignore"; do
  if [[ -f "$file" ]]; then ignored+="$(grep -vE '^[[:space:]]*(#|$)' "$file" | tr '\n' ' ')"; fi
done
if [[ "$mode" == stdin ]]; then
  lines=""
  if ! grep -qE '^[[:space:]]*\[allowlist\]' <<< "$settings"; then
    lines="$(grep -nE 'AKIA[A-Z2-7]{16}' | cut -d: -f1)"
  else
    cat > /dev/null
  fi
  if [[ -n "$report" ]]; then
    { echo "["; sed 's/.*/ {\n  "StartLine": &,\n  "RuleID": "aws-access-token"\n },/' <<< "$lines"; echo "]"; } > "$report"
  fi
  if [[ -n "$lines" ]]; then echo "leaks found: $(wc -l <<< "$lines")"; exit 1; fi
  echo "no leaks found"
  exit 0
fi
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
if ! grep -qE '^[[:space:]]*\[allowlist\]' <<< "$settings"; then
  # Each added line with a key: "commit file:aws-access-token:line", minus the ignored ones.
  while read -r sha fingerprint; do
    if [[ "$ignored" != *" $sha:$fingerprint "* && "$ignored" != *" $fingerprint "* ]]; then
      echo "leaks found: 1"; exit 1
    fi
  done < <(LC_ALL=C awk '
    /^commit / { c = $2; next }
    /^\+\+\+ / { f = substr($0, 7); next }
    /^@@ / { split($3, a, ","); l = substr(a[1], 2) + 0; next }
    /^\+/ { if ($0 ~ /AKIA[A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7][A-Z2-7]/) print c, f ":aws-access-token:" l; l++ }' \
    "$TMPDIR/fake-gitleaks.log")
fi
echo "no leaks found"
FAKE
  chmod +x "$gitleaks"
fi

# A made-up AWS access key id, built at run time so this file holds no key.
key="AK""IA""QX7WMB3KZLRT5NHP"
key2="AK""IA""ZR4PLM7VNQWT2XJD"
date=1700000000
commit() { # commit FILE CONTENT MESSAGE (each commit one minute newer)
  date=$((date + 60))
  printf '%s\n' "$2" > "$1"
  git add -A
  GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "$3"
}
commit_bytes() { # commit_bytes FILE MESSAGE < CONTENT
  date=$((date + 60))
  mkdir -p "$(dirname "$1")"
  cat > "$1"
  git add -A
  GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "$2"
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
check() { # check NAME EXPECTED_STATUS [GREP_PATTERN [ABSENT_PATTERN]] -- runs the scan in $clone with the env given
  local name="$1" expected="$2" pattern="${3:-}" absent="${4:-}" out status
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
  elif [[ -n "$absent" ]] && grep -qE "$absent" <<< "$out"; then
    echo "FAIL $name: output has /$absent/"
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
HEAD_SHA="$leaky" check "pull request: the range is <default branch tip>..head" 1 "$base\\.\\.$leaky"
HEAD_SHA="$evil" check "pull request: a key added only in a merge commit is found" 1

# Two merges of a moving branch, each with 60 conflicts resolved by hand. --remerge-diff writes each
# automatic merge (conflict markers) to a temporary object folder; git's diff prefetch checks those
# objects with a stale cache of that folder and, in a blob-less clone, asks the remote for them
# ("not our ref"; with GIT_NO_LAZY_FETCH "lazy fetching disabled", which gitleaks logs as ERR).
# With 60 objects per merge some share a first byte across the two merges, which is what it takes.
# KEY_IN set: the second merge's resolution of c/f30.txt adds a key.
conflict_branch() { # conflict_branch NAME [KEY]
  local name="$1" key_in="${2:-}" n round
  git checkout -q -b "$name-side" "$fork_point"
  git checkout -q -b "$name" "$fork_point"
  mkdir -p c
  for n in $(seq 1 60); do printf 'start %s\n' "$n" > "c/f$n.txt"; done
  date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "$name: 60 files"
  git branch -f "$name-side" HEAD
  for round in 1 2; do
    git checkout -q "$name-side"
    for n in $(seq 1 60); do printf 'side %s %s\n' "$round" "$n" > "c/f$n.txt"; done
    date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "side $round"
    git checkout -q "$name"
    for n in $(seq 1 60); do printf 'pr %s %s\n' "$round" "$n" > "c/f$n.txt"; done
    date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "PR $round"
    git merge -q --no-ff --no-commit "$name-side" > /dev/null 2>&1 || true
    for n in $(seq 1 60); do printf 'resolved %s %s\n' "$round" "$n" > "c/f$n.txt"; done
    if [[ "$round" == 2 && -n "$key_in" ]]; then printf 'aws_access_key_id=%s\n' "$key_in" >> c/f30.txt; fi
    date=$((date + 60)); git add -A
    GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "Merge $name-side into $name (conflicts resolved)"
  done
  commit "$name-tail.txt" "tail" "$name: after the merges"
  git push -q origin "$name"
}
cd "$root/work"
conflict_branch conflicts
conflict_branch conflicts-key "$key"
conflicts=$(git rev-parse conflicts)
conflicts_key=$(git rev-parse conflicts-key)
git checkout -q main
# A fresh blob-less clone: nothing of these branches fetched yet, as in the workflow's checkout.
clone="$root/clone-merges"
git clone -q --filter=blob:none --no-checkout --single-branch --branch leaky "file://$root/origin.git" "$clone"
HEAD_SHA="$conflicts" check "pull request: two merges with conflicts resolved by hand pass" 0 "gitleaks scanned 8 commit"
HEAD_SHA="$conflicts" check "pull request: the same scan again (contents fetched) passes" 0
HEAD_SHA="$conflicts_key" check "pull request: a key in a conflict resolution is found" 1
clone="$root/clone"

export EVENT_NAME=push BASE_SHA="" HEAD_SHA=""
BEFORE="$fork_point" AFTER="$leaky" check "push: the pushed commits are scanned" 1 "pushed after"
BEFORE="$(git rev-parse leaky~1)" AFTER="$leaky" check "push: only commits after the old tip" 0
BEFORE="$clean" AFTER="$leaky" check "force push: commits the old tip had not" 1 "force push"
BEFORE="$fork_point" AFTER="$evil" check "push: a key added only in a merge commit is found" 1
BEFORE="0000000000000000000000000000000000000000" AFTER="$leaky" \
  check "push of a new branch: only its newest commit, with a warning" 0 "::warning.*new branch"
BEFORE="1111111111111111111111111111111111111111" AFTER="$leaky" \
  check "push: an old tip that cannot be fetched warns" 0 "::warning.*cannot be fetched"
# A push to any branch but the default one, or a tag (see the script's header): every commit its tip
# has that the default branch has not, whatever the old tip.
zeros="0000000000000000000000000000000000000000"
PUSH_REF=refs/heads/leaky BEFORE="$zeros" AFTER="$leaky" \
  check "push of a new branch: every commit not on the default branch, a key deleted again included" 1 \
  "refs/heads/leaky has that main has not"
PUSH_REF=refs/heads/leaky BEFORE="$(git rev-parse leaky~1)" AFTER="$leaky" \
  check "push to another branch: the whole branch, not only the commits after the old tip" 1
PUSH_REF=refs/heads/clean BEFORE="$zeros" AFTER="$clean" check "push of a new branch without a key passes" 0
PUSH_REF=refs/heads/fresh BEFORE="$zeros" AFTER="$base" \
  check "push of a new branch at the default branch's tip: nothing to scan, passes" 0 "the 0 commit"
PUSH_REF=refs/tags/v9 BEFORE="$zeros" AFTER="$leaky" check "push of a tag: every commit not on the default branch" 1
PUSH_REF=refs/heads/main BEFORE="$(git rev-parse leaky~1)" AFTER="$leaky" \
  check "push to the default branch: only the commits after the old tip, as before" 0 "pushed after"
PUSH_REF=refs/heads/leaky CONFIG_BRANCH="" BEFORE="$zeros" AFTER="$leaky" \
  check "push to another branch without CONFIG_BRANCH fails" 2 "cannot fetch the default branch"

# A force push replaced a pull request's head (see the script's header). The check the ruleset
# requires runs on the newest head only, so it also reads what the earlier heads had: BEFORE (the
# synchronize event's old head) and OLD_HEADS (every tip of the PR's branch the workflow listed).
export EVENT_NAME=pull_request AFTER=""
BEFORE="$leaky" HEAD_SHA="$clean" \
  check "pull request: a key only the head a force push replaced (BEFORE) had is found" 1 "1 earlier head"
BEFORE="" OLD_HEADS="$leaky $clean" HEAD_SHA="$clean" \
  check "pull request: a key only an earlier tip of the PR's branch (OLD_HEADS) had is found" 1 "1 earlier head"
BEFORE="$leaky" OLD_HEADS="$evil $leaky" HEAD_SHA="$clean" \
  check "pull request: each earlier head counts once" 1 "2 earlier head"
BEFORE="$(git rev-parse clean~1)" OLD_HEADS="$base $fork_point $clean" HEAD_SHA="$clean" \
  check "pull request: earlier heads the head or the default branch holds add nothing" 0 "" "earlier head"
BEFORE="3333333333333333333333333333333333333333" OLD_HEADS="" HEAD_SHA="$clean" \
  check "pull request: an earlier head that cannot be fetched warns" 0 "::warning.*cannot be fetched \\(333333333333\\)"
BEFORE="" OLD_HEADS="--output=x" HEAD_SHA="$clean" check "pull request: an OLD_HEADS word that is not a commit id fails" 2 "not a commit id"
BEFORE="" OLD_HEADS="" HEAD_SHA="$clean" check "pull request: the newest head alone passes (control)" 0
export EVENT_NAME=push HEAD_SHA=""

EVENT_NAME=issues check "an unknown event fails" 2
# #239: a shallow checkout lacks the history the range needs, so git log would read less than the
# range and the scan would pass on what it never saw. Refused for every event, before any fetch.
shallow="$root/shallow-clone"
git clone -q --depth 1 --filter=blob:none --no-checkout --branch leaky "file://$root/origin.git" "$shallow"
clone="$shallow" HEAD_SHA="$leaky" EVENT_NAME=pull_request BEFORE="" \
  check "a shallow checkout fails (pull request)" 2 "shallow"
clone="$shallow" EVENT_NAME=push BEFORE="$fork_point" AFTER="$leaky" check "a shallow checkout fails (push)" 2 "shallow"
clone="$shallow" EVENT_NAME=schedule check "a shallow checkout fails (sweep)" 2 "shallow"

export EVENT_NAME=pull_request BEFORE="" AFTER=""
# The range does not depend on the PR's base (see the script's header).
BASE_SHA="1111111111111111111111111111111111111111" HEAD_SHA="$leaky" \
  check "pull request: the PR's base is not read (one that cannot be fetched changes nothing)" 1 "not on main"
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
# origin then holds every branch and commit but no file contents (a blob-less copy whose own source
# is gone). gitleaks would log "0 commits scanned" and exit 0.
clone="$root/clone-offline"
git clone -q --filter=blob:none --no-checkout --single-branch --branch leaky "file://$root/origin.git" "$clone"
git clone -q --bare --filter=blob:none "file://$root/origin.git" "$root/origin-commits.git"
git -C "$root/origin-commits.git" config remote.origin.url "$root/no-such-origin.git"
git -C "$root/origin-commits.git" config uploadpack.allowFilter true
git -C "$root/origin-commits.git" config uploadpack.allowAnySHA1InWant true
mv "$root/origin.git" "$root/origin-full.git"
mv "$root/origin-commits.git" "$root/origin.git"
BASE_SHA="$base" HEAD_SHA="$leaky" \
  check "pull request: contents that cannot be fetched after the clone fail" 2 "cannot read the commits"
mv "$root/origin.git" "$root/origin-commits.git"
mv "$root/origin-full.git" "$root/origin.git"

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

# A fork-follow rebase: the series (one fork commit) moves from upstream v1.0.0 onto a newer
# upstream tip whose own commits hold a key (a test fixture). The force push's BEFORE..AFTER holds
# those upstream commits; the scan leaves them out when upstream can be fetched, and only then.
git init -q --bare "$root/up.git"
git -C "$root/up.git" config uploadpack.allowFilter true
git init -q "$root/up"
cd "$root/up"
commit up.txt "up v1" "up 1"
git tag v1.0.0
commit fixture.txt "deviceKey=$key" "up 2: a test fixture with a key"
commit up.txt "up v3" "up 3"
git tag v1.1.0-nightly.1
# A stable tag on a release branch the default branch does not have, with a key of its own.
git checkout -q -b release/1.0 v1.0.0
commit hotfix.txt "token=$key" "up release 1.0.1: a fixture with a key"
git tag v1.0.1
git checkout -q main
git push -q "$root/up.git" main release/1.0 --tags

git init -q --bare "$root/fork.git"
git -C "$root/fork.git" config uploadpack.allowFilter true
git -C "$root/fork.git" config uploadpack.allowAnySHA1InWant true
cd "$root/up"
git checkout -q -b series v1.0.0
commit fork.txt "fork v1" "fork 1: the series"
old_series=$(git rev-parse HEAD)
git push -q "$root/fork.git" "HEAD:refs/heads/old-series"
git checkout -q -b rebased v1.1.0-nightly.1
commit fork.txt "fork v1" "fork 1: the series, rebased"
rebased=$(git rev-parse HEAD)
commit leak.txt "aws_access_key_id=$key" "fork 2: a key in the fork's own commit"
rebased_leaky=$(git rev-parse HEAD)
git checkout -q -b stable v1.0.1
commit fork.txt "fork v1" "fork 1: the series, on the stable tag"
stable=$(git rev-parse HEAD)
git push -q -f "$root/fork.git" "$rebased:refs/heads/main" "$rebased_leaky:refs/heads/leaky" "$stable:refs/heads/stable"
cd "$root"
clone="$root/clone-follow"
git clone -q --filter=blob:none --no-checkout "file://$root/fork.git" "$clone"

export EVENT_NAME=push BASE_SHA="" HEAD_SHA="" UPSTREAM=up/t3code UPSTREAM_URL="file://$root/up.git"
BEFORE="$old_series" AFTER="$rebased" \
  check "force push onto newer upstream: upstream's own commits with a key are left out" 0 "upstream up/t3code already has"
BEFORE="$old_series" AFTER="$rebased_leaky" \
  check "force push onto newer upstream: a key in a fork commit of the same push is found" 1
BEFORE="$old_series" AFTER="$stable" \
  check "force push onto an upstream tag off its default branch: the tag's commits are left out" 0
BEFORE="0000000000000000000000000000000000000000" AFTER="$(git -C "$root/up" rev-parse v1.1.0-nightly.1~1)" \
  check "push of a new branch whose tip is upstream's commit with a key: left out" 0 "::warning.*new branch"
BEFORE="$rebased" AFTER="$rebased_leaky" check "push after the rebase: the fork's new commit is scanned" 1 "pushed after"
# Fail closed: no upstream, nothing left out, even in a clone where an earlier run fetched upstream.
UPSTREAM_URL="file://$root/no-such-upstream.git" BEFORE="$old_series" AFTER="$rebased" \
  check "force push, upstream cannot be fetched: the full range, with a warning" 1 "::warning.*could not be fetched"
UPSTREAM="" BEFORE="$old_series" AFTER="$rebased" \
  check "force push, upstream unknown: the full range, with a warning" 1 "::warning.*not known"
BEFORE="$old_series" AFTER="$rebased" \
  check "force push: upstream reachable again, the scan passes again" 0
# A push to another branch leaves upstream's commits out too: the branch on upstream's stable tag
# holds that tag's commit with a key, which the default branch has not.
PUSH_REF=refs/heads/stable BEFORE="0000000000000000000000000000000000000000" AFTER="$stable" \
  check "push to another branch on an upstream tag: the tag's commits are left out" 0 "upstream up/t3code already has"
UPSTREAM="" PUSH_REF=refs/heads/stable BEFORE="0000000000000000000000000000000000000000" AFTER="$stable" \
  check "push to another branch, upstream unknown: the full range, with a warning" 1 "::warning.*not known"
PUSH_REF=refs/heads/leaky BEFORE="$rebased" AFTER="$rebased_leaky" \
  check "push to another branch: a key in the fork's own commit is found" 1
# PR #214 (c3): a pull request's earlier head from before a fork-follow rebase, on an upstream tag
# off upstream's default branch, holds that tag's commit with a key. It is upstream's: left out as
# the push scans leave it out, so an honest rebase clears the PR. Without upstream, the full range.
cd "$root/up"
git checkout -q -b pr-new "$rebased"
commit pr.txt "pr" "PR 1, rebased"
pr_new=$(git rev-parse HEAD)
git push -q "$root/fork.git" "$pr_new:refs/heads/pr"
cd "$root"
EVENT_NAME=pull_request BEFORE="" AFTER="" OLD_HEADS="$stable" HEAD_SHA="$pr_new" \
  check "pull request: an earlier head on an upstream tag (before a rebase): upstream's commits are left out" 0 \
  "upstream up/t3code already has"
EVENT_NAME=pull_request BEFORE="" AFTER="" OLD_HEADS="$rebased_leaky" HEAD_SHA="$pr_new" \
  check "pull request: an earlier head's own key is still found with upstream left out" 1
UPSTREAM="" EVENT_NAME=pull_request BEFORE="" AFTER="" OLD_HEADS="$stable" HEAD_SHA="$pr_new" \
  check "pull request: an earlier head on an upstream tag, upstream unknown: the full range, with a warning" 1 \
  "::warning.*not known"
# The daily sweep (every branch and tag) leaves upstream's commits out too, and only then.
git -C "$root/fork.git" update-ref -d refs/heads/leaky
EVENT_NAME=schedule check "daily sweep: upstream's commits on the fork's branches are left out" 0 \
  "every ref of origin but refs/pull.*upstream up/t3code already has"
UPSTREAM="" EVENT_NAME=schedule check "daily sweep, upstream unknown: the full range, with a warning" 1 "::warning.*not known"
unset UPSTREAM UPSTREAM_URL

# New origins for the cases below: a bare origin that serves blob-less clones, and a work repo.
new_origin() { # new_origin NAME [OBJECT_FORMAT]
  git init -q --bare --object-format="${2:-sha1}" "$root/$1.git"
  git -C "$root/$1.git" config uploadpack.allowFilter true
  git -C "$root/$1.git" config uploadpack.allowAnySHA1InWant true
  git init -q --object-format="${2:-sha1}" "$root/$1"
  cd "$root/$1"
  git remote add origin "$root/$1.git"
}
# A clone with a work tree (as the workflow's checkout has): its files are the PR head's.
checkout() { # checkout ORIGIN BRANCH
  clone="$root/$1-$2-checkout"
  rm -rf "${clone:?}"
  git clone -q --filter=blob:none --single-branch --branch "$2" "file://$root/$1.git" "$clone"
}
allow_all="paths = ['''.*''']"

# PR #214 (s3): an earlier head origin still serves but whose fetch fails for another reason (a
# network or server error) is tried again, and fails the job when every try fails; only origin
# saying it no longer has the commit ("not our ref") is a warning (above).
new_origin flaky
commit app.txt "app" "main 1"
git checkout -q -b pr
commit leak.txt "aws_access_key_id=$key" "old head: a key"
flaky_old=$(git rev-parse HEAD)
git push -q origin main pr
git reset -q --hard main
commit c.txt "clean" "new head"
flaky_new=$(git rev-parse HEAD)
git push -q -f origin pr
# origin's upload-pack, failing as a 502 would on the calls listed in flaky-fail: the scan's first
# call fetches the default branch; the later ones are the earlier head's.
cat > "$root/flaky-upload-pack" <<FLAKY
#!/usr/bin/env bash
n=\$(( \$(cat "$root/flaky-count" 2>/dev/null || echo 0) + 1 )); echo \$n > "$root/flaky-count"
if grep -qx "\$n" "$root/flaky-fail"; then echo "fatal: unable to access origin: The requested URL returned error: 502" >&2; exit 128; fi
exec git upload-pack "\$@"
FLAKY
chmod +x "$root/flaky-upload-pack"
flaky() { # flaky NAME EXPECTED PATTERN FAILING_CALLS...
  local name="$1" expected="$2" pattern="$3"
  shift 3
  checkout flaky pr
  git -C "$clone" config remote.origin.uploadpack "$root/flaky-upload-pack"
  printf '%s\n' "$@" > "$root/flaky-fail"
  rm -f "$root/flaky-count"
  FETCH_RETRY_DELAY=0 EVENT_NAME=pull_request BEFORE="" AFTER="" OLD_HEADS="$flaky_old" HEAD_SHA="$flaky_new" \
    check "$name" "$expected" "$pattern"
}
flaky "pull request: an earlier head fetched at once: its key is found (control)" 1 ""
flaky "pull request: an earlier head whose first fetches fail (502): tried again, its key is found" 1 "" 2 3
flaky "pull request: an earlier head whose fetch fails every time (502): fails, asks for a re-run" 2 \
  "cannot fetch the earlier head.*Re-run the job" $(seq 2 40)

# PR #214 (s4): commit and tag messages are scanned too, with the same settings.
new_origin msgs
commit app.txt "app" "main 1"
msgs_base=$(git rev-parse HEAD)
git checkout -q -b pr
commit x.txt "x" "debug: aws_access_key_id=$key"
msgs_pr=$(git rev-parse HEAD)
git checkout -q -b pr-clean "$msgs_base"
commit y.txt "y" "a plain message"
msgs_clean=$(git rev-parse HEAD)
# PR #214 (r1s-1): the commit object as stored, so its author and committer lines too.
git checkout -q -b pr-author "$msgs_base"
GIT_AUTHOR_NAME="aws_access_key_id=$key" commit a.txt "a" "a plain message"
msgs_author=$(git rev-parse HEAD)
git checkout -q -b pr-committer "$msgs_base"
GIT_COMMITTER_EMAIL="$key@example.invalid" commit c.txt "c" "a plain message"
msgs_committer=$(git rev-parse HEAD)
git checkout -q main
GIT_COMMITTER_DATE="@$date" git tag -a v9 -m "release notes: token $key" "$msgs_base"
GIT_COMMITTER_DATE="@$date" git tag -a v8 -m "release notes" "$msgs_base"
git push -q origin main pr pr-clean pr-author pr-committer v9 v8
checkout msgs pr
EVENT_NAME=pull_request BEFORE="" AFTER="" HEAD_SHA="$msgs_pr" \
  check "pull request: a key in a commit message is found" 1 "message, author or committer of commit $msgs_pr"
EVENT_NAME=pull_request BEFORE="" AFTER="" HEAD_SHA="$msgs_clean" \
  check "pull request: plain commit messages pass (control)" 0 "Scanning the commit objects.*1 in all"
EVENT_NAME=pull_request BEFORE="" AFTER="" HEAD_SHA="$msgs_author" \
  check "pull request: a key in a commit's author name is found" 1 "author or committer of commit $msgs_author"
EVENT_NAME=pull_request BEFORE="" AFTER="" HEAD_SHA="$msgs_committer" \
  check "pull request: a key in a commit's committer email is found" 1 "author or committer of commit $msgs_committer"
EVENT_NAME=push PUSH_REF=refs/tags/v9 BEFORE="0000000000000000000000000000000000000000" AFTER="$msgs_base" \
  check "push of an annotated tag: a key in its message is found" 1 "author or committer of tag object"
EVENT_NAME=push PUSH_REF=refs/tags/v8 BEFORE="0000000000000000000000000000000000000000" AFTER="$msgs_base" \
  check "push of an annotated tag: a plain message passes (control)" 0 "1 tag object\\(s\\)"
EVENT_NAME=push PUSH_REF=refs/tags/v8 BEFORE="0000000000000000000000000000000000000000" AFTER="$msgs_clean" \
  check "push of a tag that moved since: its message is not read, with a warning" 0 "::warning.*cannot be fetched as pushed"

# The daily sweep (schedule, or by hand): every branch and tag of origin, for what no push run
# scanned ([skip ci], more than three tags at once, pushes by the workflow token, older branches).
new_origin sweep
commit app.txt "app" "main 1"
git checkout -q -b side
commit k.txt "aws_access_key_id=$key" "side: a key"
git checkout -q main
git push -q origin main side
clone="$root/sweep-clone"
git clone -q --filter=blob:none --no-checkout "file://$root/sweep.git" "$clone"
EVENT_NAME=schedule check "daily sweep: a key on a branch no push run scanned is found" 1 "every ref of origin but refs/pull"
git push -q origin :side
EVENT_NAME=schedule check "daily sweep: the branch gone, the same clone passes (control)" 0 "every ref of origin but refs/pull/\\* \\(1 distinct"
# PR #214 (r1s-1): refs outside branches and tags (notes, anything) are public too: any clone can
# fetch them, and no push run scans them. Pull request heads are left out (see the script header).
git push -q origin side:refs/notes/commits
EVENT_NAME=schedule check "daily sweep: a key held only by refs/notes/commits is found" 1
git push -q origin :refs/notes/commits side:refs/keep/x
EVENT_NAME=schedule check "daily sweep: a key held only by refs/keep/x is found" 1
git push -q origin :refs/keep/x side:refs/pull/1/head
EVENT_NAME=schedule check "daily sweep: a pull request head is left out, as documented" 0 "\\(1 distinct"
git push -q origin :refs/pull/1/head
GIT_COMMITTER_DATE="@$date" git tag -a v2 -m "notes: $key" main
git push -q origin v2
EVENT_NAME=workflow_dispatch check "manual sweep: a key in a tag message is found" 1 "author or committer of tag object"
git push -q origin :refs/tags/v2
git tag tree-tag "main^{tree}"
git push -q origin tree-tag
EVENT_NAME=schedule check "daily sweep: a tag that names no commit warns" 0 "::warning.*name no commit \\(tags/tree-tag\\)"
git push -q origin :refs/tags/tree-tag
# #239: a [skip ci] push to a branch that is then deleted (or force-pushed away) before the sweep
# runs: no ref holds it any more, but GitHub still serves its commits by id. The workflow passes
# every tip GitHub's activity record lists (OLD_HEADS, as for a pull request); the sweep reads them,
# in a fresh clone too (fetched by id).
git checkout -q -b vanished main
commit v.txt "aws_access_key_id=$key" "vanished: a key [skip ci]"
vanished=$(git rev-parse HEAD)
git checkout -q main
git push -q origin vanished
git push -q origin :vanished
clone="$root/sweep-clone-2"
git clone -q --filter=blob:none --no-checkout "file://$root/sweep.git" "$clone"
OLD_HEADS="" EVENT_NAME=schedule check "daily sweep: a deleted branch's tip, not listed: not read (control)" 0
OLD_HEADS="$zeros $vanished $(git rev-parse main)" EVENT_NAME=schedule \
  check "daily sweep: a key only a deleted branch's tip (activity record) held is found" 1 "1 tip\\(s\\) no ref holds"
OLD_HEADS="3333333333333333333333333333333333333333" EVENT_NAME=schedule \
  check "daily sweep: a listed tip origin no longer serves warns" 0 "::warning.*no longer serves.*333333333333"
OLD_HEADS="3333333333333333333333333333333333333333 $vanished" EVENT_NAME=schedule \
  check "daily sweep: one listed tip gone fails the batch fetch; the others are still fetched one by one" 1 \
  "1 tip\\(s\\) no ref holds"
OLD_HEADS="--all" EVENT_NAME=schedule check "daily sweep: an OLD_HEADS word that is not a commit id fails" 2 "not a commit id"
rm -rf "${root:?}/sweep.git"
EVENT_NAME=schedule check "daily sweep: origin cannot be read: fails" 2
cd "$root"

export EVENT_NAME=pull_request BEFORE="" AFTER=""

# gitleaks settings come from the default branch, never from the PR (see the script's header).
new_origin settings
commit app.txt "app" "main 1"
settings_base=$(git rev-parse HEAD)
git checkout -q -b own-toml
printf '%s\n' '[extend]' 'useDefault = true' '[allowlist]' "$allow_all" > .gitleaks.toml
commit k.txt "aws_access_key_id=$key" "PR: a key and a .gitleaks.toml that allows every path"
git checkout -q -b own-ignore "$settings_base"
commit k.txt "aws_access_key_id=$key" "PR: a key"
own_key=$(git rev-parse HEAD)
commit .gitleaksignore "$own_key:k.txt:aws-access-token:1" "PR: ignore its own key"
git checkout -q -b fixture "$settings_base"
mkdir -p fixtures
commit fixtures/k.txt "aws_access_key_id=$key" "PR: a key in a fixture the default branch allows"
git checkout -q main
git push -q origin main own-toml own-ignore fixture
checkout settings own-toml
BASE_SHA="$settings_base" HEAD_SHA="$(git rev-parse own-toml)" \
  check "pull request: a .gitleaks.toml the PR adds does not hide its key" 1 "settings .* from the default branch main"
checkout settings own-ignore
BASE_SHA="$settings_base" HEAD_SHA="$(git rev-parse own-ignore)" \
  check "pull request: a .gitleaksignore the PR adds does not hide its key, with a notice" 1 "::notice.*changes \.gitleaksignore"
# Settings environment variables do not reach gitleaks either.
printf '%s\n' '[extend]' 'useDefault = true' '[allowlist]' "$allow_all" > "$root/env.toml"
GITLEAKS_CONFIG="$root/env.toml" BASE_SHA="$settings_base" HEAD_SHA="$(git rev-parse own-ignore)" \
  check "pull request: GITLEAKS_CONFIG does not reach gitleaks" 1
# A push to another branch reads the default branch's settings, never what its own earlier push
# set: the first push allows every path, the second adds a key.
git checkout -q -b own-toml-2 "$settings_base"
printf '%s\n' '[extend]' 'useDefault = true' '[allowlist]' "$allow_all" > .gitleaks.toml
date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "branch: allow every path"
own_toml_1=$(git rev-parse HEAD)
commit k2.txt "aws_access_key_id=$key" "branch: a key, after its own settings"
git checkout -q main
git push -q origin own-toml-2
checkout settings own-toml-2
EVENT_NAME=push PUSH_REF=refs/heads/own-toml-2 BEFORE="$own_toml_1" AFTER="$(git rev-parse own-toml-2)" \
  check "push to another branch: settings its own earlier push added do not hide its key" 1 "::notice.*reads them from the default branch main.*once it is on the default branch"
EVENT_NAME=push PUSH_REF=refs/heads/main BEFORE="$own_toml_1" AFTER="$(git rev-parse own-toml-2)" \
  check "push to the default branch reads the tip before the push (control)" 0 "settings .* from the tip before the push"
# The reviewed path: the ignore line lands on the default branch in a change of its own; the same
# PR's scan, run again, then passes.
git checkout -q main
commit .gitleaksignore "$own_key:k.txt:aws-access-token:1" "main: ignore the PR's fixture key (reviewed)"
printf '%s\n' '[extend]' 'useDefault = true' '[allowlist]' "paths = ['''fixtures/.*''']" > .gitleaks.toml
date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "main: allow fixtures/"
git push -q origin main
BASE_SHA="$settings_base" HEAD_SHA="$own_key" \
  check "pull request: an ignore line on the default branch applies on a re-run" 0
checkout settings fixture
BASE_SHA="$settings_base" HEAD_SHA="$(git rev-parse fixture)" \
  check "pull request: the default branch's .gitleaks.toml applies" 0
CONFIG_BRANCH="" BASE_SHA="$settings_base" HEAD_SHA="$own_key" \
  check "pull request: no CONFIG_BRANCH fails" 2 "cannot fetch the default branch"
CONFIG_BRANCH="no-such-branch" BASE_SHA="$settings_base" HEAD_SHA="$own_key" \
  check "pull request: a CONFIG_BRANCH that cannot be fetched fails" 2 "cannot fetch the default branch"
# A push reads the settings of the tip before the push: a push that adds an ignore line for its own
# key still fails.
clone="$root/settings-own-ignore-checkout"
EVENT_NAME=push BEFORE="$settings_base" AFTER="$(git rev-parse own-ignore)" CONFIG_BRANCH="" \
  check "push: a .gitleaksignore the push adds does not hide its key" 1 "settings .* from the tip before the push"

# Attributes: a .gitattributes in the PR marks the key file -diff (the checkout's work tree holds it).
new_origin attrs
commit app.txt "app" "main 1"
attrs_base=$(git rev-parse HEAD)
git checkout -q -b diffoff
commit .gitattributes "*.cfg -diff" "PR: *.cfg -diff"
commit k.cfg "aws_access_key_id=$key" "PR: a key in a -diff file"
git checkout -q -b count "$attrs_base"
commit .gitattributes "*.cfg -diff" "PR: *.cfg -diff"
commit k.cfg "no key" "PR: a -diff file"
git push -q origin main diffoff count
checkout attrs diffoff
BASE_SHA="$attrs_base" HEAD_SHA="$(git rev-parse diffoff)" \
  check "pull request: a key in a file the PR's .gitattributes marks -diff is found" 1
# The count reads no attributes either: gitleaks that silently skips the newest commit (the -diff
# file's) scans 1 of 2 commits, which fails.
real_gitleaks="$gitleaks"
gitleaks="$root/skip-newest-gitleaks"
cat > "$gitleaks" <<SKIP
#!/usr/bin/env bash
args=()
for arg in "\$@"; do
  if [[ "\$arg" == --log-opts=* ]]; then arg="--log-opts=--skip=1 \${arg#--log-opts=}"; fi
  args+=("\$arg")
done
"$real_gitleaks" "\${args[@]}"
SKIP
chmod +x "$gitleaks"
checkout attrs count
BASE_SHA="$attrs_base" HEAD_SHA="$(git rev-parse count)" \
  check "pull request: the count is not lowered by the PR's .gitattributes" 2 "scanned 1 commit"
gitleaks="$real_gitleaks"

# An octopus merge (3 parents) shows no diff even with --remerge-diff: the scan fails, with or
# without a key in it.
new_origin octopus
commit app.txt "app" "main 1"
octopus_base=$(git rev-parse HEAD)
git checkout -q -b o1; commit o1.txt "one" "o1"
git checkout -q -b o2 "$octopus_base"; commit o2.txt "two" "o2"
for name in octo octo-key; do
  git checkout -q -b "$name" "$octopus_base"; commit "$name.txt" "pr" "$name"
  git merge -q --no-ff --no-commit o1 o2 > /dev/null 2>&1
  if [[ "$name" == octo-key ]]; then printf 'aws_access_key_id=%s\n' "$key" > octo.txt; git add octo.txt; fi
  date=$((date + 60)); GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "Octopus merge into $name"
done
git push -q origin main o1 o2 octo octo-key
clone="$root/octopus-clone"
git clone -q --filter=blob:none --no-checkout --single-branch --branch main "file://$root/octopus.git" "$clone"
BASE_SHA="$octopus_base" HEAD_SHA="$(git rev-parse octo-key)" \
  check "pull request: a key only an octopus merge adds fails the scan" 2 "octopus merge"
BASE_SHA="$octopus_base" HEAD_SHA="$(git rev-parse octo)" \
  check "pull request: an octopus merge without a key fails the scan" 2 "octopus merge"
EVENT_NAME=push BEFORE="0000000000000000000000000000000000000000" AFTER="$(git rev-parse octo)" \
  check "push of a new branch whose tip is an octopus merge fails" 2 "octopus merge"
BASE_SHA="$octopus_base" HEAD_SHA="$octopus_base" check "pull request: an empty range passes" 0 "gitleaks scanned 0 commit"

# Criss-cross merges: two merge bases, and three (merge-ort builds virtual bases from temporary
# objects), 60 conflicts resolved by hand in each merge, a fresh blob-less clone.
new_origin cross
cross_files() { local n; for n in $(seq 1 60); do printf '%s %s\n' "$1" "$n" > "f$n.txt"; done; }
cross_commit() { date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "$1"; }
cross_merge() { # cross_merge COMMIT CONTENT [KEY]
  git merge -q --no-ff --no-commit "$1" > /dev/null 2>&1 || true
  cross_files "$2"
  if [[ -n "${3:-}" ]]; then printf 'aws_access_key_id=%s\n' "$3" >> f7.txt; fi
  cross_commit "merge $1"
}
cross_files start; cross_commit base; cross_base=$(git rev-parse HEAD)
git checkout -q -b A; cross_files A1; cross_commit A1
git checkout -q -b B "$cross_base"; cross_files B1; cross_commit B1
git checkout -q A; cross_merge B rA
git checkout -q B; cross_merge A~1 rB
for branch in A B; do git checkout -q "$branch"; cross_files "${branch}2"; cross_commit "${branch}2"; done
git checkout -q -b cross2 A; cross_merge B final
git checkout -q -b cross2-key A; cross_merge B final "$key"
# Three bases: X merges B and C, Y merges B's and X's first commits, then X merges Y.
git checkout -q -b C "$cross_base"; cross_files C1; cross_commit C1
git checkout -q -b X "$cross_base"; cross_files X1; cross_commit X1
cross_merge B rXB; cross_merge C rXC
git checkout -q -b Y C; cross_merge "$(git rev-parse B~2)" rYB; cross_merge "$(git rev-parse X~2)" rYX
git checkout -q -b cross3 X; cross_merge Y final
git checkout -q -b cross3-key X; cross_merge Y final "$key"
cross2_bases=$(git merge-base --all cross2^1 cross2^2 | wc -l)
cross3_bases=$(git merge-base --all cross3^1 cross3^2 | wc -l)
git push -q origin main A B C X Y cross2 cross2-key cross3 cross3-key
cross_clone() {
  clone="$root/cross-clone-$1"
  git clone -q --filter=blob:none --no-checkout --single-branch --branch main "file://$root/cross.git" "$clone"
}
cross_clone 2
BASE_SHA="$cross_base" HEAD_SHA="$(git rev-parse cross2)" \
  check "pull request: a criss-cross merge ($cross2_bases merge bases) with conflicts passes" 0
BASE_SHA="$cross_base" HEAD_SHA="$(git rev-parse cross2)" check "pull request: the same criss-cross scan again passes" 0
cross_clone 2-key
BASE_SHA="$cross_base" HEAD_SHA="$(git rev-parse cross2-key)" \
  check "pull request: a key in a criss-cross merge's resolution is found" 1
cross_clone 3
BASE_SHA="$cross_base" HEAD_SHA="$(git rev-parse cross3)" \
  check "pull request: a merge with $cross3_bases merge bases and conflicts passes" 0
cross_clone 3-key
BASE_SHA="$cross_base" HEAD_SHA="$(git rev-parse cross3-key)" \
  check "pull request: a key in a $cross3_bases-base merge's resolution is found" 1

# A SHA-256 repository: the view gitleaks reads has the checkout's object format.
new_origin sha256 sha256
commit app.txt "app" "main 1"
sha256_base=$(git rev-parse HEAD)
git checkout -q -b clean; commit notes.txt "notes" "PR: no key"
git checkout -q -b leaky; commit k.txt "aws_access_key_id=$key" "PR: a key"
git push -q origin main clean leaky
clone="$root/sha256-clone"
git clone -q --filter=blob:none --no-checkout --single-branch --branch main "file://$root/sha256.git" "$clone"
BASE_SHA="$sha256_base" HEAD_SHA="$(git rev-parse clean)" check "SHA-256 repository: a clean pull request passes" 0
BASE_SHA="$sha256_base" HEAD_SHA="$(git rev-parse leaky)" check "SHA-256 repository: a key is found" 1

# A pull request against a side branch that already holds a key (pushed there without a scan:
# pushes are scanned only on the default branch): the key is not on main, so it is the PR's. The
# range does not use the PR's base, so retargeting the PR changes nothing either.
new_origin side
commit app.txt "app" "main 1"
git checkout -q -b side
commit k.txt "aws_access_key_id=$key" "side branch: a key, never on main"
side=$(git rev-parse HEAD)
git checkout -q -b on-side
commit notes.txt "notes" "PR against the side branch: no key of its own"
git push -q origin main side on-side
checkout side on-side
BASE_SHA="$side" HEAD_SHA="$(git rev-parse on-side)" \
  check "pull request against a side branch that holds a key: the key is found" 1 "not on main"

# The repository's .gitleaks.toml on the default branch (gitleaks' default rules, its path skip list
# removed; see its header). FORK_SECRET_SCAN_TOML picks another file ("" for none).
repo_toml="${FORK_SECRET_SCAN_TOML-$here/../../.gitleaks.toml}"
new_origin repo
if [[ -n "$repo_toml" && -f "$repo_toml" ]]; then cp "$repo_toml" .gitleaks.toml; fi
commit app.txt "app" "main 1, with the repository's .gitleaks.toml"
repo_base=$(git rev-parse HEAD)
repo_branches=()
repo_pr() { # repo_pr BRANCH FILE CONTENT
  git checkout -q -b "$1" "$repo_base"
  mkdir -p "$(dirname "$2")"
  commit "$2" "$3" "PR: $2"
  repo_branches+=("$1")
}
# Paths gitleaks' built-in list skips: the scan reads them.
repo_pr key-toml .gitleaks.toml "aws_access_key_id=$key"
repo_pr key-toml-example docs/gitleaks.toml.example "aws_access_key_id=$key"
repo_pr key-lock pnpm-lock.yaml "aws_access_key_id=$key"
repo_pr key-svg icons/logo.svg "<svg><!-- aws_access_key_id=$key --></svg>"
repo_pr key-node-modules node_modules/x/config.js "const aws_access_key_id = '$key'"
repo_pr key-png img/x.png "aws_access_key_id=$key"
# A package version after a name with "key" in it, as pnpm writes them: no finding. (The name is
# put together at run time, so this file's own line is not a finding of generic-api-key.)
repo_pr lock-version pnpm-lock.yaml "$(printf "importers:\n  '@example/desktop-pass%s': 0.0.4-canary.v20260819050620\n" keys)"
# A token after a version-shaped start (1.0.0-<token>) is still a finding (the entry allows only a
# version's shape: lowercase words and numbers). Built at run time, as the key is.
repo_pr lock-token pnpm-lock.yaml "$(printf '  npm_auth_%s: 1.0.0-%s\n' token "Zx8Kq2Lm9Vb4""Nc7Rt5Wy3Hd6Jf1Gs0Pa")"
# The file itself (deleted and added again, so the PR's diff holds all of it) passes its own rules.
git checkout -q -b own-file "$repo_base"
if [[ -f .gitleaks.toml ]]; then
  git rm -q .gitleaks.toml; date=$((date + 60)); GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "PR: drop .gitleaks.toml"
  git checkout -q "$repo_base" -- .gitleaks.toml; date=$((date + 60)); GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "PR: add it again"
fi
repo_branches+=(own-file)
git checkout -q main
git push -q origin main "${repo_branches[@]}"
clone="$root/repo-clone"
git clone -q --filter=blob:none --no-checkout --single-branch --branch main "file://$root/repo.git" "$clone"
for branch in key-toml key-toml-example key-lock key-svg key-node-modules key-png; do
  BASE_SHA="$repo_base" HEAD_SHA="$(git rev-parse "$branch")" \
    check "repository settings: a key in $(git diff --name-only "$repo_base" "$branch") is found" 1
done
BASE_SHA="$repo_base" HEAD_SHA="$(git rev-parse lock-version)" \
  check "repository settings: a pnpm-lock.yaml package version is not a finding" 0 "" "No \\.gitleaks\\.toml"
# generic-api-key is the real gitleaks' rule; the stand-in knows only AWS key ids.
if [[ -n "${GITLEAKS_BIN:-}" ]]; then
  BASE_SHA="$repo_base" HEAD_SHA="$(git rev-parse lock-token)" \
    check "repository settings: a token after 1.0.0- in pnpm-lock.yaml is found" 1
fi
if [[ -n "$repo_toml" && -f "$repo_toml" ]]; then
  BASE_SHA="$repo_base" HEAD_SHA="$(git rev-parse own-file)" \
    check "repository settings: .gitleaks.toml passes its own rules" 0
else
  echo "FAIL repository settings: no .gitleaks.toml (${repo_toml:-none given})"
  failures=$((failures + 1))
fi
# Its shape (any gitleaks): no [extend] (useDefault brings back the default path skip list,
# disabledRules drops rules) and no [allowlist] table (a second global allowlist); every
# [[allowlists]] entry after the first names rules of the file and the values it allows, and one
# with paths has condition = "AND", so it never skips a whole file. Each value regex of such an
# entry is an exact value: ^, one fixed string with no regex syntax (escapes \<punctuation> and
# \xHH only), $; anything else is a problem, whatever it would match (see literal_value for why).
# Against the gitleaks binary (the real one only; its default config is built in): the rules, from
# the first [[rules]] to the end, are its default rules byte for byte, every one of them (the same
# rule ids in the same order, so no unbroken slice of them passes); the first entry is its global
# allowlist without the path skip list; and the binary judges that entry (with its stop words) on
# generated token shapes, each of which its rules alone find: one it hides is a problem. Each rule
# is tried on broken copies too.
cat > "$root/toml-check.py" <<'CHECK'
import collections, hashlib, json, math, os, re, subprocess, sys, tempfile, tomllib
path, binary_path = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
raw = open(path, "rb").read()
try:
    cfg = tomllib.loads(raw.decode())
except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
    print(f"cannot parse: {e}")
    sys.exit(1)
problems = []
TOP = {"title", "minVersion", "allowlists", "rules"}
problems += [f"top-level {k!r} is not allowed (only {', '.join(sorted(TOP))})" for k in sorted(set(cfg) - TOP)]
entries = cfg.get("allowlists") or []
if not entries:
    problems.append("no [[allowlists]] entries (the first is gitleaks' global value allowlist)")
NARROW = {"description", "targetRules", "condition", "paths", "regexes", "regexTarget"}


# Token shapes entry 1 may not allow, made at run time from a hash (this file holds none of them):
# hex (32, 40, 64), letters and digits (16 to 64), base64, URL-safe, a token after a version
# (1.0.0-<token>), each after 8 secret-like names in 4 ways of writing them; and AWS, GitHub, Slack
# and Bedrock keys. Each line must be a finding of gitleaks' rules alone (the judge checks that).
STOP = {w.lower() for part in [*entries[:1], *(a for r in cfg.get("rules") or [] for a in r.get("allowlists") or [])]
        for w in part.get("stopwords") or []}


def rand(seed, n, alphabet):
    out, block = [], 0
    while len(out) < n:
        for b in hashlib.sha256(f"{seed}/{block}".encode()).digest():
            if b < 256 - 256 % len(alphabet) and len(out) < n:
                out.append(alphabet[b % len(alphabet)])
        block += 1
    return "".join(out)


def tok(seed, n, alphabet):
    # Like a real key: starts with a letter or digit, has both, and is random enough for gitleaks'
    # entropy limits (3.5 for generic-api-key), and holds none of the rules' stop words (java, ...);
    # else the next one.
    for tries in range(1000):
        v = rand(f"{seed}/{tries}", n, alphabet)
        entropy = -sum(c / n * math.log2(c / n) for c in collections.Counter(v).values())
        if (v[0].isalnum() and re.search("[0-9]", v) and re.search("[A-Za-z]", v)
                and entropy >= min(3.7, math.log2(n) - 0.4) and not any(w in v.lower() for w in STOP)):
            return v
    raise SystemExit(f"no token for {seed}")


HEX, UP = "0123456789abcdef", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
B62 = "0123456789" + UP + UP.lower()
SHAPES = {"hex32": (32, HEX), "hex40": (40, HEX), "hex64": (64, HEX), "HEX40": (40, HEX.upper()),
          "alnum16": (16, B62), "alnum20": (20, B62), "alnum24": (24, B62), "alnum32": (32, B62),
          "alnum40": (40, B62), "alnum64": (64, B62), "base64-40": (40, B62 + "+/"),
          "base64-pad": (43, B62 + "+/"), "url32": (32, B62 + "-_"), "version": (24, B62)}
NAMES = ["api_key", "client_secret", "auth_token", "password", "aws_secret_access_key", "access_token",
         "secret", "apiKey"]
FORMATS = ['{n} = "{v}"', "{n}: {v}", "{N}={v}", '"{n}": "{v}",']
fixture = []  # (line, value, what it is)
for i, (shape, (n, alphabet)) in enumerate(SHAPES.items()):
    for j, name in enumerate(NAMES):
        v = tok(f"{shape}/{name}", n, alphabet)
        v = {"base64-pad": v + "=", "version": "1.0.0-" + v}.get(shape, v)
        fixture.append((FORMATS[(i + j) % len(FORMATS)].format(n=name, N=name.upper(), v=v), v, f"{name}, {shape}"))
for line, v in [("aws_access_key_id = {v}", "AK" + "IA" + tok("aws", 16, UP + "234567")),
                ("AWS_SESSION_KEY_ID={v}", "AS" + "IA" + tok("asia", 16, UP + "234567")),
                ("GITHUB_TOKEN={v}", "gh" + "p_" + tok("ghp", 36, B62)),
                ("token: {v}", "github" + "_pat_" + tok("pat", 82, B62 + "_")),
                ("SLACK_BOT_TOKEN={v}", "xo" + "xb-" + rand("s1", 10, HEX[:10]) + "-" + rand("s2", 13, HEX[:10])
                 + "-" + tok("s3", 24, B62)),
                ("bedrock = {v}", "bedrock-api-key-" + __import__("base64").b64encode(b"bedrock.amazonaws.com").decode()
                 + tok("bedrock", 40, B62 + "+/"))]:
    fixture.append((line.format(v=v), v, line.split()[0].split("=")[0] + ", " + v[:4] + "..."))

# Later entries allow exact values only (PR #214, round 2). Each earlier rule that judged a value
# regex from its text (bounded repeats, letter-and-digit classes, alternations of fixed text) was
# got round by a form it misread: (?m) anchors that match inside a multi-line key, POSIX classes
# Python's re reads differently from gitleaks' Go RE2, nested repeats, an alternation of single
# characters. This rule reads no regex syntax at all, so there is nothing to misread: ^, then only
# characters that are not syntax in either engine (printable ASCII) or an escape that stands for
# one character in both (a backslash before punctuation, or \xHH), then $. Such a regex matches
# exactly one string, the one the reviewer reads in it. Nothing is guessed about which keys it
# could match, so no sample of key shapes is needed for these entries.
SYNTAX = set("\\.+*?()|[]{}^$")
PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")  # what both engines accept after a backslash
HEXDIGITS = set("0123456789abcdefABCDEF")


def literal_value(r):
    """(the one string r matches, None) when r is an exact value (see above), else (None, why)."""
    if not (isinstance(r, str) and r.startswith("^") and r.endswith("$") and len(r) >= 3):
        return None, "is not ^value$"
    out, i, end = [], 1, len(r) - 1
    while i < end:
        c = r[i]
        if c == "\\":
            n = r[i + 1] if i + 1 < end else ""
            if n and n in PUNCT:
                out.append(n)
                i += 2
            elif n == "x" and i + 4 <= end and set(r[i + 2:i + 4]) <= HEXDIGITS:
                out.append(chr(int(r[i + 2:i + 4], 16)))
                i += 4
            elif not n:
                return None, "ends in a backslash, so its $ is a literal $ and the value is not anchored"
            else:
                return None, f"has the escape \\{n}, which is not one fixed character (only \\<punctuation> or \\xHH)"
        elif c in SYNTAX:
            return None, f"has the regex syntax {c!r} (escape it with a backslash to mean the character)"
        elif not " " <= c <= "~":
            return None, f"has the character {c!r}, which is not printable ASCII (write it as \\xHH)"
        else:
            out.append(c)
            i += 1
    value = "".join(out)
    # Python's re must agree that r matches exactly this string (a check on this reader).
    try:
        if not re.fullmatch(r, value):
            return None, "does not match its own value in Python's re (a bug in this check)"
    except re.error as e:
        return None, f"cannot be read by Python's re ({e})"
    return value, None


rule_ids = {rule.get("id") for rule in cfg.get("rules") or []}
for i, e in enumerate(entries[1:], start=2):
    where = f"[[allowlists]] entry {i} ({e.get('description', 'no description')!r})"
    problems += [f"{where}: key {k!r} is not allowed (only {', '.join(sorted(NARROW))})" for k in sorted(set(e) - NARROW)]
    if not e.get("targetRules"):
        problems.append(f"{where}: no targetRules (it would apply to every rule)")
    problems += [f"{where}: targetRules names {rule!r}, which is not a rule of this file"
                 for rule in e.get("targetRules") or [] if rule not in rule_ids]
    if not e.get("regexes"):
        problems.append(f"{where}: no regexes (it would allow every value)")
    if e.get("paths") and e.get("condition") != "AND":
        problems.append(f'{where}: paths without condition = "AND" (it would skip the whole file)')
    if e.get("regexTarget", "secret") not in ("secret", "line"):
        problems.append(f"{where}: regexTarget {e.get('regexTarget')!r}")
    for r in e.get("regexes") or []:
        _, why = literal_value(r)
        if why:
            problems.append(f"{where}: regex '{r}' {why}: a later entry allows exact values only (see the"
                            " header of .gitleaks.toml)")


def toml_value(v):
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if "'''" not in v and not re.search(r"[\x00-\x08\x0a-\x1f\x7f]", v):
        return f"'''{v}'''"
    return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")


def entry_toml(e):
    return "[[allowlists]]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in e.items()) + "\n"


def judge(rules):
    """The real gitleaks judges gitleaks' global allowlist (entry 1, the only part that is not exact
    values): the fixture is scanned with the rules alone, then with the rules and entry 1 (its stop
    words too). Any finding entry 1 removes is a token shape it allows."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        os.mkdir(src)
        # One file per line: gitleaks gives a line-target allowlist the whole line only when the
        # line starts its file (or its hunk, in a git scan); after another line it starts with "\n".
        for n, (line, _, _) in enumerate(fixture, start=1):
            with open(os.path.join(src, f"{n:03}.txt"), "w") as f:
                f.write(line + "\n")
        env = {k: v for k, v in os.environ.items() if k not in ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")}

        def scan(parts):
            conf, report = os.path.join(tmp, "judge.toml"), os.path.join(tmp, "report.json")
            with open(conf, "w") as f:
                f.write("".join(entry_toml(p) for p in parts) + rules)
            run = subprocess.run([os.path.abspath(binary_path), "dir", "--no-banner", "--no-color", "--exit-code", "0",
                                  "--config", conf, "--report-format", "json", "--report-path", report, src],
                                 cwd=tmp, env=env, capture_output=True, text=True)
            if run.returncode != 0:
                return None, (run.stdout + run.stderr).strip().splitlines()[-1:] or [f"exit {run.returncode}"]
            return {(int(os.path.basename(x["File"])[:3]), x["RuleID"]) for x in json.load(open(report))}, None

        base, err = scan([])
        if base is None:
            return [f"gitleaks cannot scan the judge's fixture with these rules: {err[0]}"]
        out = [f"the judge's fixture line {n} ({what}) is not a finding of gitleaks' rules,"
               " so it judges nothing: change its shape" for n, (_, _, what) in enumerate(fixture, start=1)
               if not any(found == n for found, _ in base)]
        parts = [("entry 1 (gitleaks' global allowlist)", [entries[0]])] if entries else []
        for what, part in parts:
            got, err = scan(part)
            if got is None:
                out.append(f"{what}: gitleaks cannot load it ({err[0]})")
            elif base - got:
                n, rule = min(base - got)
                out.append(f"{what} allows a token-shaped value: gitleaks no longer finds fixture line {n}"
                           f" ({fixture[n - 1][2]}; {rule}) and {len(base - got) - 1} more")
        return out


if binary_path:
    binary = open(binary_path, "rb").read()
    start = raw.find(b"\n[[rules]]\n")
    at = binary.find(raw[start:]) if start >= 0 else -1
    if at < 0:
        problems.append("the rules (the first [[rules]] to the end) are not this gitleaks' default rules byte for byte")
    else:
        head = binary[:at]
        a = head.rfind(b"\n[allowlist]\n")
        default = tomllib.loads(head[a:].decode()).get("allowlist") if a >= 0 else None
        # The bytes match, but an unbroken slice of the default rules (one dropped at either end)
        # would match too (#239): the full default rule id list must be the file's. gitleaks embeds
        # its default config as one string, which ends at the binary's next NUL byte.
        end = binary.find(b"\x00", a) if a >= 0 else -1
        try:
            default_ids = [r.get("id") for r in tomllib.loads(binary[a:end].decode()).get("rules") or []] if end > a else []
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            default_ids = []
        file_ids = [r.get("id") for r in cfg.get("rules") or []]
        if not default_ids:
            problems.append("cannot read this gitleaks' default rules (its config up to the next NUL byte)")
        elif file_ids != default_ids:
            missing = [i for i in default_ids if i not in file_ids]
            problems.append(f"the rule ids are not this gitleaks' default rule ids in order ({len(file_ids)} here,"
                            f" {len(default_ids)} there; missing: {', '.join(missing[:5]) or 'none'})")
        if default is None:
            problems.append("this gitleaks has no [allowlist] before its rules")
        else:
            want = {k: v for k, v in default.items() if k not in ("description", "paths")}
            got = {k: v for k, v in (entries[0] if entries else {}).items() if k != "description"}
            if got != want:
                differ = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
                problems.append("[[allowlists]] entry 1 is not this gitleaks' global allowlist without its paths"
                                f" (differs in {', '.join(differ)})")
        if start >= 0:
            problems += judge(raw[start + 1:].decode())
print("\n".join(problems))
sys.exit(1 if problems else 0)
CHECK
toml_check() { # toml_check FILE -> the checker's output; exit 1 on a problem
  if [[ -n "${GITLEAKS_BIN:-}" ]]; then python3 -I "$root/toml-check.py" "$1" "$gitleaks"
  else python3 -I "$root/toml-check.py" "$1"; fi
}
if [[ -f "$repo_toml" ]] && out="$(toml_check "$repo_toml")"; then
  echo "ok   repository settings: .gitleaks.toml has only narrow entries$([[ -n "${GITLEAKS_BIN:-}" ]] && echo ", and its rules and global allowlist are this gitleaks' own")"
else
  echo "FAIL repository settings: .gitleaks.toml (${repo_toml:-none given}): ${out:-missing}"
  failures=$((failures + 1))
fi
# Broken copies, each a python expression on the file's text (t): each must fail the checker.
toml_mutant() { # toml_mutant NAME PYTHON_EXPRESSION_ON_t
  [[ -f "$repo_toml" ]] || return 0
  python3 -I -c 'import sys; t = open(sys.argv[1]).read(); open(sys.argv[2], "w").write(eval(sys.argv[3]))' \
    "$repo_toml" "$root/mutant.toml" "$2"
  if toml_check "$root/mutant.toml" > /dev/null; then
    echo "FAIL repository settings: a .gitleaks.toml with $1 passes the check"
    failures=$((failures + 1))
  else
    echo "ok   repository settings: a .gitleaks.toml with $1 fails the check"
  fi
}
first_entry='t.index("\n[[allowlists]]\n")'
toml_mutant "[extend] useDefault (the default path skip list back)" "t[:$first_entry] + '\n[extend]\nuseDefault = true\n' + t[$first_entry:]"
toml_mutant "[extend] disabledRules" "t[:$first_entry] + '\n[extend]\ndisabledRules = [\"aws-access-token\"]\n' + t[$first_entry:]"
toml_mutant "an [allowlist] table" "t[:$first_entry] + '\n[allowlist]\npaths = [\"x\"]\n' + t[$first_entry:]"
narrow_entry() { printf "%s" "t.replace('\n[[rules]]\n', '\n[[allowlists]]\n$1\n\n[[rules]]\n', 1)"; }
toml_mutant "a path entry without targetRules" "$(narrow_entry "paths = [\\\"^test/\\\"]")"
toml_mutant "a path entry without condition AND" "$(narrow_entry "targetRules = [\\\"generic-api-key\\\"]\\npaths = [\\\"^test/\\\"]\\nregexes = [\\\"^abc\$\\\"]")"
toml_mutant "an entry without regexes" "$(narrow_entry "targetRules = [\\\"generic-api-key\\\"]\\ncondition = \\\"AND\\\"\\npaths = [\\\"^test/\\\"]")"
toml_mutant "an entry that allows any value (^.*\$)" "$(narrow_entry "targetRules = [\\\"generic-api-key\\\"]\\nregexes = [\\\"^.*\$\\\"]")"
toml_mutant "an unanchored value" "$(narrow_entry "targetRules = [\\\"generic-api-key\\\"]\\nregexes = [\\\"abc\\\"]")"
toml_mutant "an entry with stopwords" "$(narrow_entry "targetRules = [\\\"generic-api-key\\\"]\\nregexes = [\\\"^abc\$\\\"]\\nstopwords = [\\\"key\\\"]")"
# #239: an unbroken slice of the default rules is still bytes of the binary; the full rule id list
# must match (only with GITLEAKS_BIN, as the rules check needs the binary).
if [[ -n "${GITLEAKS_BIN:-}" ]]; then
  rules_at='t.index("\n[[rules]]\n")'
  toml_mutant "the default rules but the first (an unbroken slice of them)" \
    "t[:$rules_at] + t[t.index('\n[[rules]]\n', $rules_at + 1):]"
  toml_mutant "the default rules but the last (an unbroken slice of them)" "t[:t.rindex('\n[[rules]]\n') + 1]"
fi
toml_mutant "the pnpm-lock.yaml value widened back to a shape (any 14 digits)" \
  "t.replace(\"^0\\\\.0\\\\.4-canary\\\\.v20260819050620\$\", \"^0\\\\.0\\\\.4-canary\\\\.v\\\\d{14}\$\", 1)"
toml_mutant "a #91 value widened to any 16 hex characters" \
  "t.replace(\"'''^0123456789abcdef\$'''\", \"'''^[0-9a-f]{16}\$'''\", 1)"
# Later entries: each regex must be an exact value (^, one fixed string, $; see the header of
# .gitleaks.toml). Every other form is refused, whatever it would match: a rule that read regex text
# to judge "narrow" was got round each round (#212, #214) by a form it misread.
toml_entry() { # toml_entry NAME pass|fail ENTRY_LINE... -> the repository's file with one more entry
  [[ -f "$repo_toml" ]] || return 0
  local name="$1" want="$2" got=fail verb=fails
  shift 2
  [[ "$want" == fail ]] || verb=passes
  python3 -I -c 'import sys; t = open(sys.argv[1]).read(); e = "\n".join(sys.argv[3:])
open(sys.argv[2], "w").write(t.replace("\n[[rules]]\n", "\n[[allowlists]]\n" + e + "\n\n[[rules]]\n", 1))' \
    "$repo_toml" "$root/mutant.toml" "$@"
  if toml_check "$root/mutant.toml" > "$root/mutant.out"; then got=pass; fi
  if [[ "$got" == "$want" ]]; then
    echo "ok   repository settings: an entry with $name $verb the check"
  else
    echo "FAIL repository settings: an entry with $name: the check gives $got, expected $want"
    sed 's/^/    /' "$root/mutant.out"
    failures=$((failures + 1))
  fi
}
generic='targetRules = ["generic-api-key"]'
toml_entry "a | outside a group (^abc|<40 hex>\$: any 40-hex value)" fail "$generic" 'condition = "AND"' \
  "paths = ['''^test/fx\\.txt\$''']" "regexes = ['''^abc|[0-9a-f]{40}\$''']"
toml_entry "an alternation inside a group that allows 40-hex values" fail "$generic" "regexes = ['''^(?:abc|[0-9a-f]{40})\$''']"
toml_entry "a 40-character token class (^[A-Za-z0-9/+]{40}\$)" fail "$generic" "regexes = ['''^[A-Za-z0-9/+]{40}\$''']"
toml_entry "Go-only syntax Python cannot read (\\pL\\pN)" fail "$generic" "regexes = ['''^[\\pL\\pN]{16,}\$''']"
toml_entry "a whole-line pattern (regexTarget line, ^.*secret.*\$)" fail "$generic" 'regexTarget = "line"' \
  "regexes = ['''^.*secret.*\$''']"
toml_entry "a line pattern without .* for lines naming a secret" fail "$generic" 'regexTarget = "line"' \
  "regexes = ['''^\\S*secret\\S* = \"\\w+\"\$''']"
toml_entry "a UUID shape (32 hex characters in five groups)" fail "$generic" \
  "regexes = ['''^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\$''']"
toml_entry "a 36-character class ({36})" fail "$generic" "regexes = ['''^[a-z0-9-]{36}\$''']"
toml_entry "an open-ended word class (\\w+)" fail "$generic" "regexes = ['''^key_\\w+\$''']"
# Forms that are close to an exact value, and still refused.
toml_entry "an alternation of exact values inside a group" fail "$generic" "regexes = ['''^(?:abc|def)\$''']"
toml_entry "a flag after ^ (^(?i)value\$)" fail "$generic" "regexes = ['''^(?i)abc123def456\$''']"
toml_entry "a flag first ((?i)^value\$)" fail "$generic" "regexes = ['''(?i)^abc123def456\$''']"
toml_entry "an unescaped dot (any character)" fail "$generic" "regexes = ['''^abc.def\$''']"
toml_entry "an escaped last \$ (no end anchor: ^abc\\\$)" fail "$generic" "regexes = ['''^abc\\\$''']"
toml_entry "a \\d escape (a class, not one character)" fail "$generic" "regexes = ['''^abc\\d\$''']"
toml_entry "a \\n escape (only \\<punctuation> and \\xHH)" fail "$generic" "regexes = ['''^abc\\ndef\$''']"
toml_entry "a broken \\x escape (\\xZ1)" fail "$generic" "regexes = ['''^abc\\xZ1\$''']"
toml_entry "a character that is not printable ASCII" fail "$generic" "regexes = ['''^abc$(printf '\303\251')def\$''']"
toml_entry "an empty value (^\$)" fail "$generic" "regexes = ['''^\$''']"
toml_entry "targetRules naming a rule the file does not have" fail 'targetRules = ["no-such-rule"]' \
  "regexes = ['''^abc\$''']"
toml_entry "an exact value with regexTarget match" fail "$generic" 'regexTarget = "match"' "regexes = ['''^abc\$''']"
# Exact values pass: written plainly, with \<punctuation> escapes, or one character as \xHH.
toml_entry "an exact fake value in one file (reviewed)" pass 'targetRules = ["aws-access-token"]' 'condition = "AND"' \
  "paths = ['''^test/fx\\.txt\$''']" "regexes = ['''^${key:0:4}\\x$(printf %02X "'${key:4:1}")${key:5}\$''']"
ant="sk\\-ant\\-api03\\-$(printf 'q%.0s' {1..20})Fake" # an escaped fake, built at run time
toml_entry "an exact escaped fake Anthropic-shaped value" pass "$generic" "regexes = ['''^$ant\$''']"
toml_entry "an exact fake JWT" pass "$generic" "regexes = ['''^eyJhbGciOiJub25lIn0\\.eyJzdWIiOiJ0ZXN0In0\\.\$''']"
toml_entry "an exact value with an escaped backslash last (^abc\\\\\\\\\$)" pass "$generic" "regexes = ['''^abc\\\\\$''']"
# The reviewers' bypasses of #214 round 1: each entry hides a key in the real gitleaks (proved
# first, when GITLEAKS_BIN is set), and the check refuses it. The keys are built at run time.
rnd() { python3 -I -c 'import hashlib,sys; a=sys.argv[2]; out=""; i=0
while len(out) < int(sys.argv[3]):
    out += "".join(a[b % len(a)] for b in hashlib.sha256(f"{sys.argv[1]}/{i}".encode()).digest()); i += 1
print(out[:int(sys.argv[3])])' "$@"; }
toml_hides() { # toml_hides NAME RULE REGEX FILE_CONTENT
  if [[ -n "${GITLEAKS_BIN:-}" && -f "$repo_toml" ]]; then
    python3 -I -c 'import sys; t = open(sys.argv[1]).read()
e = "[[allowlists]]\ntargetRules = [\"" + sys.argv[3] + "\"]\nregexes = [\x27\x27\x27" + sys.argv[4] + "\x27\x27\x27]\n"
open(sys.argv[2], "w").write(t.replace("\n[[rules]]\n", "\n" + e + "\n[[rules]]\n", 1))' "$repo_toml" "$root/hide.toml" "$2" "$3"
    rm -rf "${root:?}/hide-src"; mkdir "$root/hide-src"; printf '%s\n' "$4" > "$root/hide-src/f.txt"
    local n=() c
    for c in "$repo_toml" "$root/hide.toml"; do
      "$gitleaks" dir --no-banner --exit-code 0 --config "$c" --report-format json --report-path "$root/hide.json" "$root/hide-src" 2> /dev/null
      n+=("$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$root/hide.json")")
    done
    if [[ "${n[0]}" -gt 0 && "${n[1]}" -eq 0 ]]; then
      echo "ok   repository settings: an entry with $1 hides a key in gitleaks (${n[0]} findings -> 0)"
    else
      echo "FAIL repository settings: an entry with $1 does not hide its key (${n[0]} -> ${n[1]}): the case proves nothing"
      failures=$((failures + 1))
    fi
  fi
  toml_entry "$1" fail "targetRules = [\"$2\"]" "regexes = ['''$3''']"
}
B62=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz
k36="$(rnd r1-36 "$B62" 36)"
h="$(rnd r1-uuid 0123456789abcdef 32)"; uuid="${h:0:8}-${h:8:4}-4${h:13:3}-a${h:17:3}-${h:20:12}"
h40="$(rnd r1-hex 0123456789abcdef 40)"
b64="$(rnd r1-pem "$B62+/" 256)"
pem="$(printf -- '-----BEGIN PRIV''ATE KEY-----\n%s\n%s\n%s\n%s\n-----END PRIV''ATE KEY-----' \
  "${b64:0:64}" "${b64:64:64}" "${b64:128:64}" "${b64:192:64}")"
xd='[[:xdigit:]]'
alt="(?:$(printf '%s|' 0 1 2 3 4 5 6 7 8 9 a b c d e f)zz)"
toml_hides "(?m) around an exact-looking value (every private key)" private-key '(?m)^-----BEGIN PRIVATE KEY-----$' "$pem"
toml_hides "a POSIX class ([[:alnum:]]{36}, which Python misreads)" generic-api-key '^[[:alnum:]]{36}$' "api_key = \"$k36\""
toml_hides "nested repeats of an alternation (36 letters or digits)" generic-api-key \
  '^(?:(?:[A-Za-z]|[0-9]{1}){6}){6}$' "api_key = \"$k36\""
toml_hides "a POSIX-class UUID shape" generic-api-key "^$xd{8}-$xd{4}-$xd{4}-$xd{4}-$xd{12}\$" "client_secret = \"$uuid\""
toml_hides "40 groups of fixed alternatives (any 40-hex value)" generic-api-key \
  "^$(for _ in $(seq 40); do printf '%s' "$alt"; done)\$" "auth_token = \"$h40\""
if [[ -n "${GITLEAKS_BIN:-}" ]]; then
  toml_mutant "one more global allowlist value" "t.replace('regexes = [', 'regexes = [\n    \'\'\'^x\$\'\'\',', 1)"
  toml_mutant "a global allowlist stopword dropped" "t.replace('stopwords = [\n', 'stopwords = [\n#', 1)"
  toml_mutant "a rule changed" "t.replace('entropy = 3', 'entropy = 4', 1)"
fi

# A fork-follow rebase force-pushes every fork commit again with a new id, so main's push scan
# reads them all again. The fork's wait tests (#91, long on main) hold fixed fake values that
# generic-api-key reads as keys: the exact-value entry allows them, on every rebase; another value
# in the same file is still found. Real gitleaks only (the stand-in knows only AWS key ids).
if [[ -n "${GITLEAKS_BIN:-}" && -f "$repo_toml" ]]; then
  git init -q --bare "$root/rb-up.git"
  git -C "$root/rb-up.git" config uploadpack.allowFilter true
  new_origin rebase
  commit up.txt "up v1" "up 1"
  git tag v1.0.0
  rb_up1=$(git rev-parse HEAD)
  cp "$repo_toml" .gitleaks.toml
  commit fork.txt "fork" "fork 1: the series, with the repository's .gitleaks.toml"
  # The three lines as #91 wrote them; the values are put together at run time.
  mkdir -p apps/server/src/waits
  printf '      key: "%s",\n' "0123456789""abcdef" > apps/server/src/waits/WaitDelivery.test.ts
  printf '      delivering({ id: "w_m", key: "%s" }),\n      key: "%s",\n' "f123456789""abcdef" "1123456789""abcdef" \
    > apps/server/src/waits/WaitService.test.ts
  date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "fork 2: the wait tests' fake values (#91)"
  rb_old=$(git rev-parse HEAD)
  printf '      key: "%s",\n' "2123456789""abcdef" >> apps/server/src/waits/WaitService.test.ts
  date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "fork 3: one more value in a wait test"
  rb_old_more=$(git rev-parse HEAD)
  git checkout -q -b up-next "$rb_up1"
  commit up.txt "up v2" "up 2"
  git tag v1.1.0
  git push -q "$root/rb-up.git" up-next:main v1.0.0 v1.1.0
  git checkout -q -b rb-new "$rb_old"
  git rebase -q --onto v1.1.0 "$rb_up1"
  git checkout -q -b rb-new-more "$rb_old_more"
  git rebase -q --onto v1.1.0 "$rb_up1"
  git push -q origin "$rb_old:refs/heads/main" rb-new rb-new-more
  checkout rebase rb-new
  export UPSTREAM=up/t3code UPSTREAM_URL="file://$root/rb-up.git"
  EVENT_NAME=push BEFORE="$rb_old" AFTER="$(git -C "$root/rebase" rev-parse rb-new)" \
    check "fork-follow rebase: #91's fake values pass by their exact values (every fork commit re-read)" 0 "upstream up/t3code already has"
  EVENT_NAME=push BEFORE="$rb_old" AFTER="$(git -C "$root/rebase" rev-parse rb-new-more)" \
    check "fork-follow rebase: another value in a wait test is found" 1
  unset UPSTREAM UPSTREAM_URL
fi

# Settings gitleaks cannot load: `[extend] path` names a file the settings folder does not hold.
new_origin extend
printf '%s\n' '[extend]' 'path = "base.toml"' > .gitleaks.toml
printf '%s\n' '[extend]' 'useDefault = true' > base.toml
commit app.txt "app" "main 1: settings that extend base.toml"
extend_base=$(git rev-parse HEAD)
git checkout -q -b clean; commit notes.txt "notes" "PR: no key"
git push -q origin main clean
checkout extend clean
BASE_SHA="$extend_base" HEAD_SHA="$(git rev-parse clean)" \
  check "settings that cannot load fail the job (2), not \"found possible secrets\"" 2 "gitleaks stopped without a result.*extend"

# Contents git takes for binary, and UTF-16 text.
new_origin contents
commit app.txt "app" "main 1"
commit_bytes existing.env "main 2" < <(printf '\377\376'; printf 'notes=none\n' | iconv -f UTF-8 -t UTF-16LE)
commit_bytes five.env "main 3" < <(printf '\377\376'; printf 'one\ntwo\nthree\nfour\nfive\n' | iconv -f UTF-8 -t UTF-16LE)
contents_base=$(git rev-parse HEAD)
contents_branches=()
contents_pr() { # contents_pr BRANCH FILE < CONTENT
  git checkout -q -b "$1" "$contents_base"
  commit_bytes "$2" "PR: $2"
  contents_branches+=("$1")
}
contents_pr nul data.cfg < <(printf 'bin\000ary\naws_access_key_id=%s\n' "$key")
contents_pr utf16le win.env < <(printf '\377\376'; printf 'aws_access_key_id=%s\n' "$key" | iconv -f UTF-8 -t UTF-16LE)
contents_pr utf16be mac.env < <(printf '\376\377'; printf 'aws_access_key_id=%s\n' "$key" | iconv -f UTF-8 -t UTF-16BE)
contents_pr bom-ascii odd.env < <(printf '\377\376\naws_access_key_id=%s\n' "$key")
contents_pr utf16-clean notes.env < <(printf '\377\376'; printf 'notes=none\n' | iconv -f UTF-8 -t UTF-16LE)
contents_pr utf16-existing existing.env < <(printf '\377\376'; printf 'notes=none\naws_access_key_id=%s\n' "$key" | iconv -f UTF-8 -t UTF-16LE)
contents_pr utf16-space "win notes.env" < <(printf '\377\376'; printf 'aws_access_key_id=%s\n' "$key" | iconv -f UTF-8 -t UTF-16LE)
# A stray last byte (an odd length): half a character, dropped before decoding.
contents_pr utf16-odd odd-length.env < <(printf '\377\376'; printf 'notes=none\n' | iconv -f UTF-8 -t UTF-16LE; printf '\n')
contents_pr utf16-odd-key odd-key.env < <(printf '\377\376'; printf 'aws_access_key_id=%s\n' "$key" | iconv -f UTF-8 -t UTF-16LE; printf '\n')
# A UTF-16 file renamed with a key line added in the same commit (git log sees a rename).
git checkout -q -b utf16-rename "$contents_base"
git mv five.env six.env
printf '\377\376' > six.env
printf 'one\ntwo\nthree\nfour\nfive\naws_access_key_id=%s\n' "$key" | iconv -f UTF-8 -t UTF-16LE >> six.env
date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "PR: rename a UTF-16 file and add a key"
contents_branches+=(utf16-rename)
git checkout -q main
git push -q origin main "${contents_branches[@]}"
clone="$root/contents-clone"
git clone -q --filter=blob:none --no-checkout --single-branch --branch main "file://$root/contents.git" "$clone"
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse nul)" \
  check "a key after a NUL byte (a file git takes for binary) is found, no file decoded" 1 "" "UTF-16 byte-order mark"
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16le)" \
  check "a key in UTF-16 LE text (byte-order mark) is found" 1 "1 file\\(s\\) start with a UTF-16 byte-order mark"
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16be)" check "a key in UTF-16 BE text (byte-order mark) is found" 1
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse bom-ascii)" \
  check "a key in plain text after the UTF-16 mark's two bytes is found (read as stored too)" 1
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16-clean)" \
  check "UTF-16 text without a key passes, every commit read" 0 "gitleaks scanned 1 commit"
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16-existing)" \
  check "a key added to a UTF-16 file the default branch already has is found" 1
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16-space)" \
  check "a key in a UTF-16 file with a space in its name is found (every file filtered)" 1
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16-odd)" \
  check "a UTF-16 file with a stray last byte and no key passes (no decoding error)" 0 "gitleaks scanned 1 commit" "cannot decode"
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16-odd-key)" \
  check "a key in a UTF-16 file with a stray last byte is found" 1
BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16-rename)" \
  check "a key added to a renamed UTF-16 file is found" 1 "file\\(s\\) start with a UTF-16 byte-order mark"
# An iconv that cannot decode UTF-16 (a runner without it): the job fails with iconv's reason,
# never passes with the decoded part missing.
mkdir -p "$root/iconv-bin"
printf '#!/bin/sh\necho "iconv: conversion from $3 unsupported" >&2\nexit 1\n' > "$root/iconv-bin/iconv"
chmod +x "$root/iconv-bin/iconv"
PATH="$root/iconv-bin:$PATH" BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse utf16le)" \
  check "UTF-16 text with an iconv that cannot decode it fails, with the reason" 2 "cannot decode a file with a UTF-16 byte-order mark.*unsupported"
PATH="$root/iconv-bin:$PATH" BASE_SHA="$contents_base" HEAD_SHA="$(git rev-parse nul)" \
  check "no UTF-16 file: iconv is not needed" 1 "" "cannot decode"

# Ignore lines name a line number, not a value. One without a commit (file:rule:line) would hide
# every key of its rule on that line in every commit: the scan refuses it. One pinned to a commit
# applies to that commit, with a warning saying why it will not survive a squash merge, and the
# push scan of the merge then fails.
new_origin squash
commit app.txt "app" "main 1"
squash_base=$(git rev-parse HEAD)
git checkout -q -b fixture-pr
mkdir -p test
commit test/fx.txt "aws_access_key_id=$key" "PR: a fake key in a test fixture"
fixture_pr=$(git rev-parse HEAD)
git checkout -q -b pinned-pr "$squash_base"
mkdir -p test
commit test/fx2.txt "aws_access_key_id=$key" "PR: another fake key in a test fixture"
pinned_pr=$(git rev-parse HEAD)
git checkout -q main
git push -q origin main fixture-pr pinned-pr
checkout squash fixture-pr
BASE_SHA="$squash_base" HEAD_SHA="$fixture_pr" check "ignore lines: the fixture key is found with none" 1
cd "$root/squash"
commit .gitleaksignore "$(printf '%s\n' "# a comment" "test/fx.txt:aws-access-token:1")" "main: an ignore line without a commit"
unpinned_main=$(git rev-parse HEAD)
git push -q origin main
BASE_SHA="$squash_base" HEAD_SHA="$fixture_pr" \
  check "ignore lines: one without a commit (file:rule:line) is refused" 2 "::error::.*1 line\\(s\\) without a commit \\(file:rule:line\\)"
cd "$root/squash"
commit .gitleaksignore "$(printf '%s\n' "# reviewed: a fake key in a test fixture" "$pinned_pr:test/fx2.txt:aws-access-token:1")" \
  "main: ignore the second fixture key, pinned to its commit"
pinned_main=$(git rev-parse HEAD)
git push -q origin main
BASE_SHA="$squash_base" HEAD_SHA="$fixture_pr" \
  check "ignore lines: comments and a pinned line are no refusal" 1 "::warning.*1 line\\(s\\) pinned to a commit" "::error::.*without a commit"
checkout squash pinned-pr
BASE_SHA="$squash_base" HEAD_SHA="$pinned_pr" \
  check "ignore lines: a commit-pinned line applies to its commit, with a warning saying why not" 0 "::warning.*pinned to a commit.*exact value in \\.gitleaks\\.toml"
cd "$root/squash"
git merge -q --squash pinned-pr
date=$((date + 60)); GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "PR: another fake key (squashed)"
squashed_pinned=$(git rev-parse HEAD)
git push -q origin main
checkout squash main
EVENT_NAME=push BEFORE="$pinned_main" AFTER="$squashed_pinned" \
  check "ignore lines: after the squash merge the commit-pinned line no longer matches" 1 "pinned to a commit"

# The reviewed path for a fake key in a test fixture: its exact value in .gitleaks.toml, in a change
# of its own on the default branch. It matches whatever the line number and commit id: the PR passes
# when a later commit of it moves the line, and so does the default branch's push scan of the squash
# merge. A different key on that line, or another key beside it, is still found. Real gitleaks only
# (the stand-in reads no [[allowlists]]).
if [[ -n "${GITLEAKS_BIN:-}" && -f "$repo_toml" ]]; then
  key_toml="^${key:0:4}\\x$(printf %02X "'${key:4:1}")${key:5}\$"
  new_origin values
  cp "$repo_toml" .gitleaks.toml
  commit app.txt "app" "main 1, with the repository's .gitleaks.toml"
  values_base=$(git rev-parse HEAD)
  git checkout -q -b shift
  mkdir -p test
  commit test/fx.txt "aws_access_key_id=$key" "PR 1: a fake key in a test fixture"
  commit test/fx.txt "$(printf '# fixture\naws_access_key_id=%s' "$key")" "PR 2: a line above it"
  git checkout -q -b swap "$values_base"
  mkdir -p test
  commit test/fx.txt "aws_access_key_id=$key2" "PR: a different key in the fixture"
  git checkout -q -b beside "$values_base"
  mkdir -p test
  commit test/fx.txt "aws_access_key_id=$key aws_other=$key2" "PR: the fake key and another on one line"
  git checkout -q main
  # The value written so that .gitleaks.toml does not hold the key itself.
  cat >> .gitleaks.toml <<TOML

[[allowlists]]
description = "test fixture: a reviewed fake key"
targetRules = ["aws-access-token"]
condition = "AND"
paths = ['''^test/fx\\.txt\$''']
regexes = ['''$key_toml''']
TOML
  date=$((date + 60)); git add -A; GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "main: allow the fixture's value (reviewed, a change of its own)"
  values_main=$(git rev-parse HEAD)
  git push -q origin main shift swap beside
  checkout values shift
  BASE_SHA="$values_base" HEAD_SHA="$(git -C "$root/values" rev-parse shift)" \
    check "exact value: the PR passes, a later commit moving the line too" 0
  cd "$root/values"
  git merge -q --squash shift
  date=$((date + 60)); GIT_AUTHOR_DATE="@$date" GIT_COMMITTER_DATE="@$date" git commit -qm "PR: a fake key in a test fixture (squashed)"
  values_squashed=$(git rev-parse HEAD)
  git push -q origin main
  checkout values main
  EVENT_NAME=push BEFORE="$values_main" AFTER="$values_squashed" \
    check "exact value: the default branch's push scan of the squash merge passes" 0
  for branch in swap beside; do
    checkout values "$branch"
    BASE_SHA="$values_base" HEAD_SHA="$(git -C "$root/values" rev-parse "$branch")" \
      check "exact value: another key on the fixture's line is found ($branch)" 1
  done
fi

# A push reads, and uses, the settings of the tip before the push. (The stand-in allows every key
# under an [allowlist]; the real gitleaks allows k.txt and the other paths named.)
pushset_toml() { printf '%s\n' '[extend]' 'useDefault = true' '[allowlist]' "paths = [$1]"; }
new_origin pushset
commit .gitleaks.toml "$(pushset_toml "'''^k\\.txt\$'''")" "main: allow k.txt (reviewed)"
pushset_before=$(git rev-parse HEAD)
commit k.txt "aws_access_key_id=$key" "main: the key that setting allows"
pushset_after=$(git rev-parse HEAD)
commit .gitleaks.toml "$(pushset_toml "'''^k\\.txt\$''', '''^other\\.txt\$'''")" "main: another setting"
pushset_change=$(git rev-parse HEAD)
git push -q origin main
checkout pushset main
EVENT_NAME=push BEFORE="$pushset_before" AFTER="$pushset_after" \
  check "push: the settings of the tip before the push apply" 0 "settings .* from the tip before the push"
# A new branch (or an old tip that cannot be fetched): AFTER's first parent, as the header says.
EVENT_NAME=push BEFORE="0000000000000000000000000000000000000000" AFTER="$pushset_after" \
  check "push of a new branch: the settings of the pushed commit's first parent apply" 0 "settings .* from the pushed commit's first parent \\(${pushset_before:0:12}\\)"
EVENT_NAME=push BEFORE="$pushset_after" AFTER="$pushset_change" \
  check "push: a settings change gets a notice that it applies from the next push" 0 "::notice.*applies from the next push" "re-run"
# No .gitleaks.toml on the settings commit: gitleaks' built-in settings, with a warning.
EVENT_NAME=push BEFORE="0000000000000000000000000000000000000000" AFTER="$pushset_before" \
  check "no .gitleaks.toml on the settings commit: a warning" 0 "::warning.*No \\.gitleaks\\.toml on .*built-in settings"
EVENT_NAME=push BEFORE="$pushset_before" AFTER="$pushset_after" \
  check "a .gitleaks.toml on the settings commit: no such warning" 0 "" "No \\.gitleaks\\.toml"
# A pull request that changes only the settings: the reviewed path, no advice to re-run it.
cd "$root/pushset"
git checkout -q -b settings-only "$pushset_after"
commit .gitleaks.toml "$(pushset_toml "'''^k\\.txt\$''', '''^third\\.txt\$'''")" "PR: a setting, nothing else"
git checkout -q -b settings-and-code "$pushset_after"
commit .gitleaks.toml "$(pushset_toml "'''^k\\.txt\$''', '''^code\\.txt\$'''")" "PR: a setting"
commit code.txt "code" "PR: and code"
git checkout -q main
git reset -q --hard "$pushset_after"
git push -q -f origin main settings-only settings-and-code
checkout pushset settings-only
BASE_SHA="$pushset_after" HEAD_SHA="$(git -C "$root/pushset" rev-parse settings-only)" \
  check "pull request changing only the settings: a notice, no re-run advice" 0 "::notice.*changes only \\.gitleaks\\.toml" "re-run"
checkout pushset settings-and-code
BASE_SHA="$pushset_after" HEAD_SHA="$(git -C "$root/pushset" rev-parse settings-and-code)" \
  check "pull request changing the settings and code: land the settings first" 0 "::notice.*land it in a pull request of its own first"

# Temporary files go to RUNNER_TEMP when it is set, and are gone afterwards. A clone of its own.
new_origin runner-temp-origin
commit app.txt "app" "main 1"
rt_base=$(git rev-parse HEAD)
git checkout -q -b clean; commit notes.txt "notes" "PR: no key"
git push -q origin main clean
checkout runner-temp-origin clean
mkdir "$root/runner-temp"
real_gitleaks="$gitleaks"
gitleaks="$root/where-gitleaks"
cat > "$gitleaks" <<WHERE
#!/usr/bin/env bash
ls "$root/runner-temp" > "$root/runner-temp-seen"
"$real_gitleaks" "\$@"
WHERE
chmod +x "$gitleaks"
RUNNER_TEMP="$root/runner-temp" BASE_SHA="$rt_base" HEAD_SHA="$(git rev-parse clean)" \
  check "RUNNER_TEMP set: the scan passes" 0
if grep -q '^fork-secret-scan\.' "$root/runner-temp-seen" 2> /dev/null && [[ -z "$(ls -A "$root/runner-temp")" ]]; then
  echo "ok   RUNNER_TEMP set: temporary files there while gitleaks runs, none left after"
else
  echo "FAIL RUNNER_TEMP set: while gitleaks ran: $(cat "$root/runner-temp-seen" 2> /dev/null); after: $(ls -A "$root/runner-temp")"
  failures=$((failures + 1))
fi
gitleaks="$real_gitleaks"

if [[ "$failures" -ne 0 ]]; then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
