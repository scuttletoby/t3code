#!/usr/bin/env bash
# Fork only: the Secret scan job of fork-tests.yml. Picks the commits a pull request or a push adds
# and runs gitleaks over them. Tests: fork-secret-scan.test.sh.
#
# Environment:
#   GITLEAKS     gitleaks binary
#   EVENT_NAME   pull_request or push
#   BASE_SHA     pull_request: the base branch commit the PR is compared with
#   HEAD_SHA     pull_request: the PR head
#   BEFORE       push: the branch tip before the push (all zeros when the branch is new)
#   AFTER        push: the branch tip after the push
#   UPSTREAM     push: owner/name of the repository this fork follows (the workflow reads this
#                fork's parent, as fork-follow.yml does); empty when it could not be read
#   UPSTREAM_URL push: git URL of upstream (default https://github.com/$UPSTREAM.git, as in
#                fork-follow.sh)
#   CONFIG_BRANCH  pull_request: the repository's default branch; its tip holds the gitleaks
#                settings the scan uses (see "Settings" below)
#   RUNNER_TEMP  folder for the scan's temporary files (else TMPDIR, else /tmp)
#
# Ranges:
#   pull_request   BASE_SHA..HEAD_SHA: every commit the PR adds, merges of the base branch
#                  included, whatever their dates. A squash merge never brings the PR's own commits
#                  to main, but they stay public under refs/pull/N/head, so they are scanned here.
#   push           BEFORE..AFTER. After a force push that is every commit the new tip has and the
#                  old one had not; commits both share were scanned when they were first pushed.
#   push, no BEFORE  a new branch (BEFORE all zeros) or an old tip that can no longer be fetched:
#                  only AFTER itself, with a warning. Everything before it is history this fork
#                  already had (upstream's, or scanned by earlier runs), and there is no older tip
#                  to tell new commits from old ones.
#   push, upstream  every push range leaves out the commits upstream's default branch or one of its
#                  v* tags holds. A fork-follow rebase force-pushes the series onto a newer upstream
#                  tag, so BEFORE..AFTER would otherwise hold hundreds of upstream's commits (and
#                  upstream's own test fixtures); only the commits this fork adds are its business.
#                  Upstream is fetched read-only, commits only, into refs/scan-upstream/*, which
#                  each run empties first. Fail closed: when UPSTREAM is empty or the fetch fails,
#                  nothing is left out (the full range, with a warning).
# Merge commits are diffed with --remerge-diff: only what the merge itself changed beyond an
# automatic merge of its parents (a conflict resolution, or a file added in the merge). Without it
# git log shows merges with no diff at all, so gitleaks would never read them. --remerge-diff knows
# only two-parent merges: an octopus merge (3 or more parents) still shows no diff, so a range with
# one fails the job (exit 2); merge the branches one at a time instead.
#
# Settings: gitleaks reads .gitleaks.toml and .gitleaksignore, which can allow any finding. They
# come from a commit the range cannot change, never from the scanned commits or the checkout (the
# pull request's head): for a pull request the default branch's tip (CONFIG_BRANCH, fetched each
# run), for a push the tip before the push (BEFORE; for a new branch AFTER's first parent; none for
# a root commit: gitleaks' defaults). gitleaks runs in a temporary folder holding only those two
# files, so it finds no other. A range that changes them gets a notice: the change takes effect
# once it is on the default branch. The reviewed path is a pull request of its own that changes
# only these files (its own scan uses the old settings, and it adds no key); once it is merged,
# re-run the scan of the pull request that needs it. Not covered: a `gitleaks:allow` comment on
# the line itself (visible in the diff), and a pull request that edits this script or the workflow
# (pull_request runs take both from the pull request).
#
# Attributes: a .gitattributes in the range could mark a file `-diff` or binary, so git log shows
# "Binary files differ" instead of its lines. Both git logs below read attributes from the empty
# tree (GIT_ATTR_SOURCE), never from the checkout or a commit; real binary contents stay binary.
#
# Offline, and counted: the checkout is a blob-less clone, so file contents are fetched on demand.
# gitleaks 8.30.1 stops reading at the first git error (a blob fetch that fails: network, GitHub
# 5xx), logs "0 commits scanned" and still exits 0. So the script runs gitleaks' own git log first,
# which fetches every blob the scan reads and fails the job if a fetch fails, then runs gitleaks with
# GIT_NO_LAZY_FETCH=1, and fails when gitleaks scanned fewer commits than that git log holds.
# gitleaks counts a commit when a file it neither deletes nor turns binary has a hunk: a pure
# rename, a mode change, an empty new file or a merge with nothing beyond the automatic merge has
# none, so the count can be below `git rev-list --count`.
#
# Merges in a blob-less checkout: --remerge-diff writes each automatic merge (a file with conflict
# markers, say) to a temporary object folder, and git's batch fetch before each diff checks objects
# with a stale cache of that folder (git 2.43 to 2.56 at least): when a lookup earlier in the same
# git log (another merge, typically) shared the temporary object's first byte, git takes it for a
# missing file content and asks the remote for it ("not our ref"). Alone in a batch that is harmless:
# git then reads the object from the folder. In a batch with a real missing content, git stops with
# "could not fetch <that content>"; under GIT_NO_LAZY_FETCH it logs "lazy fetching disabled", which
# fails the scan below. So the script fetches, in one batch first, the contents every diff in the
# range compares (each commit against each parent), and gitleaks reads a view that is not a partial
# clone: a temporary bare repository (same object format) that borrows this checkout's objects
# (objects/info/alternates) and the refs a range names, with no promisor remote, so git never
# fetches there and a content still missing is a fatal git error, which fails the scan.
set -euo pipefail

