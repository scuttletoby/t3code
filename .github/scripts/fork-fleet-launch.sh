#!/usr/bin/env bash
# Fork only: starts and stops a fleet host's one-job runners, as root, for `fork-fleet.py dispatch` (a pool's `launch`
# command in .github/fork-fleet.json). Install a root-owned copy of main's file; runbook: .github/fork-fleet.md.
#
#   start NAME   stdin: the lines ACTIONS_RUNNER_INPUT_JITCONFIG=, FORK_FLEET_RUN= and FORK_FLEET_ATTEMPT=, nothing
#                else. Runs a fresh copy of the runner template for this one job, in a transient systemd unit
#                fork-fleet-NAME, as a runner user no other unit runs as (RUNNER_USERS: one per slot), in that user's
#                network namespace (below). Before that user is used again, every process of it is killed, and so is
#                every copy it ran in: HOME and TMPDIR are inside the copy, and /tmp, /var/tmp, /dev/shm and /run are
#                the unit's own memory file systems, gone with it, so a job leaves nothing for the next one and root
#                never deletes anything in a directory a runner user can write. The environment (the JIT config, the
#                run it is for, HOME, TMPDIR and the job-started hook) comes from a root-only file, so nothing a
#                runner user can write decides it.
#   ready NEWEST exit 0 when the template's runner version (TEMPLATE/.fork-fleet-version, written by refresh) is
#                not older than NEWEST, the newest actions/runner release. An older runner would update itself in
#                its copy before the job, which this fleet refuses: refresh renews the template instead.
#   refresh      makes TEMPLATE the newest actions/runner release when it differs or is missing (the hourly timer
#                fork-fleet-refresh.timer, or by hand): reads the release, downloads its linux tarball, checks the
#                release's sha256 before unpacking, unpacks it (refusing anything a runner release never has) beside
#                TEMPLATE and swaps the two by one rename under the launch lock. Running jobs keep their copies; any
#                failure leaves the old template. Below.
#   active       the NAME of every fork-fleet-* unit that is loaded (starting, running or stopping).
#   fits MB      exit 0 when MemAvailable minus RESERVE_MB leaves MB megabytes and, with FREE_COMMAND set, that free
#                script answers free (free/v1, fork-kit/common/FREE.md: run by FREE_CHECK, its reader).
#   stop NAME    stops that unit (a job on it fails: its runner is gone).
#   prune        removes the copies and environment files whose unit is gone.
#   refresh-net  every dispatcher pass: looks this host's own public address up again (once its last answer is
#                OWN_ADDRESS_SECONDS old, or the table is gone, or a unit still waits for its IPAddressDeny) and, when
#                what is blocked would change or the table is gone, re-applies the table (it covers running jobs too)
#                and adds the new addresses to each running unit's IPAddressDeny. No lookup answers: it fails and
#                changes nothing (the last block stays), and the dispatcher starts no runner on the pool this pass.
#   check-net URL HOST:PORT...
#                the proof of the network and the walls (.github/fork-fleet.md, host step 6): runs three probes in a
#                runner's real sandbox, as a free runner user (one with this host's own public addresses left out of
#                its IPAddressDeny, so the nft table alone must refuse them). Exit 0 only when URL (public) answers,
#                every HOST:PORT (services this host reaches: its loopback, its LAN address, Tailscale) is closed from
#                inside, this host's own public address is refused by the table alone and closed in the job's own
#                sandbox, no AF_VSOCK socket, io_uring ring or 32-bit system call works inside, only the pseudo
#                devices and no socket are there, the unit sees no mount but the ones job_fs allows, each of them
#                comes from the host path job_fs resolved (no link swapped in between), and none of them receives a
#                mount the host makes later.
#   paths        reads only, for the per-OS walls (fork-fleet-walls/<os>, `mounts`): a line `DEST SOURCE` for every
#                file or folder of this host a job's unit gets (DEST where the unit has it, SOURCE the real path bound
#                there: job_view, the same rule as start's), WORK_ROOT standing for each job's copy in it.
#   tools        reads only, for the per-OS walls (`job-units`): the programs the job units are started and asked
#                with, `SYSTEMD_RUN <program>` and `SYSTEMCTL <program>`, as launch.conf sets them.
#
# The network: each runner user has a namespace of its own, fork-fleet-USER, so a job has its own loopback and reaches
# no service on this host's (nor an abstract socket). Its only link, ffh-UID (host side), goes to the public internet:
# the nft table inet fork_fleet refuses the host itself, RFC 1918, CGNAT (Tailscale), link-local, every IPv6 packet
# and this host's own public IPv4 addresses (own_address, before every start and in refresh-net), and the unit's
# IPAddressDeny refuses the same again, in the kernel, should the table ever go. Every start (re)makes the namespace
# and the table under the launch lock and fails closed: no runner starts when either fails. The walls (sandbox): no
# AF_VSOCK, io_uring or 32-bit system calls, only the pseudo devices, and of this host's files only what job_fs
# allows (an allow list: a disk or share mounted anywhere else is not in the unit).
#
# Settings: /etc/fork-fleet/launch.conf (or $FORK_FLEET_LAUNCH_CONF), KEY=value lines, owned by this user and
# writable by it alone. RUNNER_USERS (user names, comma-separated: at least the pool's slots) is required. TEMPLATE
# (an unconfigured actions/runner, owned by this user, on WORK_ROOT's filesystem, alone in a folder of its own: the
# only folder the refresh service may write, fork-fleet-refresh.service), WORK_ROOT, RUN_DIR, HOOK, LINGER_DIR,
# RESERVE_MB, MAX_SECONDS, MEMINFO, NET_BASE (10.N: the runners' 10.N.0.0/16, one /30 per user from its uid, unused by
# anything else on the host), RESOLV_CONF (the jobs' resolv.conf: public nameservers, as this host's own resolver is on
# its loopback), OWN_ADDRESS_URLS (https URLs, comma-separated, that answer with the address they see: every public
# address they give is blocked, at least one must answer), OWN_ADDRESS_SECONDS (how long refresh-net keeps using its
# last answer, at most 3600; 0: it looks up on every pass; start always looks up), JOB_PATHS (the system paths every
# job sees: fork-fleet-job-paths.txt beside this launcher), ALLOW_PATHS (more paths jobs see read-only, comma-separated, say a
# tool cache: empty by default), CANARY_DIR (where check-net keeps its two canary files while it runs: /etc, which no
# job sees whole), IP_FORWARD, SYSTEMD_RUN, SYSTEMCTL, RUNNER_RELEASE (refresh's release record, an https
# URL), RUNNER_DOWNLOAD (the https base of its tarballs) and RUNNER_MIN (the oldest runner version refresh may install,
# empty for none), FREE_COMMAND (a free/v1 script's absolute path, empty for none) and FREE_CHECK (its reader,
# free_contract.py beside this launcher) have defaults below.
# Each job unit's own limits (job_limits), so a job that runs away is held or killed, never the host's own work:
# JOB_MEMORY_MAX_MB (MemoryMax: the job is killed above it; empty, the default: MemTotal minus RESERVE_MB, shared by
# the RUNNER_USERS, so all slots together never take the reserve; at least JOB_MEMORY_MIN), JOB_MEMORY_HIGH_MB
# (MemoryHigh: the kernel slows the job and takes its memory back above it; empty: 90% of MemoryMax), JOB_SWAP_MAX_MB
# (MemorySwapMax, 0 by default: a job never fills the host's swap), JOB_CPU_WEIGHT (CPUWeight, 1 to 10000, 50 by
# default: half a normal unit's share when the host is busy), JOB_CPU_QUOTA (CPUQuota in percent, 100 = one CPU;
# empty, the default: no cap) and JOB_OOM_SCORE_ADJUST (OOMScoreAdjust, 1 to 1000, 500 by default: when the host
# itself runs out of memory, the kernel kills a job before any of the host's own processes). OOMPolicy=kill: the
# kernel killing one process of a job stops all of the job's unit.
# HOOK (and the two files beside it), WORK_ROOT and RESOLV_CONF are bound into every job too: never under a folder that
# holds data (/home, /root, /mnt, /media, /srv), also through a symlink (no_data). A bind mount of a data folder at
# one of them is not seen (it has no link to resolve): that is the operator's to avoid (.github/fork-fleet.md, step 4).
# FORK_FLEET_WALLS_ROOT: the walls' test knob (fork-fleet-walls.py), `paths` and `tools` only and never as root: a
# fake machine whose launch.conf and paths it reads.
set -euo pipefail
umask 077

