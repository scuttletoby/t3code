#!/usr/bin/env bash
# Fork only: the scan job of fork-secret-scan.yml. Picks the commits a pull request or a push adds
# and runs gitleaks over them. Tests: fork-secret-scan.test.sh.
#
# Environment:
#   GITLEAKS     gitleaks binary
#   EVENT_NAME   pull_request or push
#   HEAD_SHA     pull_request: the PR head
#   BEFORE       push: the branch tip before the push (all zeros when the branch is new);
#                pull_request: the head before this push of it (synchronize), else empty
#   OLD_HEADS    pull_request: every tip the PR's branch has had, as far as the workflow could list
#                them (whitespace-separated commit ids; may be empty)
#   AFTER        push: the branch tip after the push
#   PUSH_REF     push: the ref pushed (refs/heads/<name> or refs/tags/<name>); empty is read as the
#                default branch
#   UPSTREAM     push: owner/name of the repository this fork follows (the workflow reads this
#                fork's parent, as fork-follow.yml does); empty when it could not be read
#   UPSTREAM_URL push: git URL of upstream (default https://github.com/$UPSTREAM.git, as in
#                fork-follow.sh)
#   CONFIG_BRANCH  the repository's default branch; for a pull request and a push to any other
#                ref, its tip holds the gitleaks settings the scan uses (see "Settings" below) and
#                is what the range leaves out
#   RUNNER_TEMP  folder for the scan's temporary files (else TMPDIR, else /tmp)
#
# Ranges:
#   pull_request   <default branch tip>..HEAD_SHA: every commit the PR head has that the default
#                  branch has not, merges included, whatever their dates and whatever the PR's base.
#                  Not BASE..HEAD: a PR against a side branch (whose pushes no scan reads) would
#                  leave that branch's commits out, and retargeting the PR to the default branch
#                  fires `edited`, which does not re-run the scan; this range does not depend on the
#                  base, so a retarget changes nothing. The default branch's own commits were scanned
#                  by its push runs. A squash merge never brings the PR's own commits to main, but
#                  they stay public under refs/pull/N/head, so they are scanned here.
#   pull_request, replaced heads  a force push replaces the head, but GitHub still serves the old
#                  head's commits by id: a key there is public. The check the ruleset requires
#                  runs on the newest head only (older runs are cancelled or no longer shown), so
#                  the range also holds every earlier head (BEFORE and OLD_HEADS) that is neither
#                  in the new head nor on the default branch: `HEAD_SHA <earlier heads>
#                  ^<default tip>`. The pull request stays red until the key is rotated and its
#                  exact, now dead, value is allowed in .gitleaks.toml (a pull request of its own),
#                  or the work moves to a branch of another name. An earlier head that can no
#                  longer be fetched gets a warning (GitHub no longer serves it; its push was
#                  scanned by the push run). Each push of the head also gets a run of its own that
#                  nothing cancels (fork-secret-scan.yml).
#   push, other ref  a push to any branch but the default one, or a tag: AFTER ^<default tip>, every
#                  commit the pushed tip has that the default branch has not (a new branch
#                  included), with the default branch's settings, as for a pull request. Such a
#                  branch is unreviewed, so neither its range nor its settings may depend on its
#                  own earlier pushes.
#   push           (the default branch) BEFORE..AFTER. After a force push that is every commit the
#                  new tip has and the old one had not; commits both share were scanned when they
#                  were first pushed.
#   push, no BEFORE  (the default branch) an old tip that can no longer be fetched, or a new
#                  branch: only AFTER itself, with a warning. Everything before it is history this
#                  fork already had (upstream's, or scanned by earlier runs), and there is no older
#                  tip to tell new commits from old ones.
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
# never come from the checkout (the pull request's head). For a pull request and a push to any
# other ref: the default branch's tip (CONFIG_BRANCH, fetched each run), which the range leaves out.
# For a push to the default branch: the tip before the push (BEFORE). A push with no fetchable BEFORE (a new branch, or an old tip that is gone) reads
# AFTER's first parent, a commit of the same push that no scan reads, so there the push can set
# its own settings: gitleaks' built-in defaults instead would bring back the broad path skip list
# .gitleaks.toml removes, and a push run takes this script and the workflow from the pushed commit
# anyway. A root commit uses gitleaks' defaults. In this repository that case is the default
# branch only (other refs read the default branch's settings), which is never new. gitleaks runs in a temporary folder holding only those two
# files, so it finds no other; `[extend] path` therefore cannot load, and settings gitleaks cannot
# load fail the job (exit 2) with that reason.
# A range that changes the settings gets a notice saying when the change applies. The reviewed path
# for a change is a pull request of its own that changes only these files (its own scan uses the
# old settings, and it adds no key); once it is merged, re-run the scan of the pull request that
# needs it.
# A reviewed fake key in a test: allow its exact value in .gitleaks.toml (an entry with the rule, the
# file and the value; see "A reviewed fake key" there), or put a `gitleaks:allow` comment on the line.
# A value matches in every commit, whatever its line number or commit id, so it survives a squash
# merge (a new id), a later commit that moves the line, and a fork-follow rebase (which scans every
# fork commit again, with new ids); and it hides no other key on that line. .gitleaksignore lines
# name a line number instead: a line without a commit (file:rule:line) hides every key of that rule
# on that line in every commit, a real key a later commit puts there included, so the scan refuses
# such a line (exit 2); a line pinned to a commit (commit:file:rule:line) stops matching after a
# squash merge, so it gets a warning. With no .gitleaks.toml at all, gitleaks' built-in settings
# apply, whose path skip list hides whole files: a warning.
# A squash merge's own commit adds no line the PR's commits did not add (only line numbers and the
# commit id change), and a value entry does not depend on either, so the PR scan does not scan the
# squash result as one more diff.
#
# Not covered: a `gitleaks:allow` comment on the line itself (visible in the diff); a pull request
# that edits the workflow (pull_request runs take it from the pull request; the workflow runs this
# script from the default branch's tip, so an edit of the script alone changes nothing); text
# in another encoding than UTF-8 or UTF-16 with a byte-order mark (UTF-16 without one, UTF-32);
# compressed or packed contents (zip, gzip, images' compressed data); a key split across lines.
#
# Contents: git log runs with --text, so a file git takes for binary (a NUL byte in it) still shows
# its lines. A path whose contents in the range start with a UTF-16 byte-order mark goes through a
# textconv filter that prints it as stored and decoded to UTF-8 as well (a key in a UTF-16 .env as
# some Windows editors save it). The filter only adds lines, so a file that merely starts with those
# two bytes is still read as stored. Line numbers in the decoded part follow the stored lines. A
# stray last byte (an odd length) is dropped before decoding; when iconv cannot decode (it lacks
# UTF-16, or says anything), the filter fails and so does the job (exit 2), with iconv's message.
#
# Attributes: a .gitattributes in the range could mark a file `-diff` or binary, so git log shows
# "Binary files differ" instead of its lines. Both git logs below read attributes from a tree the
# script writes (GIT_ATTR_SOURCE: only the filter's lines, `/<path> diff=fork-secret-scan-text`),
# never from the checkout or a commit.
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
short() { printf '%s ' "$@" | sed -E 's/([0-9a-f]{12})[0-9a-f]+/\1/g; s/ $//'; }
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
other_ref=""
case "$EVENT_NAME" in
  pull_request)
    if ! fetch "$HEAD_SHA"; then
      echo "::error::Secret scan: cannot fetch commit $HEAD_SHA" >&2
      exit 2
    fi
    if ! settings="$(fetch_config_branch)"; then
      echo "::error::Secret scan: cannot fetch the default branch '${CONFIG_BRANCH:-}' (CONFIG_BRANCH), whose gitleaks settings the scan uses" >&2
      exit 2
    fi
    settings_what="the default branch $CONFIG_BRANCH (${settings:0:12})"
    changes=("$settings...$HEAD_SHA")
    # Everything the head has that the default branch has not, whatever the PR's base (see the header).
    range="$settings..$HEAD_SHA"
    what="the pull request's $(git rev-list --count "$range") commit(s) not on $CONFIG_BRANCH, merges included"
    # Earlier heads a force push replaced (see the header): what they had that neither the head nor
    # the default branch has is scanned too.
    tips=()
    gone=()
    for old in ${BEFORE:-} ${OLD_HEADS:-}; do
      zero "$old" && continue
      if ! [[ "$old" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
        echo "::error::Secret scan: '$old' in BEFORE or OLD_HEADS is not a commit id" >&2
        exit 2
      fi
      [[ " $HEAD_SHA ${tips[*]} ${gone[*]} " != *" $old "* ]] || continue
      if ! fetch "$old"; then gone+=("$old"); continue; fi
      if git merge-base --is-ancestor "$old" "$HEAD_SHA" || git merge-base --is-ancestor "$old" "$settings"; then
        continue
      fi
      tips+=("$old")
    done
    if [[ ${#tips[@]} -gt 0 ]]; then
      range="$HEAD_SHA ${tips[*]} ^$settings"
      # shellcheck disable=SC2086
      what="$what, and the $(( $(git rev-list --count $range) - $(git rev-list --count "$settings..$HEAD_SHA") )) commit(s) only the ${#tips[@]} earlier head(s) a force push replaced ($(short "${tips[@]}")) had"
    fi
    if [[ ${#gone[@]} -gt 0 ]]; then
      warning="${#gone[@]} earlier head(s) of this pull request cannot be fetched ($(short "${gone[@]}")), so they are not scanned here (each push of them had a push scan of its own)"
    fi
    opts="--remerge-diff --text $range"
    ;;
  push)
    if [[ -n "${PUSH_REF:-}" && "$PUSH_REF" != "refs/heads/${CONFIG_BRANCH:-}" ]]; then
      # A push to any other branch (or a tag): everything its tip has that the default branch has
      # not, read with the default branch's settings, as for a pull request (see the header).
      if ! fetch "$AFTER"; then
        echo "::error::Secret scan: cannot fetch commit $AFTER" >&2
        exit 2
      fi
      if ! settings="$(fetch_config_branch)"; then
        echo "::error::Secret scan: cannot fetch the default branch '${CONFIG_BRANCH:-}' (CONFIG_BRANCH), whose gitleaks settings the scan uses" >&2
        exit 2
      fi
      settings_what="the default branch $CONFIG_BRANCH (${settings:0:12})"
      other_ref=1
      changes=("$settings...$AFTER")
      range="$AFTER ^$settings"
      opts="--remerge-diff --text $range"
      # shellcheck disable=SC2086
      what="the $(git rev-list --count $range) commit(s) $PUSH_REF has that $CONFIG_BRANCH has not"
    elif ! zero "$BEFORE" && fetch "$BEFORE"; then
      settings="$BEFORE"
      settings_what="the tip before the push (${BEFORE:0:12})"
      changes=("$BEFORE" "$AFTER")
      range="$BEFORE..$AFTER"
      opts="--remerge-diff --text $range"
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
      opts="--remerge-diff --text $range"
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
      opts="--remerge-diff --text $range"
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
  echo "::error::Secret scan: cannot read the commits to scan (a file content fetch failed, or a file with a UTF-16 byte-order mark could not be decoded: see the messages above)" >&2
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
config_args=()
if [[ -f "$config/.gitleaks.toml" ]]; then
  config_args=(--config "$config/.gitleaks.toml")
else
  echo "::warning title=Secret scan::No .gitleaks.toml on $settings_what, so gitleaks' built-in settings apply: they skip whole paths (lock files, .svg, node_modules and more), where a key goes unseen. Add a .gitleaks.toml (this repository's shows how) to scan them."
fi
# .gitleaksignore lines name a line number, not a value (see the header). A line without a commit
# hides every key of its rule on that line in every commit: refused. A line pinned to a commit
# stops matching once a squash merge gives the commit a new id: a warning.
if [[ -f "$config/.gitleaksignore" ]]; then
  lines="$(grep -vE '^[[:space:]]*(#|$)' "$config/.gitleaksignore" || true)"
  unpinned="$(grep -cvE '^[[:space:]]*[0-9a-f]{40}([0-9a-f]{24})?:' <<< "$lines" || true)"
  if [[ -n "$lines" && "$unpinned" -gt 0 ]]; then
    echo "::error::Secret scan: .gitleaksignore on $settings_what has $unpinned line(s) without a commit (file:rule:line). Such a line hides every key of that rule on that line of that file, in every commit, a real one included, so the scan refuses it. Allow a reviewed fake key by its exact value in .gitleaks.toml instead (see \"A reviewed fake key\" there), or with a gitleaks:allow comment on the line." >&2
    summary "### Secret scan" "" "Failed: .gitleaksignore on $settings_what has line(s) without a commit (file:rule:line), which the scan refuses; nothing was scanned."
    exit 2
  fi
  pinned="$(grep -c . <<< "$lines" || true)"
  if [[ -n "$lines" && "$pinned" -gt 0 ]]; then
    echo "::warning title=Secret scan::.gitleaksignore on $settings_what has $pinned line(s) pinned to a commit (commit:file:rule:line). Such a line stops matching once a squash merge gives the commit a new id, so the default branch's push scan fails on the same finding. Allow the exact value in .gitleaks.toml instead."
  fi
fi
# Only a message: when the diff fails (no merge base, say), there is none.
if [[ ${#changes[@]} -gt 0 ]]; then
  changed="$(git diff --no-renames --name-only "${changes[@]}" -- .gitleaks.toml .gitleaksignore 2> /dev/null)" || changed=""
  if [[ -n "$changed" ]]; then
    files="$(tr '\n' ' ' <<< "$changed")"
    if [[ -n "$other_ref" ]]; then
      echo "::notice title=Secret scan::This push changes ${files}but the scan reads them from $settings_what. A change to them takes effect once it is on the default branch (a pull request of its own)."
    elif [[ "$EVENT_NAME" == push ]]; then
      echo "::notice title=Secret scan::This push changes ${files}but this scan read them from $settings_what. The change applies from the next push."
    elif [[ "$(git diff --no-renames --name-only "${changes[@]}" 2> /dev/null)" == "$changed" ]]; then
      echo "::notice title=Secret scan::This pull request changes only ${files}(the reviewed path for a settings change): this scan read them from $settings_what, and the change applies once it is merged."
    else
      echo "::notice title=Secret scan::This pull request changes ${files}but the scan reads them from $settings_what. A change to them takes effect once it is on the default branch: land it in a pull request of its own first, then re-run this scan."
    fi
  fi
fi

# The contents every diff in the range compares (each commit against each parent, merges included),
# in one batch before git log runs, so that git log's own batches hold either temporary objects or
# real contents (the merge base's side of an automatic merge), never both (see the header).
if ! git -c core.quotepath=false diff-tree --stdin -m -r --no-commit-id --raw --no-abbrev < "$work/commits" > "$work/raw" ||
  ! awk '$1 != ":160000" && $3 !~ /^0+$/ { print $3 } $2 != "160000" && $4 !~ /^0+$/ { print $4 }' "$work/raw" |
    sort -u > "$work/wanted" ||
  ! git cat-file --batch-check='%(objectname)' --batch-all-objects 2> /dev/null | sort -u > "$work/have" ||
  ! comm -23 "$work/wanted" "$work/have" > "$work/missing" ||
  { [[ -s "$work/missing" ]] && ! timeout 600 git -c fetch.negotiationAlgorithm=noop fetch --quiet --no-tags \
    --no-write-fetch-head --recurse-submodules=no --filter=blob:none origin --stdin < "$work/missing"; }; then
  cannot_read
fi

# Attributes and the text filter for both git logs (see the header). The filter applies to the paths
# whose contents in the range start with a UTF-16 byte-order mark: one read of the contents fetched
# above finds them; a path with a character a .gitattributes pattern could misread puts every file
# under the filter instead (it only adds lines). Each run of the filter costs a process or two, too
# slow for every file of a wide range. git runs it through sh, set in the environment (GIT_CONFIG_*,
# added to any already set).
# shellcheck disable=SC2016
if ! git cat-file --batch < "$work/wanted" | perl -e '
    binmode STDIN;
    while (defined(my $head = <STDIN>)) {
      my ($oid, $type, $size) = split " ", $head;
      die "fork-secret-scan: no content: $head" unless defined $size;
      read(STDIN, my $data, $size) == $size or die "fork-secret-scan: short read of $oid\n";
      read(STDIN, my $newline, 1);
      print "$oid\n" if $data =~ /\A(?:\xff\xfe|\xfe\xff)/;
    }' > "$work/utf16" ||
  ! awk -F '\t' 'NR == FNR { utf16[$1] = 1; next }
      { split($1, side, " "); if ((side[3] in utf16) || (side[4] in utf16)) print $2 }' \
      "$work/utf16" "$work/raw" | sort -u > "$work/utf16-paths" ||
  ! awk '/^[A-Za-z0-9._\/+@,=-]+$/ { print "/" $0 " diff=fork-secret-scan-text"; next }
      { every = 1 } END { if (every) print "* diff=fork-secret-scan-text" }' "$work/utf16-paths" > "$work/gitattributes" ||
  ! attributes="$(git hash-object -w "$work/gitattributes")" ||
  ! GIT_ATTR_SOURCE="$(printf '100644 blob %s\t.gitattributes\n' "$attributes" | git mktree)"; then
  cannot_read
fi
export GIT_ATTR_SOURCE
if [[ -s "$work/utf16-paths" ]]; then
  echo "$(wc -l < "$work/utf16-paths") file(s) start with a UTF-16 byte-order mark: read as stored and decoded"
fi
cat > "$work/textconv" <<'FILTER'
# $1: a temporary file holding one side of a diff. Print it as stored; when it starts with a
# UTF-16 byte-order mark, print it decoded to UTF-8 after it. iconv -c leaves out what is not valid
# UTF-16 (a lone surrogate) and exits 1; a stray last byte (an odd length) is dropped first, as it
# is half a character. Any other iconv failure, or any message from it, fails the filter and so
# git log, with a message naming the reason: never a pass with the decoded part missing.
case "$(od -An -N2 -tx1 -- "$1")" in
  *"ff fe"*) encoding=UTF-16LE ;;
  *"fe ff"*) encoding=UTF-16BE ;;
  *) exec cat -- "$1" ;;
esac
cat -- "$1" || exit 1
echo
size=$(($(wc -c < "$1") - 2))
exec 3>&1
err="$(tail -c +3 -- "$1" | head -c $((size - size % 2)) | iconv -c -f "$encoding" -t UTF-8 2>&1 >&3)"
status=$?
if [ "$status" -gt 1 ] || [ -n "$err" ]; then
  echo "fork-secret-scan: cannot decode a file with a UTF-16 byte-order mark ($encoding; iconv exit $status): $err" >&2
  exit 1
fi
FILTER
n="${GIT_CONFIG_COUNT:-0}"
export "GIT_CONFIG_KEY_$n=diff.fork-secret-scan-text.textconv"
export "GIT_CONFIG_VALUE_$n=sh $(printf '%q' "$work/textconv")"
export GIT_CONFIG_COUNT=$((n + 1))

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
set +e
(cd "$config" && env -u GITLEAKS_CONFIG -u GITLEAKS_CONFIG_TOML GIT_DIR="$view" GIT_NO_LAZY_FETCH=1 \
  "$gitleaks" git --no-banner --no-color --redact --verbose "${config_args[@]}" \
  --gitleaks-ignore-path "$config" --log-opts="$opts" "$config") > "$log" 2>&1
status=$?
set -e
cat "$log"
# The log without colours, read below.
plain="$(tr -d '\000' < "$log" | sed -E 's/\x1b\[[0-9;]*m//g')"
scanned="$(sed -nE 's/.* ([0-9]+) commits scanned.*/\1/p' <<< "$plain" | tail -n 1)"
echo "gitleaks scanned ${scanned:-no} commit(s); $expected commit(s) in range have changes it reads"
summary "### Secret scan" ""
if [[ -n "$warning" ]]; then summary "Warning: $warning." ""; fi
# gitleaks 8.30.1 logs git's stderr as ERR (and `ERR error="stderr is not empty"`), stops reading
# diffs once git exits, and still exits 0; the count above misses that when the dropped files all
# belong to a commit it already counted. Any ERR line with exit 0 fails the job.
errors="$(grep -aE '^([^[:space:]]+[[:space:]]+)?ERR[[:space:]]' <<< "$plain" || true)"
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
# gitleaks exits 1 both for findings ("leaks found: N") and for a fatal error, such as settings it
# cannot load ("FTL Failed to load config"; `[extend] path` cannot load here, see the header).
if [[ $status -ne 0 ]] && ! grep -aqE 'leaks found: [1-9][0-9]*' <<< "$plain"; then
  reason="$(grep -a -m 1 -E '(FTL|ERR)[[:space:]]' <<< "$plain" || true)"
  echo "::error::Secret scan: gitleaks stopped without a result (exit $status), so nothing was checked. Settings it cannot load (from $settings_what; \`[extend] path\` is not supported) cause this: ${reason:-see the log above}" >&2
  summary "Failed: gitleaks stopped without a result while scanning $what (settings from $settings_what could not be loaded?), so nothing was checked."
  exit 2
fi
if [[ $status -eq 0 ]]; then
  summary "No secrets found in $what ($scanned commit(s) with changes scanned)."
else
  summary "gitleaks found possible secrets in $what (values redacted; see the job log). Rotate any real secret: it is already public."
fi
exit "$status"