have() { git cat-file -e "$1^{commit}" 2>/dev/null; }
# Commits only, file contents on demand: the checkout is a blob-less clone.
fetch() {
  have "$1" || git fetch --no-tags --quiet --filter=blob:none origin "$1" 2>/dev/null || true
  have "$1"
}
zero() { [[ -z "$1" || "$1" =~ ^0+$ ]]; }
# Upstream's default branch and v* tags (the same tags fork-follow.sh reads), commits only, into
# refs/scan-upstream/*. Refs from an earlier run go first, so a tag upstream has deleted since is
# not left out. A failed fetch adds no exclusion at all.
fetch_upstream() {
  git for-each-ref --format='delete %(refname)' refs/scan-upstream/ | git update-ref --stdin || return 1
  [[ -n "${UPSTREAM:-}" ]] || return 1
  local url="${UPSTREAM_URL:-https://github.com/$UPSTREAM.git}"
  timeout 600 git fetch --quiet --no-tags --filter=blob:none "$url" \
    "+HEAD:refs/scan-upstream/head" "+refs/tags/v*:refs/scan-upstream/tags/v*" >&2 || return 1
  git rev-parse --quiet --verify refs/scan-upstream/head > /dev/null
}
summary() { if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then printf '%s\n' "$@" >> "$GITHUB_STEP_SUMMARY"; fi; }
# The settings commit for a pull request: the default branch's tip, commits only, into
# refs/scan-config/head (overwritten each run; a failed fetch fails the job, so an old one is never
# used).
fetch_config_branch() {
  [[ -n "${CONFIG_BRANCH:-}" ]] || return 1
  git check-ref-format "refs/heads/$CONFIG_BRANCH" || return 1
  timeout 600 git fetch --quiet --no-tags --filter=blob:none origin \
    "+refs/heads/$CONFIG_BRANCH:refs/scan-config/head" >&2 || return 1
  git rev-parse --quiet --verify 'refs/scan-config/head^{commit}'
}

