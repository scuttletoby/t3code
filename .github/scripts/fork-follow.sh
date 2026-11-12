#!/usr/bin/env bash
# Plan step of fork-follow.yml: decides what the run does, and rebases the fork's patch series onto
# a newer stable upstream tag when there is one. Run in a full clone with the series checked out.
# It pushes and publishes nothing itself. Tests: fork-follow.test.sh.
#
# Environment: UPSTREAM (owner/name), UPSTREAM_URL (git URL of upstream, default GitHub), ORIGIN
# (git remote or URL of this fork, default origin), RUN_SHA (the commit this run's workflow files
# come from, default HEAD), REPOSITORY (owner/name of this fork, for the advice in reports), REPORT
# (file that receives the reason when the run cannot go on).
#
# Output, key=value lines to $GITHUB_OUTPUT (stdout without it):
#   action         none     the series head is the newest fork release of its base already
#                  release  same base, but the series has commits no fork release has
#                  rebase   rebased onto a newer tag: release the candidate, then move the series
#                  advance  rebased onto a newer tag, and a published fork release already has the
#                           candidate's exact files: move the series to that release, build nothing
#                  stage    the release workflows of this run's commit differ from the candidate's
#                           (upstream changed them, or this is a re-run of an older run): push the
#                           candidate and stop, so it is released with its own files (see $REPORT)
#   base_tag, base_commit  the upstream tag (and its commit) the candidate sits on
#   previous_tag, previous_head  where the series was (rebase, advance, stage)
#   candidate      the commit to release or to move the series to
#   released_tag   the fork release that already has the candidate's files (none, advance)
#   dropped        subjects of patches that became empty on the new base (';'-separated)
#   own_workflows  workflow files the series itself adds (','-separated); every other one is
#                  upstream's and gets disabled
#   new_workflows  upstream workflow files added or changed between the series' old base and the
#                  new one (','-separated; empty without a new base). The branch move refuses while
#                  one of them is not listed by GitHub yet, so it cannot be disabled before main has
#                  it (a changed file may now run on push).
#
# Removed upstream workflows: .github/fork-removed-workflows.txt (read from the series head) lists
# upstream workflow files the fork deletes. A rebase conflict on listed files only is resolved by
# deleting them; any listed file the rebased candidate still has (upstream re-added it) is deleted
# by one more commit. On the same base the series must not have one. A missing or malformed list
# stops the plan: it never ships the files because it could not read which ones to delete.
# A workflow file the new upstream release adds that someone outside the fork can start (see
# outside_events) and that the list does not delete stops the plan too, naming it.
set -Eeuo pipefail

: "${UPSTREAM:?owner/name of the upstream repository}"
REPORT="${REPORT:-fork-follow-report.md}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"
ORIGIN="${ORIGIN:-origin}"
REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:-this repository}}"
logs="${RUNNER_TEMP:-$(mktemp -d)}"
upstream_url="${UPSTREAM_URL:-https://github.com/$UPSTREAM.git}"
reported=""

fail() {
  printf '%s\n' "$@" > "$REPORT"
  cat "$REPORT" >&2
  reported=1
  exit 1
}

# A command that fails outside fail() (set -e) still leaves a reason that names the step.
on_error() {
  local status="$1" line="$2" command="$3"
  # Only the script's own shell reports: a command substitution inherits the trap (set -E), and a
  # failure there that matters fails the line that uses it, which is reported here.
  ((BASH_SUBSHELL == 0)) || return 0
  [[ -z "$reported" ]] || return 0
  reported=1
  printf '%s\n' "The plan stopped at fork-follow.sh line $line (exit $status): \`$command\`" \
    "This is not one of the planned refusals; see the run log." > "$REPORT"
  cat "$REPORT" >&2
}
trap 'on_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

stable_re='^v[0-9]+\.[0-9]+\.[0-9]+$'
fork_re='^v[0-9]+\.[0-9]+\.[0-9]+-fork\.[0-9]+$'
head="$(git rev-parse HEAD)"
run_sha="${RUN_SHA:-$head}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Read-only fetches into refs this run alone uses: upstream's tags and default branch, and this
# fork's tags (its releases).
git fetch --quiet --no-tags "$upstream_url" "+refs/tags/v*:refs/upstream-tags/v*" "+HEAD:refs/upstream-head" \
  > "$logs/fetch.log" 2>&1 \
  || fail "Could not fetch the tags and default branch of $UPSTREAM ($upstream_url):" '```' "$(tail -n 20 "$logs/fetch.log")" '```'
