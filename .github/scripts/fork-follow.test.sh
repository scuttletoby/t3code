#!/usr/bin/env bash
# Tests for fork-follow.yml and its scripts against local git repositories and a fake `gh`; no
# network. FORK_FOLLOW_DIR runs the tests against the scripts in another directory.
#   bash .github/scripts/fork-follow.test.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dir="$(realpath "${FORK_FOLLOW_DIR:-$here}")"
workflow="$(realpath "${FORK_FOLLOW_WORKFLOW:-$here/../workflows/fork-follow.yml}")"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# Upstream "up/t3code" and this fork "me/t3code" live under $root/gh; git reaches them through the
# github.com URLs the scripts use.
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$root/gitconfig"
git config --global user.name test
git config --global user.email test@example.invalid
git config --global init.defaultBranch main
git config --global advice.detachedHead false
git config --global url."$root/gh/".insteadOf https://github.com/
export REPOSITORY=me/t3code GITHUB_REPOSITORY=me/t3code UPSTREAM=up/t3code
unset GITHUB_OUTPUT GITHUB_STEP_SUMMARY GITHUB_ACTIONS UPSTREAM_URL ORIGIN RUN_SHA PUSH_URL \
  SERIES_BRANCH FORK_PUSH_TOKEN OWN_WORKFLOWS HEAD_SHA TREE_REF SINCE REPO_SECRET_PRESENT \
  FORK_PUSH_ENVIRONMENT

# Fake gh, served from $root/api:
#   gh api [-X M] PATH [--paginate] [--jq EXPR]     gh issue list|comment|create|close ...
# A file $root/api/fail_<word> makes every call whose path contains <word> fail. Run listings are
# filtered by their head_sha= and status= query like GitHub's (created= is ignored).
# fork_test_gh (fork-test-gh.sh) installs it first on PATH, with no token, a .invalid GH_HOST and
# FORK_TEST_MODE, so the scripts refuse to run if any other gh would answer.
. "$here/fork-test-gh.sh"
mkdir -p "$root/bin" "$root/api"
fork_test_gh "$root/bin" << 'GH'
set -euo pipefail
api="${FAKE_API:?the gh stub needs FAKE_API}"
echo "gh $*" >> "$api/calls"
if [[ "$1" == issue ]]; then
  # Like GitHub: with Issues off, every issue command fails.
  if [[ "$(cat "$api/has_issues")" != true ]]; then
    echo "the 'me/t3code' repository has disabled issues" >&2
    exit 1
  fi
  echo "issue $2" >> "$api/writes"
  case "$2" in
    list) cat "$api/issues" 2> /dev/null || true ;;
    create) echo "https://github.com/me/t3code/issues/9" ;;
  esac
  exit 0
fi
shift
method=GET path="" jq=""
while (( $# )); do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    --jq) jq="$2"; shift 2 ;;
    --paginate) shift ;;
    *) path="$1"; shift ;;
  esac
done
for f in "$api"/fail_*; do
  [[ -e "$f" ]] || continue
  [[ "$path" != *"${f##*/fail_}"* ]] || { echo "HTTP 502: Bad Gateway ($path)" >&2; exit 1; }
done
case "$method $path" in
  "GET repos/me/t3code") body="{\"has_issues\":$(cat "$api/has_issues"),\"parent\":{\"full_name\":\"up/t3code\"}}" ;;
  "GET repos/me/t3code/actions/workflows") body="$(cat "$api/workflows.json")" ;;
  "PUT repos/me/t3code/actions/workflows/"*/disable) echo "disable $path" >> "$api/writes"; exit 0 ;;
  "GET repos/me/t3code/actions/runs?"*)
    query="${path#*\?}" sha="" st=""
    [[ "&$query" != *"&head_sha="* ]] || { sha="${query#*head_sha=}"; sha="${sha%%&*}"; }
    [[ "&$query" != *"&status="* ]] || { st="${query#*status=}"; st="${st%%&*}"; }
    body="$(jq --arg sha "$sha" --arg st "$st" \
      '.workflow_runs |= map(select(($sha == "" or .head_sha == $sha) and ($st == "" or .status == $st)))' "$api/runs.json")" ;;
  "GET repos/me/t3code/contents/.github/workflows?ref="*) body="$(cat "$api/files.json")" ;;
  "GET repos/me/t3code/environments/fork-push")
    [[ -f "$api/environment.json" ]] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    body="$(cat "$api/environment.json")" ;;
  "GET repos/me/t3code/environments/fork-push/deployment-branch-policies"*) body="$(cat "$api/branch-policies.json")" ;;
  "POST repos/me/t3code/actions/runs/"*/cancel) echo "cancel $path" >> "$api/writes"; exit 0 ;;
  "GET repos/me/t3code/releases/tags/"*)
    tag="${path##*/}"
    [[ -f "$api/release-$tag" ]] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    body="$(cat "$api/release-$tag")" ;;
  *) echo "fake gh: no route for $method $path" >&2; exit 1 ;;
esac
if [[ -n "$jq" ]]; then jq -r "$jq" <<< "$body"; else echo "$body"; fi
GH
export FAKE_API="$root/api"

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
hasnt() { ! grep -q -- "$1" "$2" 2> /dev/null; }

reset_api() {
  rm -rf "$root/api" && mkdir -p "$root/api"
  echo "${1:-false}" > "$root/api/has_issues"
  echo '{"workflows":[]}' > "$root/api/workflows.json"
  echo '{"workflow_runs":[]}' > "$root/api/runs.json"
  echo '[]' > "$root/api/files.json"
}