warning=""
case "$EVENT_NAME" in
  pull_request)
    for sha in "$BASE_SHA" "$HEAD_SHA"; do
      if ! fetch "$sha"; then
        echo "::error::Secret scan: cannot fetch commit $sha" >&2
        exit 2
      fi
    done
    if ! settings="$(fetch_config_branch)"; then
      echo "::error::Secret scan: cannot fetch the default branch '${CONFIG_BRANCH:-}' (CONFIG_BRANCH), whose gitleaks settings the scan uses" >&2
      exit 2
    fi
    settings_what="the default branch $CONFIG_BRANCH (${settings:0:12})"
    changes=("$BASE_SHA...$HEAD_SHA")
    range="$BASE_SHA..$HEAD_SHA"
    opts="--remerge-diff $range"
    what="the pull request's $(git rev-list --count "$range") commit(s) after base ${BASE_SHA:0:12}, merges included"
    ;;
  push)
    if ! zero "$BEFORE" && fetch "$BEFORE"; then
      settings="$BEFORE"
      settings_what="the tip before the push (${BEFORE:0:12})"
      changes=("$BEFORE" "$AFTER")
      range="$BEFORE..$AFTER"
      opts="--remerge-diff $range"
      if git merge-base --is-ancestor "$BEFORE" "$AFTER"; then
        what="the $(git rev-list --count "$range") commit(s) pushed after ${BEFORE:0:12}"
      else
        what="force push: the $(git rev-list --count "$range") commit(s) ${AFTER:0:12} has that the replaced tip ${BEFORE:0:12} had not"
      fi
    else
      if settings="$(git rev-parse --quiet --verify "$AFTER^1")"; then
        settings_what="the pushed commit's first parent (${settings:0:12})"
        changes=("$settings" "$AFTER")
      else
        settings=""
        settings_what="none (a root commit): gitleaks' defaults"
        changes=()
      fi
      range="--max-count=1 $AFTER"
      opts="--remerge-diff $range"
      if zero "$BEFORE"; then
        warning="a new branch has no earlier tip, so only its newest commit ${AFTER:0:12} is scanned"
      else
        warning="the earlier tip ${BEFORE:0:12} cannot be fetched (force push?), so only the newest commit ${AFTER:0:12} is scanned"
      fi
      what="the pushed commit ${AFTER:0:12}"
    fi
    # Leave out upstream's commits (see the header). --glob takes every ref under the prefix.
    if fetch_upstream; then
      full="$range"
      range="$range --not --glob=refs/scan-upstream"
      opts="--remerge-diff $range"
      # shellcheck disable=SC2086
      what="$what, minus the $(( $(git rev-list --count $full) - $(git rev-list --count $range) )) commit(s) upstream $UPSTREAM already has"
    else
      if [[ -z "${UPSTREAM:-}" ]]; then
        reason="upstream is not known (UPSTREAM is empty)"
      else
        reason="upstream $UPSTREAM could not be fetched"
      fi
      warning="${warning:+$warning; }$reason, so its commits in the range are scanned too"
    fi
    ;;
  *)
    echo "::error::Secret scan: unexpected event $EVENT_NAME" >&2
    exit 2
    ;;
esac

echo "Scanning $what ($range)"
if [[ -n "$warning" ]]; then echo "::warning title=Secret scan::$warning"; fi

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/fork-secret-scan.XXXXXX")"
trap 'rm -rf "$work"' EXIT
log="$work/gitleaks.log"
cannot_read() {
  echo "::error::Secret scan: cannot read the commits to scan (a file content fetch failed?)" >&2
  summary "### Secret scan" "" "Failed: the contents of $what could not be read, so nothing was scanned."
  exit 2
}