git fetch --quiet --no-tags "$ORIGIN" "+refs/tags/v*:refs/fork-tags/v*" > "$logs/fetch.log" 2>&1 \
  || fail "Could not fetch the tags of this fork ($ORIGIN):" '```' "$(tail -n 20 "$logs/fetch.log")" '```'

upstream_tags="$(git for-each-ref --format='%(refname:strip=2)' refs/upstream-tags | grep -E "$stable_re" | sort -rV || true)"
fork_tags="$(git for-each-ref --format='%(refname:strip=2)' refs/fork-tags | grep -E "$fork_re" | sort -rV || true)"
target_tag="$(sed -n 1p <<< "$upstream_tags")"
[[ -n "$target_tag" ]] || fail "$UPSTREAM has no stable release tag (vX.Y.Z)."
up() { git rev-parse "refs/upstream-tags/$1^{commit}"; }

# The newest fork release in the series' history names the upstream tag the series was built on.
# If upstream deleted or moved that tag, the base found below would be an older tag (or upstream's
# rewritten commits would count as the fork's own), so stop and name the tag instead.
last_release=""
for tag in $fork_tags; do
  if git merge-base --is-ancestor "refs/fork-tags/$tag^{commit}" HEAD; then
    last_release="$tag"
    break
  fi
done
if [[ -n "$last_release" ]]; then
  built_on="${last_release%-fork.*}"
  if ! git rev-parse --quiet --verify "refs/upstream-tags/$built_on^{commit}" > /dev/null; then
    fail "$UPSTREAM no longer has the tag $built_on, which the series was built on (fork release $last_release is in its history)." \
      "Upstream deleted or renamed that release. Decide by hand which upstream release the series should sit on; the series was left as it is."
  fi
  if ! git merge-base --is-ancestor "$(up "$built_on")" HEAD; then
    fail "$UPSTREAM moved the tag $built_on, which the series was built on (fork release $last_release is in its history), to a commit the series does not contain." \
      "Decide by hand whether to rebase onto the moved tag; the series was left as it is."
  fi
fi

# The series sits on the newest stable upstream tag it contains.
base_tag=""
for tag in $upstream_tags; do
  if git merge-base --is-ancestor "$(up "$tag")" HEAD; then
    base_tag="$tag"
    base="$(up "$tag")"
    break
  fi
done
[[ -n "$base_tag" ]] || fail "The series ($head) contains no stable $UPSTREAM tag."

# Every commit between that tag and the series head must be the fork's own. Upstream commits there
# mean the branch is not a patch series (for example it still equals upstream's default branch).
series_count="$(git rev-list --count "$base..HEAD")"
own_count="$(git rev-list --count "$base..HEAD" --not refs/upstream-head)"
if [[ "$series_count" != "$own_count" ]]; then
  fail "The series holds $((series_count - own_count)) upstream commits after $base_tag." \
    "Reset it to $base_tag plus the fork's own commits. The series was left as it is."
fi

# A rebase flattens merge commits, and a change made in a merge itself (a conflict resolved in the
# merge, an edited merge) would be lost without a trace. The series must be plain commits.
merges="$(git rev-list --merges "$base..HEAD")"
if [[ -n "$merges" ]]; then
  fail "The series holds merge commits after $base_tag:" \
    "$(git log --no-walk --format='- %h %s' $merges)" "" \
    "A rebase would flatten them and drop any change made in the merge itself, so the series was left as it is." \
    "Rebuild it as plain commits: check that \`git show --remerge-diff <merge>\` prints no change for each merge (or keep that change as its own commit)," \
    "run \`git rebase --onto $base_tag $base_tag <series branch>\` and force-push it. Merge fork pull requests only with squash or rebase."
fi

