#!/usr/bin/env bash
# Decides what the follow-upstream workflow does next, and rebases the fork's patch series onto a
# newer stable upstream tag when there is one. Run in a full clone with the series checked out.
# Writes key=value lines to $GITHUB_OUTPUT (stdout without it); a failure's reason goes to $REPORT.
set -euo pipefail

: "${UPSTREAM:?owner/name of the upstream repository}"
REPORT="${REPORT:-fork-follow-report.md}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"
logs="${RUNNER_TEMP:-$(mktemp -d)}"
upstream_url="${UPSTREAM_URL:-https://github.com/$UPSTREAM.git}"

fail() {
  printf '%s\n' "$@" > "$REPORT"
  cat "$REPORT" >&2
  exit 1
}

stable_tags() {
  git ls-remote --tags --refs "$upstream_url" 'v*' \
    | sed -n 's|.*refs/tags/\(v[0-9]\+\.[0-9]\+\.[0-9]\+\)$|\1|p' | sort -rV
}

fetch_upstream_tag() {
  git fetch --quiet --no-tags "$upstream_url" "+refs/tags/$1:refs/upstream-tags/$1"
  git rev-parse "refs/upstream-tags/$1^{commit}"
}

head="$(git rev-parse HEAD)"
target_tag="$(stable_tags | sed -n 1p)"
[[ -n "$target_tag" ]] || fail "$UPSTREAM has no stable release tag."

# The series sits on the newest stable upstream tag it contains.
base_tag=""
while read -r tag; do
  commit="$(fetch_upstream_tag "$tag")"
  if git merge-base --is-ancestor "$commit" HEAD; then
    base_tag="$tag"
    base="$commit"
    break
  fi
done < <(stable_tags)
[[ -n "$base_tag" ]] || fail "The series ($head) contains no stable $UPSTREAM tag."

# Every commit between that tag and the series head must be the fork's own. Upstream commits there
# mean the branch is not a patch series (for example it still equals upstream's default branch).
git fetch --quiet --no-tags "$upstream_url" "+HEAD:refs/upstream-head"
series_count="$(git rev-list --count "$base..HEAD")"
own_count="$(git rev-list --count "$base..HEAD" --not refs/upstream-head)"
if [[ "$series_count" != "$own_count" ]]; then
  fail "The series holds $((series_count - own_count)) upstream commits after $base_tag." \
    "Reset it to $base_tag plus the fork's own commits; nothing was rebased or published."
fi

node .github/scripts/fork-schema-check.cjs "$base" HEAD > "$logs/schema-check.log" 2>&1 \
  || fail "The series changes the database schema:" '```' "$(cat "$logs/schema-check.log")" '```'

released_head() {
  local version="${1#v}" last
  last="$(git ls-remote --tags --refs origin "v$version-fork.*" \
    | sed -n "s|.*refs/tags/v$version-fork\.\([0-9]\+\)$|\1|p" | sort -n | tail -n1)"
  [[ -z "$last" ]] || git ls-remote origin "refs/tags/v$version-fork.$last^{}" "refs/tags/v$version-fork.$last" \
    | sort -k2 | tail -n1 | cut -f1
}

if [[ "$target_tag" == "$base_tag" ]]; then
  if [[ "$(released_head "$base_tag")" == "$head" ]]; then
    action=none
  else
    action=release
  fi
  {
    echo "action=$action"
    echo "base_tag=$base_tag"
    echo "candidate=$head"
  } >> "$OUTPUT"
  exit 0
fi

# A newer upstream release: replay the series onto it. Patches upstream now has become empty and
# are dropped; the release notes list the series, so a dropped patch simply no longer appears.
target="$(fetch_upstream_tag "$target_tag")"
before="$(git log --format=%s "$base..HEAD")"
if ! git -c user.name=t3code-fork -c user.email=noreply@users.noreply.github.com \
  rebase --empty=drop --onto "$target" "$base" > "$logs/rebase.log" 2>&1; then
  stopped="$(git log -1 --format='%h %s' REBASE_HEAD 2>/dev/null || echo unknown)"
  conflicts="$(git diff --name-only --diff-filter=U | sed 's/^/- /')"
  git rebase --abort || true
  fail "Rebasing the series from $base_tag onto $target_tag stopped at: $stopped" "" \
    "Conflicting files:" "$conflicts" "" "Nothing was published; the last fork release stays current."
fi
node .github/scripts/fork-schema-check.cjs "$target" HEAD > "$logs/schema-check.log" 2>&1 \
  || fail "After rebasing onto $target_tag the series changes the database schema:" '```' "$(cat "$logs/schema-check.log")" '```'
dropped="$(comm -23 <(sort <<<"$before") <(git log --format=%s "$target..HEAD" | sort) | paste -sd ';' -)"
{
  echo "action=rebase"
  echo "base_tag=$target_tag"
  echo "previous_tag=$base_tag"
  echo "previous_head=$head"
  echo "candidate=$(git rev-parse HEAD)"
  echo "dropped=$dropped"
} >> "$OUTPUT"
