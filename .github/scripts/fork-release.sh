#!/usr/bin/env bash
# The guards of fork-release.yml, one subcommand each, so they can be tested without a run
# (fork-release.test.sh). Every refusal exits non-zero with a ::error:: line; nothing falls back.
#
#   resolve                 Preflight: picks base tag, version and tag for HEAD (key=value lines):
#                           <base>-fork.N on a stable base, <base>.fork.N on a nightly one.
#   remove-drafts TAG       Refuses a published TAG, then deletes drafts with TAG left by earlier runs.
#   publish ID TAG VERSION  Refuses a published TAG, a published release that is not below
#                           VERSION, or an upstream base that is no longer released (a nightly
#                           withdrawn during the build), then publishes draft ID as the latest release.
#   check-bundle DIR        The server bundle DIR (apps/server/dist) outside DIR/client must name
#                           REPOSITORY as its release repository.
#   check-archives DIR      The `t3` program of every CLI archive in DIR must name REPOSITORY.
#
# Environment: FORK_FOLLOW_CHANNEL (nightly or stable, default nightly: which upstream tags a base
# may be; fork-channel.sh), REPOSITORY (owner/name of this fork; GITHUB_REPOSITORY in a run), UPSTREAM
# (owner/name; this fork's parent when unset), UPSTREAM_URL (git URL of upstream, default GitHub),
# ORIGIN (git remote or URL of this fork, default origin). Output goes to $GITHUB_OUTPUT, else stdout.
set -euo pipefail
# Refuses outside a GitHub Actions run unless a test runs it under the gh stub (fork-gh-guard.sh).
. "$(dirname "${BASH_SOURCE[0]}")/fork-gh-guard.sh"
# The channel, the fork's release names and version_gt.
. "$(dirname "${BASH_SOURCE[0]}")/fork-channel.sh"

REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:?REPOSITORY or GITHUB_REPOSITORY}}"
ORIGIN="${ORIGIN:-origin}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"

die() {
  printf '::error::%s\n' "$*" >&2
  exit 1
}

stable_version_re='^v[0-9]+\.[0-9]+\.[0-9]+$'
# A fork version, with or without the leading v (fork_re in fork-channel.sh).
fork_version_re="^v?${fork_re#^v}"
# The updaters' own rule for a release on another track (cliReleaseChannelOf): the version ends in
# -nightly.YYYYMMDD.N or -preview.YYYYMMDD.N. A fork release of a nightly does not, so it counts.
other_track_re='^v[^-+]+-(nightly|preview)\.[0-9]{8}\.[0-9]+$'

# Prints the releases of this fork through a jq filter; a failed listing stops everything.
releases() {
  gh api "repos/$REPOSITORY/releases" --paginate --jq "$1" \
    || die "Could not list the releases of $REPOSITORY; nothing was changed."
}

# A published (non-draft) release or a tag with this name means the version is taken: "Re-run failed
# jobs" reuses the preflight's version, and publishing again would replace a live release's files.
refuse_published() {
  local tag="$1" published
  if git ls-remote --exit-code --tags "$ORIGIN" "refs/tags/$tag" > /dev/null; then
    die "Tag $tag already exists, so $tag is published or being published. Start a new run (Re-run all jobs) for the next number; nothing was changed."
  else
    # 2 means no such ref; anything else is a failed lookup, which must not read as "free".
    [[ $? == 2 ]] || die "Could not list the tags of $ORIGIN."
  fi
  published="$(releases ".[] | select((.draft | not) and .tag_name == \"$tag\") | .id")"
  if [[ -n "$published" ]]; then
    die "A published release ($published) already has tag $tag. Start a new run (Re-run all jobs); nothing was changed."
  fi
}

upstream_repo() { # prints UPSTREAM, or this fork's parent
  local upstream="${UPSTREAM:-}"
  if [[ -z "$upstream" ]]; then
    upstream="$(gh api "repos/$REPOSITORY" --jq '.parent.full_name // empty')"
    [[ -n "$upstream" ]] || die "$REPOSITORY is not a fork, so there is no upstream release to build on."
  fi
  printf '%s\n' "$upstream"
}