# The schema check exits 1 both when it refuses and when node crashes; only a refusal prints this.
schema_refusal="Fork changes must not change the database schema"
schema_check() { # schema_check BASE WHAT: fails with the check's output, a refusal or a crash
  local status=0
  node .github/scripts/fork-schema-check.cjs "$1" HEAD > "$logs/schema-check.log" 2>&1 || status=$?
  ((status != 0)) || return 0
  if ((status == 1)) && grep -qF "$schema_refusal" "$logs/schema-check.log"; then
    fail "$2 changes the database schema:" '```' "$(cat "$logs/schema-check.log")" '```'
  fi
  fail "The schema check crashed (exit $status), so it did not say whether the series touches the database schema:" \
    '```' "$(tail -n 20 "$logs/schema-check.log")" '```' "The series was left as it is."
}
schema_check "$base" "The series"

# The upstream workflow files the fork deletes, from the series head. Fail closed: without a
# readable list the plan cannot keep them deleted, so it stops instead of shipping them.
removed_file=.github/fork-removed-workflows.txt
removed_re='^\.github/workflows/[^/,[:space:]]+\.ya?ml$'
git cat-file -e "$head:$removed_file" 2> /dev/null \
  || fail "The series has no $removed_file, the list of upstream workflow files the fork deletes." \
    "Without it the plan cannot keep them deleted, so it stopped; the series was left as it is." \
    "Restore the file with a fork patch (an empty list holds only comment lines)."
removed=()
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
  if [[ ! "$line" =~ $removed_re || "$line" =~ ^\.github/workflows/fork- ]]; then
    fail "$removed_file has a line that is not an upstream workflow path (.github/workflows/<name>.yml, not fork-*):" \
      "\`$line\`" "The series was left as it is. Fix the line with a fork patch."
  fi
  removed+=("$line")
done < <(git show "$head:$removed_file")
is_removed() { local p; for p in "${removed[@]}"; do [[ "$p" != "$1" ]] || return 0; done; return 1; }
present_removed() { # present_removed COMMIT: the listed files COMMIT has, one per line
  local p
  for p in "${removed[@]}"; do
    git cat-file -e "$1:$p" 2> /dev/null && printf '%s\n' "$p"
  done
  return 0
}

