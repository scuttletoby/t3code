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
# are dropped.
target="$(up "$target_tag")"
before="$(git log --format=%s "$base..HEAD")"
if ! git -c user.name=t3code-fork -c user.email=noreply@users.noreply.github.com \
  rebase --empty=drop --onto "$target" "$base" > "$logs/rebase.log" 2>&1; then
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
schema_check "$target" "After rebasing onto $target_tag, the series"
candidate="$(git rev-parse HEAD)"
refuse_upstream_fork_named "$target" "$target_tag" "$candidate"
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
