#!/usr/bin/env bash
# Fork only: the scan job of fork-secret-scan.yml. Picks the commits a pull request or a push adds
# (or, in the daily sweep, every ref of origin holds but pull request heads) and runs gitleaks over
# their changes and their commit objects (message, author, committer). Tests:
# fork-secret-scan.test.sh.
#
# Environment:
#   GITLEAKS     gitleaks binary
#   EVENT_NAME   pull_request, push, or schedule / workflow_dispatch (the sweep, fork-secret-sweep.yml)
#   HEAD_SHA     pull_request: the PR head
#   BEFORE       push: the branch tip before the push (all zeros when the branch is new);
#                pull_request: the head before this push of it (synchronize), else empty
#   OLD_HEADS    pull_request: every tip the PR's branch has had, as far as the workflow could list
#                them; schedule / workflow_dispatch (the sweep): every tip any ref has had, as
#                GitHub's activity record lists them (whitespace-separated commit ids; may be empty)
#   SCANNED_GONE  the sweep: those OLD_HEADS GitHub's API has no commit for at all (the workflow
#                asks GitHub's GraphQL API, repository.object(oid)) that a run with a successful
#                secret-scan job had as its head commit (the workflow asks GitHub's Actions API),
#                with the job's token; whitespace-separated commit ids; may be empty. See "sweep,
#                gone tips"
#   DELETED_TIPS the sweep: those OLD_HEADS GitHub's API has no commit for at all that origin did
#                not serve either (the workflow asked both; or its record, #691, has the tip as read
#                by a scan on an earlier day, so git is not asked again), SCANNED_GONE among them;
#                whitespace-separated commit ids; may be empty. The workflow gives it as
#                DELETED_TIPS_FILE only (the list can outgrow the environment); the variable is read
#                when no file is set, as for the others. See "sweep, deleted tips"
#   OLD_HEADS_FILE, SCANNED_GONE_FILE, DELETED_TIPS_FILE  a file holding that list, read instead of
#                the variable when set (#637: the kernel caps one environment string at 128 KiB,
#                and the sweep's list outgrew it); a file that cannot be read fails the job
#   AFTER       push: the branch tip after the push
#   PUSH_REF     push: the ref pushed (refs/heads/<name> or refs/tags/<name>); empty is read as the
#                default branch
#   UPSTREAM     owner/name of the repository this fork follows (the workflow reads this fork's
#                parent, as fork-follow.yml does); empty when it could not be read
#   UPSTREAM_URL git URL of upstream (default https://github.com/$UPSTREAM.git, as in
#                fork-follow.sh)
#   FETCH_RETRY_DELAY  seconds between tries of an earlier head's fetch, times the try (default 5)
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
#                  ^<default tip>`, minus what upstream has (as for a push, below: a head from
#                  before a fork-follow rebase holds the upstream commits main was rebased away
#                  from). The pull request stays red until the key is rotated and its
#                  exact, now dead, value is allowed in .gitleaks.toml (a pull request of its own),
#                  or the work moves to a branch of another name. An earlier head origin says it
#                  no longer has ("not our ref", "no such object") gets a warning (GitHub no longer
#                  serves it; its push was scanned by the push run). Any other fetch failure (the
#                  network, a 5xx) is tried 4 times, then fails the job (exit 2): re-run it. Each
#                  push of the head also gets a run of its own that nothing cancels
#                  (fork-secret-scan.yml).
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
#   schedule, workflow_dispatch  the sweep: every ref origin lists but refs/pull/* (branches, tags,
#                  notes such as refs/notes/commits, any other refs/<name>/*; fetched into
#                  refs/scan-sweep/*, which each run empties first), minus what upstream has, with
#                  the default branch's settings. It catches what no push run read: GitHub starts
#                  no push run for a push with [skip ci] in its message, for a push of more than 3
#                  tags at once, for a push made with a workflow's GITHUB_TOKEN, or for a ref that
#                  is not a branch or tag (notes), and branches older than this scan were never
#                  read. A finding is old by then: rotate it. A failed sweep opens (or comments on)
#                  an issue (fork-secret-sweep.yml), as a schedule run alerts no one otherwise.
#   sweep, vanished tips  a [skip ci] push to a branch that is deleted or force-pushed away before
#                  the sweep runs is held by no ref, but GitHub still serves its commits by id. So
#                  the sweep also reads every tip OLD_HEADS lists (the workflow lists GitHub's
#                  activity record: each push, force push and deletion with the tips before and
#                  after), fetched by id, all at once and then one by one; any fetch failure but
#                  "not our ref" fails the job.
#   sweep, gone tips  a listed tip origin says it no longer has ("not our ref") may be one no run
#                  read. When GitHub's API has no commit of that id at all and a successful
#                  secret-scan job read it while it was served (SCANNED_GONE), it gets a warning
#                  with the count (#482: the old tips of force-pushed or deleted branches; GitHub
#                  deletes them within hours, so most are gone before a daily sweep). Any other gone
#                  tip fails the sweep (exit 2), after the rest is scanned, naming each id: no run
#                  is known to have scanned it, and it may have been public for hours (a [skip ci]
#                  push, a push with a workflow's token) or GitHub may still show it on the web.
#                  A branch's own workflow file runs its push and pull request scans, so this
#                  trusts the people who can push here, as OLD_HEADS already does. The way out is
#                  the reviewed list of tips known gone, .github/fork-secret-sweep-gone-tips.txt, read
#                  from the default branch's tip (CONFIG_BRANCH) only, never from the checkout or
#                  another branch: one line per tip, `<full commit id> <why it is known gone>`, plus
#                  # comments and blank lines. A listed tip origin no longer serves gets a warning; one
#                  it serves (it came back) is scanned, with a warning to drop its line. No file is an
#                  empty list; a file that is not a plain file, cannot be read, has any other line, or
#                  has a carriage return anywhere (CRLF line ends: one rule for every line, comments
#                  included) fails the job (exit 2), whether or not a tip is gone. Entries the record
#                  no longer lists change nothing.
#   sweep, deleted tips  a listed tip DELETED_TIPS names that this clone lacks is gone without a
#                  fetch (#691): one by one, with retries, the fetches of over a hundred deleted
#                  tips made every sweep slower. It is then a gone tip as above (a warning only when
#                  SCANNED_GONE or the reviewed list names it). One this clone has is scanned.
#
# Commit objects: each commit of the range as git stores it (`git cat-file`: its message, author
# and committer names and emails, signature and other headers), and on a tag push the pushed tag's
# object (each annotated tag in the chain, with its message and tagger; the sweep reads every
# ref's, but those of tag objects upstream also has), go through `gitleaks stdin` with the same
# settings. A finding names the commit or tag. (Not `git log --format=%B`: that is the message
# alone, and a key in an author or committer field would pass.)
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
# Shallow checkout: a range in a shallow clone stops at its graft, so the scan would pass on commits
# it never read; the job fails (exit 2) before anything else. The workflow checks out fetch-depth: 0.
#
# Not covered (the job summary lists these too): a `gitleaks:allow` comment on the line itself
# (visible in the diff); ref names (branch, tag and note names); the pull request's title, body and
# comments (GitHub data, not git); commits only an outside pull request (refs/pull/N/head) holds, as
# the sweep leaves refs/pull/* out; a ref that names a tree or blob rather than a commit (the sweep
# warns, and reads only its tag messages); a pull request
# that edits the workflow (pull_request runs take it from the pull request; the workflow runs this
# script from the default branch's tip, so an edit of the script alone changes nothing); text
# in another encoding than UTF-8 or UTF-16 with a byte-order mark (UTF-16 without one, UTF-32);
# compressed or packed contents (zip, gzip, images' compressed data); a key split across lines;
# a vanished tip (see "sweep, vanished tips") GitHub's activity record does not list (it records
# branches: a deleted tag or notes ref is not in it; an entry older than what GitHub keeps of the
# record (the workflow reads all of it, with no time window)) or that is on the reviewed list of
# tips known gone (origin will not send it by id any more, although GitHub's web pages may still show
# it; a warning).
#
# Credentials: the script sends whatever the checkout's git config holds. actions/checkout keeps the
# job's token there (persist-credentials, on); the workflow's step right after the checkout takes it
# out and fails if any extraheader is left (#317), so origin fetches here carry none. That works for a
# public repository; a private one would need its own token for these fetches (they fail without
# one, and so does the scan). gitleaks reads a view without that config (below).
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

