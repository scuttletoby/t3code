#!/usr/bin/env bash
# Plan step of fork-follow.yml: decides what the run does, and rebases the fork's patch series onto
# a newer upstream tag of the followed channel when there is one (FORK_FOLLOW_CHANNEL, nightly by
# default, or stable; fork-channel.sh). Run in a full clone with the series checked out.
# It pushes and publishes nothing itself. Tests: fork-follow.test.sh.
#
# Nightly: the series may sit on a commit of upstream's default branch that no nightly tag marks
# (it was made from that branch). Its base is then that commit, and the plan rebases it onto the
# newest nightly, which must contain it; while no nightly does, the plan refuses and waits.
#
# Environment: FORK_FOLLOW_CHANNEL, UPSTREAM (owner/name), UPSTREAM_URL (git URL of upstream,
# default GitHub), ORIGIN (git remote or URL of this fork, default origin), RUN_SHA (the commit
# this run's workflow files come from, default HEAD), REPOSITORY (owner/name of this fork, for the
# advice in reports), REPORT (file that receives the reason when the run cannot go on).
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
upstream_url="${UPSTREAM_URL:-https://github.com/$UPSTREAM.git}"
reported=""

# quoted TEXT...: the lines of TEXT as they are, for a report GitHub renders (an issue comment): a
# blank line, then every line indented by four spaces, a code block that no line in TEXT can end. A
# fence could be ended: git prints upstream's file names raw, and a name may hold a newline and ```.
quoted() { printf '\n'; printf '%s\n' "$@" | sed 's/^/    /'; }

# The report names files and patches that upstream and the series chose. Every @ in it gets a
# zero-width space after it, so a name like @someone mentions no one when the report is posted.
zwsp=$'\xe2\x80\x8b'
fail() {
  printf '%s\n' "$@" | sed "s/@/@$zwsp/g" > "$REPORT"
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

# The logs of the steps below. In a run they go to RUNNER_TEMP, which is the runner's. Anywhere
# else the plan makes a folder of its own and removes it however it ends (also on TERM, INT and
# HUP; a subshell does not inherit the trap). Only KILL leaves it, and its name says whose it was.
if [[ -n "${RUNNER_TEMP:-}" ]]; then
  logs="$RUNNER_TEMP"
else
  logs="$(mktemp -d "${TMPDIR:-/tmp}/fork-follow.XXXXXX")"
  trap 'rm -rf "$logs" || true' EXIT
fi

head="$(git rev-parse HEAD)"
run_sha="${RUN_SHA:-$head}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/fork-channel.sh"
fork_channel_init || fail "FORK_FOLLOW_CHANNEL is '$FORK_FOLLOW_CHANNEL'; it must be nightly or stable. Nothing was planned."

# Read-only fetches into refs this run alone uses: upstream's tags and default branch, and this
# fork's tags (its releases). Pruned, so a tag deleted since an earlier fetch here is gone too.
git fetch --quiet --prune --no-tags "$upstream_url" "+refs/tags/v*:refs/upstream-tags/v*" "+HEAD:refs/upstream-head" \
  > "$logs/fetch.log" 2>&1 \
  || fail "Could not fetch the tags and default branch of $UPSTREAM ($upstream_url):" "$(quoted "$(tail -n 20 "$logs/fetch.log")")"
git fetch --quiet --prune --no-tags "$ORIGIN" "+refs/tags/v*:refs/fork-tags/v*" > "$logs/fetch.log" 2>&1 \
  || fail "Could not fetch the tags of this fork ($ORIGIN):" "$(quoted "$(tail -n 20 "$logs/fetch.log")")"

# Newest first by version (fork-channel.sh), not by name or by when a tag was made.
upstream_tags="$(git for-each-ref --format='%(refname:strip=2)' refs/upstream-tags | { grep -E "$channel_re" || true; } | newest_first)"
fork_tags="$(git for-each-ref --format='%(refname:strip=2)' refs/fork-tags | { grep -E "$fork_re" || true; } | newest_first)"
target_tag="$(sed -n 1p <<< "$upstream_tags")"
[[ -n "$target_tag" ]] || fail "$UPSTREAM has no $channel release tag ($channel_form)."
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
  built_on="${last_release%[-.]fork.*}"
  if ! git rev-parse --quiet --verify "refs/upstream-tags/$built_on^{commit}" > /dev/null; then
    fail "$UPSTREAM no longer has the tag $built_on, which the series was built on (fork release $last_release is in its history)." \
      "Upstream deleted or renamed that release. Decide by hand which upstream release the series should sit on; the series was left as it is."
  fi
  if ! git merge-base --is-ancestor "$(up "$built_on")" HEAD; then
    fail "$UPSTREAM moved the tag $built_on, which the series was built on (fork release $last_release is in its history), to a commit the series does not contain." \
      "Decide by hand whether to rebase onto the moved tag; the series was left as it is."
  fi
fi

# The series sits on the newest upstream tag of the channel it contains.
base_tag=""
for tag in $upstream_tags; do
  if git merge-base --is-ancestor "$(up "$tag")" HEAD; then
    base_tag="$tag"
    base="$(up "$tag")"
    break
  fi
done
# Nightly: or on a later commit of upstream's default branch that no nightly tag marks. Its base is
# then that commit (base_tag names it by its short hash), and only a rebase can move it onto a tag.
untagged=""
if [[ "$channel" == nightly ]] && fork_point="$(git merge-base HEAD refs/upstream-head)" \
  && [[ "$fork_point" != "${base:-}" ]] \
  && { [[ -z "$base_tag" ]] || git merge-base --is-ancestor "$base" "$fork_point"; }; then
  untagged=1
  base="$fork_point"
  base_tag="${base:0:12}"
fi
[[ -n "$base_tag" ]] || fail "The series ($head) contains no $channel $UPSTREAM tag."

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
    fail "$2 changes the database schema:" "$(quoted "$(cat "$logs/schema-check.log")")"
  fi
  fail "The schema check crashed (exit $status), so it did not say whether the series touches the database schema:" \
    "$(quoted "$(tail -n 20 "$logs/schema-check.log")")" "" "The series was left as it is."
}
schema_check "$base" "The series"

