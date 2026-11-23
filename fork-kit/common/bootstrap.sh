#!/bin/sh
# fork-kit bootstrap: installs one kit of one release of this fork, for the kits that share
# fork-kit/common/kit_install.py. Run as root:
#
#   sh fork-kit-bootstrap.sh --kit KIT --repo FORK_OWNER/FORK --sha RELEASE_SHA [kit options]
#
# KIT and its options (each kit's sbin/*-install says what they do):
#   agent-guards  fork-kit/agent-guards/sbin/agent-guards-install  [--settings | --no-settings]
#   cleaner   fork-kit/cleaner/sbin/fork-cleaner-install   [--timer | --no-timer]
#   gardener  fork-kit/gardener/sbin/gardener-install      [--timer | --no-timer]
#   golive    fork-kit/golive/sbin/t3-golive-install       (none)
#   schedule-kick  fork-kit/schedule-kick/sbin/schedule-kick-install  [--timer | --no-timer]
#   update-policy  fork-kit/update-policy/sbin/update-policy-install  [--timer | --no-timer] [--force]
#
# It refuses a sha that is not a commit on the fork's protected release branch (ci-release), downloads exactly
# that commit, prints the sha and the hash of what it downloaded, and runs the kit's installer (safe to run again).
# Only the options listed for the kit reach its installer: never a second --sha or --source.
# Needs: curl, tar, python3. Linux with systemd only.
set -eu
umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset TMPDIR CURL_HOME SSL_CERT_FILE SSL_CERT_DIR || true

die() { echo "fork-kit-bootstrap: $*" >&2; exit 1; }

kit='' repo='' sha='' opt='' force='' branch='ci-release' api='https://api.github.com' codeload='https://codeload.github.com'
while [ $# -gt 0 ]; do
  case "$1" in
    --kit) [ $# -ge 2 ] || die "--kit needs a value"; kit="$2"; shift 2 ;;
    --repo) [ $# -ge 2 ] || die "--repo needs a value"; repo="$2"; shift 2 ;;
    --sha) [ $# -ge 2 ] || die "--sha needs a value"; sha="$2"; shift 2 ;;
    --release-branch) [ $# -ge 2 ] || die "--release-branch needs a value"; branch="$2"; shift 2 ;;
    --timer|--no-timer) opt="$1"; shift ;;
    --settings|--no-settings) opt="$1"; shift ;;
    --force) force="$1"; shift ;;
    *) die "unknown option $1 (see the top of this file)" ;;
  esac
done
case "$kit" in
  agent-guards) installer='sbin/agent-guards-install' ;;
  cleaner) installer='sbin/fork-cleaner-install' ;;
  gardener) installer='sbin/gardener-install' ;;
  golive) installer='sbin/t3-golive-install'; [ -z "$opt" ] || die "$opt is not an option of --kit golive" ;;
  schedule-kick) installer='sbin/schedule-kick-install' ;;
  update-policy) installer='sbin/update-policy-install' ;;
  *) die "--kit must be one of: agent-guards cleaner gardener golive schedule-kick update-policy" ;;
esac
[ -z "$force" ] || [ "$kit" = update-policy ] || die "--force is not an option of --kit $kit"
case "$kit:$opt" in
  agent-guards:--timer|agent-guards:--no-timer|cleaner:--*settings|gardener:--*settings|schedule-kick:--*settings|update-policy:--*settings)
    die "$opt is not an option of --kit $kit" ;;
esac
printf '%s\n' "$repo" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}' || die "--repo must be OWNER/NAME"
printf '%s\n' "$sha" | grep -Eqx '[0-9a-f]{40}' || die "--sha must be the full 40-character release commit"
printf '%s\n' "$branch" | grep -Eqx '[A-Za-z0-9._/-]{1,100}' || die "bad --release-branch"
[ "$(id -u)" = 0 ] || die "run this as root"

work="$(mktemp -d /tmp/fork-kit-bootstrap.XXXXXXXX)"
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
dir="$top/fork-kit/$kit"
[ -f "$dir/$installer" ] || die "release $sha has no fork-kit/$kit installer (is it a release with the kit's installer?)"
echo "fork-kit-bootstrap: $kit kit, release $sha of $repo, download sha256 $(sha256sum "$work/kit.tar.gz" | cut -d' ' -f1)"
python3 -I -B "$dir/$installer" install --source "$dir" --sha "$sha" ${opt:+"$opt"} ${force:+"$force"}
