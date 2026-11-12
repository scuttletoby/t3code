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
  SERIES_BRANCH FORK_PUSH_TOKEN OWN_WORKFLOWS HEAD_SHA

# Fake gh, served from $root/api:
#   gh api [-X M] PATH [--paginate] [--jq EXPR]     gh issue list|comment|create|close ...
# A file $root/api/fail_<word> makes every call whose path contains <word> fail.
mkdir -p "$root/bin" "$root/api"
cat > "$root/bin/gh" << 'GH'
#!/usr/bin/env bash
set -euo pipefail
api="$FAKE_API"
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
  "GET repos/me/t3code/actions/runs?head_sha="*) body="$(cat "$api/runs.json")" ;;
  "POST repos/me/t3code/actions/runs/"*/cancel) echo "cancel $path" >> "$api/writes"; exit 0 ;;
  "GET repos/me/t3code/releases/tags/"*)
    tag="${path##*/}"
    [[ -f "$api/release-$tag" ]] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    body="$(cat "$api/release-$tag")" ;;
  *) echo "fake gh: no route for $method $path" >&2; exit 1 ;;
esac
if [[ -n "$jq" ]]; then jq -r "$jq" <<< "$body"; else echo "$body"; fi
GH
chmod +x "$root/bin/gh"
export PATH="$root/bin:$PATH" FAKE_API="$root/api"

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

# Minor: upstream changed a reusable workflow the release calls; the run's own copy is older.
new_upstream
(cd "$root/upwork" && git checkout -q main && echo "on: workflow_call # v2" > .github/workflows/release-desktop.yml \
  && git add -A && git commit -q -m u5 && git tag v0.1.11 && git push -q https://github.com/up/t3code.git main --tags) > /dev/null 2>&1
new_fork v0.1.10
(cd "$root/work" && RUN_SHA="$(git rev-parse HEAD)" REPORT="$root/report.md" GITHUB_OUTPUT="$root/plan.out" \
  bash "$dir/fork-follow.sh") > "$root/out" 2>&1
check "plan: changed release workflows -> stage, with the command that releases the candidate" \
  eval 'action_is stage && has "release-desktop.yml" "$root/report.md" && has "gh workflow run fork-release.yml" "$root/report.md"'

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

# --- workflows: fork-workflows.sh ------------------------------------------------------------
sweep() { REPORT="$root/report.md" GITHUB_OUTPUT="$root/sweep.out" bash "$dir/fork-workflows.sh" > "$root/out" 2>&1; }
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
 {"id":11,"status":"queued","path":".github/workflows/web-preview.yml"},
 {"id":12,"status":"in_progress","path":".github/workflows/fork-tests.yml"},
 {"id":13,"status":"completed","path":".github/workflows/relay-preview.yml"}]}
J
OWN_WORKFLOWS=.github/workflows/fork-follow.yml,.github/workflows/fork-tests.yml HEAD_SHA=abc sweep
check "workflows: an active upstream workflow is disabled and named; own, disabled and dynamic ones are left" \
  eval 'has "workflows/2/disable" "$root/api/writes" && [[ $(grep -c disable "$root/api/writes") == 1 ]] && has "disabled=.github/workflows/web-preview.yml" "$root/sweep.out"'
check "workflows: unfinished runs of upstream workflows are cancelled, own and finished ones are not" \
  eval 'has "runs/11/cancel" "$root/api/writes" && [[ $(grep -c cancel "$root/api/writes") == 1 ]]'
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

# --- the workflow file ---------------------------------------------------------------------------
desktop="$here/../workflows/release-desktop.yml"
declared="$(sed -n '/^  workflow_call:/,/^    inputs:/p' "$desktop" | sed -n 's/^      \([A-Z_]*\):$/\1/p' | sort)"
passed="$(sed -n '/^  release:/,/^  [a-z_]*:$/p' "$workflow" | sed -n 's/^      \([A-Z_]*\): \${{ secrets\.\([A-Z_]*\) }}$/\1=\2/p' | sort)"
check "workflow: release gets exactly the signing secrets by name, no 'secrets: inherit'" \
  eval '! grep -q "secrets: inherit" "$workflow" && [[ -n "$declared" && "$passed" == "$(sed "s/.*/&=&/" <<< "$declared")" ]]'
check "workflow: FORK_PUSH_TOKEN reaches only fork-follow-push.sh steps, never a checkout" \
  eval '! grep -q "token: \${{ secrets.FORK_PUSH_TOKEN" "$workflow" && [[ $(grep -c "secrets.FORK_PUSH_TOKEN" "$workflow") == $(grep -cE "^\s*(run: )?bash \.github/scripts/fork-follow-push\.sh" "$workflow") ]]'
# Each job that reads FORK_PUSH_TOKEN runs in the Environment that holds it, named as in env.
token_jobs="$(awk '/^  [a-z_-]+:$/ { job = $1 } /secrets\.FORK_PUSH_TOKEN/ { print job }' "$workflow" | sort -u)"
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