# Fork lines in upstream files (.github/fork-seams.json): only the pinned seams, so every fork
# setting stays in the fork's own files. Exit 1 is a refusal; anything else is a crash. BASE is an
# upstream commit, which --from-upstream says (the series' own list judges it, even when upstream
# is a fork with a list of its own). After a rebase (--after-rebase), a "pending" ceiling or "lost"
# count above the file's lines is only a note: upstream taking a fork line drops it in the rebase,
# and the next run (from the new base) would refuse it. So the series gets a commit that lowers each
# one (#325); a count that rose is still refused, as raising one is the owner's review.
seam_refusal="Fork changes to upstream files must stay within the seam lines"
seam_run() { # seam_run BASE [--after-rebase]: the check's exit status; its output in seam-check.log
  local status=0 lower=()
  rm -f "$logs/seam-lowered.json"
  [[ -z "${2:-}" ]] || lower=("$2" --lowered-list "$logs/seam-lowered.json")
  node .github/scripts/fork-seam-check.cjs --from-upstream ${lower[@]+"${lower[@]}"} "$1" HEAD > "$logs/seam-check.log" 2>&1 || status=$?
  return "$status"
}
seam_check() { # seam_check BASE WHAT [--after-rebase]
  local status=0
  seam_run "$1" ${3:+"$3"} || status=$?
  if ((status == 0)) && [[ -f "$logs/seam-lowered.json" ]]; then
    local lowered
    lowered="$(grep '^Lowered: ' "$logs/seam-check.log" | sed 's/^Lowered: /- /')"
    cp "$logs/seam-lowered.json" .github/fork-seams.json
    git add -- .github/fork-seams.json
    as_fork commit -q -m "fork seams: lower counts after the rebase onto $target_tag" -m "$lowered"
    printf '%s\n' "$2 lowered the seam list (.github/fork-seams.json) in a commit of its own:" "$lowered" >&2
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf '%s\n' "### Seam list lowered: $2" "" "$lowered" "" \
        "Committed into the series; the fork lines or upstream lines these counted are gone." \
        "" >> "$GITHUB_STEP_SUMMARY"
    fi
    # The lowered list is judged again: it must pass and lower nothing more.
    status=0
    seam_run "$1" ${3:+"$3"} || status=$?
    if ((status == 0)) && [[ -f "$logs/seam-lowered.json" ]]; then
      fail "The seam list fork-follow lowered still lowers again, so it was not committed as checked:" \
        "$(quoted "$(cat "$logs/seam-check.log")")" "" "The series was left as it is."
    fi
  fi
  if ((status == 0)); then
    local notes
    notes="$(grep '^Note: ' "$logs/seam-check.log" || true)"
    [[ -n "$notes" ]] || return 0
    printf '%s\n' "$notes" >&2
    # The log is seen only when the plan fails: the notes go to the run's summary too.
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf '%s\n' "### Seam check notes: $2" "" "$(sed 's/^Note: /- /' <<< "$notes")" "" \
        "A ceiling or count still named here (one fork-follow could not lower) is refused by the next run; fix it with a pull request to main (docs/fork/updates.md)." \
        "" >> "$GITHUB_STEP_SUMMARY"
    fi
    return 0
  fi
  if ((status == 1)) && grep -qF "$seam_refusal" "$logs/seam-check.log"; then
    fail "$2 does not fit the seam list (.github/fork-seams.json):" "$(quoted "$(cat "$logs/seam-check.log")")" "" \
      "The series was left as it is."
  fi
  fail "The seam check crashed (exit $status), so it did not say whether the series stays within the seams:" \
    "$(quoted "$(tail -n 20 "$logs/seam-check.log")")" "" "The series was left as it is."
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
    "$(quoted "$named")" "" \
    "The workflow sweep never disables a fork-* workflow, so they would run in the fork. The series was left as it is." \
    "Add a fork patch that deletes them (or rename the fork's rule in fork-workflows.sh and fork-follow.sh), then start the follow workflow again."
}