# git_retry: a copy of fork-git-retry.sh (the workflow runs this script alone, from the default branch, outside the
# checkout); fork-git-retry.test.py holds it to that file. A fetch whose failure fails the scan goes through it: tried
# again after a connection error only, within the same time limit the fetch had.
git_retry() {
  local net real waits err code=0 limit try=1 end=$((SECONDS + ${GIT_RETRY_TOTAL:-150}))
  net='Failed to connect|Could(n.t| not) resolve|Connection (refused|reset|timed out)|Operation timed out|Network is unreachable'
  net+='|Temporary failure in name resolution|Recv failure|Send failure|SSL_ERROR_SYSCALL|SSL_(connect|read)|gnutls_handshake'
  net+='|TLS connect error|returned error: (429|5[0-9][0-9])|RPC failed|early EOF|unexpected disconnect|hung up unexpectedly'
  net+='|was not closed cleanly|transfer closed with|Empty reply from server'
  real='not our ref|find remote ref|no such remote ref|unadvertised object|Authentication failed|could not read (Username|Password)'
  real+='|Repository not found|returned error: 4([01][0-9]|2[0-8]|[3-9][0-9])|not appear to be a git repository|invalid refspec'
  read -r -a waits <<< "${GIT_RETRY_WAITS:-5 10 20}"
  while :; do
    limit=$((end - SECONDS))
    ((limit < ${GIT_RETRY_TRY:-90})) || limit=${GIT_RETRY_TRY:-90}
    ((limit > 0)) || limit=1
    code=0
    if [[ -n "${GIT_RETRY_STDIN:-}" ]]; then
      { err="$(LC_ALL=C timeout -k 5 "$limit" git "$@" 2>&1 >&3 3>&- < "$GIT_RETRY_STDIN")" || code=$?; } 3>&1
    else
      { err="$(LC_ALL=C timeout -k 5 "$limit" git "$@" 2>&1 >&3 3>&-)" || code=$?; } 3>&1
    fi
    [[ -z "$err" ]] || printf '%s\n' "$err" >&2
    ((code)) || return 0
    if ((code != 124 && code != 137)) && { ! grep -qE "$net" <<< "$err" || grep -qE "$real" <<< "$err"; }; then
      return "$code"
    fi
    if ((try > ${#waits[@]})) || ((SECONDS + ${waits[try - 1]} + 5 > end)); then
      echo "::error::git $1: a connection error on try $try, the last one (git_retry: no tries or time left)" >&2
      return "$code"
    fi
    echo "::warning::git $1: a connection error on try $try (exit $code); trying again in ${waits[try - 1]} s" >&2
    sleep "${waits[try - 1]}"
    try=$((try + 1))
  done
}

# OLD_HEADS_FILE / SCANNED_GONE_FILE / DELETED_TIPS_FILE, when set, are read instead of the variable (#637), which is
# then no longer exported; a file that cannot be read fails the job rather than leaving the list empty.
for list in OLD_HEADS SCANNED_GONE DELETED_TIPS; do
  file_var="${list}_FILE"
  [[ -n "${!file_var:-}" ]] || continue
  if ! words="$(cat -- "${!file_var}" 2> /dev/null)"; then
    echo "::error::Secret scan: cannot read $file_var (${!file_var}), so the tips it lists would go unscanned. Re-run the job." >&2
    exit 2
  fi
  # Unexported first: a list this long in the environment stops every program the script starts.
  export -n "$list"
  printf -v "$list" '%s' "$words"
done

# GIT_NO_LAZY_FETCH: in a blob-less clone a missing commit would otherwise be fetched here, one by
# one with no timeout, behind fetch_earlier's back.
have() { GIT_NO_LAZY_FETCH=1 git cat-file -e "$1^{commit}" 2>/dev/null; }
# Commits only, file contents on demand: the checkout is a blob-less clone.
fetch() {
  have "$1" || git_retry fetch --no-tags --quiet --filter=blob:none origin "$1" 2>/dev/null || true
  have "$1"
}
# An earlier head of a pull request (see "replaced heads"): 0 when it is here; 1 when origin answers
# that it does not have it ("not our ref", "no such object": GitHub no longer serves it); 2 when the
# fetch still fails after 4 tries for any other reason (network, a 5xx), with git's message.
fetch_earlier() {
  local try err=""
  have "$1" && return 0
  for try in 1 2 3 4; do
    if err="$(timeout 300 git fetch --no-tags --quiet --filter=blob:none origin "$1" 2>&1)" && have "$1"; then
      return 0
    fi
    if grep -qiE 'not our ref|no such object' <<< "$err"; then return 1; fi
    if [[ $try -lt 4 ]]; then sleep "$(( ${FETCH_RETRY_DELAY:-5} * try ))"; fi
  done
  printf '%s\n' "$err" >&2
  return 2
}
zero() { [[ -z "$1" || "$1" =~ ^0+$ ]]; }
# earlier WHAT ID...: the earlier tips a pull request's branch (BEFORE, OLD_HEADS) or, in the sweep,
# any ref (OLD_HEADS) has had. Each id once, all zeros skipped; a word that is not a commit id fails
# the job. Sets listed (fetched, so readable) and gone (origin says it no longer has them); a fetch
# that fails for any other reason fails the job (see fetch_earlier). An id in skip_fetch (the sweep's
# DELETED_TIPS) this clone lacks is gone without a fetch.
declare -A skip_fetch=()
earlier() {
  local label="$1" old got ids=() missing=()
  shift
  listed=()
  gone=()
  for old in "$@"; do
    zero "$old" && continue
    if ! [[ "$old" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
      echo "::error::Secret scan: '$old' in BEFORE or OLD_HEADS is not a commit id" >&2
      exit 2
    fi
    [[ " ${ids[*]} " != *" $old "* ]] || continue
    ids+=("$old")
    have "$old" || [[ -n "${skip_fetch[$old]:-}" ]] || missing+=("$old")
  done
  # One fetch for all of them first (the sweep may list hundreds); one by one, with retries, only
  # when it fails (any of them gone fails the whole fetch).
  if [[ ${#missing[@]} -gt 0 ]]; then
    timeout 600 git fetch --no-tags --quiet --filter=blob:none origin "${missing[@]}" 2> /dev/null || true
  fi
  for old in "${ids[@]}"; do
    if [[ -n "${skip_fetch[$old]:-}" ]] && ! have "$old"; then gone+=("$old"); continue; fi
    got=0
    fetch_earlier "$old" || got=$?
    if [[ $got -eq 1 ]]; then gone+=("$old"); continue; fi
    if [[ $got -ne 0 ]]; then
      echo "::error::Secret scan: cannot fetch $label $old, and origin did not say it no longer has it (a network or server failure?), so what it had cannot be scanned. Re-run the job." >&2
      exit 2
    fi
    listed+=("$old")
  done
}
short() { printf '%s ' "$@" | sed -E 's/([0-9a-f]{12})[0-9a-f]+/\1/g; s/ $//'; }
# Upstream's default branch and v* tags (the same tags fork-follow.sh reads), commits only, into
# refs/scan-upstream/*. Refs from an earlier run go first, so a tag upstream has deleted since is
# not left out. A failed fetch adds no exclusion at all.
fetch_upstream() {
  git for-each-ref --format='delete %(refname)' refs/scan-upstream/ | git update-ref --stdin || return 1
  [[ -n "${UPSTREAM:-}" ]] || return 1
  local url="${UPSTREAM_URL:-https://github.com/$UPSTREAM.git}"
  GIT_RETRY_TRY=600 GIT_RETRY_TOTAL=600 git_retry fetch --quiet --no-tags --filter=blob:none "$url" \
    "+HEAD:refs/scan-upstream/head" "+refs/tags/v*:refs/scan-upstream/tags/v*" >&2 || return 1
  git rev-parse --quiet --verify refs/scan-upstream/head > /dev/null
}
# Leave upstream's commits out of $range (see "push, upstream"), and say so in $what; when upstream is
# unknown or cannot be fetched, nothing is left out, with a warning.
without_upstream() {
  local full="$range" reason
  if fetch_upstream; then
    upstream_used=1
    range="$range --not --glob=refs/scan-upstream"
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
}
# The tag objects from $1 down to the commit (or other object) it names, one per line.
tag_chain() {
  local obj="$1"
  while [[ "$(git cat-file -t "$obj")" == tag ]]; do
    echo "$obj"
    obj="$(git cat-file tag "$obj" | sed -n '1s/^object //p')"
  done
}
summary() { if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then printf '%s\n' "$@" >> "$GITHUB_STEP_SUMMARY"; fi; }
# The settings commit for a pull request: the default branch's tip, commits only, into
# refs/scan-config/head (overwritten each run; a failed fetch fails the job, so an old one is never
# used).
fetch_config_branch() {
  [[ -n "${CONFIG_BRANCH:-}" ]] || return 1
  git check-ref-format "refs/heads/$CONFIG_BRANCH" || return 1
  GIT_RETRY_TRY=600 GIT_RETRY_TOTAL=600 git_retry fetch --quiet --no-tags --filter=blob:none origin \
    "+refs/heads/$CONFIG_BRANCH:refs/scan-config/head" >&2 || return 1
  git rev-parse --quiet --verify 'refs/scan-config/head^{commit}'
}
# The reviewed list of tips known gone (see "sweep, gone tips") on commit $1, the default branch's
# tip: sets known_gone to " <id> <id> ... ". No file is an empty list; a list the scan cannot trust
# (not a plain file, unreadable, a line that is not `<full commit id> <why>`, a carriage return on
# any line) fails the job (exit 2). A tree that cannot be read fails too: read as no file, the list
# would be empty, which is strictest for gone tips but would hide that the list was never read.
gone_list=.github/fork-secret-sweep-gone-tips.txt
read_gone_list() {
  local entry mode type oid text line id n=0 where="on the default branch ${CONFIG_BRANCH:-} (${1:0:12})"
  known_gone=" "
  if ! entry="$(git ls-tree "$1" -- "$gone_list")"; then
    echo "::error::Secret scan: cannot read $gone_list $where (the reviewed list of tips known gone)" >&2
    exit 2
  fi
  [[ -n "$entry" ]] || return 0
  read -r mode type oid _ <<< "$entry"
  if [[ "$type" != blob || ( "$mode" != 100644 && "$mode" != 100755 ) ]]; then
    echo "::error::Secret scan: $gone_list $where is not a plain file ($mode $type), so the list of tips known gone cannot be read" >&2
    exit 2
  fi
  if ! text="$(git cat-file blob "$oid")"; then
    echo "::error::Secret scan: cannot read $gone_list $where (the reviewed list of tips known gone; see the messages above)" >&2
    exit 2
  fi
  # Read again from the local object (fetched above). A NUL byte is dropped by the shell (its entry
  # would still count) and makes diffs show the file as binary, hiding that entry: refuse it.
  if [[ "$(git cat-file blob "$oid" | LC_ALL=C tr -cd '\000' | wc -c)" -ne 0 ]]; then
    echo "::error::Secret scan: $gone_list $where has a NUL byte, so the list of tips known gone cannot be trusted. Save it as plain text on the default branch (a reviewed pull request)." >&2
    exit 2
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    n=$((n + 1))
    # A carriage return anywhere fails, so `<id>\r` and `<id> why\r` (and a CRLF comment) are judged
    # alike: the file is saved with LF line ends.
    if [[ "$line" == *$'\r'* ]]; then
      echo "::error::Secret scan: line $n of $gone_list $where has a carriage return (CRLF line ends?), so the list of tips known gone cannot be trusted. Save it with LF line ends on the default branch (a reviewed pull request)." >&2
      exit 2
    fi
    [[ ! "$line" =~ ^[[:space:]]*(#|$) ]] || continue
    id=""
    if [[ "$line" =~ ^([0-9a-f]{40}([0-9a-f]{24})?)[[:space:]]+[^[:space:]] ]]; then id="${BASH_REMATCH[1]}"; fi
    if [[ -z "$id" ]] || zero "$id"; then
      echo "::error::Secret scan: line $n of $gone_list $where is not '<full commit id> <why it is known gone>', so the list of tips known gone cannot be trusted. Fix it on the default branch (a reviewed pull request)." >&2
      exit 2
    fi
    known_gone+="$id "
  done <<< "$text"
}

# A shallow checkout (fetch-depth other than 0) ends the history at its graft: a range then holds
# fewer commits than it should and the scan passes on what it never read (#239). Refused.
if [[ "$(git rev-parse --is-shallow-repository 2> /dev/null)" != false ]]; then
  echo "::error::Secret scan: the checkout is shallow (or not a git repository), so the range would miss commits and the scan would pass on what it never read. Check out with fetch-depth: 0." >&2
  summary "### Secret scan" "" "Failed: the checkout is shallow, so nothing was scanned. Check out with fetch-depth: 0."
  exit 2
fi

warning=""
gone_failure=""
other_ref=""
tag_objects=()
upstream_used=""
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
    earlier "the earlier head" ${BEFORE:-} ${OLD_HEADS:-}
    tips=()
    for old in "${listed[@]}"; do
      [[ "$old" != "$HEAD_SHA" ]] || continue
      if git merge-base --is-ancestor "$old" "$HEAD_SHA" || git merge-base --is-ancestor "$old" "$settings"; then
        continue
      fi
      tips+=("$old")
    done
    if [[ ${#gone[@]} -gt 0 ]]; then
      warning="${#gone[@]} earlier head(s) of this pull request cannot be fetched ($(short "${gone[@]}")), so they are not scanned here (each push of them had a push scan of its own)"
    fi
    if [[ ${#tips[@]} -gt 0 ]]; then
      range="$HEAD_SHA ${tips[*]} ^$settings"
      what="$what, and what the ${#tips[@]} earlier head(s) a force push replaced ($(short "${tips[@]}")) had"
      # An earlier head from before a fork-follow rebase holds the upstream commits main was rebased
      # away from (see the header).
      without_upstream
      # shellcheck disable=SC2086
      what="$what: $(git rev-list --count $range) commit(s) in all"
    fi
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
      # shellcheck disable=SC2086
      what="the $(git rev-list --count $range) commit(s) $PUSH_REF has that $CONFIG_BRANCH has not"
    elif ! zero "$BEFORE" && fetch "$BEFORE"; then
      settings="$BEFORE"
      settings_what="the tip before the push (${BEFORE:0:12})"
      changes=("$BEFORE" "$AFTER")
      range="$BEFORE..$AFTER"
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
      if zero "$BEFORE"; then
        warning="a new branch has no earlier tip, so only its newest commit ${AFTER:0:12} is scanned"
      else
        warning="the earlier tip ${BEFORE:0:12} cannot be fetched (force push?), so only the newest commit ${AFTER:0:12} is scanned"
      fi
      what="the pushed commit ${AFTER:0:12}"
    fi
    # Leave out upstream's commits (see the header).
    without_upstream
    # A tag push: the tag's own message too (see "Commit objects" in the header). The tag is fetched as
    # pushed; when it is gone already (deleted, or moved by a later push), that push's run reads it.
    if [[ "${PUSH_REF:-}" == refs/tags/* ]]; then
      if git check-ref-format "$PUSH_REF" &&
        GIT_RETRY_TRY=300 GIT_RETRY_TOTAL=300 git_retry fetch --quiet --no-tags --filter=blob:none origin "+$PUSH_REF:refs/scan-tag/pushed" >&2 &&
        [[ "$(git rev-parse --quiet --verify 'refs/scan-tag/pushed^{commit}')" == "$(git rev-parse --quiet --verify "$AFTER^{commit}")" ]]; then
        mapfile -t tag_objects < <(tag_chain refs/scan-tag/pushed)
      else
        warning="${warning:+$warning; }the tag $PUSH_REF cannot be fetched as pushed (deleted or moved since?), so its message is not scanned here"
      fi
    fi
    ;;
  schedule | workflow_dispatch)
    # The daily sweep (see "sweep" in the header): every ref of origin but refs/pull/*, with the
    # default branch's settings, minus what upstream has.
    if ! settings="$(fetch_config_branch)"; then
      echo "::error::Secret scan: cannot fetch the default branch '${CONFIG_BRANCH:-}' (CONFIG_BRANCH), whose gitleaks settings the scan uses" >&2
      exit 2
    fi
    settings_what="the default branch $CONFIG_BRANCH (${settings:0:12})"
    read_gone_list "$settings"
    changes=()
    if ! git for-each-ref --format='delete %(refname)' refs/scan-sweep/ | git update-ref --stdin ||
      ! GIT_RETRY_TRY=900 GIT_RETRY_TOTAL=900 git_retry fetch --quiet --no-tags --filter=blob:none origin \
        "+refs/*:refs/scan-sweep/*" "^refs/pull/*" >&2; then
      echo "::error::Secret scan: cannot fetch the refs of origin. Re-run the job." >&2
      exit 2
    fi
    tips=()
    odd=()
    while read -r ref; do
      if tip="$(git rev-parse --quiet --verify "$ref^{commit}")"; then
        tips+=("$tip")
      else
        odd+=("${ref#refs/scan-sweep/}")
      fi
      mapfile -t -O "${#tag_objects[@]}" tag_objects < <(tag_chain "$ref")
    done < <(git for-each-ref --format='%(refname)' refs/scan-sweep/)
    if [[ ${#tips[@]} -eq 0 ]]; then
      echo "::error::Secret scan: origin has no ref naming a commit to scan" >&2
      exit 2
    fi
    if [[ ${#odd[@]} -gt 0 ]]; then
      warning="${#odd[@]} ref(s) name no commit (${odd[*]:0:5}), so their contents are not scanned, only their tag messages"
    fi
    mapfile -t tips < <(printf '%s\n' "${tips[@]}" | sort -u)
    what="every ref of origin but refs/pull/* (${#tips[@]} distinct tip(s))"
    # Tips no ref holds any more (a branch deleted or force-pushed after a push no run scanned):
    # every tip GitHub's activity record lists (OLD_HEADS; see "sweep" in the header), but those
    # the workflow found deleted (DELETED_TIPS) that this clone lacks, which are gone unfetched.
    for old in ${DELETED_TIPS:-}; do
      if ! [[ "$old" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || zero "$old"; then
        echo "::error::Secret scan: '$old' in DELETED_TIPS is not a commit id" >&2
        exit 2
      fi
      skip_fetch[$old]=1
    done
    earlier "the listed tip" ${OLD_HEADS:-}
    vanished=()
    for old in "${listed[@]}"; do
      [[ " ${tips[*]} " == *" $old "* ]] || vanished+=("$old")
    done
    if [[ ${#vanished[@]} -gt 0 ]]; then
      tips+=("${vanished[@]}")
      what="$what, and ${#vanished[@]} tip(s) no ref holds that GitHub's activity record lists"
    fi
    # Gone tips (see "sweep, gone tips"): on the reviewed list, a warning; any other fails the sweep
    # once the rest is scanned. A tip on the list origin serves (it came back) is scanned above.
    # A gone tip GitHub deleted after a successful scan read it (SCANNED_GONE, the workflow's
    # lookup): a warning; never an exclusion of a tip origin serves (that one is in listed, and scanned).
    scanned_gone=" "
    for old in ${SCANNED_GONE:-}; do
      if ! [[ "$old" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || zero "$old"; then
        echo "::error::Secret scan: '$old' in SCANNED_GONE is not a commit id" >&2
        exit 2
      fi
      scanned_gone+="$old "
    done
    gone_known=()
    gone_scanned=()
    gone_new=()
    back=()
    for old in "${gone[@]}"; do
      if [[ "$known_gone" == *" $old "* ]]; then
        gone_known+=("$old")
      elif [[ "$scanned_gone" == *" $old "* ]]; then
        gone_scanned+=("$old")
      else
        gone_new+=("$old")
      fi
    done
    for old in "${listed[@]}"; do
      [[ "$known_gone" != *" $old "* ]] || back+=("$old")
    done
    if [[ ${#gone_known[@]} -gt 0 ]]; then
      warning="${warning:+$warning; }${#gone_known[@]} tip(s) on the list of tips known gone ($gone_list) cannot be fetched, so they are not scanned ($(short "${gone_known[@]:0:5}"))"
    fi
    if [[ ${#gone_scanned[@]} -gt 0 ]]; then
      warning="${warning:+$warning; }${#gone_scanned[@]} tip(s) GitHub's activity record lists cannot be fetched: GitHub has deleted them, and a successful secret-scan job read each one while it was served ($(short "${gone_scanned[@]:0:5}"))"
    fi
    if [[ ${#back[@]} -gt 0 ]]; then
      warning="${warning:+$warning; }${#back[@]} tip(s) on the list of tips known gone are served by origin, so they are scanned ($(short "${back[@]:0:5}")): remove their lines from $gone_list"
    fi
    if [[ ${#gone_new[@]} -gt 0 ]]; then
      gone_failure="${#gone_new[@]} tip(s) GitHub's activity record lists cannot be fetched: origin no longer has them (${gone_new[*]}), and no run is known to have scanned them (a [skip ci] push whose branch is gone?): no successful secret-scan job had one as its head commit, GitHub may still show one (its API still has a commit of that id), or the workflow did not ask. Their content was never scanned and may have been public. Look at each by hand; once one is known gone, add a line '<id> <why>' for it to $gone_list on the default branch (a reviewed pull request)"
      echo "::error::Secret scan: $gone_failure" >&2
      # An annotation is cut at about 4 KB, so the full list once more, one id per line.
      printf 'not fetchable, no scan known: %s\n' "${gone_new[@]}"
    fi
    range="${tips[*]}"
    without_upstream
    # shellcheck disable=SC2086
    what="$what: $(git rev-list --count $range) commit(s)"
    ;;
  *)
    echo "::error::Secret scan: unexpected event $EVENT_NAME" >&2
    exit 2
    ;;
esac

opts="--remerge-diff --text $range"
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
  { [[ -s "$work/missing" ]] && ! GIT_RETRY_TRY=600 GIT_RETRY_TOTAL=600 GIT_RETRY_STDIN="$work/missing" git_retry \
    -c fetch.negotiationAlgorithm=noop fetch --quiet --no-tags --no-write-fetch-head --recurse-submodules=no \
    --filter=blob:none origin --stdin; }; then
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
# Why the sweep fails comes first: the scan's own result below may read "No secrets found".
if [[ -n "$gone_failure" ]]; then summary "Failed: $gone_failure." ""; fi
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

# Commit objects (see the header): each of the range's commits and tag objects as stored (message,
# author, committer, tagger), each under a line naming it, through gitleaks stdin with the same
# settings. A tag object upstream also has is upstream's, as its commits are.
messages="$work/messages"
# shellcheck disable=SC2086
if ! git cat-file --batch='--- fork-secret-scan: commit %(objectname)' < "$work/commits" > "$messages"; then cannot_read; fi
if [[ ${#tag_objects[@]} -gt 0 ]]; then
  upstream_tags=" "
  if [[ -n "$upstream_used" ]]; then
    while read -r ref; do
      upstream_tags+="$(tag_chain "$ref" | tr '\n' ' ')"
    done < <(git for-each-ref --format='%(refname)' refs/scan-upstream/tags/)
  fi
  tags_read=0
  for obj in $(printf '%s\n' "${tag_objects[@]}" | sort -u); do
    [[ "$upstream_tags" != *" $obj "* ]] || continue
    { echo "--- fork-secret-scan: tag object $obj"; git cat-file tag "$obj"; } >> "$messages" || cannot_read
    tags_read=$((tags_read + 1))
  done
fi
message_count="$(grep -c '^--- fork-secret-scan: ' "$messages" || true)"
echo "Scanning the commit objects of the range (message, author, committer) and ${tags_read:-0} tag object(s): $message_count in all"
set +e
(cd "$config" && env -u GITLEAKS_CONFIG -u GITLEAKS_CONFIG_TOML \
  "$gitleaks" stdin --no-banner --no-color --redact --verbose "${config_args[@]}" \
  --gitleaks-ignore-path "$config" --report-format json --report-path "$work/messages.json" < "$messages") > "$work/messages.log" 2>&1
mstatus=$?
set -e
cat "$work/messages.log"
mplain="$(tr -d '\000' < "$work/messages.log" | sed -E 's/\x1b\[[0-9;]*m//g')"
merrors="$(grep -aE '^([^[:space:]]+[[:space:]]+)?(ERR|FTL)[[:space:]]' <<< "$mplain" || true)"
mleaks=""
if grep -aqE 'leaks found: [1-9][0-9]*' <<< "$mplain"; then mleaks=1; fi
if [[ $mstatus -eq 0 && -n "$merrors" ]] || [[ $mstatus -ne 0 && -z "$mleaks" ]]; then
  echo "::error::Secret scan: gitleaks stopped or logged an error while reading the messages (exit $mstatus), so they were not all checked: ${merrors:-see the log above}" >&2
  summary "Failed: gitleaks could not read the commit and tag objects of $what (see the job log), so they were not checked."
  exit 2
fi
if [[ $mstatus -ne 0 ]]; then
  # Which messages: the line each finding starts on, under the nearest naming line above it.
  sed -nE 's/^[[:space:]]*"StartLine": ([0-9]+),?$/\1/p' "$work/messages.json" | sort -nu > "$work/message-lines"
  while read -r name; do
    echo "::error title=Secret scan::A possible secret in the message, author or committer of $name (value redacted). Rotate any real secret: it is already public."
  done < <(awk 'NR == FNR { want[$1] = 1; next }
      /^--- fork-secret-scan: / { name = substr($0, 23) }
      (FNR in want) && !(name in said) { said[name] = 1; print name }' "$work/message-lines" "$messages")
fi

if [[ $status -eq 0 && $mstatus -eq 0 ]]; then
  summary "No secrets found in $what ($scanned commit(s) with changes scanned; $message_count commit and tag object(s): messages, authors, committers)."
else
  summary "gitleaks found possible secrets in $what (values redacted; see the job log). Rotate any real secret: it is already public."
fi
summary "" "Not covered: ref names; pull request titles, bodies and comments; commits only an outside pull request's head (refs/pull/*) holds; refs that name a tree or blob; a vanished tip GitHub's activity record does not list or that is on the reviewed list of tips known gone; a \`gitleaks:allow\` line; text in other encodings; packed or compressed contents; a key split across lines (see the header of fork-secret-scan.sh)."

# A tip no run read and origin no longer serves fails the sweep (see "sweep, gone tips"), after the
# scan above, so its findings are still reported. The summary says so first (above).
if [[ -n "$gone_failure" ]]; then exit 2; fi
if [[ $status -ne 0 ]]; then exit "$status"; fi
exit "$mstatus"