# Every message goes out through here, and text from outside is in some of them (a lookup service's answer, curl's
# or iptables' words, a caller's argument): printable ASCII only, so ESC, BEL, a bidi mark or any other byte reaches a
# terminal as ?, never raw (an OSC 52 clipboard write, a hidden OSC 8 link), and at most 2000 bytes.
shown() {
  local LC_ALL=C s=$1
  s=${s//[^ -~]/?}
  printf '%s' "${s:0:2000}"
}
# A probe's lines (check-net's: host file, socket and mount names, a job's view of them) through the same shown().
shown_lines() {
  local l
  while IFS= read -r l || [[ -n $l ]]; do printf '%s\n' "$(shown "$l")"; done
}
warn() { printf 'fork-fleet-launch: %s\n' "$(shown "$*")" >&2; }
die() {
  warn "$@"
  exit 1
}

# A path this user owns and no one else may write (not a symlink).
owned_safe() {
  local uid mode
  [[ -e $1 && ! -L $1 ]] || die "$1 is missing or a symlink"
  read -r uid mode < <(stat -c '%u %a' -- "$1")
  [[ $uid == "$EUID" ]] || die "$1 must belong to uid $EUID"
  (((8#$mode & 8#022) == 0)) || die "$1 must be writable by its owner only"
}

# The fake machine of the walls' tests, or '' (this one): refused for every verb but `paths` and `tools`, and when
# root runs this.
ROOT=${FORK_FLEET_WALLS_ROOT-}
readonly_verb=0
[[ ${1-}:$# != paths:1 && ${1-}:$# != tools:1 ]] || readonly_verb=1
if [[ -n $ROOT ]]; then
  ((readonly_verb && EUID != 0)) || die "FORK_FLEET_WALLS_ROOT is set: it is for the walls' tests only (paths and tools, not as root)"
  # shellcheck disable=SC2015 # die when either fails
  ROOT=$(realpath -e -- "$ROOT") && [[ $ROOT =~ ^/[A-Za-z0-9._/-]+$ ]] || die "FORK_FLEET_WALLS_ROOT is no plain folder"
fi
conf=${FORK_FLEET_LAUNCH_CONF:-/etc/fork-fleet/launch.conf}
# paths and tools on a host not set up yet (no conf at all): what jobs would get with the defaults below.
conf_file=/dev/null
# shellcheck disable=SC2015 # read the conf unless both hold
((readonly_verb)) && [[ ! -e $ROOT$conf && ! -L $ROOT$conf ]] || { owned_safe "$ROOT$conf" && conf_file=$ROOT$conf; }
RUNNER_USERS='' TEMPLATE=/opt/fork-fleet/template/runner-template WORK_ROOT=/var/lib/fork-fleet/runners RUN_DIR=/run/fork-fleet HOOK=/opt/fork-fleet/hook.sh
LINGER_DIR=/var/lib/systemd/linger RESERVE_MB=2048 MAX_SECONDS=3600 MEMINFO=/proc/meminfo NET_BASE=10.204
RESOLV_CONF=/etc/fork-fleet/resolv.conf IP_FORWARD=/proc/sys/net/ipv4/ip_forward SYSTEMD_RUN=systemd-run
SYSTEMCTL=systemctl OWN_ADDRESS_URLS=https://api.ipify.org,https://checkip.amazonaws.com OWN_ADDRESS_SECONDS=120
ALLOW_PATHS='' CANARY_DIR=/etc
JOB_PATHS='' RUNNER_RELEASE=https://api.github.com/repos/actions/runner/releases/latest
RUNNER_DOWNLOAD=https://github.com/actions/runner/releases/download RUNNER_MIN='' FREE_COMMAND='' FREE_CHECK=''
JOB_MEMORY_MAX_MB='' JOB_MEMORY_HIGH_MB='' JOB_SWAP_MAX_MB=0 JOB_CPU_WEIGHT=50 JOB_CPU_QUOTA='' JOB_OOM_SCORE_ADJUST=500
setting='^(RUNNER_USERS|TEMPLATE|WORK_ROOT|RUN_DIR|HOOK|LINGER_DIR|RESERVE_MB|MAX_SECONDS|MEMINFO|NET_BASE|RESOLV_CONF|IP_FORWARD|SYSTEMD_RUN|SYSTEMCTL|OWN_ADDRESS_URLS|OWN_ADDRESS_SECONDS|JOB_PATHS|ALLOW_PATHS|CANARY_DIR|RUNNER_RELEASE|RUNNER_DOWNLOAD|RUNNER_MIN|FREE_COMMAND|FREE_CHECK|JOB_MEMORY_MAX_MB|JOB_MEMORY_HIGH_MB|JOB_SWAP_MAX_MB|JOB_CPU_WEIGHT|JOB_CPU_QUOTA|JOB_OOM_SCORE_ADJUST)=([^[:space:]]*)$'
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
  [[ $line =~ $setting ]] || die "$conf: not a setting: ${line%%=*}"
  printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
done <"$conf_file"
[[ -n $RUNNER_USERS && -n $TEMPLATE ]] || ((readonly_verb)) || die "$conf: RUNNER_USERS and TEMPLATE are required"
[[ $RESERVE_MB =~ ^[0-9]{1,7}$ && $MAX_SECONDS =~ ^[1-9][0-9]{0,6}$ ]] || die "$conf: RESERVE_MB and MAX_SECONDS are numbers"
{ [[ $NET_BASE =~ ^10\.(0|[1-9][0-9]{0,2})$ ]] && ((BASH_REMATCH[1] <= 255)); } || die "$conf: NET_BASE is 10.N (N up to 255)"
own_url='https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?'
[[ $OWN_ADDRESS_URLS =~ ^$own_url(,$own_url)*$ ]] || die "$conf: OWN_ADDRESS_URLS is a comma-separated list of https URLs"
{ [[ $OWN_ADDRESS_SECONDS =~ ^[0-9]{1,4}$ ]] && ((10#$OWN_ADDRESS_SECONDS <= 3600)); } ||
  die "$conf: OWN_ADDRESS_SECONDS is a number of seconds, at most 3600"
PLAIN_PATH='(/[A-Za-z0-9._@+-]+)+'
[[ $ALLOW_PATHS =~ ^($PLAIN_PATH(,$PLAIN_PATH)*)?$ ]] || die "$conf: ALLOW_PATHS is a comma-separated list of plain paths"
# An absolute path (a relative one would be read from wherever this runs); on the fake machine, the fake one's.
[[ $JOB_PATHS =~ ^($PLAIN_PATH)?$ ]] || die "$conf: JOB_PATHS is a plain absolute path"
own_job_paths=0
if [[ -n $JOB_PATHS ]]; then JOB_PATHS=$ROOT$JOB_PATHS; else
  JOB_PATHS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/fork-fleet-job-paths.txt own_job_paths=1
fi
[[ $CANARY_DIR =~ ^$PLAIN_PATH$ ]] || die "$conf: CANARY_DIR is a plain absolute path"
[[ $FREE_COMMAND =~ ^($PLAIN_PATH)?$ && $FREE_CHECK =~ ^($PLAIN_PATH)?$ ]] ||
  die "$conf: FREE_COMMAND and FREE_CHECK are plain absolute paths"
[[ -n $FREE_CHECK ]] || FREE_CHECK=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/free_contract.py
MB_SETTING='(0|[1-9][0-9]{0,6})'
[[ $JOB_MEMORY_MAX_MB =~ ^$MB_SETTING?$ && $JOB_MEMORY_HIGH_MB =~ ^$MB_SETTING?$ && $JOB_SWAP_MAX_MB =~ ^$MB_SETTING$ ]] ||
  die "$conf: JOB_MEMORY_MAX_MB, JOB_MEMORY_HIGH_MB and JOB_SWAP_MAX_MB are numbers of megabytes"
{ [[ $JOB_CPU_WEIGHT =~ ^[1-9][0-9]{0,4}$ ]] && ((JOB_CPU_WEIGHT <= 10000)); } || die "$conf: JOB_CPU_WEIGHT is 1 to 10000"
[[ $JOB_CPU_QUOTA =~ ^([1-9][0-9]{0,4})?$ ]] || die "$conf: JOB_CPU_QUOTA is a percent (100: one CPU), or empty for none"
{ [[ $JOB_OOM_SCORE_ADJUST =~ ^[1-9][0-9]{0,3}$ ]] && ((JOB_OOM_SCORE_ADJUST <= 1000)); } ||
  die "$conf: JOB_OOM_SCORE_ADJUST is 1 to 1000 (a job is killed before the host's own processes, never after)"

NAME='^[a-z0-9][a-z0-9-]{0,62}-[0-9]{1,20}$'
name_ok() { [[ $1 =~ $NAME ]] || die "not a runner name: $1"; }

active() {
  local unit rest
  "$SYSTEMCTL" list-units --all --plain --no-legend --type=service 'fork-fleet-*' | while read -r unit rest; do
    unit=${unit%.service}
    unit=${unit#fork-fleet-}
    if [[ $unit =~ $NAME ]]; then printf '%s\n' "$unit"; fi
  done
}

# The directories every verb that changes the host needs, and the lock that runs those verbs one at a time (two
# dispatchers, one per pool, may drive one host). WORK_ROOT: each runner user passes through it to its own copy
# (0711: it cannot list the others). RUN_DIR: root only.
setup() {
  local real
  # Never under a root that holds data (no_data), as written or as resolved: checked before anything is made there.
  bound_real "$WORK_ROOT"
  (umask 022 && mkdir -p -- "$WORK_ROOT")
  mkdir -p -- "$RUN_DIR"
  owned_safe "$WORK_ROOT"
  owned_safe "$RUN_DIR"
  # From here on its real path (a link in a folder above it resolved): the copies are made, bound and pruned there.
  bound_real "$WORK_ROOT"
  WORK_ROOT=$real
  chmod 0711 -- "$WORK_ROOT"
  chmod 0700 -- "$RUN_DIR"
  launch_lock
}

# The launch lock alone (RUN_DIR made and checked already).
launch_lock() {
  launch_lock_open
  flock -w 100 "$lock" || die "another launcher holds $RUN_DIR/launch.lock"
}

# Opens RUN_DIR/launch.lock read-only (flock needs no more) as $lock, never through a link: root runs this outside any
# sandbox, and a plain `>` would truncate whatever a link planted there points to (the job hook, launch.conf). Made
# empty when missing (noclobber: O_EXCL, which a dangling link fails too); a link or anything but a plain file is
# refused. The sandboxed refresh service cannot write RUN_DIR: it only opens the file the dispatcher's first prune or
# start made (or a refresh by hand).
launch_lock_open() {
  local path=$RUN_DIR/launch.lock outcome=${1:-nothing changed}
  if [[ ! -e $path && ! -L $path ]]; then
    (set -o noclobber && : >"$path") 2>/dev/null || [[ -e $path || -L $path ]] ||
      die "there is no $path yet and this launcher may not make it (the refresh service only reads $RUN_DIR): the" \
        "dispatcher's next prune or start makes it; $outcome"
  fi
  [[ -f $path && ! -L $path ]] ||
    die "$path is a symlink or not a plain file: refused (only root writes $RUN_DIR: find out who made it); $outcome"
  exec {lock}<"$path"
}

# The first file under $1 that a template must not have: one not this user's, one others may write, a special file
# (device, FIFO, socket) or a setuid, setgid or sticky one. Empty when there is none. (A runner release has none: the
# template is hard-linked into every job's copy, and a device node there would be a way out of the job's sandbox.)
unsafe_file() {
  find "$1" \( ! -uid "$EUID" -o \( ! -type f ! -type d ! -type l \) -o \( ! -type l -perm /7022 \) \) -print -quit
}

VERSION='^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$'

template_version() {
  local v
  [[ -f $TEMPLATE/.fork-fleet-version && ! -L $TEMPLATE/.fork-fleet-version ]] ||
    die "$TEMPLATE has no .fork-fleet-version: refresh the template (.github/fork-fleet.md, host step 7)"
  v=$(<"$TEMPLATE/.fork-fleet-version")
  [[ $v =~ $VERSION ]] || die "$TEMPLATE/.fork-fleet-version is not a version: refresh the template (host step 7)"
  printf '%s\n' "$v"
}

# Exit 0 when the template's runner is not older than NEWEST. A JIT runner older than the newest release updates
# itself in its copy before the job; this fleet does not rely on that, so it starts no runner until refresh (its
# timer, or by hand) renews the template.
ready() {
  local have
  [[ $1 =~ $VERSION ]] || die "ready: not a version: $1"
  [[ -e $TEMPLATE || -L $TEMPLATE ]] ||
    die "there is no runner template ($TEMPLATE): no runner starts until \`fork-fleet-launch.sh refresh\` makes one" \
      "(.github/fork-fleet.md, host step 7)"
  owned_safe "$TEMPLATE"
  have=$(template_version)
  if older "$have" "$1"; then
    die "the runner template is actions/runner $have, older than the newest release $1: no runner starts until" \
      "\`fork-fleet-launch.sh refresh\` refreshes the template (its timer, or by hand:" \
      ".github/fork-fleet.md, host step 7)"
  fi
}

# Exit 0 when version $1 is older than version $2 (both x.y.z).
older() {
  local i
  local -a a b
  IFS=. read -ra a <<<"$1"
  IFS=. read -ra b <<<"$2"
  for i in 0 1 2; do
    if ((10#${a[i]} != 10#${b[i]})); then
      ((10#${a[i]} < 10#${b[i]}))
      return
    fi
  done
  return 1
}

# refresh's reader of the release record (stdin; $1 the platform, like linux-x64): prints "VERSION SHA256" of the
# tarball actions-runner-PLATFORM-VERSION.tar.gz. The sha256 is the asset's digest and the one the release notes give
# (<!-- BEGIN SHA PLATFORM -->); at least one must be there, and the two must agree.
read -r -d '' RELEASE_PY <<'EOF' || true
import json, re, sys
plat = sys.argv[1]
try:
    r = json.load(sys.stdin)
except ValueError:
    sys.exit('the release record is not JSON')
tag = r.get('tag_name') if isinstance(r, dict) else None
m = re.fullmatch(r'v?([0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5})', str(tag))
if not m:
    sys.exit(f'the newest release is {tag!r}, not a version')
v = m.group(1)
name = f'actions-runner-{plat}-{v}.tar.gz'
assets = [a for a in r.get('assets') or [] if isinstance(a, dict) and a.get('name') == name]
if not assets:
    sys.exit(f'release {v} has no {name}')
sums = set()
for a in assets:
    if a.get('digest') is not None:
        d = re.fullmatch(r'sha256:([0-9a-f]{64})', str(a['digest']))
        if not d:
            sys.exit(f'release {v}: the digest of {name} is not a sha256')
        sums.add(d.group(1))
p = re.escape(plat)
for d in re.findall(rf'<!-- BEGIN SHA {p} -->(.*?)<!-- END SHA {p} -->', str(r.get('body') or ''), re.S):
    if not re.fullmatch(r'[0-9a-fA-F]{64}', d.strip()):
        sys.exit(f'release {v}: the sha256 of {name} in its notes is not one')
    sums.add(d.strip().lower())
if len(sums) != 1:
    sys.exit(f'release {v} gives ' + ('no sha256' if not sums else 'two different sha256') + f' for {name}')
print(v, sums.pop())
EOF

# renameat2(RENAME_EXCHANGE): the two directories swap names in one step, or nothing changes. (mv --exchange is
# coreutils 9.5, newer than some hosts have.)
read -r -d '' EXCHANGE_PY <<'EOF' || true
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.renameat2(-100, os.fsencode(sys.argv[1]), -100, os.fsencode(sys.argv[2]), 2) != 0:
    sys.exit(os.strerror(ctypes.get_errno()))
EOF

# refresh's unpacker (fork-fleet-unpack): $1 the tarball (sha256 checked), $2 the empty folder to fill, $3 the version,
# $4 the floor (the oldest version a later refresh may roll this template back to).
# The tarball is treated as hostile (a compromised release passes the sha256 check): read once, checked whole, then
# written by this same reader (no second parser to disagree with). Refused: an absolute name, a "..", a name twice, a
# name under a symlink or a file, a symlink or hard link that leads outside the folder, a device, FIFO or other special
# file, a sparse file of any format (GNU or pax: its map is read into memory), a .fork-fleet-* name of its own, more
# than MAX_ENTRIES entries or MAX_BYTES of files (the type and size refused at each header, before the data is
# decompressed), a file whose size its data does not have (a pax GNU.sparse.realsize: gzip would decompress again from
# the start for each file after it), a long-name or pax header over MAX_HEADER bytes or such headers over MAX_NAMES
# bytes read in all (each refused before it is read into memory), names and link targets over MAX_NAMES bytes in all
# (as kept in memory), global pax headers of more than MAX_GLOBAL keys (tarfile copies them into every entry after
# them) or with any key tarfile sets on each entry, more headers in a row than tarfile can follow, a name or link
# target over MAX_NAME, symlinks that take more than MAX_STEPS steps to follow (fanned-out links would take hours).
# Stripped (as tar's --no-same-owner --no-same-permissions would): owner (all root's), setuid, setgid, sticky and
# group/other write, and every directory is 0755. Files are created with O_EXCL|O_NOFOLLOW and symlinks last, so nothing
# is ever written through a link; the version and floor files too.
read -r -d '' UNPACK_PY <<'EOF' || true
# fork-fleet-unpack
import os, posixpath, sys, tarfile, zlib
src, dest, version, floor = sys.argv[1:5]
MAX_ENTRIES, MAX_BYTES = 100000, 4 << 30
MAX_HEADER, MAX_NAME, MAX_STEPS = 65536, 4096, 1000000
# MAX_NAMES bounds both the long-name and pax headers read (counted in the 512-byte blocks read, so empty ones cost
# too) and the names and link targets kept for all MAX_ENTRIES entries. A runner release reads about 1.1 MB of such
# headers (GNU long names; no pax and no global header) and keeps about 0.9 MB of names.
MAX_NAMES, MAX_GLOBAL = 64 << 20, 64
# The global pax keys tarfile sets on every entry after them (a 4300-digit uid is a new int in each).
PER_ENTRY = {*tarfile.PAX_FIELDS, *(f'GNU.sparse.{k}' for k in ('name', 'size', 'realsize', 'major', 'minor', 'map',
                                                                'offset', 'numblocks'))}
headers = names = total = 0
NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
os.umask(0o022)


def no(why):
    sys.exit(f'refused: {why}')


def kept(s):
    """The bytes a name takes in memory: a str is 1, 2 or 4 bytes a character, by its widest one (one emoji makes
    4096 characters 16 KiB, from 4099 bytes of header)."""
    w = max(s, default='')
    return len(s) * (1 if w <= '\xff' else 2 if w <= '\uffff' else 4)


def parts(name):
    if name.startswith('/'):
        no(f'{name!r} is an absolute path')
    p = [c for c in name.split('/') if c not in ('', '.')]
    if '..' in p:
        no(f'{name!r} has a ".."')
    return p


class Capped(tarfile.TarInfo):
    """Refuses a GNU long name or pax header over MAX_HEADER bytes, or past MAX_NAMES bytes of them read in all,
    before tarfile reads it whole into memory; global pax keys past MAX_GLOBAL, or one tarfile sets on each entry,
    before tarfile copies them into the next entry; and a sparse file of any format before tarfile reads its map (a
    runner release has none)."""

    def _proc_member(self, tar):
        global headers
        if len(tar.pax_headers) > MAX_GLOBAL:
            no(f'more than {MAX_GLOBAL} global pax keys')
        if bad := sorted(tar.pax_headers.keys() & PER_ENTRY):   # a new copy in each entry, or a size it does not have
            no(f'a global pax header with {bad[0]}')
        if self.type == tarfile.GNUTYPE_SPARSE:
            no(f'{self.name!r} is a sparse file')
        if self.type in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE,
                         tarfile.SOLARIS_XHDTYPE):
            if self.size > MAX_HEADER:
                no(f'a header of {self.size} bytes (more than {MAX_HEADER})')
            headers += tarfile.BLOCKSIZE + self._block(self.size)
            if headers > MAX_NAMES:
                no(f'more than {MAX_NAMES} bytes of long names and pax headers')
        return super()._proc_member(tar)

    # A pax header's sparse map, in any of its three formats, is read (from the header or the entry's data) into
    # lists with no bound; called once the next header is read, before the map is.
    def _proc_gnusparse_00(self, *_):
        no('a pax sparse file')

    _proc_gnusparse_01 = _proc_gnusparse_10 = _proc_gnusparse_00


try:
    tar = tarfile.open(src, 'r:gz', tarinfo=Capped)
    members = []
    for m in tar:
        # Each name as it is read: many long names would fill memory before a check after the loop.
        if len(m.name) > MAX_NAME or len(m.linkname) > MAX_NAME:
            no(f'a name or link target longer than {MAX_NAME}: {m.name[:100]!r}')
        names += kept(m.name) + kept(m.linkname)
        if names > MAX_NAMES:
            no(f'more than {MAX_NAMES} bytes of names and link targets')
        # A size its data does not have (a pax GNU.sparse.realsize sets it, and tarfile skips the data by the old
        # one): each file after it would seek back, and gzip seeks back by decompressing from the start again.
        if m.isreg() and tar.offset != m.offset_data + m._block(m.size):
            no(f'{m.name[:100]!r} has a size its data does not have')
        if 'GNU.sparse.realsize' in m.pax_headers:
            no(f'{m.name[:100]!r} has a pax GNU.sparse.realsize')
        # The type and the size at the header, before tarfile decompresses past the data (a gzip bomb).
        if not (m.isdir() or m.issym() or m.islnk() or m.isreg()):
            what = 'device' if m.ischr() or m.isblk() else 'FIFO' if m.isfifo() else 'special'
            no(f'{m.name!r} is a {what} file')
        if m.isreg():
            total += m.size
            if total > MAX_BYTES:
                no(f'more than {MAX_BYTES} bytes of files')
        m.pax_headers = {}   # its copy of the global pax keys (and its own): not used after this
        members.append(m)
        if len(members) > MAX_ENTRIES:
            no(f'more than {MAX_ENTRIES} entries')
except RecursionError:   # tarfile recurses on each header in a row (pax, long name) before an entry
    no('more headers in a row than tarfile can follow')
except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError) as e:   # ValueError: int() of 4301 digits
    sys.exit(f'not a gzip tarball: {e}')

kinds, links, order, plan = {}, {}, {}, []
for i, m in enumerate(members):
    p = parts(m.name)
    kind = 'dir' if m.isdir() else 'symlink' if m.issym() else 'hardlink' if m.islnk() else 'file'   # no other
    if not p:
        if kind != 'dir':
            no(f'{m.name!r}, the top, is not a directory')
        continue
    path = '/'.join(p)
    if p[0].startswith('.fork-fleet-'):
        no(f'it has a {p[0]} of its own')
    if path in kinds and not (kind == 'dir' == kinds[path]):
        no(f'{path!r} twice')
    kinds[path], order[path] = kind, i
    if kind == 'symlink':
        links[path] = m.linkname
    plan.append((path, kind, m))


steps = 0


def resolve(link, depth=0):
    """Where symlink `link` leads, as a list of names under dest, following the tarball's own symlinks; None when
    it leads outside (or loops). All calls together take at most MAX_STEPS steps."""
    global steps
    target = links[link]
    if depth > 40 or not target or target.startswith('/'):
        return None
    at = link.split('/')[:-1]
    for c in target.split('/'):
        steps += 1
        if steps > MAX_STEPS:
            no(f'its symlinks take more than {MAX_STEPS} steps to follow')
        if c in ('', '.'):
            continue
        if c == '..':
            if not at:
                return None
            at.pop()
            continue
        at.append(c)
        if kinds.get('/'.join(at)) == 'symlink':
            at = resolve('/'.join(at), depth + 1)
            if at is None:
                return None
    return at


for path, kind, m in plan:
    p = path.split('/')
    for n in range(1, len(p)):
        if kinds.get('/'.join(p[:n]), 'dir') != 'dir':
            no(f'{path!r} is under {"/".join(p[:n])!r}, which is no directory')
    if kind == 'symlink' and resolve(path) is None:
        no(f'the symlink {path!r} -> {m.linkname!r} leads outside')
    if kind == 'hardlink':
        t = '/'.join(parts(m.linkname))
        if kinds.get(t) != 'file' or order[t] > order[path]:
            no(f'the hard link {path!r} -> {m.linkname!r} is not to a file before it')


def at(path):
    return os.path.join(dest, path)


try:
    dirs, written = [], 0
    for path, kind, m in plan:
        up = posixpath.dirname(path)
        if up:
            os.makedirs(at(up), 0o755, exist_ok=True)   # no symlink exists yet: they come last
        if kind == 'dir':
            if not os.path.isdir(at(path)):
                os.mkdir(at(path), 0o700)
            dirs.append(path)
        elif kind == 'file':
            fd = os.open(at(path), NEW, 0o600)
            with os.fdopen(fd, 'wb') as out:
                data = tar.extractfile(m)
                while chunk := data.read(1 << 20):
                    written += len(chunk)
                    if written > MAX_BYTES:
                        no(f'more than {MAX_BYTES} bytes of files')
                    out.write(chunk)
                os.fchmod(out.fileno(), m.mode & 0o755)
        elif kind == 'hardlink':
            os.link(at('/'.join(parts(m.linkname))), at(path), follow_symlinks=False)
    for path in reversed(dirs):
        fd = os.open(at(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(fd, 0o755)   # every runner user passes through
        finally:
            os.close(fd)
    for path, kind, m in plan:
        if kind == 'symlink':
            os.symlink(m.linkname, at(path))
    for name, text in (('.fork-fleet-version', version), ('.fork-fleet-floor', floor)):
        if not text:
            continue   # no floor: the first template, or one with no good version
        fd = os.open(at(name), NEW, 0o644)
        with os.fdopen(fd, 'w') as out:
            out.write(text + '\n')
            os.fchmod(out.fileno(), 0o644)
except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
    sys.exit(f'did not unpack: {e}')
EOF

# The release's name for this machine. The launcher itself runs on Linux (systemd, nft), WSL2 included; another OS
# needs its own sandbox first (the per-OS walls layer).
runner_platform() {
  local os arch
  os=$(uname -s) arch=$(uname -m)
  [[ $os == Linux ]] || die "refresh: no runner template for $os here (the launcher runs on Linux only)"
  case $arch in
    x86_64) printf 'linux-x64\n' ;;
    aarch64 | arm64) printf 'linux-arm64\n' ;;
    armv7l) printf 'linux-arm\n' ;;
    *) die "refresh: actions/runner has no release for $arch" ;;
  esac
}

# The newest runner into TEMPLATE, or nothing when it has it already. Only forward, by at most one major version (a
# bigger jump is refused: no real release does that, a compromised one might); a template NEWER than the newest
# release is rolled back to it (a release GitHub withdrew must not stay, nor make every later release "older"), but
# never below its floor: the version it replaced (.fork-fleet-floor, written when it came; without one, one major
# version down, as forward), and never below RUNNER_MIN. So one withdrawn release is undone, and an old release
# marked "latest" by mistake is refused. TEMPLATE sits alone in a folder of its own, the only one the sandboxed service
# may write (no file the dispatcher runs as root is there). The new one is unpacked in a temp folder beside
# TEMPLATE (one filesystem, so the swap is a rename), checked, flushed to disk, then swapped in under the launch lock:
# no start copies a half-swapped template, and running jobs keep their copies (hard links of the old files). A cut
# download, a bad checksum or any other failure leaves the old template as it was; the temp folder goes on exit, and a
# leftover of a run that was killed goes with the next one. Two refreshes run one after the other (a lock on TEMPLATE's
# folder). It writes nothing else: RUN_DIR's launch lock is only opened read-only.
refresh() {
  local base parent platform meta out newest sum have='' floor='' lowest tmp url f bad
  base=${TEMPLATE##*/} parent=${TEMPLATE%/*}
  [[ $TEMPLATE =~ ^/[A-Za-z0-9._/-]+$ && $base =~ ^[A-Za-z0-9._-]+$ && $base != . && $base != .. && -n $parent ]] ||
    die "$conf: TEMPLATE is not a plain absolute path in a folder: $TEMPLATE"
  [[ $RUNNER_RELEASE =~ ^https://[A-Za-z0-9.:/_-]+$ && $RUNNER_DOWNLOAD =~ ^https://[A-Za-z0-9.:/_-]+$ ]] ||
    die "$conf: RUNNER_RELEASE and RUNNER_DOWNLOAD are https URLs"
  [[ -z $RUNNER_MIN || $RUNNER_MIN =~ $VERSION ]] || die "$conf: RUNNER_MIN is not a version: $RUNNER_MIN"
  platform=$(runner_platform)
  # The template's own folder: made on the first run by hand (the service cannot: it may write only this folder).
  if [[ ! -e $parent && ! -L $parent ]]; then
    f=${parent%/*}
    owned_safe "${f:-/}"
    mkdir -m 0755 -- "$parent" 2>/dev/null ||
      die "refresh: there is no $parent, the template's folder: run refresh once by hand (.github/fork-fleet.md," \
        "host step 7)"
  fi
  owned_safe "$parent"
  for f in "$parent"/* "$parent"/.*; do
    [[ ${f##*/} == @(.|..|"$base"|".$base.refresh."*) ]] || [[ ! -e $f && ! -L $f ]] ||
      die "refresh: $parent holds ${f##*/}, not only the template: the template must have a folder of its own (the" \
        "only one the refresh service may write). Move it once: .github/fork-fleet.md, host step 7"
  done
  # Two refreshes: one after the other, by a lock on the template's folder itself (opened read-only: refresh writes
  # nothing outside that folder; the sandboxed service can write nothing else).
  exec {rlock}<"$parent"
  flock -w 1800 "$rlock" || die "refresh: another refresh still holds $parent after 30 minutes"
  # RUN_DIR: only read here (the service cannot write it). Made when missing only by a refresh by hand (as setup does).
  if [[ ! -e $RUN_DIR && ! -L $RUN_DIR ]]; then
    mkdir -m 0700 -- "$RUN_DIR" 2>/dev/null ||
      die "refresh: there is no $RUN_DIR yet and this refresh may not make it: the dispatcher's next prune or start" \
        "makes it; the template stays as it is"
  fi
  owned_safe "$RUN_DIR"
  launch_lock_open 'the template stays as it is' # before any download: a refresh that could not swap fetches nothing
  for tmp in "$parent/.$base.refresh."*; do
    if [[ -e $tmp || -L $tmp ]]; then rm -rf --one-file-system -- "$tmp"; fi
  done
  if [[ -e $TEMPLATE || -L $TEMPLATE ]]; then
    owned_safe "$TEMPLATE"
    [[ -d $TEMPLATE ]] || die "$TEMPLATE is not a directory"
    if [[ -f $TEMPLATE/.fork-fleet-version && ! -L $TEMPLATE/.fork-fleet-version ]]; then
      have=$(<"$TEMPLATE/.fork-fleet-version")
      [[ $have =~ $VERSION ]] || have=''
    fi
    if [[ -n $have && -f $TEMPLATE/.fork-fleet-floor && ! -L $TEMPLATE/.fork-fleet-floor ]]; then
      floor=$(<"$TEMPLATE/.fork-fleet-floor")
      [[ $floor =~ $VERSION ]] && ! older "$have" "$floor" || floor=''
    fi
  fi
  meta=$(curl -q -fsSL --proto =https --proto-redir =https --max-time 120 --max-filesize 10485760 \
    -H 'Accept: application/vnd.github+json' -- "$RUNNER_RELEASE") ||
    die "refresh: could not read the newest release ($RUNNER_RELEASE): the template stays as it is"
  out=$(python3 -I -c "$RELEASE_PY" "$platform" <<<"$meta") || die "refresh: the template stays as it is"
  read -r newest sum <<<"$out"
  [[ $newest =~ $VERSION && $sum =~ ^[0-9a-f]{64}$ ]] || die "refresh: an unexpected release record: $out"
  if [[ $have == "$newest" ]]; then
    echo "refresh: the template is actions/runner $have, the newest release is $newest: nothing to do"
    return 0
  elif [[ -n $RUNNER_MIN ]] && older "$newest" "$RUNNER_MIN"; then
    die "refresh: the newest release $newest is older than RUNNER_MIN $RUNNER_MIN: not taken, the template stays as" \
      "it is"
  elif [[ -n $have ]] && older "$newest" "$have"; then
    lowest=${floor:-$((10#${have%%.*} > 0 ? 10#${have%%.*} - 1 : 0)).0.0} # no floor file: one major version down
    if older "$newest" "$lowest"; then
      die "refresh: the template is actions/runner $have, the newest release $newest is older than $lowest, the" \
        "oldest it may go back to by itself: not taken, the template stays as it is (to take it: .github/fork-fleet.md," \
        "host step 7)"
    fi
    echo "refresh: the template is actions/runner $have, newer than the newest release $newest (a release GitHub" \
      "withdrew?): rolling it back to $newest" >&2
    floor=$newest # no second step back below it
  elif [[ -n $have ]] && ((10#${newest%%.*} > 10#${have%%.*} + 1)); then
    die "refresh: the newest release $newest is more than one major version ahead of the template's $have: not" \
      "taken by itself, the template stays as it is (to take it: .github/fork-fleet.md, host step 7)"
  fi

  [[ -n $have ]] && ! older "$have" "$newest" || floor=$have # forward: the floor is the version replaced (if any)
  tmp=$(mktemp -d -- "$parent/.$base.refresh.XXXXXX")
  # Expanded now (the trap runs after this function's locals are gone). TERM and the like end the script through it.
  # shellcheck disable=SC2064
  trap "rm -rf --one-file-system -- $(printf '%q' "$tmp")" EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  url=$RUNNER_DOWNLOAD/v$newest/actions-runner-$platform-$newest.tar.gz
  curl -q -fsSL --proto =https --proto-redir =https --max-time 1800 --max-filesize 1073741824 \
    -o "$tmp/runner.tar.gz" -- "$url" ||
    die "refresh: the download of $url failed: the template stays as it is"
  printf '%s  %s\n' "$sum" "$tmp/runner.tar.gz" | sha256sum --check --strict --status - ||
    die "refresh: $url does not match the release's sha256 $sum: refused, the template stays as it is"
  mkdir -m 0755 -- "$tmp/new"
  python3 -I -c "$UNPACK_PY" "$tmp/runner.tar.gz" "$tmp/new" "$newest" "$floor" ||
    die "refresh: $url did not unpack: the template stays as it is"
  rm -f -- "$tmp/runner.tar.gz"
  for f in run.sh bin/Runner.Listener; do
    [[ -f $tmp/new/$f && ! -L $tmp/new/$f && -x $tmp/new/$f ]] ||
      die "refresh: $url has no run.sh or bin/Runner.Listener: the template stays as it is"
  done
  for f in .env .path .runner .credentials .credentials_rsaparams; do
    [[ ! -e $tmp/new/$f && ! -L $tmp/new/$f ]] || die "refresh: $url has a $f: the template stays as it is"
  done
  bad=$(unsafe_file "$tmp/new")
  [[ -z $bad ]] ||
    die "refresh: $url has ${bad#"$tmp/new/"}, a file a template must not have: the template stays as it is"
  sync -f -- "$tmp/new" # the files are on disk before their name is: a power cut leaves the old or the new one

  flock -w 100 "$lock" || die "refresh: another launcher holds $RUN_DIR/launch.lock: the template stays as it is"
  if [[ -e $TEMPLATE || -L $TEMPLATE ]]; then
    owned_safe "$TEMPLATE"
    python3 -I -c "$EXCHANGE_PY" "$tmp/new" "$TEMPLATE" ||
      die "refresh: could not swap $TEMPLATE for the new one (a filesystem without RENAME_EXCHANGE?): the template" \
        "stays as it is"
  else
    mv -T -- "$tmp/new" "$TEMPLATE"
  fi
  flock -u "$lock"
  sync -f -- "$parent" # after the unlock: a whole-filesystem flush need not hold up start
  echo "refresh: the template is actions/runner $newest (was ${have:-none})"
}

# The runner users: each a user of its own, in no group but its own, not lingering, with a network of its own.
check_users() {
  local user uid gid groups
  local -A user_of=() nets=()
  [[ $RUNNER_USERS =~ ^[a-z_][a-z0-9_-]{0,31}(,[a-z_][a-z0-9_-]{0,31})*$ ]] || die "$conf: RUNNER_USERS is not a list of user names"
  for user in ${RUNNER_USERS//,/ }; do
    uid=$(id -u -- "$user" 2>/dev/null) || die "no user $user"
    gid=$(id -g -- "$user")
    groups=$(id -G -- "$user")
    [[ $uid != 0 && $uid != "$EUID" ]] || die "each runner user must be a user of its own, not $user"
    [[ -z ${user_of[$uid]+set} ]] || die "$conf: $user and ${user_of[$uid]} are one user: each slot needs a user of its own"
    user_of[$uid]=$user
    [[ $groups == "$gid" ]] || die "$user is in other groups ($groups): a runner user is in its own group only"
    [[ ! -e $LINGER_DIR/$user ]] || die "$user lingers (loginctl disable-linger $user)"
    [[ -z ${nets[$((uid % 16384))]+set} ]] ||
      die "$conf: $user and ${nets[$((uid % 16384))]} would share one network (a /30 from uid % 16384): give one another uid"
    nets[$((uid % 16384))]=$user
  done
}

# The runner user a new unit runs as: one no loaded fork-fleet-* unit runs as.
free_user() {
  local name names user busy=' '
  names=$(active)
  for name in $names; do
    user=$("$SYSTEMCTL" show --property=User --value -- "fork-fleet-$name.service") || die "no user of unit fork-fleet-$name"
    [[ -n $user ]] || die "unit fork-fleet-$name runs as no runner user"
    busy+="$user "
  done
  for user in ${RUNNER_USERS//,/ }; do
    if [[ $busy != *" $user "* ]]; then
      printf '%s\n' "$user"
      return 0
    fi
  done
  die "every runner user ($RUNNER_USERS) has a unit: no slot is free"
}

# Leaves nothing of a runner user's last job: its processes (kill(-1) as that user reaches every one of them,
# children started meanwhile included) and the copies it ran in (with their HOME and TMPDIR). Its /tmp, /var/tmp,
# /dev/shm and /run were the unit's own and went with it: nothing here deletes inside a directory a runner user can
# write (a path through one could be swapped for a symlink between a check and the delete).
reset_user() {
  local user=$1 uid gid i rc
  uid=$(id -u -- "$user")
  gid=$(id -g -- "$user")
  for ((i = 0; ; i++)); do
    rc=0
    pgrep -u "$uid" >/dev/null || rc=$?
    ((rc == 0 || rc == 1)) || die "pgrep failed ($rc)"
    ((rc == 1)) && break
    ((i < 20)) || die "processes of $user are still running"
    # bash's own kill: kill(1) of util-linux and of procps read these arguments differently.
    setpriv --reuid="$uid" --regid="$gid" --clear-groups -- /bin/bash --norc --noprofile -c 'kill -s KILL -- -1' \
      2>/dev/null || true
    sleep 0.1
  done
  prune_now
  [[ -z $(find "$WORK_ROOT" -mindepth 1 -maxdepth 1 -user "$uid" -print -quit) ]] ||
    die "a copy of $user is left in $WORK_ROOT"
}

fits() {
  local kb
  [[ $1 =~ ^[0-9]{1,7}$ ]] || die "fits: not a number of megabytes: $1"
  kb=$(awk '$1 == "MemAvailable:" { print $2 }' "$MEMINFO")
  [[ $kb =~ ^[0-9]+$ ]] || die "no MemAvailable in $MEMINFO"
  ((kb / 1024 - RESERVE_MB >= $1)) || return 1
  [[ -n $FREE_COMMAND ]] || return 0
  owned_safe "$FREE_CHECK"
  # Its one line (free, busy: why, not available: why) for the dispatcher's log; any exit but 0 is not free.
  /usr/bin/python3 -I "$FREE_CHECK" ask --need-mb "$1" "$FREE_COMMAND" >&2
}

# The ranges no job reaches: the host itself is refused on its own (the input chain), these on the way out: RFC 1918,
# CGNAT (Tailscale and the like), link-local, loopback, the IETF and benchmark ranges, multicast and the reserved rest.
PRIVATE4=(0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16
  198.18.0.0/15 224.0.0.0/4 240.0.0.0/4)

# This host's own public IPv4 addresses, as OWN_ADDRESS_URLS see them (curl over IPv4, https only, no proxy): a job
# must not reach them, as the router's admin page and every port forwarded back into this home or host answer there.
# Every public address a lookup gives is blocked (two WAN links: each lookup may see another one); a lookup that
# fails or answers no public IPv4 address is said and left out. None at all: no runner.
own_address() {
  local u got own='' cidr n m bad
  local octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
  for u in ${OWN_ADDRESS_URLS//,/ }; do
    if ! got=$(curl -4 -fsS --noproxy '*' --max-time 10 --proto =https -- "$u" 2>&1); then
      warn "own address: $u gave no answer (${got:0:200})"
      continue
    fi
    got=${got//[[:space:]]/}
    if [[ ! $got =~ ^$octet(\.$octet){3}$ ]]; then
      warn "own address: $u answered no IPv4 address (${got:0:60})"
      continue
    fi
    n=$(ip4_int "$got") bad=''
    for cidr in "${PRIVATE4[@]}"; do
      m=$(((0xffffffff << (32 - ${cidr#*/})) & 0xffffffff))
      (((n & m) != $(ip4_int "${cidr%/*}"))) || bad=1
    done
    if [[ -n $bad ]]; then
      warn "own address: $u answered $got, not a public address"
      continue
    fi
    [[ " $own " == *" $got "* ]] || own+="${own:+ }$got"
  done
  [[ -n $own ]] || die "own address: no lookup (OWN_ADDRESS_URLS) gave this host's public IPv4 address: no runner," \
    "as the job's walls could not block it"
  printf '%s\n' "$own"
}

# What the table and the units block (sets blocked): the addresses own_address found now ($1), then those blocked
# before (RUN_DIR/own-address), newest first, at most 4 or one per lookup URL if there are more: a lookup that is down
# this time (each WAN link seen by another one) or an address that just changed opens nothing that was blocked, and
# every address found now is always in (more URLs than 4 never push one out). The edge left: a lookup that lies with
# a new public address on every pass while the honest ones are down pushes the real one out after that many passes;
# the honest lookup's next answer blocks it again. Under the launch lock.
merge_own() {
  local a old='' urls cap=4
  local -a all
  read -ra urls <<<"${OWN_ADDRESS_URLS//,/ }"
  ((${#urls[@]} <= cap)) || cap=${#urls[@]}
  blocked=$1
  [[ ! -f $RUN_DIR/own-address ]] || old=$(<"$RUN_DIR/own-address")
  for a in $old; do
    if [[ $a =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ && " $blocked " != *" $a "* ]]; then blocked+=" $a"; fi
  done
  read -ra all <<<"$blocked"
  blocked=${all[*]:0:cap}
}

ip4_int() {
  local -a o
  IFS=. read -ra o <<<"$1"
  printf '%s\n' $(((10#${o[0]} << 24) | (10#${o[1]} << 16) | (10#${o[2]} << 8) | 10#${o[3]}))
}

must() { "$@" || die "network: $* failed: no runner starts without its network (.github/fork-fleet.md, host step 1)"; }

# (Re)applies the table, blocking $blocked (merge_own), and keeps that list in RUN_DIR/own-address. Interrupts no
# connection (the table is replaced in one transaction; conntrack keeps the flows). Under the launch lock.
table_up() {
  local private=${PRIVATE4[*]} a own=''
  for a in $blocked; do own+="    iifname \"ffh-*\" ip daddr $a counter reject with icmpx admin-prohibited"$'\n'; done
  must nft -f - <<EOF
table inet fork_fleet
delete table inet fork_fleet
table inet fork_fleet {
  set private4 {
    type ipv4_addr; flags interval
    elements = { ${private// /, } }
  }
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "ffh-*" counter reject with icmpx admin-prohibited
  }
  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "ffh-*" ip daddr @private4 counter reject with icmpx admin-prohibited
${own}    iifname "ffh-*" meta nfproto ipv6 counter drop
    oifname "ffh-*" ct state != { established, related } counter drop
  }
  chain postrouting {
    type nat hook postrouting priority 100; policy accept;
    ip saddr $NET_BASE.0.0/16 oifname != "ffh-*" masquerade
  }
}
EOF
  printf '%s\n' "$blocked" >"$RUN_DIR/own-address.new"
  mv -f -- "$RUN_DIR/own-address.new" "$RUN_DIR/own-address"
}

# Makes what is missing of runner user $1's network and (re)applies the table; changes nothing that is right already.
# Dies on any failure, so start never runs a unit without it. Under the launch lock: two launchers never apply it at
# once.
net_up() {
  local user=$1 uid ns host peer off net gw addr
  uid=$(id -u -- "$user")
  ns=fork-fleet-$user host=ffh-$uid peer=ffp-$uid
  # Its /30 in NET_BASE.0.0/16: the host side .1, the namespace .2 (off is a multiple of 4, so both stay in it).
  off=$((uid % 16384 * 4))
  net=$NET_BASE.$((off / 256))
  gw=$net.$((off % 256 + 1)) addr=$net.$((off % 256 + 2))
  [[ $(<"$IP_FORWARD") == 1 ]] ||
    die "network: $IP_FORWARD is not 1: the runners would reach nothing (.github/fork-fleet.md, host step 1)"
  table_up
  docker_rules
  ip -n "$ns" link show lo >/dev/null 2>&1 || must ip netns add "$ns"
  ip link show "$host" >/dev/null 2>&1 || must ip link add "$host" type veth peer name "$peer" netns "$ns"
  must ip addr replace "$gw/30" dev "$host"
  must ip link set "$host" up
  must ip -n "$ns" link set lo up
  must ip -n "$ns" addr replace "$addr/30" dev "$peer"
  must ip -n "$ns" link set "$peer" up
  must ip -n "$ns" route replace default via "$gw"
}

# Docker's FORWARD policy is DROP: on a host with Docker, what the table lets through needs these two rules too. Only
# accepts, so without them a job reaches less, never more; a host without the chain needs none.
# shellcheck disable=SC2086 # $r is split into iptables' arguments on purpose
docker_rules() {
  local r rc said
  command -v iptables >/dev/null 2>&1 && iptables -w 60 -S DOCKER-USER >/dev/null 2>&1 || return 0
  for r in "-o ffh-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" "-i ffh-+ -j ACCEPT"; do
    rc=0
    said=$(iptables -w 60 -C DOCKER-USER $r 2>&1) || rc=$?
    case $rc in # 1 is the "no" of -C. Anything else, like a limit (4), is no answer at all
      0) ;;
      1) must iptables -w 60 -I DOCKER-USER 1 $r ;;
      *) die "network: iptables -C DOCKER-USER gave no answer (exit $rc): $said" ;;
    esac
  done
}

# refresh-net (each dispatcher pass): the address may change while a job runs (a new lease, a VPN that went down).
# The table covers every runner's link, so re-applying it covers running jobs; their units get the new addresses too.
# The same addresses in another order change nothing. A unit whose set-property failed is tried again next pass.
# A pass within OWN_ADDRESS_SECONDS of the last answer (RUN_DIR/own-lookup: when it was asked, written once a pass
# is done; not when the pass ended, which may wait up to 100 s for the launch lock) changes nothing and asks no lookup
# service (each may throttle a host that asks every 20 s), unless the table is gone or a unit waits for its retry.
refresh_net() {
  local own name blocked old='' retry=$RUN_DIR/deny-retry asked=${EPOCHSECONDS-}
  if lookup_fresh && [[ ! -e $retry ]] && nft list table inet fork_fleet >/dev/null 2>&1; then return 0; fi
  own=$(own_address)
  setup
  [[ ! -f $RUN_DIR/own-address ]] || old=$(<"$RUN_DIR/own-address")
  merge_own "$own"
  if [[ $(addr_set "$blocked") == "$(addr_set "$old")" && ! -e $retry ]] &&
    nft list table inet fork_fleet >/dev/null 2>&1; then
    lookup_done "$asked"
    return 0
  fi
  local failed=''
  : >"$retry"
  table_up
  for name in $(active); do
    "$SYSTEMCTL" set-property --runtime -- "fork-fleet-$name.service" "IPAddressDeny=$blocked" || failed+=" $name"
  done
  if [[ -n $failed ]]; then
    warn "refresh-net: IPAddressDeny not changed for$failed (the table blocks $blocked); tried again next pass"
  else rm -f -- "$retry"; fi
  lookup_done "$asked"
}

# Whether refresh-net's last answer is younger than OWN_ADDRESS_SECONDS: RUN_DIR/own-lookup (root's, as RUN_DIR is)
# holds when it came. None, a garbled one or one from the future (the clock set back): not fresh.
lookup_fresh() {
  local last now=${EPOCHSECONDS-} f=$RUN_DIR/own-lookup
  [[ $now =~ ^[1-9][0-9]*$ && -O $RUN_DIR && -f $f && -O $f && ! -L $f ]] || return 1
  last=$(<"$f")
  [[ $last =~ ^[1-9][0-9]{0,11}$ ]] && ((last <= now && now - last < 10#$OWN_ADDRESS_SECONDS))
}

# $1: when the lookup was asked (empty without EPOCHSECONDS: never fresh, so every pass looks up).
lookup_done() {
  printf '%s\n' "$1" >"$RUN_DIR/own-lookup.new"
  mv -f -- "$RUN_DIR/own-lookup.new" "$RUN_DIR/own-lookup"
}

addr_set() { tr ' ' '\n' <<<"$1" | sort -u | tr '\n' ' '; }

# The jobs' resolv.conf: this host's resolver listens on its loopback, which a job's namespace does not have.
resolv_target() {
  local target
  owned_safe "$RESOLV_CONF"
  [[ -f $RESOLV_CONF && $RESOLV_CONF =~ ^/[A-Za-z0-9._/-]+$ ]] || die "$RESOLV_CONF: not a plain file path"
  grep -q '^nameserver ' -- "$RESOLV_CONF" || die "$RESOLV_CONF names no nameserver (.github/fork-fleet.md, host step 1)"
  target=$(readlink -f -- /etc/resolv.conf)
  [[ $target =~ ^/[A-Za-z0-9._/-]+$ ]] || die "/etc/resolv.conf leads to $target: not a plain file path"
  printf '%s\n' "$target"
}

# THE rule of a folder on the way to a bound path (a link's, and each one above the real path, `/` too): it is root's
# (or this user's, who runs the launcher), and only its owner may write it, or it is sticky (then only an entry's owner
# may rename or remove it). Its owner, or its group or all when they may write it, could rename a link or a plain
# folder in it away and put their own link in its place, between the check and the bind, so the bind comes from
# wherever they like. In a sticky one that others may write (/var/tmp), any of them may have made the entry the path
# goes through ($3, when it is there): as its owner they may still rename it away, so it must be root's or this
# user's, as a link must. Dies otherwise; $2: how the path goes through it.
closed_folder() {
  local mode uid
  mode=$(stat -c '0%a' -- "$ROOT${1:-/}") || die "$2: cannot read the folder ${1:-/}"
  uid=$(stat -c %u -- "$ROOT${1:-/}") || die "$2: cannot read the folder ${1:-/}"
  [[ $uid == 0 || $uid == "$EUID" ]] ||
    die "$2, a folder of uid $uid: its owner could swap a link into it (only root's folders, or this user's:" \
      "chown root: ${1:-/}, or move the path out of it)"
  ((mode & 01000 || !(mode & 022))) ||
    die "$2, which others may write (its group or all): they could swap a link into it" \
      "(chmod go-w ${1:-/}, or make it sticky: chmod +t ${1:-/})"
  if ((mode & 022)) && [[ -n ${3:-} && -e $ROOT$3 ]]; then
    uid=$(stat -c %u -- "$ROOT$3") || die "$2: cannot read $3"
    [[ $uid == 0 || $uid == "$EUID" ]] ||
      die "$2: $3 is uid $uid's, in ${1:-/}, which is sticky but others may write: that user could rename it away" \
        "and put a link in its place (only root's, or this user's; or move it out of ${1:-/})"
  fi
}

# Every symlink on the way from $1 to its real path (on the fake machine: in it) is root's (or this user's, who runs
# the launcher: root, but for the walls' tests), in a folder no other user may swap it in: one a user can repoint
# leads a bind between the check and the job wherever that user likes, past every check. Dies otherwise. That
# folder is closed too (closed_folder: root's, as its owner may remove any link in it, sticky or not).
links_owned() {
  local rest=${1#/} cur='' part target uid hops=0
  while [[ -n $rest ]]; do
    part=${rest%%/*}
    if [[ $rest == */* ]]; then rest=${rest#*/}; else rest=''; fi
    case $part in
      '' | .) continue ;;
      ..) cur=${cur%/*}; continue ;;
    esac
    if [[ -L $ROOT$cur/$part ]]; then
      ((++hops <= 40)) || die "$1: too many links on the way (a loop?)"
      uid=$(stat -c %u -- "$ROOT$cur/$part") || die "$1: cannot read the link $cur/$part"
      [[ $uid == 0 || $uid == "$EUID" ]] ||
        die "$1 goes through $cur/$part, a link of uid $uid: only root's links, or that user could repoint it"
      closed_folder "$cur" "$1 goes through $cur/$part, in ${cur:-/}"
      target=$(readlink -- "$ROOT$cur/$part") || die "$1: cannot read the link $cur/$part"
      [[ $target != /* ]] || cur=''
      rest=${target#/}${rest:+/$rest}
    else
      cur+=/$part
    fi
  done
}

# A path's real path: every symlink in every part of it resolved (on the fake machine: in it), as systemd follows
# them in a bind's source. What job_view checks and binds, so a link in a folder above a path (/opt/x -> /c, /home,
# a share) never leads a job past the checks. Dies when it is no plain path, or when a folder above it is not closed
# (closed_folder, the same rule as a link's folder): then the bind could come from another place than the one checked.
real_path() {
  local real d below
  links_owned "$1"
  real=$(realpath -m -- "$ROOT$1") || die "$1: cannot resolve it"
  if [[ -n $ROOT ]]; then
    [[ $real == "$ROOT"/* ]] || die "$1 leads out of FORK_FLEET_WALLS_ROOT"
    real=${real#"$ROOT"}
  fi
  [[ $real =~ ^$PLAIN_PATH$ ]] || die "$1 leads to ${real:-/}: not a plain path"
  d=$real
  while below=$d && d=${d%/*} && [[ -n $d ]]; do
    [[ ! -e $ROOT$d ]] || closed_folder "$d" "$1 leads to $real, under $d" "$below"
  done
  closed_folder '' "$1 leads to $real, under /" "$below"
  printf '%s\n' "$real"
}

# A path no job ever sees of this host's: its own of these or none (/proc, /sys, /dev, /run, /tmp, /var/tmp), all of
# /etc, or one that holds data (no_data). The rule of the list's and ALLOW_PATHS' lines; $3 says how to take the line
# out where it came from (job_view: re-run the installer for its own list, else edit JOB_PATHS' file or launch.conf).
never() {
  case $1/ in /proc/* | /sys/* | /dev/* | /run/* | /tmp/* | /var/tmp/*)
    die "$2: a job never sees it from this host (its own, none, or data): not a path to allow" ;;
  esac
  # All of /etc (issue #243): only the entries a build needs (the list's /etc lines), never the logins, units, cron,
  # /etc/default and backup tools' folders beside them. An older kit's list still names it: refused, no runner starts.
  [[ $1 != /etc ]] || die "$2: all of /etc is never in a job's view (an older kit's list?): $3"
  # The host's passwd, group and hosts (issue #370): job_fs writes a job's own; bound over them, the host's would be
  # seen. An older kit's list still names them: refused, no runner starts.
  case $1 in /etc/passwd | /etc/group | /etc/hosts)
    die "$2: a job has the launcher's own, never this host's (an older kit's list?): $3" ;;
  esac
  # The other usual files that name this host's accounts or LAN hosts, and each folder that holds one (issues #613,
  # #636; the walls' SECRET_ETC and SECRET_ELSEWHERE name them too, for an older launcher, and a test keeps the walls'
  # LAUNCHER_REFUSES and these patterns one, both ways): passwd's, group's and hosts' backup copies (passwd-, passwd~,
  # passwd.bak, passwd.OLD), subuid and subgid and theirs, tcp wrappers' rules and hosts.equiv, the network and ethers
  # name files, the logs and login records (/var/log: wtmp, btmp, lastlog, auth.log), Debian's /var/backups
  # (passwd.bak), libnss-extrausers' folder, the mail spools (a file per user name), SSSD's cache (passwd and group,
  # readable by all) and the DHCP leases (the LAN's host names and MACs: dnsmasq's, ISC dhclient's on Debian and RHEL,
  # dhcpcd's and NetworkManager's; issue #656).
  case $1 in
    /etc/passwd* | /etc/group* | /etc/hosts* | /etc/subuid* | /etc/subgid* | /etc/networks | /etc/ethers | /var | \
      /var/log | /var/log/* | /var/backups | /var/backups/* | /var/lib | /var/lib/extrausers | /var/lib/extrausers/* | \
      /var/mail | /var/mail/* | /var/spool | /var/spool/mail | /var/spool/mail/* | /var/lib/sss | /var/lib/sss/* | \
      /var/lib/misc | /var/lib/misc/dnsmasq.leases | /var/lib/dhcp | /var/lib/dhcp/* | /var/lib/dhclient | \
      /var/lib/dhclient/* | /var/lib/dhcpcd | /var/lib/dhcpcd/* | /var/lib/NetworkManager | /var/lib/NetworkManager/*)
      die "$2: names this host's accounts or LAN hosts, or is a folder of such a file; a job never sees it: $3" ;;
  esac
  no_data "$1" "$2"
}

# A path that holds data (/home, /root, /mnt, /media, /srv): no job sees one, by any name. The rule of every other
# path a job's unit gets (bound_real); the own-or-none roots are never's alone, as these may be there (the jobs'
# resolv.conf may be a file in /run, like systemd-resolved's, and the walls proof keeps its files in /var/tmp).
no_data() {
  case $1/ in /home/* | /root/* | /mnt/* | /media/* | /srv/*)
    die "$2: a job never sees it from this host (it holds data): not a path to allow" ;;
  esac
}

# The real path of a file or folder a job's unit gets besides the list's (the hook and the two files beside it,
# WORK_ROOT, the jobs' resolv.conf), in real: refused when it holds data (no_data), as written or once resolved.
bound_real() {
  no_data "$1" "$1"
  real=$(real_path "$1")
  no_data "$real" "$1 (that is $real)"
}

# THE rule of what a job sees of this host's files, in one place (the per-OS walls read it through `paths`): an allow
# list, never a list of what to hide, as a disk or share may be mounted anywhere (/mnt, /media, /srv, /data, /net).
# The unit's root is an empty directory of its own holding only: each path of JOB_PATHS and ALLOW_PATHS that exists
# here, read-only (a symlink here, like /bin -> usr/bin, is the same symlink there); the hook and the two files it
# runs, read-only; the job's copy in WORK_ROOT, read-write; the jobs' resolv.conf; and systemd's own /proc, /sys, a
# /dev of pseudo devices and memory file systems for /run, /tmp, /var/tmp and /dev/shm. Each is bound from its real
# path (real_path: never through a link in a folder above it), and the list's are refused when that real path, or
# the one written, is one no job sees (never). Each path is one mount, not its submounts (norbind: WSL2's driver
# share under /usr/lib/wsl, a disk under /etc stay out), and no mount the host makes later reaches the unit
# (MountFlags=private). Sets view_ro (SOURCE:DEST) and view_links (DEST and its link's text).
job_view() {
  local line p real fix i n hook_dir=${HOOK%/*}
  local -a paths=() plain=()
  [[ $HOOK =~ ^$PLAIN_PATH$ && $WORK_ROOT =~ ^$PLAIN_PATH$ ]] || die "HOOK ($HOOK) and WORK_ROOT must be plain paths"
  owned_safe "$JOB_PATHS"
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    paths+=("$line")
  done <"$JOB_PATHS"
  n=${#paths[@]}
  # shellcheck disable=SC2206 # plain paths only, checked below
  paths+=(${ALLOW_PATHS//,/ })
  view_ro=() view_links=()
  i=0
  for p in "${paths[@]}"; do
    i=$((i + 1))
    # Where the line came from, so a refusal names the way out (issue #594): the installer rewrites only its own list.
    if ((i > n)); then
      fix="take it out of ALLOW_PATHS in $conf"
    elif ((own_job_paths)); then
      fix='re-run the installer'
    else
      fix="take it out of $JOB_PATHS (JOB_PATHS in $conf)"
    fi
    [[ $p =~ ^$PLAIN_PATH$ && /$p/ != */./* && /$p/ != */../* ]] || die "$JOB_PATHS or ALLOW_PATHS: not a plain path: $p"
    never "$p" "$p" "$fix"
    if [[ -L $ROOT$p ]]; then
      view_links+=("$p" "$(readlink -- "$ROOT$p")")
    elif [[ -e $ROOT$p ]]; then
      real=$(real_path "$p")
      never "$real" "$p (that is $real)" "$fix"
      view_ro+=("$real:$p")
      [[ $real != "$p" ]] || plain+=("$p")
    fi
  done
  # A list path that a link above it leads elsewhere must land in one the list has as it is (/lib/x -> /usr/lib/x
  # is in /usr): /opt/win/Users with /opt/win -> /c leads to /c/Users, which no line allows.
  for line in "${view_ro[@]}"; do
    real=${line%%:*}
    [[ $real != "${line#*:}" ]] || continue
    for p in "${plain[@]}"; do
      [[ $real != "$p" && $real != "$p"/* ]] || continue 2
    done
    die "${line#*:} leads to $real, which no line of $JOB_PATHS or ALLOW_PATHS allows as it is"
  done
  for p in "$HOOK" "$hook_dir/fork-fleet.py" "$hook_dir/fork-fleet.json"; do
    [[ $p == "$HOOK" || -e $ROOT$p ]] || continue
    bound_real "$p"
    view_ro+=("$real:$p")
  done
}

# A job's own /etc/passwd, group and hosts in its root $1 (issue #370), for its user $2 with HOME $3: root, nobody and
# that user, their groups, and localhost and this host's name on loopback. Never the host's: they name every account,
# its GECOS (often a person's name) and home folder, and the LAN's host names. New files (noclobber) in a folder job_fs
# just made, root's: nothing to lead a write astray. A job cannot change them (ProtectSystem=strict, and root's).
job_etc() {
  local root=$1 user=$2 home=${3:-/nonexistent} uid gid group name passwd groups hosts f
  uid=$(id -u -- "$user") gid=$(id -g -- "$user") group=$(id -gn -- "$user")
  [[ $uid =~ ^[0-9]+$ && $gid =~ ^[0-9]+$ && $group =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "$user: no plain uid, gid or group name"
  name=$(uname -n)
  passwd='root:x:0:0:root:/root:/usr/sbin/nologin'$'\n'
  ((uid == 65534)) || passwd+='nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin'$'\n'
  passwd+="$user:x:$uid:$gid::$home:/bin/sh"$'\n'
  groups='root:x:0:'$'\n'
  ((gid == 65534)) || groups+='nogroup:x:65534:'$'\n'
  groups+="$group:x:$gid:"$'\n'
  hosts='127.0.0.1 localhost'$'\n''::1 localhost ip6-localhost ip6-loopback'$'\n'
  # Its own name, as uname shows it to the job anyway (Java's getLocalHost and the like look it up); one with a _ too
  # (issue #594), though no DNS name has one. A name with any other character (a blank, a #) gets no line.
  [[ ! $name =~ ^[A-Za-z0-9_]([A-Za-z0-9._-]{0,252})$ || $name == localhost ]] || hosts+="127.0.1.1 $name"$'\n'
  (umask 022 && mkdir -p -- "$root/etc")
  # The folder was just made, so nothing may be there yet (issues #636, #656): noclobber opens what is not a regular
  # file without O_EXCL, and a FIFO, a device (a link to a tty waits for carrier) or a link to one would not refuse
  # the write but hold it, and the start would hang till the agent's own limit. Any entry at all is refused before
  # the write. Only root could plant one (the folder is root's); what is planted after this check is the write's and
  # the plain-file check's below.
  for f in passwd group hosts; do
    [[ ! -e $root/etc/$f && ! -L $root/etc/$f ]] ||
      die "$root/etc/$f is there already (a file, link, FIFO or device), in a folder just made: refused before" \
        "the write"
  done
  # One write a file, each checked: in a subshell left of ||, set -e is off, so a failed write would not stop the
  # next (issue #594); the first that fails (a file already there, a full disk) stops them all and refuses the start.
  (
    set -o noclobber
    umask 022
    printf '%s' "$passwd" >"$root/etc/passwd" && printf '%s' "$groups" >"$root/etc/group" &&
      printf '%s' "$hosts" >"$root/etc/hosts"
  ) || die "could not write $root/etc's passwd, group and hosts as new files: refused"
  # Each a plain file of its own now (issue #613 item 6): noclobber refuses only a regular file already there, so a link
  # to /dev/null planted there would take the write and leave the job no passwd at all. Only root could plant one
  # (the folder is root's, just made); refused all the same.
  for f in passwd group hosts; do
    [[ -f $root/etc/$f && ! -L $root/etc/$f && -s $root/etc/$f ]] || die "$root/etc/$f is not the plain file just written: refused"
  done
}

# job_view for a unit: its root ($1, root's), its copy ($2, read-write; already a real path, in WORK_ROOT's), the
# jobs' resolv.conf at $3 and its user $4. Sets props_fs and resolv_bind; seen, the mount points check-net's probe
# allows (its own /proc, /sys, /dev and memory file systems are allowed there); from, each of them with the device
# and inode its source has here, which the probe compares with what the unit has there (a link swapped in after
# job_view shows); and linked, the entries its root has of job_fs's own (the list's links, /etc/resolv.conf when $3 is
# elsewhere, and its own passwd, group and hosts: job_etc; the probe allows them).
job_fs() {
  local root=$1 rw=$2 resolv=$3 user=$4 i b real
  local -a ro=() view_ro view_links
  job_view
  rm -rf --one-file-system -- "$root"
  mkdir -m 0755 -- "$root"
  job_etc "$root" "$user" "${rw:+$rw/home}"
  linked=' /etc/passwd /etc/group /etc/hosts'
  for ((i = 0; i < ${#view_links[@]}; i += 2)); do
    # Each folder on the way open to the job (#667): `mkdir -p -m` sets the last one only, and umask is 077 here.
    (umask 022 && mkdir -p -- "$root${view_links[i]%/*}")
    ln -s -- "${view_links[i + 1]}" "$root${view_links[i]}"
    linked+=" ${view_links[i]}"
  done
  # A host whose /etc/resolv.conf is a link (systemd-resolved's, into /run) has the jobs' file bound at its target:
  # the job's /etc/resolv.conf is then that same link, as no whole /etc brings it any more (issue #243).
  if [[ $resolv != /etc/resolv.conf && ! -e $root/etc/resolv.conf && ! -L $root/etc/resolv.conf ]]; then
    (umask 022 && mkdir -p -- "$root/etc")
    ln -s -- "$resolv" "$root/etc/resolv.conf"
    linked+=' /etc/resolv.conf'
  fi
  linked=${linked# }
  seen='/' from=''
  for b in "${view_ro[@]}"; do
    ro+=("$b:norbind") seen+=" ${b#*:}" from+=" ${b#*:}=$(stat -L -c '%d:%i' -- "${b%%:*}")"
  done
  props_fs=(-p "RootDirectory=$root" -p MountAPIVFS=yes -p MountFlags=private -p "BindReadOnlyPaths=${ro[*]}")
  if [[ -n $rw ]]; then
    props_fs+=(-p "BindPaths=$rw:$rw:norbind")
    seen+=" $rw"   # made after this (start), under WORK_ROOT's real path
  fi
  bound_real "$RESOLV_CONF"
  b=$real
  resolv_bind=$b:$resolv
  seen+=" $resolv" from+=" $resolv=$(stat -L -c '%d:%i' -- "$b")"
  from=${from# }
}

# paths: job_view, for the walls, with WORK_ROOT (each job's copy is in it) and the jobs' resolv.conf. Reads only.
paths() {
  local b real out=''
  local -a view_ro view_links
  job_view
  for b in "${view_ro[@]}"; do out+="${b#*:} ${b%%:*}"$'\n'; done
  bound_real "$WORK_ROOT"
  out+="$real $real"$'\n'
  bound_real "$RESOLV_CONF"
  # All or nothing: a refused path prints no line at all, so no reader takes the lines before it for the whole list.
  printf '%s/etc/resolv.conf %s\n' "$out" "$real"
}

# The sandbox of every runner unit, and of check-net's probes (which prove it): the props array. Its own network
# namespace, the private ranges and this host's own public addresses ($3) refused again in the kernel, of this host's
# files only what job_fs allows ($4 the unit's root, $5 its copy), its own /run, /dev/shm, /tmp and /var/tmp (memory,
# gone with the unit), no /home, no other user's processes, its own IPC, the system read-only, only the pseudo devices
# (PrivateDevices: /dev/null, zero, full, random, urandom, tty and ptys; no GPU, /dev/dxg (WSL2's channel to Windows),
# disk, kvm, fuse or tun: Docker's client, Node, git and Java need none), and the walls below.
# AF_VSOCK is not namespaced: it is a VM's channel to its host (virtio-vsock, Hyper-V sockets: WSL2's way to
# Windows). RestrictAddressFamilies refuses it to socket(), but io_uring's IORING_OP_SOCKET opens sockets no seccomp
# filter sees, and on x86_64 a 64-bit process can make i386 (int 0x80) or x32 system calls, which a filter for the
# native arch does not check. So io_uring fails with ENOSYS (libuv, Node and the like then use plain calls) and a
# system call of another arch ends the process (SIGSYS).
sandbox() {
  local user=$1 resolv=$2 own=$3 resolv_bind
  local -a props_fs
  job_fs "$4" "$5" "$resolv" "$user"
  props=(
    -p "NetworkNamespacePath=/run/netns/fork-fleet-$user"
    -p "IPAddressDeny=${PRIVATE4[*]} $own ::/0" -p IPAddressAllow=localhost
    "${props_fs[@]}"
    -p "TemporaryFileSystem=/run /dev/shm:size=1G,mode=1777 /tmp:size=1G,mode=1777 /var/tmp:size=1G,mode=1777"
    -p "BindReadOnlyPaths=$resolv_bind"
    -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p ProtectProc=invisible -p PrivateIPC=yes
    -p RemoveIPC=yes -p PrivateDevices=yes
    -p RestrictAddressFamilies=~AF_VSOCK
    -p "SystemCallFilter=~io_uring_setup:ENOSYS io_uring_enter:ENOSYS io_uring_register:ENOSYS"
    -p SystemCallArchitectures=native
  )
}

# The least MemoryMax a job gets (megabytes): less, and no start (a smaller host sets fewer RUNNER_USERS).
JOB_MEMORY_MIN=1024
# Sets mem_max and mem_high (megabytes) for a job unit (the settings above): JOB_MEMORY_MAX_MB, or MemTotal minus
# RESERVE_MB shared by the RUNNER_USERS (each slot's share, so the jobs together never reach the reserve).
job_limits() {
  local kb users=${RUNNER_USERS//[^,]/}
  mem_max=$JOB_MEMORY_MAX_MB
  if [[ -z $mem_max ]]; then
    kb=$(awk '$1 == "MemTotal:" { print $2 }' "$MEMINFO")
    [[ $kb =~ ^[0-9]+$ ]] || die "no MemTotal in $MEMINFO"
    mem_max=$(((kb / 1024 - RESERVE_MB) / (${#users} + 1)))
  fi
  ((mem_max >= JOB_MEMORY_MIN)) || die "start: a job would get ${mem_max} MB (MemoryMax), under $JOB_MEMORY_MIN MB:" \
    "refused (set JOB_MEMORY_MAX_MB, a smaller RESERVE_MB or fewer RUNNER_USERS in $conf)"
  mem_high=${JOB_MEMORY_HIGH_MB:-$((mem_max * 9 / 10))}
  ((mem_high <= mem_max)) || die "$conf: JOB_MEMORY_HIGH_MB ($mem_high) is over the job's MemoryMax ($mem_max MB)"
}

start() {
  local name=$1 dir env_file line key value user resolv bad left own blocked seen from linked f real wait code
  local mem_max mem_high
  local -a props
  local -A got=()
  name_ok "$name"
  while IFS= read -r line; do
    [[ $line =~ ^(ACTIONS_RUNNER_INPUT_JITCONFIG|FORK_FLEET_RUN|FORK_FLEET_ATTEMPT)=(.*)$ ]] || die "start: an unexpected input line"
    key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    [[ -z ${got[$key]+set} ]] || die "start: $key twice"
    case $key in
      # A length check of its own: bash's regex refuses a bound above 255 (it would refuse every config).
      ACTIONS_RUNNER_INPUT_JITCONFIG) [[ $value =~ ^[A-Za-z0-9+/=]+$ ]] && ((${#value} <= 65536)) ;;
      *) [[ $value =~ ^[1-9][0-9]{0,19}$ ]] ;;
    esac || die "start: $key is not well formed"
    got[$key]=$value
  done
  ((${#got[@]} == 3)) || die "start: needs ACTIONS_RUNNER_INPUT_JITCONFIG, FORK_FLEET_RUN and FORK_FLEET_ATTEMPT"
  # The unit's own limits (memory, CPU, OOM), before anything is made: a setting that cannot be used makes nothing.
  job_limits

  # The template: an unconfigured runner nobody but this user can change (each copy hard-links its files).
  owned_safe "$TEMPLATE"
  [[ -x $TEMPLATE/run.sh ]] || die "$TEMPLATE has no run.sh"
  for f in .env .path .runner .credentials .credentials_rsaparams; do
    [[ ! -e $TEMPLATE/$f && ! -L $TEMPLATE/$f ]] || die "$TEMPLATE/$f exists: the template must be an unconfigured runner (.github/fork-fleet.md)"
  done
  bad=$(unsafe_file "$TEMPLATE")
  [[ -z $bad ]] || die "$TEMPLATE has a file that is not this user's or that others may write, a special file or a" \
    "setuid, setgid or sticky one: $bad"
  owned_safe "$HOOK"
  # The two files the hook runs: every job sees them (job_view), so no runner user may change them for the next one.
  # Its folder too (where it really is): they are checked here but bound later (job_view binds them if they exist
  # then), so no one else may make one there in between (a shared folder such as /var/tmp, sticky or not, is refused).
  real=$(real_path "$HOOK")
  owned_safe "${real%/*}"
  for f in "${HOOK%/*}/fork-fleet.py" "${HOOK%/*}/fork-fleet.json"; do [[ ! -e $f && ! -L $f ]] || owned_safe "$f"; done
  template_version >/dev/null
  check_users
  resolv=$(resolv_target)
  own=$(own_address)

  setup
  # cp -al hard-links the template's files: it cannot cross filesystems (and a full copy would be writable).
  [[ $(stat -c %d -- "$TEMPLATE") == "$(stat -c %d -- "$WORK_ROOT")" ]] ||
    die "TEMPLATE ($TEMPLATE) and WORK_ROOT ($WORK_ROOT) must be on one filesystem: cp -al hard-links the template"
  dir=$WORK_ROOT/$name
  env_file=$RUN_DIR/$name.env
  [[ ! -e $dir ]] || die "$dir exists: a runner of that name ran already (prune removes it once its unit is gone)"
  [[ ! -e $env_file && ! -L $env_file ]] ||
    die "$env_file exists: refused (prune removes a plain leftover once no unit of that name is loaded)"
  user=$(free_user)
  reset_user "$user"
  merge_own "$own"
  net_up "$user"
  sandbox "$user" "$resolv" "$blocked" "$RUN_DIR/$name.root" "$dir"
  # Expanded now: the trap runs at exit, after this function's locals are gone (set -u would fail it, file left).
  # shellcheck disable=SC2064
  trap "rm -f -- $(printf '%q' "$env_file")" EXIT
  cp -al -- "$TEMPLATE" "$dir"
  # The copy's top directory, HOME and TMPDIR are the runner user's, and no one else's: no other runner user can
  # read its JIT config, tokens or caches. Everything else in it is the template's (root's, read-only).
  mkdir -m 0700 -- "$dir/home" "$dir/tmp"
  chown -- "$user:" "$dir" "$dir/home" "$dir/tmp"
  chmod 0700 -- "$dir"
  # noclobber: a new file (O_EXCL), never one, or a link, someone put there (root would write through it).
  (
    set -o noclobber
    {
      printf 'ACTIONS_RUNNER_INPUT_JITCONFIG=%s\n' "${got[ACTIONS_RUNNER_INPUT_JITCONFIG]}"
      printf 'FORK_FLEET_RUN=%s\nFORK_FLEET_ATTEMPT=%s\n' "${got[FORK_FLEET_RUN]}" "${got[FORK_FLEET_ATTEMPT]}"
      printf 'HOME=%s\nTMPDIR=%s\n' "$dir/home" "$dir/tmp"
      printf 'ACTIONS_RUNNER_HOOK_JOB_STARTED=%s\n' "$HOOK"
    } >"$env_file"
  ) || die "start: could not make $env_file, a new file: refused"
  # Type=exec: systemd-run returns once run.sh started, so the environment file has been read and can go.
  # ProtectSystem=strict: the job writes its copy and its own memory file systems, nothing else.
  # Stop timeouts of its own (at most the walls' STOP_MAX): systemd's defaults never decide how long a job that ignores
  # SIGTERM at RuntimeMaxSec runs on.
  # Within START_BY of this launcher's own start (issue #656): a drop-in (root's, made after the walls ran) with
  # another Type= (oneshot, or notify with TimeoutStartSec=infinity) keeps systemd-run waiting for the start job, and
  # the agent's limit would kill this launcher before the check below, leaving the unit unchecked (RuntimeMaxSec does
  # not apply to a oneshot). Past the deadline the unit is killed and the start refused, as below. With less than
  # START_MIN left, nothing starts: never `timeout 0`, which is no deadline at all, and never so little time that
  # timeout could stop systemd-run after it asked for the unit but before systemd made it (kill_unit would then see
  # nothing, and the unit come after).
  wait=$((START_BY - SECONDS))
  ((wait >= START_MIN)) ||
    die "start: ${SECONDS}s in already, START_BY ($START_BY s) left too little time (under $START_MIN s) to start" \
      "and check a unit: refused"
  code=0
  timeout -k 2 "$wait" "$SYSTEMD_RUN" --quiet --unit "fork-fleet-$name" --collect --uid "$user" \
    --working-directory "$dir" -p Type=exec -p KillMode=control-group -p "EnvironmentFile=$env_file" \
    "${props[@]}" -p "ReadWritePaths=$dir" \
    -p "MemoryMax=${mem_max}M" -p "MemoryHigh=${mem_high}M" -p "MemorySwapMax=${JOB_SWAP_MAX_MB}M" \
    -p "CPUWeight=$JOB_CPU_WEIGHT" -p "CPUQuota=${JOB_CPU_QUOTA:+$JOB_CPU_QUOTA%}" \
    -p "OOMScoreAdjust=$JOB_OOM_SCORE_ADJUST" -p OOMPolicy=kill \
    -p "RuntimeMaxSec=$MAX_SECONDS" -p TimeoutStopSec=90 -p TimeoutAbortSec=90 -- "$dir/run.sh" || code=$?
  # What systemd really applies (issues #365 item 1, #636), drop-ins and defaults included, not what was asked for: a
  # unit that could run on past its limit (a stop timeout over STOP_MAX, a RuntimeMaxSec over MAX_SECONDS, a kill that
  # leaves processes or never comes, a Type= that is not exec; or what cannot be read) is killed at once, and the start
  # refused. Only a unit systemd shows gone or dead with no process left after the kill is said to be killed (issues
  # #636, #656). A systemd-run that fails is killed too (#667): without --no-block it fails whenever the start job
  # does (failed, timeout), after the unit was made and run.sh ran, and a drop-in (KillMode=none with a failing
  # ExecStartPost=, say) can leave the job running in a failed unit, past RuntimeMaxSec and unchecked. That kill can
  # reach no other start's unit: $dir was refused above if a runner of this name ran already, and prune removes it only
  # once the unit is gone; a unit that never was shows gone.
  why='a job could run on past its limit'
  if ((code == 124 || code == 137)); then
    bad="did not start within ${wait}s (the start still waited: a Type= that is not exec?)"
  elif ((code != 0)); then
    bad="failed to start (its start exited $code)" why='what it started could run on, unchecked'
  else
    bad=$(runs_on "fork-fleet-$name.service")
  fi
  if [[ -n $bad ]]; then
    left=$(kill_unit "fork-fleet-$name.service" "$user") || die "start: fork-fleet-$name.service $bad: $why;" \
      "could not kill it (${left:-unreadable}): refused; kill it by hand:" \
      "systemctl kill --signal=KILL -- fork-fleet-$name.service"
    warn "start: fork-fleet-$name.service $bad: $why; $left and refused"
    ((code == 0 || code == 124 || code == 137)) || exit "$code"
    exit 1
  fi
}

# At most this long (seconds) may systemd wait for a job unit to stop before it kills it (the walls' STOP_MAX: a test
# keeps the two one): at RuntimeMaxSec it asks the job to stop, and only kills it when that runs out.
STOP_MAX=300
# What runs_on reads of a job unit (`systemctl show`): each must be printed once, but RuntimeRandomizedExtraUSec, which
# systemd before 250 has not (and so cannot add to RuntimeMaxSec either).
JOB_SHOW=(TimeoutStopUSec TimeoutAbortUSec RuntimeMaxUSec RuntimeRandomizedExtraUSec KillMode SendSIGKILL
  FinalKillSignal Type LoadState ActiveState ControlGroup)
# How often kill_unit looks (every half second) for the unit to be gone before it says it could not kill it.
KILL_TRIES=10
# By when (seconds after this launcher began: bash's SECONDS) systemd-run must have returned (issue #656), and the
# least time it may have (#667: see start). The lock wait counts: it is part of the agent's 100 s too.
START_BY=80
START_MIN=10
# By when start's check and kill are done (seconds after this launcher began): each systemctl call of runs_on and
# kill_unit runs at most CALL_MAX s and never past KILL_BY (systemctl), then gets a KILL 1 s later, and kill_unit looks
# no more once KILL_BY is reached. A test keeps KILL_BY, with START_BY's own kill grace before it and the last call's
# after it, inside the agent's own `start` limit (fork-fleet-desk-wire.py's VERBS, 100 s).
KILL_BY=92
CALL_MAX=5

# Why job unit $1, as systemd applies it, could run on past its limit, or nothing (issues #365 item 1, #636): a stop
# timeout (TimeoutStopUSec, TimeoutAbortUSec) not at most STOP_MAX or a RuntimeMaxUSec not at most MAX_SECONDS (each
# endless: infinity or 0, or longer), RuntimeRandomizedExtraUSec not 0, KillMode not control-group (what it leaves runs
# on), SendSIGKILL not yes or FinalKillSignal not SIGKILL (a job that ignores the stop is never killed), Type not exec
# (what start asked for: issue #656); any other key or word, a key missing or twice, a LoadState but loaded, or a show
# that fails: fail closed. A unit systemd no longer has (it ended and --collect removed it) has nothing to check: show
# then prints the manager's defaults, not the unit's, and nothing of it runs. That is LoadState=not-found with
# ActiveState=inactive and no ControlGroup (issue #656): a unit whose file root removed while it runs (and a
# daemon-reload) shows not-found too, but active, with its cgroup: checked, so refused.
runs_on() {
  local out line key val
  local -a args=() words
  local -A got=()
  for key in "${JOB_SHOW[@]}"; do args+=(-p "$key"); done
  out=$(systemctl_by show "${args[@]}" --all -- "$1" 2>/dev/null) || {
    echo "has settings systemctl show cannot read"
    return
  }
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    [[ $line =~ ^([A-Za-z]+)=(.*)$ && " ${JOB_SHOW[*]} " == *" ${BASH_REMATCH[1]} "* ]] ||
      { echo "has a show line it did not ask for"; return; }
    key=${BASH_REMATCH[1]}
    [[ -z ${got[$key]+x} ]] || { echo "has $key twice"; return; }
    read -ra words <<<"${BASH_REMATCH[2]}"
    got[$key]=${words[*]}
  done <<<"$out"
  [[ ${got[LoadState]-}:${got[ActiveState]-}:${got[ControlGroup]-x} != not-found:inactive: ]] || return 0
  for key in "${JOB_SHOW[@]}"; do
    [[ -n ${got[$key]+x} || $key == RuntimeRandomizedExtraUSec ]] ||
      { echo "has no $key: systemctl show did not print it"; return; }
  done
  [[ ${got[LoadState]} == loaded ]] ||
    { echo "has LoadState=${got[LoadState]} (ActiveState=${got[ActiveState]}), not loaded"; return; }
  [[ ${got[Type]} == exec ]] || { echo "has Type=${got[Type]}, not exec: not what start asked for"; return; }
  within TimeoutStopUSec "${got[TimeoutStopUSec]}" "$STOP_MAX" STOP_MAX || return 0
  within TimeoutAbortUSec "${got[TimeoutAbortUSec]}" "$STOP_MAX" STOP_MAX || return 0
  within RuntimeMaxUSec "${got[RuntimeMaxUSec]}" "$MAX_SECONDS" MAX_SECONDS || return 0
  val=${got[RuntimeRandomizedExtraUSec]-0}
  [[ $val == 0 ]] || { echo "has RuntimeRandomizedExtraUSec=$val, not 0: it runs on past RuntimeMaxSec"; return; }
  [[ ${got[KillMode]} == control-group ]] ||
    { echo "has KillMode=${got[KillMode]}, not control-group: what it leaves runs on"; return; }
  [[ ${got[SendSIGKILL]} == yes ]] ||
    { echo "has SendSIGKILL=${got[SendSIGKILL]}: a job that ignores the stop is never killed"; return; }
  [[ ${got[FinalKillSignal]} == 9 ]] ||
    { echo "has FinalKillSignal=${got[FinalKillSignal]}, not SIGKILL (9): a job that ignores the stop lives"; return; }
}

# Whether $1's value $2, a time as systemd prints it, is more than 0 and at most $3 seconds (the setting $4); else
# says why and fails.
within() {
  local ms
  [[ -n $2 ]] || { echo "has an empty $1"; return 1; }
  [[ $2 != infinity && $2 != 0 ]] || { echo "has $1=$2: endless"; return 1; }
  ms=$(span_ms "$2" "$(($3 * 1000))")
  [[ -n $ms ]] || { echo "has $1=$2, not a time it can read"; return 1; }
  ((ms > 0 && ms <= $3 * 1000)) || { echo "has $1=$2, over $4 ($3 s) or endless"; return 1; }
}

# The milliseconds of $1, a time as systemd prints it (format_timespan: words such as 1h, 1min, 30s, 500ms, 1.5s), or
# more than $2 when it is longer than $2 ms; nothing when it is no such time (infinity and 0 are the caller's). A
# fraction counts as one more of its unit and microseconds round up: never less than systemd's.
span_ms() {
  local ms=0 word n u cap=$(($2 + 1))
  local -a words
  read -ra words <<<"$1"
  for word in "${words[@]}"; do
    [[ $word =~ ^([0-9]{1,9})(\.([0-9]{1,6}))?(us|ms|s|min|h|d|w|month|y)$ ]] || return 0
    n=$((10#${BASH_REMATCH[1]}))
    [[ -z ${BASH_REMATCH[3]} ]] || n=$((n + 1))
    case ${BASH_REMATCH[4]} in
      us) n=$(((n + 999) / 1000)) u=1 ;;
      ms) u=1 ;;
      s) u=1000 ;;
      min) u=60000 ;;
      h) u=3600000 ;;
      d) u=86400000 ;;
      w) u=604800000 ;;
      month) u=2629800000 ;;
      y) u=31557600000 ;;
    esac
    # One guard keeps ms at most cap and n * u from bash's 64-bit wrap (issue #656: a sum that wraps could come out
    # a short time): n * u is added only when it fits in what is left under cap.
    if ((n > (cap - ms) / u)); then ms=$cap; else ms=$((ms + n * u)); fi
  done
  echo "$ms"
}

# systemctl "$@" for start's check and kill: at most CALL_MAX s and never past KILL_BY (at least 1 s), then KILL 1 s
# later (a D-Bus call that hangs fails, and every caller fails closed on that).
systemctl_by() {
  local t=$((KILL_BY - SECONDS))
  ((t <= CALL_MAX)) || t=$CALL_MAX
  ((t >= 1)) || t=1
  timeout -k 1 "$t" "$SYSTEMCTL" "$@"
}

# Kills unit $1, which ran as runner user $2 (SIGKILL to all its processes, then a stop, so nothing starts them again),
# and looks every half second, KILL_TRIES times and not past KILL_BY, until systemd shows it inactive or failed with no
# ControlGroup (one --collect removed shows that too) and $2 has no process left. systemd keeps a stopped unit's cgroup
# while processes are left in it (issue #656: a KillMode=process or none stops only the main one, and the unit shows
# inactive while the rest run on), and lets it go once it is empty; older systemd lets it go anyway, so the runner
# user's own processes are counted too (#667: free_user gives each job a user no other process has). `show --all`:
# some systemd versions leave out a property that is empty, as ControlGroup is for a unit that is gone, unless --all
# is given (it still prints only the -p keys). Prints "killed", or "gone" when the kill failed (no such unit: a
# systemd-run that never made it) but nothing of it is left. Fails, printing what it last showed (nothing when show
# failed) and whether the kill itself failed, when it never does: a kill or stop may fail (D-Bus), and then a unit
# that ignores the stop, or what it left, runs on.
kill_unit() {
  local out state='' cg='' line i killed=1 uid procs rc
  uid=$(id -u -- "$2") || { printf 'its user %s is unreadable' "$2"; return 1; }
  systemctl_by kill --signal=KILL -- "$1" || killed=0
  systemctl_by stop --no-block -- "$1" || true
  for ((i = 0; i < KILL_TRIES; i++)); do
    state='' cg='' procs=''
    if out=$(systemctl_by show -p ActiveState -p ControlGroup --all -- "$1" 2>/dev/null); then
      while IFS= read -r line; do
        case $line in ActiveState=*) state=${line#*=} ;; ControlGroup=*) cg=${line#*=} cg=${cg:-none} ;; esac
      done <<<"$out"
    fi
    if [[ ($state == inactive || $state == failed) && $cg == none ]]; then
      rc=0
      pgrep -u "$uid" >/dev/null || rc=$?
      if ((rc == 1)); then
        if ((killed)); then printf 'killed'; else printf 'gone'; fi
        return 0
      fi
      procs="; $2 has processes left"
      ((rc == 0)) || procs="; pgrep failed ($rc)"
    fi
    ((i + 1 < KILL_TRIES && SECONDS < KILL_BY)) || break
    sleep 0.5
  done
  if [[ -z $state ]]; then
    printf 'unreadable'
  elif [[ -n $procs ]]; then
    printf 'ActiveState=%s, no ControlGroup%s' "$state" "$procs"
  elif [[ $state == inactive || $state == failed ]]; then
    printf 'ActiveState=%s, but processes left in ControlGroup=%s' "$state" "${cg:-unreadable}"
  else
    printf 'ActiveState=%s' "$state"
  fi
  ((killed)) || printf '; the kill failed'
  return 1
}

# This host's own public addresses ($1, as the units block them) from inside, with the nft table alone: run in the
# sandbox, as the runner user, with these addresses left out of the unit's IPAddressDeny. Each must get the table's
# refusal (its reject: No route to host), not a timeout: a router that drops the packet would prove nothing. A job's
# unit has them in IPAddressDeny too, which drops the packet in the kernel before it reaches the table (no answer,
# no refusal): PROBE takes that silence as closed only once this probe saw the table refuse each of them.
read -r -d '' OWN_PROBE <<'EOF' || true
bad=0
[[ $1 =~ [^[:space:]] ]] || { echo "FAIL  no own public address to try"; bad=1; }
for own in $1; do
  if err=$(LC_ALL=C timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/443"' "$own" 2>&1); then
    echo "FAIL  this host's own public address $own is reachable from inside with the table alone"
    bad=1
  elif [[ $err =~ (No\ route\ to\ host) ]]; then
    echo "ok    this host's own public address $own is refused by the table alone (${BASH_REMATCH[1]})"
  else
    echo "FAIL  this host's own public address $own is not refused by the table alone (${err:-no answer in 5 s})"
    bad=1
  fi
done
exit "$bad"
EOF

# Runs in the sandbox as the runner user: $1 the URL that must answer, $2 this host's own public addresses, which the
# walls must refuse, $3 `refused` when OWN_PROBE saw the table alone refuse each of them (else `unproven`), then HOST
# PORT pairs that must be closed. An own address must get a refusal, or no answer at all only after OWN_PROBE passed
# (the unit's IPAddressDeny drops it before the table: the one difference between the two units); an answer, any
# other error (a reset: the packet got through) or no answer without that proof fails. No answer is timeout's own
# exit (124) with nothing said: a connect that ended silently any other way (killed: SIGKILL, the OOM killer) fails.
read -r -d '' PROBE <<'EOF' || true
url=$1 table=$3 bad=0
for own in $2; do
  ec=0
  err=$(LC_ALL=C timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/443"' "$own" 2>&1) || ec=$?
  if ((ec == 0)); then
    echo "FAIL  this host's own public address $own is reachable from inside"
    bad=1
  elif [[ $table != refused ]]; then
    echo "FAIL  this host's own public address $own: the table alone did not refuse it (above), so this proves nothing (${err:-no answer in 5 s})"
    bad=1
  elif [[ $err =~ (Operation\ not\ permitted|No\ route\ to\ host|Permission\ denied|Network\ is\ unreachable) ]]; then
    echo "ok    this host's own public address $own is refused (${BASH_REMATCH[1]})"
  elif ((ec == 124)) && [[ -z $err ]]; then
    echo "ok    this host's own public address $own gets no answer (the unit's IPAddressDeny drops it; the table alone refuses it, above)"
  else
    echo "FAIL  this host's own public address $own is not refused by the walls (${err:-exit $ec, nothing said})"
    bad=1
  fi
done
shift 3
while (($# >= 2)); do
  if timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>/dev/null; then
    echo "FAIL  $1:$2 is reachable from inside"
    bad=1
  else
    echo "ok    $1:$2 is closed"
  fi
  shift 2
done
if code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -- "$url"); then
  echo "ok    $url answers (HTTP $code)"
else
  echo "FAIL  $url does not answer"
  bad=1
fi
exit "$bad"
EOF

# The walls' probe (python3 -I -c, arguments job or host, the mount points job_fs allows, then each bound one's
# POINT=DEVICE:INODE: its source as job_fs checked it, seen from outside, the links its root has, and the canaries'
# paths). job: in the sandbox,
# as the runner user; a line per wall, exit 1 when one is open or could not be tried. host: as root, outside, the
# control: whether this host has what the walls close (where it has not, the refusal inside shows nothing; the walls
# are still set). Each 32-bit call is made in a child that may not dump core: the filter ends it with SIGSYS; another
# signal means this kernel has no such calls. i386: getpid (20) through int 0x80; x32: getpid (39 | 0x40000000).
# Then what the files show: device nodes besides the pseudo ones (a GPU, /dev/dxg); every mount point the unit has
# (the allowed ones, its own /proc, /sys and /dev, and nothing else); whether any of its mounts receives the host's
# mounts (mountinfo's master: or shared: tag: a disk mounted after the job started would show up in it); whether each
# bound point is the very file or folder job_fs checked (a link swapped in before systemd bound it would show);
# entries in /mnt or /media; and sockets in the places a desktop keeps them (WSLg's X11 and PulseAudio). Other mount
# points are read from mountinfo, never looked at: a share that hangs cannot stop the probe. Then /etc (issue #243),
# checked by looking: every entry of the job's /etc is one the list (a mount point), an entry job_fs made (its fourth
# argument) or the jobs' resolv.conf put there, and check-net's canaries (its fifth: files outside the list, one
# others may read, one only the runner's group may) are not there nor readable. And the job's passwd, group and hosts
# are job_etc's own (issue #370): root, nobody, its user and loopback only, never the host's accounts or LAN names.
read -r -d '' WALLS <<'EOF' || true
import ctypes, errno, mmap, os, re, signal, socket, stat, sys
libc = ctypes.CDLL(None, use_errno=True)
def alone(f):
    pid = os.fork()
    if pid == 0:
        try:
            libc.prctl(4, ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
            code = 1 if f() >= 0 else 0
        except BaseException:
            code = 3
        os._exit(code)
    st = os.waitpid(pid, 0)[1]
    if os.WIFSIGNALED(st):
        return 'killed' if os.WTERMSIG(st) == signal.SIGSYS else 'none'
    return {0: 'none', 1: 'open'}.get(os.WEXITSTATUS(st), 'untried')
def i386():
    m = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
    m.write(bytes.fromhex('b814000000cd80c3'))   # mov eax, 20; int 0x80; ret
    return ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.addressof(ctypes.c_char.from_buffer(m)))()
try:
    socket.socket(40, socket.SOCK_STREAM).close()
    vsock = 'open'
except OSError as e:
    vsock = errno.errorcode.get(e.errno, str(e.errno))
ring = libc.syscall(425, 1, ctypes.create_string_buffer(120))
uring = 'open' if ring >= 0 else errno.errorcode.get(ctypes.get_errno(), '?')
calls = {}
if os.uname().machine == 'x86_64':
    calls = {'i386': alone(i386), 'x32': alone(lambda: libc.syscall(0x40000027))}
budget = [20000]
def walk(top, keep, depth=4):
    """What keep() takes under top, on top's own file system, at most depth levels down and budget entries in all."""
    try:
        dev = os.lstat(top).st_dev
    except OSError:
        return
    todo = [(top, 0)]
    while todo:
        d, n = todo.pop()
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for x in names:
            budget[0] -= 1
            if budget[0] < 0:
                return
            p = os.path.join(d, x)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if keep(st.st_mode):
                yield p
            if stat.S_ISDIR(st.st_mode) and st.st_dev == dev and n < depth and p != '/dev/pts':
                todo.append((p, n + 1))
mounts = []
for m in open('/proc/self/mountinfo', 'rb'):
    f = m.split()
    sep = f.index(b'-')
    point = os.fsdecode(re.sub(rb'\\([0-7]{3})', lambda o: bytes([int(o[1], 8)]), f[4]))
    mounts.append((point, f[sep + 1].decode(), [t.decode() for t in f[6:sep]], f[2], os.fsdecode(f[3])))
allowed = set(sys.argv[2].split()) if len(sys.argv) > 2 else set()
top = [(dev, root) for p, _, _, dev, root in mounts if p == '/'][-1:]
def own(point, dev, root):
    # The allowed ones; the unit's own memory and API file systems; systemd's own in every unit with a root directory
    # (empty, or the host's os-release); a part of the unit's own root (a stand-in ProtectHome/ProtectSystem make
    # there) or an empty, unreadable one of systemd's (/systemd/inaccessible/).
    return point in allowed or point in ('/run', '/tmp', '/var/tmp', '/run/systemd/incoming',
                                         '/run/host/.os-release-stage') or \
        any(point == a or point.startswith(a + '/') for a in ('/proc', '/sys', '/dev')) or \
        root.startswith('/systemd/inaccessible/') or \
        (point != '/' and top == [(dev, top[0][1])] and root.startswith(top[0][1].rstrip('/') + '/'))
foreign = [f'{p} ({k})' for p, k, _, dev, root in mounts if not own(p, dev, root)]
def moved(entry):
    point, _, want = entry.partition('=')
    try:
        st = os.stat(point)
    except OSError:
        return True
    return f'{st.st_dev}:{st.st_ino}' != want
sources = sys.argv[3].split() if len(sys.argv) > 3 else []
swapped = [x.partition('=')[0] for x in sources if moved(x)]
receive = [p for p, _, tags, _, _ in mounts if any(t.startswith(('master:', 'shared:')) for t in tags)]
devs = [p[5:] for p in walk('/dev', lambda m: stat.S_ISCHR(m) or stat.S_ISBLK(m))
        if p[5:] not in ('null', 'zero', 'full', 'random', 'urandom', 'tty', 'ptmx')]
def entries(d):
    try:
        return [os.path.join(d, x) for x in os.listdir(d)]
    except OSError:
        return []
mnt = entries('/mnt') + entries('/media')
socks = [p for d in ('/tmp', '/var/tmp', '/run', '/dev/shm') for p in walk(d, stat.S_ISSOCK)]
whole = budget[0] >= 0
def few(xs):
    return ', '.join(sorted(xs)[:8]) + (' ...' if len(xs) > 8 else '')
ETC = '/etc'
links = set(sys.argv[4].split()) if len(sys.argv) > 4 else set()
listed = {p[5:] for p in allowed | links if p.startswith('/etc/')}
def etc_extra():
    """What /etc holds beyond the entries job_fs put there (names under /etc), or None when it cannot be listed."""
    extra, todo = [], ['']
    try:
        while todo:
            d = todo.pop()
            for x in os.listdir(os.path.join(ETC, d)):
                rel = os.path.join(d, x)
                p = os.path.join(ETC, rel)
                if rel in listed:
                    continue
                if any(a.startswith(rel + '/') for a in listed) and os.path.isdir(p) and not os.path.islink(p):
                    todo.append(rel)
                else:
                    extra.append(rel)
    except OSError:
        return None
    return extra
etc = etc_extra()
def not_own():
    """Issue #370: what the job's passwd, group and hosts hold beyond job_fs's own (job_etc): an account or group but
    root's, nobody's and this process's own, or a name on an address but loopback. The host's would name every account
    (GECOS, home) and the LAN's hosts. A passwd or group with no line of this process's own (an empty one: issue #613
    item 6) is not job_etc's either."""
    out = []
    for name, ids, mine in (('passwd', {0, 65534, os.getuid()}, os.getuid()),
                            ('group', {0, 65534, os.getgid()}, os.getgid())):
        try:
            # Bytes beyond ASCII (an old Latin-1 GECOS on the host) as \x escapes: read, never a crash (issue #594).
            with open(os.path.join(ETC, name), encoding='ascii', errors='backslashreplace') as f:
                own = False
                for x in f:
                    w = x.rstrip('\n').split(':')
                    if x.strip() and not (len(w) >= 4 and w[2].isdigit() and int(w[2]) in ids):
                        out.append(f'{name} {w[0]}')
                    own = own or (len(w) >= 4 and w[2].isdigit() and int(w[2]) == mine)
                if not own:
                    out.append(f'{name} (no line of its own)')
        except OSError:
            out.append(f'{name} (cannot read it)')
    try:
        with open(os.path.join(ETC, 'hosts'), encoding='ascii', errors='backslashreplace') as f:
            for x in f:
                w = x.split('#')[0].split()
                if w and not (w[0] == '::1' or w[0].startswith('127.')):
                    out.append(f'hosts {w[0]}')
    except OSError:
        out.append('hosts (cannot read it)')
    return out
own = not_own()
canaries = sys.argv[5].split() if len(sys.argv) > 5 else []
def there(p):
    try:
        with open(p, 'rb') as f:
            f.read(1)
        return True
    except OSError:
        return os.path.lexists(p)
seen_canaries = [c for c in canaries if there(c)]
if sys.argv[1] == 'host':
    print('host: AF_VSOCK ' + ('opens here: the wall matters' if vsock == 'open' else
                               f'does not open here either ({vsock}): the refusal inside shows nothing on this host'))
    print('host: io_uring ' + ('gives a ring here: the wall matters' if uring == 'open' else
                               f'gives no ring here either ({uring})'))
    for k, v in calls.items():
        print(f'host: {k} system calls ' + ('work here: the wall matters' if v == 'open' else 'do not work here'))
    for what, xs in (('devices besides the pseudo ones', devs), ('mounts no unit has', foreign),
                     ('mounts that receive later mounts', receive), ('entries in /mnt and /media', mnt),
                     ('sockets', socks)):
        print(f'host: {what}: ' + (few(xs) if xs else 'none here either: the refusal inside shows nothing'))
    print('host: /etc: ' + ('cannot be listed' if etc is None else
                            f'{len(etc)} entries no list line puts in a job\'s: the wall matters' if etc else
                            'nothing beyond the list here either: the refusal inside shows nothing'))
    print(f'host: canaries: {len(seen_canaries)} of {len(canaries)} there' + (': the wall matters' if seen_canaries else
                                                                              ''))
    print(f'host: passwd, group, hosts: {len(own)} entries a job\'s own leave out' + (': the wall matters' if own else ''))
    sys.exit(0)
bad = 0
def line(ok, text):
    global bad
    bad |= not ok
    print(('ok    ' if ok else 'FAIL  ') + text)
line(vsock != 'open', 'AF_VSOCK ' + ('opens inside' if vsock == 'open' else f'is refused ({vsock})'))
line(uring == 'ENOSYS', 'io_uring ' + ('gives a ring inside' if uring == 'open' else
                                       f'is refused ({uring})' if uring == 'ENOSYS' else
                                       f'is not refused by the unit\'s filter ({uring})'))
for k, v in calls.items():
    line(v in ('killed', 'none'), f'{k} system calls ' + {
        'killed': 'end the process (SIGSYS)', 'none': 'do not work on this kernel', 'open': 'work inside',
        'untried': 'could not be tried'}[v])
line(not devs, 'devices: ' + (f'host ones inside: {few(devs)}' if devs else 'only the pseudo ones'))
line(bool(allowed) and not foreign, 'mounts: ' + (f'not allowed inside: {few(foreign)}' if foreign else
                                                  'only the allowed ones' if allowed else 'no allow list given'))
line(bool(sources) and not swapped, 'mount sources: ' + (
    f'not what job_fs checked: {few(swapped)}' if swapped else 'each the host path job_fs checked' if sources else
    'none given'))
line(not receive, 'later mounts: ' + (f'the host\'s reach {few(receive)}' if receive else
                                      'no mount inside receives the host\'s'))
line(not mnt, '/mnt and /media: ' + (f'hold {few(mnt)} inside' if mnt else 'nothing inside'))
line(etc == [], '/etc: ' + ('cannot list it' if etc is None else
                            f'holds {few(etc)}, which no list line puts there' if etc else 'only the listed entries'))
line(not own, 'passwd, group, hosts: ' + (f'hold {few(own)}, which are not the job\'s own' if own else
                                         'only root, nobody, the job\'s own user and loopback'))
line(bool(canaries) and not seen_canaries, 'canaries: ' + (
    f'{few(seen_canaries)} there inside' if seen_canaries else f'{len(canaries)} not there inside' if canaries else
    'none given'))
line(whole and not socks, 'sockets: ' + (f'{few(socks)} inside' if socks else 'none in /tmp, /var/tmp, /run, /dev/shm'
                                         if whole else 'too many files to look through'))
sys.exit(bad)
EOF

# check-net's canaries (issue #243): two files in CANARY_DIR (/etc, root's: no list line names them), one others may
# read and one only the runner user's group ($1's) may, so a job that saw them would read both. Made fresh each time
# (a file left by a check-net that was cut off is removed first) and removed when check-net ends (its trap). Root
# writes them, so their folder is checked as a bound path is: CANARY_DIR is its own real path (no link on the way),
# each folder above it is closed (closed_folder, through real_path), and CANARY_DIR itself is root's and only root may
# write it (owned_safe); then no one else can put a link where a canary goes. Still, each is a new file (noclobber:
# O_EXCL, which a link fails too) made with its mode (umask, no chmod that follows a link), and chown -h.
make_canaries() {
  local c=$CANARY_DIR/fork-fleet-canary gid real
  real=$(real_path "$CANARY_DIR")
  [[ $real == "$CANARY_DIR" ]] ||
    die "check-net: CANARY_DIR $CANARY_DIR leads to $real: name the folder itself, with no link on the way"
  owned_safe "$CANARY_DIR"
  [[ -d $CANARY_DIR ]] || die "check-net: CANARY_DIR $CANARY_DIR is not a folder"
  gid=$(id -g -- "$1") || die "check-net: no group of $1"
  rm -f -- "$c" "$c-group"
  (
    set -o noclobber
    umask 022
    printf 'fork-fleet canary: no job may see this file\n' >"$c"
    umask 027
    printf 'fork-fleet canary: no job may see this file\n' >"$c-group"
  ) 2>/dev/null || die "check-net: could not make the canaries in $CANARY_DIR as new files: refused"
  chown -h -- ":$gid" "$c-group"
}

check_net() {
  local url=$1 target host port user resolv own blocked seen from linked canaries rc=0 prop n=0 table=refused
  local -a pairs=() props table_props=()
  shift
  [[ $url =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]] || die "check-net: not a plain http(s) URL: $url"
  (($# > 0)) || die "check-net: name at least one HOST:PORT that must be closed"
  for target; do
    [[ $target =~ ^([0-9]{1,3}(\.[0-9]{1,3}){3}):([0-9]{1,5})$ ]] || die "check-net: not an IPv4 HOST:PORT: $target"
    host=${BASH_REMATCH[1]} port=${BASH_REMATCH[3]}
    # shellcheck disable=SC2016 # $0 and $1 are the inner bash's own arguments
    timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$host" "$port" 2>/dev/null ||
      die "check-net: this host does not reach $target either, so its refusal inside would prove nothing:" \
        "name a service this host reaches"
    pairs+=("$host" "$port")
  done
  [[ -x /usr/bin/python3 ]] || die "check-net: the walls' probe needs /usr/bin/python3"
  check_users
  resolv=$(resolv_target)
  own=$(own_address)
  setup
  user=$(free_user)
  reset_user "$user"
  merge_own "$own"
  net_up "$user"
  sandbox "$user" "$resolv" "$blocked" "$RUN_DIR/check-net.root" ''
  # The same sandbox with this host's own addresses left out of IPAddressDeny: the nft table alone stands between
  # the job and them (OWN_PROBE).
  for prop in "${props[@]}"; do
    if [[ $prop == IPAddressDeny=* ]]; then
      prop="IPAddressDeny=${PRIVATE4[*]} ::/0"
      n=$((n + 1))
    fi
    table_props+=("$prop")
  done
  ((n == 1)) || die "check-net: the sandbox has $n IPAddressDeny settings, not one"
  canaries="$CANARY_DIR/fork-fleet-canary $CANARY_DIR/fork-fleet-canary-group"
  # Expanded now, as start's env file: removed however check-net ends (one a kill left is replaced next time).
  # shellcheck disable=SC2064
  trap "rm -f -- $(printf '%q' "$CANARY_DIR/fork-fleet-canary") $(printf '%q' "$CANARY_DIR/fork-fleet-canary-group")" EXIT
  make_canaries "$user"
  # Every probe's words (the names of files any local user may make among them) reach the terminal through shown().
  /usr/bin/python3 -I -c "$WALLS" host "$seen" "$from" "$linked" "$canaries" 2>&1 | shown_lines
  echo "check-net: as $user, in its namespace fork-fleet-$user"
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-own --uid "$user" "${table_props[@]}" \
    -p RuntimeMaxSec=60 -- /bin/bash --norc --noprofile -c "$OWN_PROBE" check-own "$blocked" </dev/null 2>&1 |
    shown_lines || { rc=4 && table=unproven; }
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-net --uid "$user" "${props[@]}" \
    -p RuntimeMaxSec=120 -- /bin/bash --norc --noprofile -c "$PROBE" check-net "$url" "$blocked" "$table" \
    "${pairs[@]}" </dev/null 2>&1 |
    shown_lines || rc=$((rc | 1))
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-walls --uid "$user" "${props[@]}" \
    -p RuntimeMaxSec=60 -- /usr/bin/python3 -I -c "$WALLS" job "$seen" "$from" "$linked" "$canaries" </dev/null 2>&1 |
    shown_lines || rc=$((rc | 2))
  ((rc == 0)) || die "check-net: the runners' network or walls are NOT as they must be (see above): do not use this pool"
  echo "check-net: the runners reach $url and none of the $((${#pairs[@]} / 2)) services named nor this host's own" \
    "public address; AF_VSOCK, io_uring and 32-bit system calls are closed; no host device, socket or mount but the" \
    "allowed ones is there, and no mount of the host's reaches it later"
}

prune() {
  setup
  prune_now
}

# Root's rm -rf here cannot be led astray (no directory in it swapped for a symlink mid-walk, as in /dev/shm before):
# it removes a copy, or an env file or unit root (job_fs) in root's 0700 RUN_DIR, only once no unit of that name is
# loaded. The unit's processes went with its cgroup, a runner user has no other way to run anything (no login,
# lingering, cron or at), and no other unit, of that user too, has the copy in its file system (job_fs).
prune_now() {
  local live entry name
  live=$(active)
  for entry in "$WORK_ROOT"/* "$RUN_DIR"/*.env "$RUN_DIR"/*.root; do
    [[ -e $entry && ! -L $entry ]] || continue
    name=${entry##*/}
    name=${name%.env}
    name=${name%.root}
    [[ $name =~ $NAME ]] || continue
    if grep -qxF -- "$name" <<<"$live"; then continue; fi
    rm -rf --one-file-system -- "$entry"
  done
}

case "${1-}:$#" in
  start:2) start "$2" ;;
  ready:2) ready "$2" ;;
  active:1) active ;;
  fits:2) fits "$2" ;;
  stop:2)
    name_ok "$2"
    "$SYSTEMCTL" stop -- "fork-fleet-$2.service"
    ;;
  prune:1) prune ;;
  refresh:1) refresh ;;
  refresh-net:1) refresh_net ;;
  paths:1) paths ;;
  tools:1) printf 'SYSTEMD_RUN %s\nSYSTEMCTL %s\n' "$(shown "$SYSTEMD_RUN")" "$(shown "$SYSTEMCTL")" ;;
  check-net:*)
    (($# >= 3)) || die "usage: check-net URL HOST:PORT..."
    shift
    check_net "$@"
    ;;
  *) die "usage: start NAME | ready NEWEST | active | fits MB | stop NAME | prune | refresh | refresh-net | paths | tools | check-net URL HOST:PORT..." ;;
esac