# Before the seam check, which also refuses an upstream file named like the fork's, so the report
# says what to do about a workflow (as after a rebase, below).
refuse_upstream_fork_named "$base" "$base_tag" "$head"
seam_check "$base" "The series"

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
      found+=("$path: its triggers could not be read ($(tr '\n' ' ' < "$logs/triggers.log"| sed 's/ *$//'))")
    elif grep -qE "$outside_events" <<< "$events" || grep -qwE 'pull_request_target|workflow_run' "$logs/workflow.yml"; then
      found+=("$path: on $(paste -sd ',' <<< "$events" | sed 's/,/, /g')")
    fi
  done
  ((${#found[@]} == 0)) || fail "$UPSTREAM $3 adds workflow files that someone outside the fork can start, or whose triggers could not be read:" \
    "$(quoted "${found[@]}")" "" \
    "The workflow sweep cannot stop the first run of such a file, so the series was left as it is." \
    "Add each one to $removed_file with a fork patch (the next follow then deletes it) and start the follow workflow again;" \
    "or, to keep one, rebase onto $3 by hand (see the conflict advice in fork-follow.yml)."
}

# The newest fork release of a tag (<tag>-fork.N or <tag>.fork.N): "<tag> <commit>", or nothing.
latest_release() {
  local last sep=-
  [[ "$(fork_sep "$1")" != . ]] || sep='\.'
  last="$(grep -E "^${1//./\\.}${sep}fork\.[0-9]+$" <<< "$fork_tags" | head -n1 || true)"
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
      fail "A workflow file between ${2:0:12} and ${3:0:12} has a ',' or a newline in its name, so it cannot be named to the workflow sweep:" \
        "$(quoted "$path")" "" "The series was left as it is. Rename or delete it with a fork patch."
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

# Nightly: the newest nightly must contain the series' base. A nightly tag moved or added to an older
# commit would otherwise take the series backwards, and an untagged base newer than every nightly
# has no tag a release could name yet.
target="$(up "$target_tag")"
if [[ "$channel" == nightly ]] && ! git merge-base --is-ancestor "$base" "$target"; then
  if [[ -n "$untagged" ]] && git merge-base --is-ancestor "$target" "$base"; then
    fail "The series sits on $UPSTREAM commit $base_tag, which is newer than the newest nightly, $target_tag." \
      "A fork release names the upstream tag it sits on, so the series was left as it is; a run after the next nightly follows it."
  fi
  fail "$UPSTREAM's newest nightly, $target_tag (${target:0:12}), does not contain the series' base $base_tag (${base:0:12})." \
    "Upstream moved or deleted a nightly tag, or tagged another branch. The series was left as it is; decide by hand which nightly it should sit on."
fi
# Upstream cuts nightlies from its default branch. A nightly-shaped tag on another branch that
# contains the base would pass the check above and bring that branch's code to every install.
if [[ "$channel" == nightly ]] && ! git merge-base --is-ancestor "$target" refs/upstream-head; then
  fail "$UPSTREAM's newest nightly, $target_tag (${target:0:12}), is not on its default branch, so it is not one of upstream's nightlies (or upstream rewrote that branch)." \
    "The series was left as it is; decide by hand which nightly it should sit on."
fi

if [[ -z "$untagged" && "$target_tag" == "$base_tag" ]]; then
  # The candidate here is the series head itself, so a listed file in it is refused, not deleted.
  kept="$(present_removed "$head")"
  [[ -z "$kept" ]] || fail "The series still has workflow files that $removed_file lists as deleted:" \
    "$(quoted "$kept")" "" \
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
before="$(git log --format=%s "$base..HEAD")"

# The workflow checks out with --filter=blob:none, so git fetches a missing blob from the promisor
# remote when it first reads it. While applying a pick, git 2.56 (the ubuntu-26.04 runners) asked
# that remote in one batch for the blob the pick itself had just merged; no server has it, so the
# fetch failed ("not our ref") and the pick stopped with a git error, not a conflict (#475). Before
# the rebase, fetch in one batch every blob it reads: each patch's two sides, the series' own, and
# both sides of every file upstream changed. The rebase then has nothing to fetch. A blob already
# here is not asked for. The upstream fetch above sends every blob of the target except those the
# base's history already has (a file upstream brought back to an earlier content), so whatever is
# asked for is in the series or the base's history, which the remote has; a gitlink never is. The promisor is extensions.partialclone or, as
# `git clone --filter` and `git fetch --filter` leave it, the remote marked `promisor = true` (in
# any spelling git takes for true: yes, on, 1).
prefetch_rebase_blobs() { # prefetch_rebase_blobs BASE TARGET
  local promisor
  promisor="$(git config --get extensions.partialclone || true)"
  [[ -n "$promisor" ]] || promisor="$(git config --type=bool --get-regexp '^remote\..*\.promisor$' \
    | awk '$2 == "true" { sub(/^remote\./, "", $1); sub(/\.promisor$/, "", $1); print $1; exit }' || true)"
  [[ -n "$promisor" ]] || return 0
  # raw_blobs SIDES: the blob ids (not gitlinks) of `--raw` lines, on the old side or both.
  raw_blobs() { awk -v both="$1" '/^:/ { if ($1 ~ /^:(100|120)/) print $3; if (both && $2 ~ /^(100|120)/) print $4 }'; }
  {
    git rev-list --objects --missing=print "$1..HEAD" | sed -n 's/^?//p'
    git log --raw --no-renames --no-abbrev --format= "$1..HEAD" | raw_blobs 1
    git diff-tree -r --no-renames "$1" "$2" | raw_blobs 1
  } | awk '$0 !~ /^0+$/' | sort -u > "$logs/prefetch.wanted"
  # Objects already here (listing them never fetches); only the rest is asked for.
  git cat-file --batch-all-objects --batch-check='%(objectname)' | sort -u > "$logs/prefetch.here"
  comm -23 "$logs/prefetch.wanted" "$logs/prefetch.here" > "$logs/prefetch.ids"
  [[ -s "$logs/prefetch.ids" ]] || return 0
  git -c fetch.negotiationAlgorithm=noop fetch --quiet "$promisor" --no-tags --no-write-fetch-head \
    --recurse-submodules=no --filter=blob:none --stdin < "$logs/prefetch.ids" > "$logs/prefetch.log" 2>&1 \
    || fail "Could not fetch the $(wc -l < "$logs/prefetch.ids") files the rebase from $base_tag onto $target_tag reads from $promisor (this checkout has no file contents of its own):" \
      "$(quoted "$(tail -n 20 "$logs/prefetch.log")")" "" "Nothing was rebased; the series was left as it is."
}
prefetch_rebase_blobs "$base" "$target"

# Git's own words for a stop: the last rebase command's output without progress lines, its last
# 20 lines. The output of earlier steps (a resolved listed file) stays in their own logs.
git_said() {
  tr '\r' '\n' < "$last_log" \
    | awk '!/^(Rebasing \([0-9]+\/[0-9]+\)|Auto packing the repository|See "git help gc")/ && NF' | tail -n 20
}
as_fork() { git -c user.name=t3code-fork -c user.email=noreply@users.noreply.github.com "$@"; }
rebasing() { [[ -d "$(git rev-parse --git-path rebase-merge)" || -d "$(git rev-parse --git-path rebase-apply)" ]]; }
rebase_ok=0
last_log="$logs/rebase.log"
if as_fork rebase --empty=drop --onto "$target" "$base" > "$last_log" 2>&1; then
  rebase_ok=1
fi
steps=0
resumed="" # the patch whose listed files were deleted last, and the command that went on from it
while ((!rebase_ok)) && rebasing && ((steps++ <= series_count)); do
  unmerged=()
  while IFS= read -r -d '' path; do unmerged+=("$path"); done \
    < <(git -c core.quotepath=off diff -z --name-only --diff-filter=U)
  ((${#unmerged[@]})) || break
  for path in "${unmerged[@]}"; do is_removed "$path" || break 2; done
  last_log="$logs/rebase-step-$steps.log"
  resumed="$(git log -1 --format='%h %s' REBASE_HEAD 2> /dev/null || echo "a patch")"
  git rm -q --ignore-unmatch -- "${unmerged[@]}" > "$last_log" 2>&1
  if git diff --cached --quiet HEAD; then
    resumed+=$'\n'--skip
    GIT_EDITOR=true as_fork rebase --skip >> "$last_log" 2>&1 && rebase_ok=1
  else
    resumed+=$'\n'--continue
    GIT_EDITOR=true as_fork rebase --continue >> "$last_log" 2>&1 && rebase_ok=1
  fi
done
if ((!rebase_ok)); then
  # With no rebase left, git either never began it or ended it in a --continue/--skip above.
  # REBASE_HEAD names a patch that stopped on a conflict. A pick that stopped on a git error has
  # none; the last line of the rebase's done list is then the patch it was applying. With no done
  # list git stopped before its first pick, while it checked out the target.
  done_list="$(git rev-parse --git-path rebase-merge/done)"
  if ! rebasing && [[ -n "$resumed" ]]; then
    stopped="the patch after ${resumed%$'\n'*}: once its listed files were deleted, \`git rebase ${resumed##*$'\n'}\` failed and left no rebase state that names the patch"
  elif ! rebasing; then
    stopped="its start: git did not begin the rebase, so no patch was applied"
  elif git rev-parse -q --verify REBASE_HEAD > /dev/null; then
    stopped="$(git log -1 --format='%h %s' REBASE_HEAD)"
  elif [[ -d "$(git rev-parse --git-path rebase-merge)" && ! -s "$done_list" ]]; then
    stopped="its start: git stopped while it checked out $target_tag, before it applied any patch"
  else
    picked="$(awk '$1 ~ /^(pick|p)$/ { c = $2 } END { print c }' "$done_list" 2> /dev/null || true)"
    stopped="$(git log -1 --format='%h %s' "${picked:-none}" 2> /dev/null)" \
      || stopped="a patch git did not name (its output is below)"
  fi
  conflicts="$(git diff --name-only --diff-filter=U)"
  said="$(git_said)"
  aborted=()
  git rebase --abort > "$logs/rebase-abort.log" 2>&1 \
    || aborted=("\`git rebase --abort\` failed as well, so this checkout is still rebasing. It said:" \
      "$(quoted "$(tail -n 20 "$logs/rebase-abort.log")")" "")
  if [[ -n "$conflicts" ]]; then
    why=("Conflicting files:" "$(quoted "$conflicts")")
  else
    why=("Git stopped with an error, not a conflict: no file is left unmerged.")
  fi
  fail "Rebasing the series from $base_tag onto $target_tag stopped at: $stopped" "" \
    "${why[@]}" "" "What git said:" "$(quoted "${said:-(git printed nothing)}")" "" ${aborted[@]+"${aborted[@]}"} \
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
# Before the seam check, which also refuses an upstream file named like the fork's, so the report
# says what to do about a workflow.
refuse_upstream_fork_named "$target" "$target_tag" "$candidate"
seam_check "$target" "After rebasing onto $target_tag, the series" --after-rebase
candidate="$(git rev-parse HEAD)" # The seam check may have committed a lowered list.
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