# The range's commits, each with its parents.
# shellcheck disable=SC2086
git rev-list --parents $range > "$work/parents" || cannot_read
cut -d ' ' -f 1 "$work/parents" > "$work/commits"
# An octopus merge shows no diff even with --remerge-diff (see the header).
octopus="$(awk 'NF > 3 { printf "%s%s", sep, substr($1, 1, 12); sep = " " }' "$work/parents")"
if [[ -n "$octopus" ]]; then
  echo "::error::Secret scan: the range holds an octopus merge (3 or more parents): $octopus. git log shows no diff for it, so nothing it adds could be scanned. Merge the branches one at a time." >&2
  summary "### Secret scan" "" "Failed: $what holds an octopus merge ($octopus), which cannot be scanned. Merge the branches one at a time."
  exit 2
fi

# The gitleaks settings from the settings commit (see the header), into a folder of their own.
config="$work/config"
mkdir "$config"
for name in .gitleaks.toml .gitleaksignore; do
  [[ -n "$settings" ]] || break
  entry="$(git ls-tree "$settings" -- "$name")" || cannot_read
  [[ -n "$entry" ]] || continue
  read -r mode type oid _ <<< "$entry"
  if [[ "$type" != blob || ( "$mode" != 100644 && "$mode" != 100755 ) ]]; then
    echo "::error::Secret scan: $name on $settings_what is not a plain file ($mode $type)" >&2
    exit 2
  fi
  git cat-file blob "$oid" > "$config/$name" || cannot_read
