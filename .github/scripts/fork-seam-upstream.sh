#!/usr/bin/env bash
# Usage: fork-seam-upstream.sh <default> <base> <head>
# Prints the upstream commit the default branch <default> sits on (the merge-base of <default> and
# upstream's default branch) for fork-seam-check.cjs --upstream-base, or nothing. fork-checks runs it
# on a pull request. It takes <default>, never <head>: a head can merge a newer upstream commit (even
# with `git merge -s ours`, keeping its own tree), and an upstream commit main's copy does not count
# from turns the check of a moved entry into a note. Only a change to .github/fork-seams.json can
# move a "pending" file to "files", the one case the checker needs upstream for, so any other change
# prints nothing and fetches nothing (no network, no API call). Upstream is UPSTREAM_URL, else this
# repository's parent (GITHUB_REPOSITORY), read from GitHub's public API with one request and no
# token (fork-checks hands no step a token). The fetch is read-only and commits only
# (--filter=tree:0: the merge-base needs no trees, and the commit it finds is in <default>'s history,
# whose trees the checkout has), and <default> is a local ref first, so the fetch negotiates from it
# and gets only the upstream commits <default> lacks (the checkout fetched by SHA and made no ref).
# When anything fails it prints a warning on stderr (a GitHub annotation, so the pull request shows
# it), nothing on stdout, and exits 0: the check then runs as before (it counts a moved entry's
# lines; fork-follow's run on main checks which they are).
set -uo pipefail

default="${1:?usage: fork-seam-upstream.sh <default> <base> <head>}"
base="${2:?usage: fork-seam-upstream.sh <default> <base> <head>}"
head="${3:?usage: fork-seam-upstream.sh <default> <base> <head>}"
list=.github/fork-seams.json

skip() {
  local message="$*, so the seam check cannot tell which lines a \"pending\" file moved to \"files\" lists (fork-follow's run on main checks them)."
  message="${message//'%'/%25}"
  echo "::warning title=Seam check without upstream::$message" >&2
  exit 0
}

merge_base="$(git merge-base "$base" "$head")" || skip "no merge-base of $base and $head"
git diff --quiet "$merge_base" "$head" -- "$list"
case $? in
  0) exit 0 ;; # The list is main's: nothing can move.
  1) ;;
  *) skip "could not compare $list with the merge-base" ;;
esac

url="${UPSTREAM_URL:-}"
if [[ -z "$url" ]]; then
  api="${GITHUB_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY:-}"
  parent="$(curl -fsSL --max-time 30 -H "Accept: application/vnd.github+json" "$api" \
    | node -e 'let s = ""; process.stdin.on("data", (d) => (s += d)).on("end", () => {
        try { process.stdout.write(String(JSON.parse(s).parent?.full_name ?? "")); }
        catch { console.error("not JSON"); process.exitCode = 1; } })')" \
    || skip "could not read which repository this one is a fork of ($api; GitHub allows 60 such requests an hour per address, or set the repository variable UPSTREAM_URL)"
  [[ -n "$parent" ]] || skip "this repository is not a fork"
  [[ "$parent" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || skip "the parent's name is not owner/name"
  url="${GITHUB_SERVER_URL:-https://github.com}/$parent.git"
fi

default_sha="$(git rev-parse --verify -q "$default^{commit}")" || skip "the default branch commit $default is missing"
git update-ref refs/seam-upstream/default "$default_sha" || skip "could not record $default as a ref"
timeout 90 git fetch --quiet --no-tags --no-recurse-submodules --filter=tree:0 "$url" \
  "+HEAD:refs/seam-upstream/head" >&2 \
  || skip "could not fetch upstream's default branch ($url)"
upstream_base="$(git merge-base "$default_sha" refs/seam-upstream/head)" \
  || skip "the default branch has no commit in common with upstream's default branch"
echo "$upstream_base"
