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
unset GITHUB_STEP_SUMMARY GITHUB_OUTPUT UPSTREAM UPSTREAM_URL RUNNER_TEMP GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML
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
# an [allowlist] allows every key; a .gitleaksignore (--gitleaks-ignore-path, default ".", and
# SOURCE) line `<commit>:...` ignores the keys of that commit.
set -uo pipefail
opts="" config="" ignore_path="." source="."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --log-opts=*) opts="${1#--log-opts=}" ;;
    --config) config="$2"; shift ;;
    --gitleaks-ignore-path) ignore_path="$2"; shift ;;
    -*) ;;
    *) [[ "$1" == git ]] || source="$1" ;;
  esac
  shift
done
settings=""
if [[ -n "$config" ]]; then settings="$(cat "$config")"
elif [[ -n "${GITLEAKS_CONFIG:-}" ]]; then settings="$(cat "$GITLEAKS_CONFIG")"
elif [[ -n "${GITLEAKS_CONFIG_TOML:-}" ]]; then settings="$GITLEAKS_CONFIG_TOML"
elif [[ -f "$source/.gitleaks.toml" ]]; then settings="$(cat "$source/.gitleaks.toml")"
fi
ignored=" "
for file in "$ignore_path" "$ignore_path/.gitleaksignore" "$source/.gitleaksignore"; do
  if [[ -f "$file" ]]; then ignored+="$(grep -oE '^[0-9a-f]{40,64}' "$file" | tr '\n' ' ')"; fi
done
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
if [[ "$settings" != *"[allowlist]"* ]]; then
  # The commits whose diffs add a key, minus the ignored ones.
  for sha in $(grep -E '^commit |^\+.*(AKIA)[A-Z2-7]{16}' "$TMPDIR/fake-gitleaks.log" |
    awk '/^commit / { c = $2; next } { print c }' | sort -u); do
    if [[ "$ignored" != *" $sha "* ]]; then echo "leaks found: 1"; exit 1; fi
  done
fi
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
  git clone -q --filter=blob:none --single-branch --branch "$2" "file://$root/$1.git" "$clone"
}
allow_all="paths = ['''.*''']"
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

# Temporary files go to RUNNER_TEMP when it is set, and are gone afterwards.
mkdir "$root/runner-temp"
real_gitleaks="$gitleaks"
gitleaks="$root/where-gitleaks"
cat > "$gitleaks" <<WHERE
#!/usr/bin/env bash
ls "$root/runner-temp" > "$root/runner-temp-seen"
"$real_gitleaks" "\$@"
WHERE
chmod +x "$gitleaks"
RUNNER_TEMP="$root/runner-temp" BASE_SHA="$sha256_base" HEAD_SHA="$(git rev-parse clean)" \
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