# The upstream tag a fork version is built on: X.Y.Z-fork.N on vX.Y.Z, X.Y.Z-nightly.D.R.fork.N on
# vX.Y.Z-nightly.D.R.
fork_base_tag() { local v="v${1#v}"; if [[ "$v" == *-nightly.* ]]; then printf '%s\n' "${v%.fork.*}"; else printf '%s\n' "${v%-fork.*}"; fi; }

# Only released upstream versions, checked by preflight and again right before publishing (a
# nightly upstream withdraws during the build must not reach auto-updating installs).
# Stable tag: never above the release upstream marks latest. Nightly tag: upstream published a
# release of that tag itself (a nightly is a pre-release, which "latest" never names; a tag whose
# release is missing or still a draft was not released; this fork's token cannot see another
# repository's drafts, so a draft reads as missing).
require_released() { # require_released UPSTREAM TAG
  local upstream="$1" tag="$2" draft upstream_latest
  if [[ "$tag" == *-nightly.* ]]; then
    draft="$(gh api "repos/$upstream/releases/tags/$tag" --jq '.draft')" \
      || die "Could not read $upstream's release of $tag (it has none, or the lookup failed). Only released upstream versions are built and published."
    [[ "$draft" == false ]] || die "$upstream's release of $tag is not published (draft: $draft). Only released upstream versions are built and published."
  else
    upstream_latest="$(gh api "repos/$upstream/releases/latest" --jq '.tag_name')"
    [[ "$upstream_latest" =~ $stable_version_re ]] \
      || die "$upstream's latest release is '$upstream_latest', not a stable vX.Y.Z tag."
    if version_gt "$tag" "$upstream_latest"; then
      die "$tag sorts above $upstream's latest release $upstream_latest. Only released upstream versions are built and published."
    fi
  fi
}

# The server's updater (packages/shared/src/cliRelease.ts newestCliReleaseVersion) takes the
# newest-created non-draft release on its channel, not the highest version. So every new release
# must be above every published one, or `t3 update` would see a downgrade and stall until a higher
# build exists. Stable channel = any vX.Y.Z[-...] tag except upstream-style nightly and preview builds.
#
# A higher version is not newer code for one pair: upstream names a nightly after the release it
# leads up to, so vX.Y.Z-nightly.D.R (older than vX.Y.Z) still sorts above a published X.Y.Z-fork.N.
# Its fork build would install older upstream code over the stable one, which may lack migrations
# that build already ran. So a nightly build of the same X.Y.Z as a published stable fork release
# must contain that release's upstream tag (GitHub's compare of the two upstream tags).
refuse_older() { # refuse_older VERSION UPSTREAM
  local version="$1" upstream="$2" other published mine status
  mine="v${version#v}"
  published="$(releases '.[] | select(.draft | not) | .tag_name')"
  for other in $published; do
    [[ "$other" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-.*)?$ ]] || continue
    [[ ! "$other" =~ $other_track_re ]] || continue
    [[ "$other" =~ $stable_version_re || "$other" =~ $fork_version_re ]] \
      || die "Published release $other is on the server's stable channel but has no X.Y.Z, X.Y.Z-fork.N or X.Y.Z-nightly.D.N.fork.N version, so $version cannot be ordered against it; nothing was changed."
    version_gt "$version" "$other" \
      || die "$other is published and not below $version. The server updates to the newest-created release, so publishing $version would show every server a downgrade. Build a newer upstream release instead; to go back, install the older release with t3 update --version X --allow-downgrade. Nothing was changed."
    if [[ "$mine" == *-nightly.* && "$other" != *-nightly.* && "$other" == *-fork.* \
      && "${mine%%-*}" == "${other%%-*}" ]]; then
      status="$(gh api "repos/$upstream/compare/$(fork_base_tag "$other")...$(fork_base_tag "$mine")" --jq '.status')" \
        || die "Could not compare $upstream $(fork_base_tag "$other") with $(fork_base_tag "$mine"), so $version cannot be ordered against the published $other; nothing was changed."
      [[ "$status" == ahead || "$status" == identical ]] \
        || die "$other is published and built on $upstream $(fork_base_tag "$other"), which $(fork_base_tag "$mine") does not contain ($status). Upstream names a nightly after the release it leads up to, so $version sorts above $other but holds older upstream code; every install would update to it. Build a nightly that contains $(fork_base_tag "$other") instead. Nothing was changed."
    fi
  done
}