# Events someone outside the fork can start a workflow with. GitHub runs such a workflow before
# the sweep can see and disable it, so a new one must not reach the series. Directly: an outside
# pull request (pull_request_target, main's copy), a review or a review comment on a pull request
# from a fork/* branch (pull_request_review, pull_request_review_comment: that branch's copy, with
# the repository's token), an issue or a comment (issues, issue_comment), a discussion
# (discussion, discussion_comment), a star (watch), a fork, a wiki edit if the wiki is open
# (gollum). Indirectly: a workflow of a given name that an outside pull request can add
# (workflow_run), and the checks, commit statuses and deployments that apps post on an outside
# pull request's commits (check_run, check_suite, status, deployment_status). pull_request from
# another fork gets a read-only token and no secrets, so the sweep is enough for it. The rest need
# write access or more.
outside_events='^(pull_request_target|pull_request_review|pull_request_review_comment|issues|issue_comment|discussion|discussion_comment|watch|fork|gollum|workflow_run|check_run|check_suite|status|deployment_status)$'
refuse_outside_workflows() { # refuse_outside_workflows UPSTREAM_BASE UPSTREAM_TARGET TAG CANDIDATE
  local path list=() found=() events status
  workflow_paths A "$1" "$2"
  IFS=, read -r -a list <<< "$paths"
  for path in "${list[@]}"; do
    [[ "$path" == *.yml || "$path" == *.yaml ]] || continue
    git cat-file -e "$4:$path" 2> /dev/null || continue
    git show "$4:$path" > "$logs/workflow.yml"
    status=0
    events="$(node "$here/fork-workflow-triggers.cjs" "$logs/workflow.yml" 2> "$logs/triggers.log")" || status=$?
    if ((status != 0)); then
      found+=("- \`$path\`: its triggers could not be read ($(tr '\n' ' ' < "$logs/triggers.log"| sed 's/ *$//'))")
    elif grep -qE "$outside_events" <<< "$events" || grep -qwE 'pull_request_target|workflow_run' "$logs/workflow.yml"; then
      found+=("- \`$path\`: on $(paste -sd ',' <<< "$events" | sed 's/,/, /g')")
    fi
  done
  ((${#found[@]} == 0)) || fail "$UPSTREAM $3 adds workflow files that someone outside the fork can start, or whose triggers could not be read:" \
    "${found[@]}" "" \
    "The workflow sweep cannot stop the first run of such a file, so the series was left as it is." \
    "Add each one to $removed_file with a fork patch (the next follow then deletes it) and start the follow workflow again;" \
    "or, to keep one, rebase onto $3 by hand (see the conflict advice in fork-follow.yml)."
}

# The newest fork release of a tag: "<tag> <commit>", or nothing.
latest_release() {
  local last
  last="$(grep -E "^${1//./\\.}-fork\.[0-9]+$" <<< "$fork_tags" | sort -V | tail -n1 || true)"
  [[ -z "$last" ]] || echo "$last $(git rev-parse "refs/fork-tags/$last^{commit}")"
}

# Workflow paths, read NUL-separated with core.quotepath=off, so a name with non-ASCII bytes, a
# quote or a backslash comes out as GitHub lists it. The lists are ','-joined, so a name with a ','
# or a newline cannot be named and the plan refuses it. These run in the plan's own shell (no
# command substitution), so a refusal stops the plan.
workflow_paths() { # workflow_paths FILTER FROM TO: sets paths, the ','-joined files FROM..TO shows as FILTER
  local path
  paths=""
  git -c core.quotepath=off diff -z --no-renames --name-only --diff-filter="$1" "$2" "$3" -- .github/workflows \
    > "$logs/paths" || fail "Could not list the workflow files that differ between ${2:0:12} and ${3:0:12}."
  while IFS= read -r -d '' path; do
    if [[ "$path" == *[,$'\n']* ]]; then
      fail "The workflow file \`$path\` (between ${2:0:12} and ${3:0:12}) has a ',' or a newline in its name, so it cannot be named to the workflow sweep." \
        "The series was left as it is. Rename or delete it with a fork patch."
    fi
    paths+="${paths:+,}$path"
  done < "$logs/paths"
}
own_workflows() { # own_workflows UPSTREAM_COMMIT TIP: sets own_list, the workflow files the series adds
  workflow_paths A "$1" "$2"
  own_list="$paths"
}
new_workflows() { # new_workflows FROM TO: sets new_list, the upstream workflow files added or changed
  local path list=()
  workflow_paths AM "$1" "$2" # a rename counts as new
  IFS=, read -r -a list <<< "$paths"
  new_list=""
  for path in "${list[@]}"; do
    [[ "$path" != *.yml && "$path" != *.yaml ]] || new_list+="${new_list:+,}$path"
  done
}

# The sweep (fork-workflows.sh) takes every .github/workflows/fork-*.y[a]ml as the fork's own, even
# one the series does not have yet, and never disables it. A file of that name that the upstream
# release has and the candidate keeps would stay enabled, so the plan refuses it.
fork_named() { # fork_named COMMIT: its .github/workflows/fork-*.y[a]ml files, one per line
  git -c core.quotepath=off ls-tree -r -z --name-only "$1" -- .github/workflows \
    | tr '\0' '\n' | { grep -E '^\.github/workflows/fork-[^/]*\.ya?ml$' || true; }
}
refuse_upstream_fork_named() { # refuse_upstream_fork_named UPSTREAM_COMMIT TAG CANDIDATE
  local named
  named="$(comm -12 <(fork_named "$1" | sort) <(fork_named "$3" | sort))"
  [[ -z "$named" ]] || fail "$UPSTREAM $2 has workflow files named like the fork's own (fork-*.yml), and the candidate keeps them:" \
    "$(sed 's/^/- /' <<< "$named")" "" \
    "The workflow sweep never disables a fork-* workflow, so they would run in the fork. The series was left as it is." \
    "Add a fork patch that deletes them (or rename the fork's rule in fork-workflows.sh and fork-follow.sh), then start the follow workflow again."
}

# The release job runs the reusable workflows of this run's commit (RUN_SHA), not the candidate's.
# They differ after upstream changed them, and on a re-run (or a queued run) of a run whose commit
# is older than the series. Building then would mix two versions of the release steps.
if ! git cat-file -e "$run_sha^{commit}" 2> /dev/null; then
  git fetch --quiet --no-tags "$ORIGIN" "$run_sha" > "$logs/fetch.log" 2>&1 \
    || fail "This run's commit $run_sha is not in the fork, so the plan cannot compare its release workflows with the candidate's." \
      "Start a new run (\`gh workflow run fork-follow.yml --repo $REPOSITORY\`) instead of re-running this one."
fi
called() { # the release workflow and every local workflow it calls, at commit $1, one per line
  local queue=(.github/workflows/fork-release.yml) seen="" file used
  while ((${#queue[@]})); do
    file="${queue[0]}"
    queue=("${queue[@]:1}")
    grep -qxF -- "$file" <<< "$seen" && continue
    seen+="$file"$'\n'
    while read -r used; do
      [[ -z "$used" ]] || queue+=("$used")
    done <<< "$({ git show "$1:$file" 2> /dev/null || true; } \
      | sed -n 's|^[[:space:]-]*uses:[[:space:]]*["'\'']\{0,1\}\./\(\.github/workflows/[^[:space:]@#"'\'']*\).*|\1|p')"
  done
  printf '%s' "$seen"
}
changed_release_workflows() { # lists the release workflow files that differ between RUN_SHA and $1
  local file
  while read -r file; do
    [[ -n "$file" ]] || continue
    if [[ "$(git rev-parse --quiet --verify "$run_sha:$file" || true)" != "$(git rev-parse --quiet --verify "$1:$file" || true)" ]]; then
      printf -- '- %s\n' "$file"
    fi
  done <<< "$(sort -u <<< "$(called "$run_sha")"$'\n'"$(called "$1")")"
}
stage_report() { # stage_report CANDIDATE TAG CHANGED
  printf '%s\n' \
    "The release workflows differ between this run's commit (${run_sha:0:12}) and the candidate ${1:0:12} on $2:" \
    "${3%$'\n'}" "" \
    "A run uses the reusable workflows of its own commit, so this run would build the candidate with other release steps than its own; it built nothing." \
    "The candidate ${1:0:12} is on the branch fork-follow/candidate. Release it with its own workflow files:" \
    "\`gh workflow run fork-release.yml --repo $REPOSITORY --ref fork-follow/candidate -f ref=$1 -f publish=true\`" \
    "The next follow run then finds that release (and moves the series to it if it is not there yet)." > "$REPORT"
}

if [[ "$target_tag" == "$base_tag" ]]; then
  refuse_upstream_fork_named "$base" "$base_tag" "$head"
  # The candidate here is the series head itself, so a listed file in it is refused, not deleted.
  kept="$(present_removed "$head")"
  [[ -z "$kept" ]] || fail "The series still has workflow files that $removed_file lists as deleted:" \
    "$(sed 's/^/- /' <<< "$kept")" "" \
    "The plan does not release them. Delete them with a fork patch (or take them off the list), then start the follow workflow again."
  read -r released_tag released_commit <<< "$(latest_release "$base_tag")" || true
  if [[ "${released_commit:-}" == "$head" ]]; then
    action=none
  else
    action=release
    released_tag=""
    changed="$(changed_release_workflows "$head")"
    if [[ -n "$changed" ]]; then
      action=stage
      stage_report "$head" "$base_tag" "$changed"
    fi
  fi
  own_workflows "$base" HEAD
  {
    echo "action=$action"
    echo "base_tag=$base_tag"
    echo "base_commit=$base"
    echo "candidate=$head"
    echo "released_tag=${released_tag:-}"
    echo "own_workflows=$own_list"
    echo "new_workflows="
  } >> "$OUTPUT"
  exit 0
fi

# A newer upstream release: replay the series onto it. Patches upstream now has become empty and
# are dropped. A stop whose conflicts are all on listed (deleted) workflow files is resolved by
# deleting them: upstream edited or brought back a file the fork deletes.
target="$(up "$target_tag")"
before="$(git log --format=%s "$base..HEAD")"
as_fork() { git -c user.name=t3code-fork -c user.email=noreply@users.noreply.github.com "$@"; }
rebasing() { [[ -d "$(git rev-parse --git-path rebase-merge)" || -d "$(git rev-parse --git-path rebase-apply)" ]]; }
rebase_ok=0
if as_fork rebase --empty=drop --onto "$target" "$base" > "$logs/rebase.log" 2>&1; then
  rebase_ok=1
fi
steps=0
while ((!rebase_ok)) && rebasing && ((steps++ <= series_count)); do
  unmerged=()
  while IFS= read -r -d '' path; do unmerged+=("$path"); done \
    < <(git -c core.quotepath=off diff -z --name-only --diff-filter=U)
  ((${#unmerged[@]})) || break
  for path in "${unmerged[@]}"; do is_removed "$path" || break 2; done
  git rm -q --ignore-unmatch -- "${unmerged[@]}" >> "$logs/rebase.log" 2>&1
  if git diff --cached --quiet HEAD; then
    GIT_EDITOR=true as_fork rebase --skip >> "$logs/rebase.log" 2>&1 && rebase_ok=1
  else
    GIT_EDITOR=true as_fork rebase --continue >> "$logs/rebase.log" 2>&1 && rebase_ok=1
  fi
done
if ((!rebase_ok)); then
  stopped="$(git log -1 --format='%h %s' REBASE_HEAD 2> /dev/null || echo unknown)"
  conflicts="$(git diff --name-only --diff-filter=U | sed 's/^/- /')"
  git rebase --abort || true
  fail "Rebasing the series from $base_tag onto $target_tag stopped at: $stopped" "" \
    "Conflicting files:" "${conflicts:-(none listed; see the run log)}" "" \
    "The series was left as it is. To follow by hand: \`git fetch https://github.com/$UPSTREAM.git tag $target_tag\`," \
    "\`git rebase --onto $target_tag $base_tag <series branch>\`, resolve, run the tests, then push the branch with" \
    "\`--force-with-lease\`, and start the follow workflow once (\`gh workflow run fork-follow.yml --repo $REPOSITORY\`):" \
    "it releases the series and disables the upstream workflows that push started."
fi
# Upstream brought back a listed file where no fork patch deletes it any more (the deleting patch
# became empty and was dropped once upstream had deleted the file itself): delete it again.
kept="$(present_removed HEAD)"
if [[ -n "$kept" ]]; then
  mapfile -t kept_list <<< "$kept"
  git rm -q -- "${kept_list[@]}"
  as_fork commit -q -m "fork: delete upstream workflows listed in $removed_file" \
    -m "Upstream $target_tag has them again: $(paste -sd ' ' <<< "$kept")"
fi
schema_check "$target" "After rebasing onto $target_tag, the series"
candidate="$(git rev-parse HEAD)"
refuse_upstream_fork_named "$target" "$target_tag" "$candidate"
refuse_outside_workflows "$base" "$target" "$target_tag" "$candidate"
dropped="$(comm -23 <(sort <<< "$before") <(git log --format=%s "$target..HEAD" | sort) | paste -sd ';' -)"

# A published fork release of the target with exactly these files exists (an earlier run published
# it, then could not move the series): move the series to it rather than build it again.
read -r released_tag released_commit <<< "$(latest_release "$target_tag")" || true
action=rebase
if [[ -n "${released_commit:-}" ]] \
  && [[ "$(git rev-parse "$released_commit^{tree}")" == "$(git rev-parse "$candidate^{tree}")" ]]; then
  action=advance
  candidate="$released_commit"
else
  released_tag=""
  changed="$(changed_release_workflows "$candidate")"
  if [[ -n "$changed" ]]; then
    action=stage
    stage_report "$candidate" "$target_tag" "$changed"
  fi
fi
own_workflows "$target" "$candidate"
new_workflows "$base" "$target"
{
  echo "action=$action"
  echo "base_tag=$target_tag"
  echo "base_commit=$target"
  echo "previous_tag=$base_tag"
  echo "previous_head=$head"
  echo "candidate=$candidate"
  echo "released_tag=${released_tag:-}"
  echo "dropped=$dropped"
  echo "own_workflows=$own_list"
  echo "new_workflows=$new_list"
} >> "$OUTPUT"
