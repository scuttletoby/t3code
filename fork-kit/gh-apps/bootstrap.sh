#!/bin/sh
# gh-apps bootstrap: installs the gh-apps kit of one release of this fork. Run as root, in a shell no agent sees:
#
#   sh gh-apps-bootstrap.sh --repo FORK_OWNER/FORK --sha RELEASE_SHA --agent-user AGENT_USER [--gh-path /usr/bin/gh]
#
# It refuses a sha that is not a commit on the fork's protected release branch (ci-release), downloads exactly
# that commit, prints the sha and the hash of what it downloaded, and runs its installer (safe to run again).
# Needs: curl, tar, python3, sudo, visudo, useradd, gh. Linux only for now.
set -eu
umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset TMPDIR CURL_HOME SSL_CERT_FILE SSL_CERT_DIR || true

die() { echo "gh-apps-bootstrap: $*" >&2; exit 1; }

repo='' sha='' agent='' gh_path='' branch='ci-release' api='https://api.github.com' codeload='https://codeload.github.com'
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="${2-}"; shift 2 ;;
    --sha) sha="${2-}"; shift 2 ;;
    --agent-user) agent="${2-}"; shift 2 ;;
    --gh-path) gh_path="${2-}"; shift 2 ;;
    --release-branch) branch="${2-}"; shift 2 ;;
    *) die "unknown option $1 (see the top of this file)" ;;
  esac
done
printf '%s\n' "$repo" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}' || die "--repo must be OWNER/NAME"
printf '%s\n' "$sha" | grep -Eqx '[0-9a-f]{40}' || die "--sha must be the full 40-character release commit"
printf '%s\n' "$agent" | grep -Eqx '[a-z_][a-z0-9_-]{0,31}' || die "--agent-user must be a user name"
printf '%s\n' "$branch" | grep -Eqx '[A-Za-z0-9._/-]{1,100}' || die "bad --release-branch"
[ "$(id -u)" = 0 ] || die "run this as root"

work="$(mktemp -d /tmp/gh-apps-bootstrap.XXXXXXXX)"
trap 'rm -rf "${work:?}"' EXIT
trap 'exit 1' INT TERM HUP
fetch() { curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 0 -H 'Accept: application/vnd.github+json' -o "$2" "$1"; }

fetch "$api/repos/$repo/branches/$branch" "$work/branch.json" || die "cannot read branch $branch of $repo"
fetch "$api/repos/$repo/compare/$sha...$branch" "$work/compare.json" || die "cannot compare $sha with $branch"
python3 -I -B - "$work/branch.json" "$work/compare.json" "$sha" <<'PY' || die "$sha is not a release: it must be a commit on the protected branch $branch"
import json, sys
branch, compare = (json.load(open(p, encoding='utf-8')) for p in sys.argv[1:3])
ok = (branch.get('protected') is True and compare.get('status') in ('ahead', 'identical')
      and compare.get('base_commit', {}).get('sha') == sys.argv[3])
sys.exit(0 if ok else 1)
PY

# codeload.github.com answers a commit tarball directly (no redirect)
fetch "$codeload/$repo/tar.gz/$sha" "$work/kit.tar.gz" || die "cannot download $repo at $sha"
mkdir "$work/src"
tar -xzf "$work/kit.tar.gz" -C "$work/src" --no-same-owner --no-same-permissions
top="$(find "$work/src" -mindepth 1 -maxdepth 1 -type d)"
[ "$(printf '%s\n' "$top" | wc -l)" = 1 ] && [ -n "$top" ] || die "the download does not have one top folder"
kit="$top/fork-kit/gh-apps"
[ -f "$kit/sbin/gh-apps-install" ] || die "release $sha has no fork-kit/gh-apps (is it a release with the kit?)"
echo "gh-apps-bootstrap: release $sha of $repo, download sha256 $(sha256sum "$work/kit.tar.gz" | cut -d' ' -f1)"
set -- install --source "$kit" --sha "$sha" --agent-user "$agent"
[ -z "$gh_path" ] || set -- "$@" --gh-path "$gh_path"
python3 -I -B "$kit/sbin/gh-apps-install" "$@"
