#!/usr/bin/env bash
# Usage: fork-seam-upstream.sh <base> <head>
# Prints the upstream commit <head> sits on (the merge-base of <head> and upstream's default branch)
# for fork-seam-check.cjs --upstream-base, or nothing. fork-checks runs it on a pull request. Only a
# change to .github/fork-seams.json can move a "pending" file to "files", the one case the checker
# needs upstream for, so any other change prints nothing and fetches nothing (no network, no API
# call). Upstream is UPSTREAM_URL, else this repository's parent (GITHUB_REPOSITORY), read from
# GitHub's public API with one request and no token (fork-checks hands no step a token),
# then fetched read-only and blobless into refs/seam-upstream/head (the runner's checkout already
# has upstream's history up to main's base, so only newer upstream commits and trees come).
# When anything fails it prints a note on stderr, nothing on stdout, and exits 0: the check then runs
# as it did before (it counts a moved entry's lines; fork-follow's run on main checks which they are).
set -uo pipefail

base="${1:?usage: fork-seam-upstream.sh <base> <head>}"
head="${2:?usage: fork-seam-upstream.sh <base> <head>}"
list=.github/fork-seams.json

skip() {
  echo "Note: $*, so the seam check cannot tell which lines a \"pending\" file moved to \"files\" lists (fork-follow's run on main checks them)." >&2
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
    || skip "could not read which repository this one is a fork of ($api)"
  [[ -n "$parent" ]] || skip "this repository is not a fork"
  [[ "$parent" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || skip "the parent's name is not owner/name"
  url="${GITHUB_SERVER_URL:-https://github.com}/$parent.git"
fi

timeout 180 git fetch --quiet --no-tags --no-recurse-submodules --filter=blob:none "$url" \
  "+HEAD:refs/seam-upstream/head" >&2 \
  || skip "could not fetch upstream's default branch ($url)"
upstream_base="$(git merge-base "$head" refs/seam-upstream/head)" \
  || skip "the head has no commit in common with upstream's default branch"
echo "$upstream_base"