cmd_resolve() {
  local upstream
  upstream="$(upstream_repo)"
  local url="${UPSTREAM_URL:-https://github.com/$upstream.git}"
  local head channel channel_re channel_form
  fork_channel_init || die "FORK_FOLLOW_CHANNEL is '$FORK_FOLLOW_CHANNEL'; it must be nightly or stable."
  head="$(git rev-parse HEAD)"

  # Read-only fetch of upstream's tags and default branch into refs this run alone uses.
  git fetch --quiet --prune --no-tags "$url" "+refs/tags/v*:refs/upstream-tags/v*" "+HEAD:refs/upstream-head"

  # Lists are read into variables first: a failure inside <(...) would go unnoticed.
  local refs tag
  local -a contained=()
  refs="$(git for-each-ref --format='%(refname:strip=2)' refs/upstream-tags)"
  refs="$({ grep -E "$channel_re" <<< "$refs" || true; } | newest_first)"
  for tag in $refs; do
    if git merge-base --is-ancestor "refs/upstream-tags/$tag^{commit}" HEAD; then
      contained+=("$tag")
    fi
  done
  (( ${#contained[@]} > 0 )) || die "$head contains no $channel $upstream release tag ($channel_form)."
  local base_tag="${contained[0]}"
  local base
  base="$(git rev-parse "refs/upstream-tags/$base_tag^{commit}")"

  # The base must be the newest tag of the channel in the series' history, not just the highest: a
  # stray tag on an old commit (say v1.0.0) would otherwise outrank every later real release.
  for tag in "${contained[@]:1}"; do
    git merge-base --is-ancestor "refs/upstream-tags/$tag^{commit}" "$base" \
      || die "$base_tag sorts highest but is not the newest $channel $upstream tag in this history ($tag is not part of it). Delete or fix the stray tag; nothing was built."
  done

  # Nightly: upstream cuts nightlies from its default branch. A nightly-shaped tag on another
  # branch (which the checks above accept when it is the newest one in this history) is not one.
  if [[ "$channel" == nightly ]]; then
    git merge-base --is-ancestor "$base" refs/upstream-head \
      || die "$base_tag is not on $upstream's default branch, so it is not one of upstream's nightlies (or upstream rewrote that branch). Only nightlies of the default branch are built; nothing was built."
  fi

  require_released "$upstream" "$base_tag"

  # Releases only: every commit after the base tag must be the fork's own. Upstream commits there
  # are unreleased upstream code (for example a branch that still equals upstream's default branch).
  local total own
  total="$(git rev-list --count "$base..HEAD")"
  own="$(git rev-list --count "$base..HEAD" --not refs/upstream-head)"
  if [[ "$total" != "$own" ]]; then
    die "$head holds $((total - own)) unreleased $upstream commits after $base_tag. Build a branch of $base_tag plus the fork's own commits."
  fi

  local version="${base_tag#v}" sep sep_re=- last
  sep="$(fork_sep "$base_tag")"
  [[ "$sep" != . ]] || sep_re='\.'
  last="$(git ls-remote --tags --refs "$ORIGIN" "v${version}${sep}fork.*" \
    | sed -n "s|.*refs/tags/v${version//./\\.}${sep_re}fork\.\([0-9]\+\)$|\1|p" | sort -n | tail -n1)"
  version="${version}${sep}fork.$(( ${last:-0} + 1 ))"
  refuse_older "$version" "$upstream"
  {
    echo "ref=$head"
    echo "upstream=$upstream"
    echo "base_tag=$base_tag"
    echo "version=$version"
    echo "tag=v$version"
  } >> "$OUTPUT"
  echo "Building $version from $(git rev-parse --short HEAD) on $upstream $base_tag." \
    | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}" >&2
}

cmd_remove_drafts() {
  local tag="${1:?tag}" id
  refuse_published "$tag"
  local ids
  ids="$(releases ".[] | select(.draft and .tag_name == \"$tag\") | .id")"
  for id in $ids; do
    gh api -X DELETE "repos/$REPOSITORY/releases/$id" > /dev/null
    echo "Removed leftover draft $id for $tag."
  done
  [[ -n "$ids" ]] || echo "No leftover draft for $tag."
}

cmd_publish() {
  local id="${1:?release id}" tag="${2:?tag}" version="${3:?version}"
  [[ "$version" =~ $fork_version_re ]] || die "'$version' is not a fork version."
  refuse_published "$tag"
  local upstream
  upstream="$(upstream_repo)"
  # Checked again here: a release published since preflight must not end up above this one. After
  # this check VERSION is the highest release, so the server and the desktop updater agree on it.
  refuse_older "$version" "$upstream"
  # And upstream must still have released the base: a nightly withdrawn during the build (release
  # deleted or set back to draft) is not published to auto-updating installs.
  require_released "$upstream" "$(fork_base_tag "$version")"
  gh api -X PATCH "repos/$REPOSITORY/releases/$id" -F draft=false -f make_latest=true > /dev/null
  echo "Published $tag (latest: true)."
  {
    echo "published=true"
    echo "latest=true"
  } >> "$OUTPUT"
}

# The web client (DIR/client) names this repository on every Actions build anyway: upstream's
# apps/web/vite.config.ts bakes GITHUB_REPOSITORY into it. Only the server code counts here.
cmd_check_bundle() {
  local dir="${1:?directory}" file files
  [[ -d "$dir" ]] || die "No server bundle at $dir."
  files="$(find "$dir" -path "$dir/client" -prune -o -type f -print)"
  [[ -n "$files" ]] || die "The server bundle $dir holds no files outside client/."
  while IFS= read -r file; do
    if grep -qaF "\"$REPOSITORY\"" "$file"; then
      echo "The server bundle names $REPOSITORY (${file#"$dir"/})."
      return 0
    fi
  done <<< "$files"
  die "The server bundle $dir (outside client/) does not name $REPOSITORY as its release repository, so it would update from elsewhere."
}

cmd_check_archives() {
  local dir="${1:?directory}" archive work name stem program found=0
  shopt -s nullglob
  for archive in "$dir"/t3-*.tar.gz "$dir"/t3-*.zip; do
    found=1
    work="$(mktemp -d)"
    case "$archive" in
      *.tar.gz) tar -xzf "$archive" -C "$work" ;;
      *.zip) python3 -I -m zipfile -e "$archive" "$work" ;;
    esac
    # Only the `t3` program at <stem>/ (scripts/build-cli-archive.ts): it bundles the server again,
    # and its updater is what counts. client/ names this repository on every Actions build anyway.
    name="$(basename "$archive")"
    stem="${name%.tar.gz}"
    stem="${stem%.zip}"
    program="$work/$stem/t3"
    [[ "$archive" != *.zip ]] || program="$work/$stem/t3.exe"
    [[ -f "$program" ]] \
      || die "$name has no ${program#"$work"/}, so the server program in it cannot be checked."
    grep -qaF "\"$REPOSITORY\"" "$program" \
      || die "${program#"$work"/} in $name does not name $REPOSITORY as its release repository, so it would update from elsewhere."
    rm -rf "$work"
    echo "${program#"$work"/} in $name names $REPOSITORY."
  done
  (( found )) || die "No CLI archive in $dir."
}

command="${1:-}"
shift || true
case "$command" in
  resolve) cmd_resolve "$@" ;;
  remove-drafts) cmd_remove_drafts "$@" ;;
  publish) cmd_publish "$@" ;;
  check-bundle) cmd_check_bundle "$@" ;;
  check-archives) cmd_check_archives "$@" ;;
  version-gt) version_gt "$@" ;;
  *) die "usage: $0 resolve | remove-drafts TAG | publish ID TAG VERSION | check-bundle DIR | check-archives DIR" ;;
esac
