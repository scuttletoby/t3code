#!/bin/sh
# pc-agents bootstrap: runs `pc-agents setup` from one release of this fork. As root, in a fresh WSL2 distro:
#
#   sh pc-agents-bootstrap.sh --repo FORK_OWNER/FORK --sha RELEASE_SHA
#
# It refuses a sha that is not a commit on the fork's protected release branch (ci-release), downloads exactly that
# commit, prints the sha and the hash of what it downloaded, and runs its setup (safe to run again). The setup reads
# the "pc-agents" section of /etc/fork-kit/machine.json (fork-kit/pc-agents/README.md). Needs curl, tar, python3.
set -eu
umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset TMPDIR CURL_HOME SSL_CERT_FILE SSL_CERT_DIR PC_AGENTS_ROOT || true

die() { echo "pc-agents-bootstrap: $*" >&2; exit 1; }

repo='' sha='' branch='ci-release' api='https://api.github.com' codeload='https://codeload.github.com'
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="${2-}"; shift 2 ;;
    --sha) sha="${2-}"; shift 2 ;;
    *) die "unknown option $1 (see the top of this file)" ;;
  esac
done
printf '%s\n' "$repo" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}' || die "--repo must be OWNER/NAME"
printf '%s\n' "$sha" | grep -Eqx '[0-9a-f]{40}' || die "--sha must be the full 40-character release commit"
[ "$(id -u)" = 0 ] || die "run this as root"

work="$(mktemp -d /root/pc-agents-bootstrap.XXXXXXXX)"
trap 'rm -rf "${work:?}"' EXIT
trap 'exit 1' INT TERM HUP
fetch() { curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 0 -H 'Accept: application/vnd.github+json' -o "$2" "$1"; }

fetch "$api/repos/$repo/branches/$branch" "$work/branch.json" || die "cannot read branch $branch of $repo"
fetch "$api/repos/$repo/compare/$sha...$branch" "$work/compare.json" || die "cannot compare $sha with $branch"
python3 -I -B - "$work/branch.json" "$work/compare.json" "$sha" <<'PY' || die "$sha is not a release: it must be a commit on the protected branch $branch"
import json, sys
branch, compare = (json.load(open(p, encoding='utf-8')) for p in sys.argv[1:3])
ok = (branch.get('protected') is True and compare.get('status') in ('ahead', 'identical')
      and (compare.get('merge_base_commit') or {}).get('sha') == sys.argv[3])
sys.exit(0 if ok else 1)
PY

fetch "$codeload/$repo/tar.gz/$sha" "$work/kit.tar.gz" || die "cannot download $repo at $sha"
mkdir "$work/src"
tar -xzf "$work/kit.tar.gz" -C "$work/src" --no-same-owner --no-same-permissions
top="$(find "$work/src" -mindepth 1 -maxdepth 1 -type d)"
[ "$(printf '%s\n' "$top" | wc -l)" = 1 ] && [ -n "$top" ] || die "the download does not have one top folder"
[ -f "$top/fork-kit/pc-agents/sbin/pc-agents" ] || die "release $sha has no fork-kit/pc-agents"
echo "pc-agents-bootstrap: release $sha of $repo, download sha256 $(sha256sum "$work/kit.tar.gz" | cut -d' ' -f1)"
python3 -I -B "$top/fork-kit/pc-agents/sbin/pc-agents" setup --source "$top" --sha "$sha"
