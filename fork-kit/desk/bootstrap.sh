#!/bin/sh
# desk bootstrap: installs the desk kit (a desktop's side of a fork fleet desk pool) of one release of this fork.
# Run as root on the desktop (on WSL2: inside the distro), after the owner wrote the machine file's `desk` section:
#
#   sh desk-bootstrap.sh --repo FORK_OWNER/FORK --sha RELEASE_SHA [--fleet-config FILE | --release-fleet-config]
#
# --fleet-config: the fleet config of the project whose jobs this desktop runs (that repository's file, at the commit
# its always-on host uses); without it, the release's own .github/fork-fleet.json (the fork's CI), or the project's
# from the last install. --release-fleet-config: back to the release's own after a project's was used.
# It refuses a sha that is not a commit on the fork's protected release branch (ci-release), downloads exactly that
# commit, prints the sha and the hash of what it downloaded, and runs its installer (safe to run again).
# Needs: curl, tar, python3. Linux with systemd only (WSL2 with systemd on).
set -eu
umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset TMPDIR CURL_HOME SSL_CERT_FILE SSL_CERT_DIR || true

die() { echo "desk-bootstrap: $*" >&2; exit 1; }

repo='' sha='' branch='ci-release' fleet='' release_fleet='' api='https://api.github.com'
codeload='https://codeload.github.com'
while [ $# -gt 0 ]; do
  case "$1" in
    --repo | --sha | --release-branch | --fleet-config) [ $# -ge 2 ] || die "$1 needs a value" ;;
  esac
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --sha) sha="$2"; shift 2 ;;
    --release-branch) branch="$2"; shift 2 ;;
    --fleet-config) fleet="$2"; shift 2 ;;
    --release-fleet-config) release_fleet=1; shift ;;
    *) die "unknown option $1 (see the top of this file)" ;;
  esac
done
printf '%s\n' "$repo" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}' || die "--repo must be OWNER/NAME"
printf '%s\n' "$sha" | grep -Eqx '[0-9a-f]{40}' || die "--sha must be the full 40-character release commit"
printf '%s\n' "$branch" | grep -Eqx '[A-Za-z0-9._/-]{1,100}' || die "bad --release-branch"
[ -z "$fleet" ] || [ -f "$fleet" ] || die "--fleet-config $fleet: no such file"
[ -z "$fleet" ] || [ -z "$release_fleet" ] || die "--fleet-config and --release-fleet-config: one or the other"
[ "$(id -u)" = 0 ] || die "run this as root"

work="$(mktemp -d /tmp/desk-bootstrap.XXXXXXXX)"
trap 'rm -rf "${work:?}"' EXIT
trap 'exit 1' INT TERM HUP
fetch() { curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 0 -H 'Accept: application/vnd.github+json' -o "$2" "$1"; }

fetch "$api/repos/$repo/branches/$branch" "$work/branch.json" || die "cannot read branch $branch of $repo"
fetch "$api/repos/$repo/compare/$sha...$branch" "$work/compare.json" || die "cannot compare $sha with $branch"
python3 -I -B - "$work/branch.json" "$work/compare.json" "$sha" <<'PY' || die "$sha is not a release: it must be a commit on the protected branch $branch"
import json, sys
branch, compare = (json.load(open(p, encoding='utf-8')) for p in sys.argv[1:3])
# base_commit is always the sha asked for; merge_base_commit is the newest commit both share: it is the sha itself
# only when the sha is on the branch
ok = (branch.get('protected') is True and compare.get('status') in ('ahead', 'identical')
      and (compare.get('merge_base_commit') or {}).get('sha') == sys.argv[3])
sys.exit(0 if ok else 1)
PY

# codeload.github.com answers a commit tarball directly (no redirect)
fetch "$codeload/$repo/tar.gz/$sha" "$work/kit.tar.gz" || die "cannot download $repo at $sha"
mkdir "$work/src"
tar -xzf "$work/kit.tar.gz" -C "$work/src" --no-same-owner --no-same-permissions
top="$(find "$work/src" -mindepth 1 -maxdepth 1 -type d)"
[ "$(printf '%s\n' "$top" | wc -l)" = 1 ] && [ -n "$top" ] || die "the download does not have one top folder"
[ -f "$top/fork-kit/desk/sbin/desk-install" ] || die "release $sha has no fork-kit/desk (is it a release with the kit?)"
echo "desk-bootstrap: release $sha of $repo, download sha256 $(sha256sum "$work/kit.tar.gz" | cut -d' ' -f1)"
set -- install --source "$top" --sha "$sha"
[ -z "$fleet" ] || set -- "$@" --fleet-config "$fleet"
[ -z "$release_fleet" ] || set -- "$@" --release-fleet-config
python3 -I -B "$top/fork-kit/desk/sbin/desk-install" "$@"