# --- repositories --------------------------------------------------------------------------------
up_git() { (cd "$root/upwork" && "$@" > /dev/null 2>&1); }
up_push() { up_git git push -q --force https://github.com/up/t3code.git "$@"; }

# Upstream: u1 (v0.1.8) - u2 (v0.1.9) - u3 (v0.1.10) - u4 (upstream main). Each adds a file, and
# upstream's release-desktop.yml is the same in all of them unless a test changes it.
new_upstream() {
  rm -rf "$root/gh" "$root/upwork"
  mkdir -p "$root/gh/up" "$root/gh/me"
  git init -q --bare "$root/gh/up/t3code.git"
  git init -q --bare "$root/gh/me/t3code.git"
  git init -q "$root/upwork"
  (
    cd "$root/upwork"
    mkdir -p .github/workflows
    echo "on: workflow_call" > .github/workflows/release-desktop.yml
    echo "on: push" > .github/workflows/ci.yml
    echo base > shared.txt
    git add -A && git commit -q -m u1 && git tag v0.1.8
    echo u2 > u2.txt && git add -A && git commit -q -m u2 && git tag -a -m v0.1.9 v0.1.9
    echo u3 > u3.txt && git add -A && git commit -q -m u3 && git tag v0.1.10
    echo u4 > u4.txt && git add -A && git commit -q -m u4
    git push -q https://github.com/up/t3code.git main --tags
  )
}

# The fork's checkout in $root/work: upstream rev $1 plus the fork's own commit (its workflow
# files and scripts, and feature.txt), pushed as the fork's main.
new_fork() {
  rm -rf "$root/work"
  git clone -q https://github.com/up/t3code.git "$root/work" 2> /dev/null
  (
    cd "$root/work"
    git remote set-url origin https://github.com/me/t3code.git
    git checkout -q -B main "$1"
    mkdir -p .github/scripts .github/workflows
    cp "$here/fork-schema-check.cjs" .github/scripts/
    printf 'jobs:\n  desktop:\n    uses: ./.github/workflows/release-desktop.yml\n' > .github/workflows/fork-release.yml
    echo "on: workflow_dispatch" > .github/workflows/fork-follow.yml
    echo feature > feature.txt
    git add -A && git commit -q -m "fork: own change"
    git push -q --force origin main 2> /dev/null
  )
}
work() { (cd "$root/work" && "$@" > /dev/null 2>&1); }
fork_tag() { work git tag -f "$1" "${2:-HEAD}" && work git push -q --force origin "refs/tags/$1"; }

plan() { # runs the plan in $root/work; output in $root/plan.out, report in $root/report.md
  rm -f "$root/report.md" "$root/plan.out"
  (cd "$root/work" && REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" bash "$dir/fork-follow.sh") > "$root/out" 2>&1
}
out() { sed -n "s/^$1=//p" "$root/plan.out" 2> /dev/null | tail -n1; }
action_is() { [[ "$(out action)" == "$1" ]]; }
series() { git -C "$root/gh/me/t3code.git" rev-parse "refs/heads/${1:-main}" 2> /dev/null; }
work_head() { git -C "$root/work" rev-parse HEAD; }

# --- plan: fork-follow.sh ---------------------------------------------------------------------
new_upstream
reset_api
new_fork v0.1.10
plan
check "plan: same base, no fork release yet -> release" action_is release
fork_tag v0.1.10-fork.1
plan
check "plan: the series head is the newest release -> none, naming it" \
  eval 'action_is none && [[ "$(out released_tag)" == v0.1.10-fork.1 ]]'
check "plan: own_workflows lists only the files the series adds" \
  eval '[[ "$(out own_workflows)" == .github/workflows/fork-follow.yml,.github/workflows/fork-release.yml ]]'

new_upstream
new_fork v0.1.9
before="$(work_head)"
plan
check "plan: a newer tag with a clean rebase -> rebase onto it" \
  eval 'action_is rebase && [[ "$(out base_tag)" == v0.1.10 && "$(out previous_head)" == "$before" ]]'

new_fork v0.1.9
(cd "$root/work" && echo fork > u3.txt && git add -A && git commit -q -m "fork: edit u3" && git push -q origin main) > /dev/null 2>&1
plan
check "plan: a conflict names the stopped patch and its file and aborts the rebase" \
  eval 'has "fork: edit u3" "$root/report.md" && has "u3.txt" "$root/report.md" && [[ ! -d "$root/work/.git/rebase-merge" ]]'
check "plan: the conflict advice says to start the follow workflow once after the manual push" \
  has "gh workflow run fork-follow.yml" "$root/report.md"

new_fork main
plan
check "plan: upstream commits inside the series are refused" has "upstream commits after" "$root/report.md"

# Major: merge commits. A fork PR merged with a merge commit that carries its own change.
new_fork v0.1.9
(
  cd "$root/work"
  git checkout -q -b side HEAD~0 && echo side > side.txt && git add -A && git commit -q -m "fork: side"
  git checkout -q main && echo other > other.txt && git add -A && git commit -q -m "fork: other"
  git merge -q --no-ff --no-commit side && echo evil > evil.txt && git add -A && git commit -q -m "Merge side"
  git push -q origin main
) > /dev/null 2>&1
before="$(work_head)"
plan
check "plan: merge commits in the series are refused and named, the series left as it is" \
  eval '! action_is rebase && has "merge commits" "$root/report.md" && has "Merge side" "$root/report.md" && [[ "$(work_head)" == "$before" ]]'

# Minor: a failed fetch leaves a reason that names it.
new_fork v0.1.10
(cd "$root/work" && UPSTREAM_URL="$root/nowhere.git" REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: a failed upstream fetch writes a reason naming it" has "Could not fetch" "$root/report.md"

# Minor: any other failing command still writes a reason (fake git fails `rev-list --merges`).
cat > "$root/bin/git-fail-merges" << 'G'
#!/usr/bin/env bash
for a in "$@"; do [[ "$a" != --merges ]] || { echo "fatal: simulated" >&2; exit 128; }; done
exec "$REAL_GIT" "$@"
G
chmod +x "$root/bin/git-fail-merges"
mkdir -p "$root/failgit" && ln -sf "$root/bin/git-fail-merges" "$root/failgit/git"
(cd "$root/work" && REAL_GIT="$(command -v git)" PATH="$root/failgit:$PATH" REPORT="$root/report.md" \
  GITHUB_OUTPUT="$root/plan.out" bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: an unexpected failure writes a reason naming the line" has "fork-follow.sh line" "$root/report.md"

# Minor: upstream deletes or moves the tag the released series sits on.
new_upstream
new_fork v0.1.10
fork_tag v0.1.10-fork.1
git -C "$root/gh/up/t3code.git" tag -d v0.1.10 > /dev/null
plan
check "plan: upstream deleted the tag the series was built on -> named, not 'reset to v0.1.9'" \
  eval 'has "no longer has the tag v0.1.10" "$root/report.md" && hasnt "Reset it to v0.1.9" "$root/report.md"'
new_upstream
new_fork v0.1.10
fork_tag v0.1.10-fork.1
(cd "$root/upwork" && git checkout -q -b moved v0.1.9 && echo again > u3.txt && git add -A && git commit -q -m u3b \
  && git tag -f v0.1.10 && git push -q --force https://github.com/up/t3code.git refs/tags/v0.1.10) > /dev/null 2>&1
plan
check "plan: upstream moved the tag the series was built on -> named" has "moved the tag v0.1.10" "$root/report.md"

# State: empty or tagless history. No stable tag upstream, or none inside the series.
new_upstream
new_fork v0.1.10
for t in v0.1.8 v0.1.9 v0.1.10; do git -C "$root/gh/up/t3code.git" tag -d "$t" > /dev/null; done
work git tag -d v0.1.8 v0.1.9 v0.1.10
plan
check "plan: upstream has no stable tag -> refused with a reason, nothing planned" \
  eval 'has "no stable release tag" "$root/report.md" && [[ -z "$(out action)" ]]'
new_upstream
new_fork v0.1.8
git -C "$root/gh/up/t3code.git" tag -d v0.1.8 > /dev/null
work git tag -d v0.1.8
plan
check "plan: the series contains no stable upstream tag -> refused with a reason" \
  eval 'has "contains no stable" "$root/report.md" && [[ -z "$(out action)" ]]'

# Major: a release was published, then the series branch did not move. The next plan moves the
# branch to that release instead of building and publishing another one.
new_upstream
new_fork v0.1.9
plan
released="$(out candidate)"
work git push -q origin "$released:refs/heads/fork-follow/candidate"
fork_tag v0.1.10-fork.1 "$released"
work git checkout -q main && work git reset -q --hard "$(series)"
plan
check "plan: a published release of the rebased files exists -> advance to it, build nothing" \
  eval 'action_is advance && [[ "$(out candidate)" == "$released" && "$(out released_tag)" == v0.1.10-fork.1 ]]'
work git reset -q --hard "$(series)"
(cd "$root/work" && echo more > more.txt && git add -A && git commit -q -m "fork: more" && git push -q origin main) > /dev/null 2>&1
plan
check "plan: a release of different files does not count -> rebase" action_is rebase

# Round 3: upstream workflow files new on the target release are named, so the branch move can
# refuse while GitHub does not list one. A rename counts as new; a same-base plan names none.
new_upstream
(cd "$root/upwork" && git checkout -q main && echo "on: push" > .github/workflows/nightly.yml \
  && git mv .github/workflows/ci.yml .github/workflows/ci-v2.yml && git add -A && git commit -q -m u5 \
  && git tag v0.1.11 && git push -q https://github.com/up/t3code.git main --tags) > /dev/null 2>&1
new_fork v0.1.10
plan
check "plan: a rebase names the upstream workflow files new on the target release (a rename too)" \
  eval 'action_is rebase && [[ "$(out new_workflows)" == .github/workflows/ci-v2.yml,.github/workflows/nightly.yml ]]'
new_fork v0.1.11
plan
check "plan: same base -> no new upstream workflow files" \
  eval 'action_is release && grep -qx "new_workflows=" "$root/plan.out"'

# Minor: upstream changed a reusable workflow the release calls; the run's own copy is older.
new_upstream
(cd "$root/upwork" && git checkout -q main && echo "on: workflow_call # v2" > .github/workflows/release-desktop.yml \
  && git add -A && git commit -q -m u5 && git tag v0.1.11 && git push -q https://github.com/up/t3code.git main --tags) > /dev/null 2>&1
new_fork v0.1.10
(cd "$root/work" && RUN_SHA="$(git rev-parse HEAD)" REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: changed release workflows -> stage, with the command that releases the candidate" \
  eval 'action_is stage && has "release-desktop.yml" "$root/report.md" && has "gh workflow run fork-release.yml" "$root/report.md"'

# Round 2: the same comparison on the release path. A re-run (or queued run) of an older run: its
# commit is the series on v0.1.10, while the series already sits on v0.1.11, whose
# release-desktop.yml differs. Releasing would build v0.1.11 with v0.1.10's release steps.
# The stage plan above left the rebased series (on v0.1.11) checked out; the fork's main is still
# the series on v0.1.10, the older run's commit. Make the rebased series the fork's main.
old_run="$(series)"
work git push -q --force origin HEAD:main
(cd "$root/work" && RUN_SHA="$old_run" REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: release path, the run's commit has other release workflows -> stage, not release" \
  eval 'action_is stage && [[ "$(out base_tag)" == v0.1.11 && "$(out candidate)" == "$(work_head)" ]] && has "release-desktop.yml" "$root/report.md"'
(cd "$root/work" && RUN_SHA="$(git rev-parse HEAD)" REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: release path, the run's own commit -> release" action_is release
rm -f "$root/plan.out"
(cd "$root/work" && RUN_SHA=1234567890123456789012345678901234567890 REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: the run's commit is not in the fork -> refused with a reason, nothing planned" \
  eval 'has "is not in the fork" "$root/report.md" && [[ -z "$(out action)" ]]'

# Nit: a release workflow that names a file missing at a commit makes `git show` fail inside a
# command substitution; that must not leave a stray "plan stopped" reason while the plan goes on.
new_upstream
new_fork v0.1.9
(cd "$root/work" && printf '    uses: ./.github/workflows/missing.yml\n' >> .github/workflows/fork-release.yml \
  && git add -A && git commit -q --amend --no-edit && git push -q --force origin main) > /dev/null 2>&1
plan
check "plan: a release workflow naming a missing file leaves no stray reason" \
  eval 'action_is rebase && [[ ! -e "$root/report.md" ]]'

# Nit: the schema check crashing is not reported as a schema change; a refusal still is.
new_upstream
new_fork v0.1.10
mkdir -p "$root/fakenode"
printf '#!/bin/sh\necho "Error: Cannot find module x (MODULE_NOT_FOUND)" >&2\nexit 1\n' > "$root/fakenode/node"
chmod +x "$root/fakenode/node"
(cd "$root/work" && PATH="$root/fakenode:$PATH" REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
rm -f "$root/plan.out"
check "plan: a crashing schema check is reported as a crash, not a schema change" \
  eval 'has "schema check crashed (exit 1)" "$root/report.md" && hasnt "changes the database schema" "$root/report.md" && [[ -z "$(out action)" ]]'
printf '#!/bin/sh\necho "Fork changes must not change the database schema (no down-migrations exist):" >&2\necho "- a.sql" >&2\nexit 1\n' > "$root/fakenode/node"
(cd "$root/work" && PATH="$root/fakenode:$PATH" REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: a schema check refusal is reported as a schema change" \
  eval 'has "The series changes the database schema" "$root/report.md" && has "a.sql" "$root/report.md"'

# --- pushes: fork-follow-push.sh -------------------------------------------------------------
new_upstream
new_fork v0.1.10
push() { (cd "$root/work" && REPORT="$root/report.md" GITHUB_OUTPUT="$root/push.out" PUSH_URL="${PUSH_URL:-$root/gh/me/t3code.git}" \
  BASE_TAG=v0.1.10 bash "$dir/fork-follow-push.sh" "$@") > "$root/out" 2>&1; }
rm -f "$root/report.md"
check "push: a missing FORK_PUSH_TOKEN fails and says so" eval '! push check && has "FORK_PUSH_TOKEN is missing" "$root/report.md"'
rm -f "$root/report.md"
check "push: a missing FORK_PUSH_TOKEN names the Environment that should hold it" \
  eval '! FORK_PUSH_ENVIRONMENT=fork-push push check && has "missing in the Environment fork-push" "$root/report.md" && has "deployment branches to main" "$root/report.md"'
export FORK_PUSH_TOKEN=test-token
check "push: a token GitHub refuses (expired) fails and says so" \
  eval '! PUSH_URL="$root/gh/me/gone.git" push check && has "refused FORK_PUSH_TOKEN" "$root/report.md"'

check "push: a working token passes the check" push check

prev="$(series)"
work git commit -q --allow-empty -m "fork: next"
next="$(work_head)"
# Minor: a refused candidate push (say, a token without Workflows write) leaves a reason.
hook="$root/gh/me/t3code.git/hooks/pre-receive"
printf '#!/bin/sh\necho "refusing to allow a token without workflows permission" >&2\nexit 1\n' > "$hook" && chmod +x "$hook"
rm -f "$root/report.md"
check "push: a refused candidate push fails with git's reason" \
  eval '! push candidate "$next" && has "Could not push the candidate" "$root/report.md" && has "workflows permission" "$root/report.md"'
rm -f "$hook"
push candidate "$next"
check "push: the candidate goes to fork-follow/candidate" eval '[[ "$(series fork-follow/candidate)" == "$next" ]] && has candidate_pushed=true "$root/push.out"'
push advance "$prev" "$next"
check "push: advance moves the series and keeps the old one as a tag" \
  eval '[[ "$(series)" == "$next" && "$(git -C "$root/gh/me/t3code.git" rev-parse "refs/tags/fork-follow/pre-v0.1.10-${prev:0:12}")" == "$prev" ]]'
push advance "$prev" "$next"
check "push: advance again after it moved (re-run) succeeds without a push" has moved=already "$root/push.out"
work git commit -q --allow-empty -m "fork: merged meanwhile"
work git push -q origin HEAD:main
moved="$(series)"
rm -f "$root/report.md"
push advance "$next" "$prev"
check "push: advance refuses when the branch moved since the plan, and leaves it" \
  eval '[[ "$(series)" == "$moved" ]] && has "moved since the plan" "$root/report.md"'
push cleanup
check "push: cleanup deletes the candidate branch" eval '! series fork-follow/candidate > /dev/null'
unset FORK_PUSH_TOKEN

# --- guard: fork-follow-guard.sh -----------------------------------------------------------------
guard() { REPORT="$root/report.md" FORK_PUSH_ENVIRONMENT=fork-push bash "$dir/fork-follow-guard.sh" > "$root/out" 2>&1; }
good_env() {
  reset_api
  echo '{"name":"fork-push","deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' > "$root/api/environment.json"
  echo '{"total_count":1,"branch_policies":[{"id":1,"name":"main","type":"branch"}]}' > "$root/api/branch-policies.json"
}
good_env
rm -f "$root/report.md"
check "guard: a repository or organisation secret FORK_PUSH_TOKEN -> refused, says to delete it" \
  eval '! REPO_SECRET_PRESENT=true guard && has "repository or organisation secret named FORK_PUSH_TOKEN exists" "$root/report.md" && has "Repository secrets" "$root/report.md"'
check "guard: the secret check's answer is missing -> refused" \
  eval '! REPO_SECRET_PRESENT= guard && has "could not tell" "$root/report.md"'
export REPO_SECRET_PRESENT=false
check "guard: only the Environment holds it and only main may deploy -> passes" guard
rm -f "$root/api/environment.json"
check "guard: the Environment does not exist -> refused, says how to create it" \
  eval '! guard && has "does not exist" "$root/report.md" && has "Selected branches and tags" "$root/report.md" && has "branch main" "$root/report.md"'
echo '{"name":"fork-push","deployment_branch_policy":null}' > "$root/api/environment.json"
check "guard: any branch may deploy (no rule, as GitHub auto-creates it) -> refused" \
  eval '! guard && has "Any branch may deploy" "$root/report.md" && has "Selected branches and tags" "$root/report.md"'
echo '{"name":"fork-push","deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}' > "$root/api/environment.json"
check "guard: protected branches only (not provably main alone) -> refused" \
  eval '! guard && has "Protected branches only" "$root/report.md"'
good_env
echo '{"total_count":2,"branch_policies":[{"id":1,"name":"main","type":"branch"},{"id":2,"name":"v*","type":"tag"}]}' > "$root/api/branch-policies.json"
check "guard: an extra tag rule -> refused, the rules listed" \
  eval '! guard && has "tag \`v\*\`" "$root/report.md" && has "exactly one rule" "$root/report.md"'
echo '{"total_count":1,"branch_policies":[{"id":1,"name":"main*","type":"branch"}]}' > "$root/api/branch-policies.json"
check "guard: a pattern rule (main*) -> refused" eval '! guard && has "main\*" "$root/report.md"'
echo '{"total_count":0,"branch_policies":[]}' > "$root/api/branch-policies.json"
check "guard: no rule at all -> refused" eval '! guard && has "(none)" "$root/report.md"'
# Round 3: a series branch that is itself a pattern, with the matching rule, is still refused.
echo '{"total_count":1,"branch_policies":[{"id":1,"name":"main*","type":"branch"}]}' > "$root/api/branch-policies.json"
check "guard: a series branch that is a pattern (main*), even with the rule 'branch main*' -> refused" \
  eval '! SERIES_BRANCH="main*" guard && has "must let only the branch main\*" "$root/report.md"'
good_env
check "guard: --environment-only (before the branch move) passes without the secret answer" \
  eval 'REPO_SECRET_PRESENT= REPORT="$root/report.md" FORK_PUSH_ENVIRONMENT=fork-push bash "$dir/fork-follow-guard.sh" --environment-only > "$root/out" 2>&1'
echo '{"name":"fork-push","deployment_branch_policy":null}' > "$root/api/environment.json"
check "guard: --environment-only still refuses an Environment any branch may deploy to" \
  eval '! REPORT="$root/report.md" FORK_PUSH_ENVIRONMENT=fork-push bash "$dir/fork-follow-guard.sh" --environment-only > "$root/out" 2>&1 && has "Any branch may deploy" "$root/report.md"'
good_env
touch "$root/api/fail_environments"
check "guard: the Environment cannot be read -> refused with gh's reason" \
  eval '! guard && has "Could not read the Environment" "$root/report.md" && has "502" "$root/report.md"'
unset REPO_SECRET_PRESENT

# --- workflows: fork-workflows.sh ------------------------------------------------------------
sweep() { rm -f "$root/sweep.out"; REPORT="$root/report.md" GITHUB_OUTPUT="$root/sweep.out" bash "$dir/fork-workflows.sh" > "$root/out" 2>&1; }
reset_api
cat > "$root/api/workflows.json" << 'J'
{"workflows":[
 {"id":1,"state":"active","path":".github/workflows/fork-follow.yml"},
 {"id":2,"state":"active","path":".github/workflows/web-preview.yml"},
 {"id":3,"state":"disabled_manually","path":".github/workflows/ci.yml"},
 {"id":4,"state":"active","path":"dynamic/pages/pages-build-deployment"}]}
J
cat > "$root/api/runs.json" << 'J'
{"workflow_runs":[
 {"id":11,"status":"queued","head_sha":"abc","event":"push","path":".github/workflows/web-preview.yml"},
 {"id":12,"status":"in_progress","head_sha":"abc","event":"push","path":".github/workflows/fork-tests.yml"},
 {"id":13,"status":"completed","conclusion":"success","head_sha":"abc","event":"push","path":".github/workflows/relay-preview.yml"},
 {"id":14,"status":"in_progress","head_sha":"def","event":"pull_request_target","path":".github/workflows/relay-preview-cleanup.yml@refs/heads/main"},
 {"id":15,"status":"completed","conclusion":"cancelled","head_sha":"abc","event":"push","path":".github/workflows/ci.yml"},
 {"id":16,"status":"completed","conclusion":"success","head_sha":"def","event":"schedule","path":".github/workflows/fork-tests.yml"}]}
J
cat > "$root/api/files.json" << 'J'
[{"type":"file","path":".github/workflows/fork-follow.yml"},
 {"type":"file","path":".github/workflows/web-preview.yml"},
 {"type":"file","path":".github/workflows/ci.yml"},
 {"type":"file","path":".github/workflows/release.yml"},
 {"type":"file","path":".github/workflows/README.md"},
 {"type":"dir","path":".github/workflows/scripts"}]
J
OWN_WORKFLOWS=.github/workflows/fork-follow.yml,.github/workflows/fork-tests.yml HEAD_SHA=abc SINCE=2026-11-12T06:23:00Z sweep
check "workflows: an active upstream workflow is disabled and named; own, disabled and dynamic ones are left" \
  eval 'has "workflows/2/disable" "$root/api/writes" && [[ $(grep -c disable "$root/api/writes") == 1 ]] && has "disabled=.github/workflows/web-preview.yml" "$root/sweep.out"'
check "workflows: unfinished runs of upstream workflows are cancelled, own and finished ones are not" \
  eval 'has "runs/11/cancel" "$root/api/writes" && hasnt "runs/12/cancel" "$root/api/writes" && hasnt "runs/13/cancel" "$root/api/writes"'
check "workflows: an upstream run from another event and commit (pull_request_target) is cancelled too" \
  eval 'has "runs/14/cancel" "$root/api/writes" && [[ $(grep -c cancel "$root/api/writes") == 2 ]]'
check "workflows: upstream runs that finished before the sweep are named with event and conclusion; own and cancelled ones are not" \
  eval '[[ "$(sed -n "s/^finished=//p" "$root/sweep.out")" == "13 (.github/workflows/relay-preview.yml: success after push)" ]]'
check "workflows: an unlisted upstream file from an earlier release is named apart, with no warning" \
  eval '[[ -z "$(sed -n "s/^unregistered=//p" "$root/sweep.out")" && "$(sed -n "s/^unregistered_old=//p" "$root/sweep.out")" == ".github/workflows/release.yml" ]] && hasnt "::warning::.*does not list yet" "$root/out"'
rm -f "$root/api/writes"
OWN_WORKFLOWS=.github/workflows/fork-follow.yml,.github/workflows/fork-tests.yml HEAD_SHA=abc NEW_WORKFLOWS=.github/workflows/release.yml sweep
check "workflows: an unlisted upstream file new on this release is named with a warning, not called disabled" \
  eval '[[ "$(sed -n "s/^unregistered=//p" "$root/sweep.out")" == ".github/workflows/release.yml" && -z "$(sed -n "s/^unregistered_old=//p" "$root/sweep.out")" ]] && has "::warning::.*new on this release.*does not list yet" "$root/out"'
rm -f "$root/api/writes" "$root/report.md"
check "workflows: before the move, a new unlisted upstream file refuses it, after disabling the listed ones" \
  eval '! OWN_WORKFLOWS=.github/workflows/fork-follow.yml HEAD_SHA=abc NEW_WORKFLOWS=.github/workflows/release.yml REQUIRE_NEW_OFF=true sweep && has "main was not moved" "$root/report.md" && has "release.yml" "$root/report.md" && has "A person decides" "$root/report.md" && has "workflows/2/disable" "$root/api/writes"'
rm -f "$root/report.md"
check "workflows: before the move, a new upstream file GitHub lists (now disabled) and older unlisted ones do not refuse it" \
  eval 'OWN_WORKFLOWS=.github/workflows/fork-follow.yml HEAD_SHA=abc NEW_WORKFLOWS=.github/workflows/web-preview.yml REQUIRE_NEW_OFF=true sweep && [[ ! -e "$root/report.md" ]]'
reset_api
OWN_WORKFLOWS="" sweep
check "workflows: an empty own list disables nothing (refusing to guess)" eval '[[ ! -e "$root/api/writes" ]] && has "refusing to guess" "$root/report.md"'
touch "$root/api/fail_workflows"
OWN_WORKFLOWS=.github/workflows/fork-follow.yml sweep
check "workflows: a failed listing fails with a reason" has "Could not list the workflows" "$root/report.md"

# --- report: fork-follow-report.sh -----------------------------------------------------------
new_upstream
new_fork v0.1.10
report() { # report KEY=VALUE...: the summary in $root/summary.md, status in $root/status
  rm -f "$root/summary.md"
  (cd "$root/work" && env GITHUB_STEP_SUMMARY="$root/summary.md" RUN_URL=run REPORTS="$root/reports" ORIGIN="$root/gh/me/t3code.git" "$@" \
    bash "$dir/fork-follow-report.sh") > "$root/out" 2>&1
  echo $? > "$root/status"
}
failed_run() { [[ "$(cat "$root/status")" != 0 ]]; }
rm -rf "$root/reports" && mkdir -p "$root/reports"
echo "Rebasing the series from v0.1.9 onto v0.1.10 stopped at: abc fork: x" > "$root/reports/plan.md"
reset_api false
report ACTION= PLAN_RESULT=failure
check "report: Issues disabled -> the reason is in the summary, no issue call, the run fails" \
  eval 'failed_run && has "stopped at" "$root/summary.md" && has "Issues are disabled" "$root/summary.md" && hasnt "issue" "$root/api/writes"'
check "report: Issues enabled -> one issue is opened on failure" \
  eval 'reset_api true; report ACTION= PLAN_RESULT=failure; failed_run && has "issue create" "$root/api/writes"'

# Major: release published, then the series branch did not move.
rm -rf "$root/reports" && mkdir -p "$root/reports"
echo "main moved since the plan" > "$root/reports/advance.md"
prev="$(series)"
work git commit -q --allow-empty -m "fork: rebased"
cand="$(work_head)"
fork_tag v0.1.10-fork.2 "$cand"
reset_api false
echo '{"draft":false}' > "$root/api/release-v0.1.10-fork.2"
report ACTION=rebase PLAN_RESULT=success RELEASE_RESULT=success ADVANCE_RESULT=failure \
  BASE_TAG=v0.1.10 PREVIOUS_TAG=v0.1.9 PREVIOUS_HEAD="$prev" CANDIDATE="$cand"
check "report: published but not moved -> says both, never 'nothing was published'" \
  eval 'failed_run && has "v0.1.10-fork.2 \*\*published\*\*" "$root/summary.md" && has "not moved" "$root/summary.md" && hasnt -i "nothing was published" "$root/summary.md"'
rm -rf "$root/reports" && mkdir -p "$root/reports"
report ACTION=rebase PLAN_RESULT=success RELEASE_RESULT=failure ADVANCE_RESULT=skipped \
  BASE_TAG=v0.1.11 PREVIOUS_HEAD="$prev" CANDIDATE="$(git -C "$root/work" rev-parse HEAD~1)"
check "report: a failed release with no tag -> 'none published', read from the repository" \
  eval 'failed_run && has "none published" "$root/summary.md" && has "A job failed without writing a reason" "$root/summary.md"'

# Minor: an issue opened by a failure closes once a run finds nothing to do (fixed by hand).
reset_api true
echo 7 > "$root/api/issues"
echo '{"draft":false}' > "$root/api/release-v0.1.10-fork.2"
work git push -q --force origin "$cand:refs/heads/main"
report ACTION=none PLAN_RESULT=success RELEASE_RESULT=skipped ADVANCE_RESULT=skipped BASE_TAG=v0.1.10 \
  CANDIDATE="$cand" RELEASED_TAG=v0.1.10-fork.2
check "report: action none closes an open follow issue and the run passes" \
  eval '! failed_run && has "issue close" "$root/api/writes"'
reset_api false
echo "The release workflows differ" > "$root/reports/plan.md"
report ACTION=stage PLAN_RESULT=failure CANDIDATE="$cand"
check "report: stage fails the run with the reason" eval 'failed_run && has "release workflows differ" "$root/summary.md"'
reset_api false
report ACTION=none PLAN_RESULT=success CANDIDATE="$cand" DISABLED=",.github/workflows/web-preview.yml,,.github/workflows/web-preview.yml" SWEEP_RESULT=success
check "report: names each disabled upstream workflow once" \
  eval '[[ $(grep -o "web-preview.yml" "$root/summary.md" | wc -l) == 1 ]]'
reset_api false
report ACTION=none PLAN_RESULT=success CANDIDATE="$cand" SWEEP_RESULT=success \
  FINISHED="13 (.github/workflows/relay-preview.yml: success after push)" UNREGISTERED=".github/workflows/release.yml"
check "report: names upstream runs that finished first and files GitHub does not list yet" \
  eval 'has "finished before a sweep could stop them" "$root/summary.md" && has "relay-preview.yml: success after push" "$root/summary.md" && has "does not list yet" "$root/summary.md" && has "release.yml" "$root/summary.md"'
reset_api false
echo '{"draft":false}' > "$root/api/release-v0.1.10-fork.2"
report ACTION=none PLAN_RESULT=success CANDIDATE="$cand" SWEEP_RESULT=success UNREGISTERED_OLD=".github/workflows/publish-aur.yml"
check "report: unlisted upstream files from earlier releases go in a collapsed note, not a bold row, and pass" \
  eval '! failed_run && has "<details>" "$root/summary.md" && has "publish-aur.yml" "$root/summary.md" && hasnt "\*\*.*publish-aur" "$root/summary.md"'
# Round 3: "Re-run failed jobs" keeps the guard's earlier result; the token jobs skip themselves.
rm -rf "$root/reports" && mkdir -p "$root/reports"
reset_api false
report ACTION= GUARD_RESULT=success GUARD_ATTEMPT=1 RUN_ATTEMPT=2 PLAN_RESULT=skipped
check "report: the guard passed in an earlier attempt, the plan skipped -> fails, says to re-run all jobs" \
  eval 'failed_run && has "did not run in this attempt" "$root/summary.md" && has "Re-run all jobs" "$root/summary.md"'
report ACTION=rebase GUARD_RESULT=success GUARD_ATTEMPT=1 RUN_ATTEMPT=2 PLAN_RESULT=success RELEASE_RESULT=success \
  ADVANCE_RESULT=skipped BASE_TAG=v0.1.10 PREVIOUS_HEAD="$prev" CANDIDATE="$cand"
check "report: released, but the move skipped for an earlier attempt's guard -> fails and says why" \
  eval 'failed_run && has "guard passed in attempt 1" "$root/summary.md"'
echo '{"draft":false}' > "$root/api/release-v0.1.10-fork.2"
report ACTION=none GUARD_RESULT=success GUARD_ATTEMPT=1 RUN_ATTEMPT=2 PLAN_RESULT=success CANDIDATE="$cand" RELEASED_TAG=v0.1.10-fork.2
check "report: a re-run of the report alone after a good run does not cry wolf" eval '! failed_run'
rm -rf "$root/reports" && mkdir -p "$root/reports"
echo "A repository or organisation secret named FORK_PUSH_TOKEN exists" > "$root/reports/guard.md"
report ACTION= GUARD_RESULT=failure PLAN_RESULT=skipped
check "report: the guard refused -> the run fails with its reason, and says no job got the token" \
  eval 'failed_run && has "secret named FORK_PUSH_TOKEN exists" "$root/summary.md" && has "guard refused" "$root/summary.md"'

# --- gh: no script reaches GitHub outside a run (fork-gh-guard.sh) ------------------------------
# A gh that is not the stub (a test file run without it, an older test, a person running a script
# by hand) answers like GitHub and records every call. Each script that calls gh must refuse before
# it calls any gh: neither this one nor the stub.
mkdir -p "$root/decoy" "$root/nomarker" "$root/reports"
cat > "$root/decoy/gh" << 'D'
#!/usr/bin/env bash
echo "gh $*" >> "$DECOY_CALLS"
echo '{"workflows":[{"id":9,"state":"active","path":".github/workflows/fork-tests.yml"}]}'
D
chmod +x "$root/decoy/gh"
grep -vxF "$FORK_GH_STUB_MARKER" "$root/bin/gh" > "$root/nomarker/gh" && chmod +x "$root/nomarker/gh"
gh_scripts=(fork-workflows.sh fork-follow-guard.sh fork-follow-report.sh)
refuses() { # refuses ENV...: every gh script, run with ENV, fails, says why, and calls no gh
  local s
  for s in "${gh_scripts[@]}"; do
    rm -f "$root/decoy.calls" "$root/api/calls" "$root/report.md"
    if (cd "$root/work" && env DECOY_CALLS="$root/decoy.calls" OWN_WORKFLOWS=.github/workflows/fork-follow.yml \
      REPORT="$root/report.md" REPO_SECRET_PRESENT=false FORK_PUSH_ENVIRONMENT=fork-push REPORTS="$root/reports" \
      ACTION=none PLAN_RESULT=success "$@" bash "$dir/$s") > "$root/out" 2>&1; then
      echo "$s passed" >> "$root/out"
      return 1
    fi
    [[ ! -e "$root/decoy.calls" && ! -e "$root/api/calls" ]] || { echo "$s called gh" >> "$root/out"; return 1; }
    grep -q "Refusing to run $s" "$root/out" || { echo "$s gave no refusal" >> "$root/out"; return 1; }
  done
}
reset_api
check "gh: a test run without the stub first on PATH fails closed and never calls gh" \
  refuses PATH="$root/decoy:$PATH"
check "gh: run by hand (no test mode, not in a GitHub Actions run) refuses before any gh call" \
  refuses FORK_TEST_MODE= PATH="$root/decoy:$PATH"
check "gh: test mode with a GitHub token set refuses, even with the stub" refuses GH_TOKEN=x
check "gh: test mode with GITHUB_TOKEN set refuses, even with the stub" refuses GITHUB_TOKEN=x
check "gh: test mode with GH_HOST github.com refuses" refuses GH_HOST=github.com
check "gh: a gh named as the stub but without its marker line refuses" \
  refuses PATH="$root/nomarker:$PATH" FORK_GH_STUB="$root/nomarker/gh"
rm -f "$root/api/calls"
check "gh: in a GitHub Actions run (not a test) the scripts use gh" \
  eval '(cd "$root/work" && env -u FORK_TEST_MODE GITHUB_ACTIONS=true OWN_WORKFLOWS=.github/workflows/fork-follow.yml \
    REPORT="$root/report.md" GITHUB_OUTPUT="$root/sweep.out" bash "$dir/fork-workflows.sh") > "$root/out" 2>&1 && has "actions/workflows" "$root/api/calls"'
# A script that changes PATH after it started still cannot reach another gh: every call checks.
check "gh: each gh call checks again (PATH changed after the start) and refuses" \
  eval '! (. "$here/fork-gh-guard.sh"; PATH="$root/decoy:$PATH"; DECOY_CALLS="$root/decoy.calls" gh api user) > "$root/out" 2>&1 && [[ ! -e "$root/decoy.calls" ]] && has "Refusing to call gh" "$root/out"'

# --- the workflow file ---------------------------------------------------------------------------
desktop="$here/../workflows/release-desktop.yml"
declared="$(sed -n '/^  workflow_call:/,/^    inputs:/p' "$desktop" | sed -n 's/^      \([A-Z_]*\):$/\1/p' | sort)"
passed="$(sed -n '/^  release:/,/^  [a-z_]*:$/p' "$workflow" | sed -n 's/^      \([A-Z_]*\): \${{ secrets\.\([A-Z_]*\) }}$/\1=\2/p' | sort)"
check "workflow: release gets exactly the signing secrets by name, no 'secrets: inherit'" \
  eval '! grep -q "secrets: inherit" "$workflow" && [[ -n "$declared" && "$passed" == "$(sed "s/.*/&=&/" <<< "$declared")" ]]'
# The guard's presence check is the only other mention, and it yields true/false, never the value.
presence="REPO_SECRET_PRESENT: \${{ secrets.FORK_PUSH_TOKEN != '' }}"
check "workflow: FORK_PUSH_TOKEN reaches only fork-follow-push.sh steps, never a checkout" \
  eval '! grep -q "token: \${{ secrets.FORK_PUSH_TOKEN" "$workflow" && [[ $(grep "secrets.FORK_PUSH_TOKEN" "$workflow" | grep -cvF "$presence") == $(grep -cE "^\s*(run: )?bash \.github/scripts/fork-follow-push\.sh" "$workflow") ]]'
guard_job="$(sed -n '/^  guard:$/,/^  [a-z_-]*:$/p' "$workflow" | sed '$d')"
check "workflow: a Guard job without an Environment sees only whether a repository/organisation secret exists, and runs the guard" \
  eval '[[ -n "$guard_job" ]] && ! grep -q "environment:" <<< "$guard_job" && [[ $(grep -cF "$presence" "$workflow") == 1 ]] && grep -qF "$presence" <<< "$guard_job" && grep -q "fork-follow-guard.sh" <<< "$guard_job" && grep -q "actions: read" <<< "$guard_job"'
plan_head="$(sed -n "/^  plan:$/,/^    steps:$/p" "$workflow")"
advance_head="$(sed -n "/^  advance:$/,/^    steps:$/p" "$workflow")"
attempt_check="needs.guard.outputs.attempt == github.run_attempt"
check "workflow: the plan (the first job with the token) waits for the guard; advance waits for the guard and the plan" \
  eval 'grep -qx "    needs: \[guard\]" <<< "$plan_head" && grep -qx "    needs: \[guard, plan, release\]" <<< "$advance_head"'
# Round 3: the plan's only job-level condition is this attempt's guard, with the implied success():
# always(), !cancelled() or failure() there would let it run after the guard refused.
check "workflow: the plan runs only after a guard that passed in this attempt (no always/cancelled/failure)" \
  eval '[[ "$(grep -c "^    if:" <<< "$plan_head")" == 1 ]] && grep -qxF "    if: $attempt_check" <<< "$plan_head" && ! grep -qE "always\(\)|cancelled\(\)|failure\(\)" <<< "$plan_head"'
check "workflow: advance needs the plan's success and this attempt's guard" \
  eval 'grep -qF "needs.plan.result == '"'"'success'"'"'" <<< "$advance_head" && grep -qF "$attempt_check" <<< "$advance_head" && ! grep -qE "always\(\)|failure\(\)" <<< "$advance_head"'
check "workflow: the guard's attempt output is set only by a step after the guard script" \
  eval 'grep -qF "attempt: \${{ steps.passed.outputs.attempt }}" <<< "$guard_job" && awk "/fork-follow-guard.sh/ { g = NR } /id: passed/ { p = NR } /attempt=\\\$GITHUB_RUN_ATTEMPT/ { a = NR } END { exit !(g && p > g && a > p) }" <<< "$guard_job"'
advance_job="$(sed -n '/^  advance:$/,/^  [a-z_-]*:$/p' "$workflow" | sed '$d')"
check "workflow: before the move, advance checks the Environment again and sweeps, refusing new unlisted files" \
  eval 'awk "/fork-follow-guard.sh --environment-only/ { g = NR } /REQUIRE_NEW_OFF: \"true\"/ { r = NR } /NEW_WORKFLOWS: .*new_workflows/ && !n { n = NR } /fork-follow-push.sh advance/ { m = NR } END { exit !(g && r && n && m && g < r && r < m && n < m) }" <<< "$advance_job"'
check "workflow: the move step runs only when the checks before it passed (no if:)" \
  eval 'awk "/- name: Move the series branch/ { s = 1; next } s && /^      - / { exit } s && /^        if:/ { bad = 1 } END { exit bad }" <<< "$advance_job"'
check "workflow: action none deletes a leftover candidate branch" \
  eval 'grep -B2 -A3 "action == .none." "$workflow" | grep -q "fork-follow-push.sh cleanup"'
check "workflow: the last sweep reports runs since the run began" \
  eval 'sed -n "/^  report:/,\$p" "$workflow" | grep -q "SINCE: \${{ needs.guard.outputs.started }}"'
# Each job that reads FORK_PUSH_TOKEN runs in the Environment that holds it, named as in env.
token_jobs="$(grep -vF "$presence" "$workflow" | awk '/^  [a-z_-]+:$/ { job = $1 } /secrets\.FORK_PUSH_TOKEN/ { print job }' | sort -u)"
env_jobs="$(awk '/^  [a-z_-]+:$/ { job = $1 } /^    environment: fork-push$/ { print job }' "$workflow" | sort -u)"
check "workflow: the jobs that read FORK_PUSH_TOKEN, and only they, run in the Environment fork-push" \
  eval '[[ -n "$token_jobs" && "$token_jobs" == "$env_jobs" ]] && grep -qx "  FORK_PUSH_ENVIRONMENT: fork-push" "$workflow"'
check "workflow: every checkout drops its credentials" \
  eval '[[ $(grep -c "actions/checkout@" "$workflow") == $(grep -c "persist-credentials: false" "$workflow") ]]'
check "workflow: every action is pinned by commit" \
  eval '! grep -E "^\s*-?\s*uses: [^.][^@]*@" "$workflow" | grep -vqE "@[0-9a-f]{40}( |$)"'
check "workflow: the report runs on every run (also action none)" \
  eval 'sed -n "/^  report:/,\$p" "$workflow" | grep -q "^    if: always()$"'
check "workflow: upstream workflows are swept after each push" \
  eval '[[ $(grep -c "fork-workflows.sh" "$workflow") -ge 3 ]]'

echo
if (( failures )); then
  echo "$failures failed"
  exit 1
fi
echo "all passed"