done
echo "gitleaks settings (.gitleaks.toml, .gitleaksignore) from $settings_what"
# Only a message: when the diff fails (no merge base, say), there is none.
if [[ ${#changes[@]} -gt 0 ]]; then
  changed="$(git diff --no-renames --name-only "${changes[@]}" -- .gitleaks.toml .gitleaksignore 2> /dev/null)" || changed=""
  if [[ -n "$changed" ]]; then
    echo "::notice title=Secret scan::This range changes $(tr '\n' ' ' <<< "$changed")but the scan reads them from $settings_what. A change to them takes effect once it is on the default branch: land it in a pull request of its own first, then re-run this scan."
  fi
fi

# Attributes from the empty tree for both git logs (see the header).
GIT_ATTR_SOURCE="$(git hash-object -t tree /dev/null)"
export GIT_ATTR_SOURCE

# The contents every diff in the range compares (each commit against each parent, merges included),
# in one batch before git log runs, so that git log's own batches hold either temporary objects or
# real contents (the merge base's side of an automatic merge), never both (see the header).
if ! git diff-tree --stdin -m -r --no-commit-id --raw --no-abbrev < "$work/commits" |
    awk '$1 != ":160000" && $3 !~ /^0+$/ { print $3 } $2 != "160000" && $4 !~ /^0+$/ { print $4 }' |
    sort -u > "$work/wanted" ||
  ! git cat-file --batch-check='%(objectname)' --batch-all-objects 2> /dev/null | sort -u > "$work/have" ||
  ! comm -23 "$work/wanted" "$work/have" > "$work/missing" ||
  { [[ -s "$work/missing" ]] && ! timeout 600 git -c fetch.negotiationAlgorithm=noop fetch --quiet --no-tags \
    --no-write-fetch-head --recurse-submodules=no --filter=blob:none origin --stdin < "$work/missing"; }; then
  cannot_read
fi

# gitleaks runs `git log -p -U0 <log-opts>`. The same command here fetches what it still lacks;
# counts the commits gitleaks will scan (see the header); and fails the job (pipefail) when a fetch
# fails.
# shellcheck disable=SC2086
if ! expected="$(git log -p -U0 $opts 2> "$work/fetch.err" | awk '
  /^commit / { sha = $2; next }
  /^diff --git / { deleted = 0; next }
  /^deleted file mode / { deleted = 1; next }
  /^@@ / { if (!deleted && sha != "" && !(sha in seen)) { seen[sha] = 1; n++ } }
  END { print n + 0 }')"; then
  cat "$work/fetch.err" >&2
  cannot_read
fi
# git exited 0, so every content it read was there. A batch that asked for a temporary
# --remerge-diff object fails with "not our ref" (see the header): shown, not judged; the offline
# scan below is the check.
if [[ -s "$work/fetch.err" ]]; then
  echo "git's messages while reading the contents (\"not our ref\" for a temporary merge object is expected; see the script header):"
  sed 's/^/    /' "$work/fetch.err"
fi

# The view gitleaks reads (see the header): this checkout's objects, the refs a range may name
# (refs/scan-upstream), and origin's URL for gitleaks' finding links, but no promisor remote.
view="$work/view.git"
git init -q --bare --object-format="$(git rev-parse --show-object-format)" "$view"
printf '%s\n' "$(git rev-parse --path-format=absolute --git-common-dir)/objects" > "$view/objects/info/alternates"
git for-each-ref --format='create %(refname) %(objectname)' refs/scan-upstream/ | git --git-dir="$view" update-ref --stdin
if url="$(git config --get remote.origin.url)"; then git --git-dir="$view" config remote.origin.url "$url"; fi

# gitleaks runs in the settings folder and scans it as its source, so the only .gitleaks.toml and
# .gitleaksignore it can find are the ones copied there; GIT_DIR points its git at the view.
gitleaks="$GITLEAKS"
if [[ "$gitleaks" == */* ]]; then gitleaks="$(realpath "$gitleaks")"; fi
config_args=()
if [[ -f "$config/.gitleaks.toml" ]]; then config_args=(--config "$config/.gitleaks.toml"); fi
set +e
(cd "$config" && env -u GITLEAKS_CONFIG -u GITLEAKS_CONFIG_TOML GIT_DIR="$view" GIT_NO_LAZY_FETCH=1 \
  "$gitleaks" git --no-banner --no-color --redact --verbose "${config_args[@]}" \
  --gitleaks-ignore-path "$config" --log-opts="$opts" "$config") > "$log" 2>&1
status=$?
set -e
cat "$log"
scanned="$(sed -E 's/\x1b\[[0-9;]*m//g' "$log" | sed -nE 's/.* ([0-9]+) commits scanned.*/\1/p' | tail -n 1)"
echo "gitleaks scanned ${scanned:-no} commit(s); $expected commit(s) in range have changes it reads"
summary "### Secret scan" ""
if [[ -n "$warning" ]]; then summary "Warning: $warning." ""; fi
# gitleaks 8.30.1 logs git's stderr as ERR (and `ERR error="stderr is not empty"`), stops reading
# diffs once git exits, and still exits 0; the count above misses that when the dropped files all
# belong to a commit it already counted. Any ERR line with exit 0 fails the job.
errors="$(sed -E 's/\x1b\[[0-9;]*m//g' "$log" | grep -E '^([^[:space:]]+[[:space:]]+)?ERR[[:space:]]' || true)"
if [[ $status -eq 0 && -n "$errors" ]]; then
  echo "::error::Secret scan: gitleaks logged an error and still passed, so it may not have read every change: $(head -n 1 <<< "$errors")" >&2
  summary "Failed: gitleaks logged an error while scanning $what (see the job log), so the scan may be incomplete."
  exit 2
fi
if [[ $status -eq 0 && ( -z "$scanned" || "$scanned" -lt "$expected" ) ]]; then
  echo "::error::Secret scan: gitleaks scanned ${scanned:-no} commit(s), but $expected commit(s) in range have changes (a git error stopped it?)" >&2
  summary "Failed: gitleaks scanned ${scanned:-no} of the $expected commit(s) with changes in $what, so the rest were not checked."
  exit 2
fi
if [[ $status -eq 0 ]]; then
  summary "No secrets found in $what ($scanned commit(s) with changes scanned)."
else
  summary "gitleaks found possible secrets in $what (values redacted; see the job log). Rotate any real secret: it is already public."
fi
exit "$status"
