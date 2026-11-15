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
#   fits MB      exit 0 when MemAvailable minus RESERVE_MB leaves MB megabytes.
#   stop NAME    stops that unit (a job on it fails: its runner is gone).
#   prune        removes the copies and environment files whose unit is gone.
#   refresh-net  every dispatcher pass: looks this host's own public address up again and, when what is blocked would
#                change or the table is gone, re-applies the table (it covers running jobs too) and adds the new
#                addresses to each running unit's IPAddressDeny. No lookup answers: it fails and changes nothing (the
#                last block stays), and the dispatcher starts no runner on the pool this pass.
#   check-net URL HOST:PORT...
#                the proof of the network and the walls (.github/fork-fleet.md, host step 6): runs two probes in a
#                runner's real sandbox, as a free runner user. Exit 0 only when URL (public) answers, every HOST:PORT
#                (services this host reaches: its loopback, its LAN address, Tailscale) is closed from inside, this
#                host's own public address is refused, no AF_VSOCK socket, io_uring ring or 32-bit system call
#                works inside, only the pseudo devices and no socket are there, the unit sees no mount but the ones
#                job_fs allows, each of them comes from the host path job_fs resolved (no link swapped in between),
#                and none of them receives a mount the host makes later.
#   paths        reads only, for the per-OS walls (fork-fleet-walls/<os>, `mounts`): a line `DEST SOURCE` for every
#                file or folder of this host a job's unit gets (DEST where the unit has it, SOURCE the real path bound
#                there: job_view, the same rule as start's), WORK_ROOT standing for each job's copy in it.
#
# The network: each runner user has a namespace of its own, fork-fleet-USER, so a job has its own loopback and reaches
# no service on this host's (nor an abstract socket). Its only link, ffh-UID (host side), goes to the public internet:
# the nft table inet fork_fleet refuses the host itself, RFC 1918, CGNAT (Tailscale), link-local, every IPv6 packet
# and this host's own public IPv4 addresses (own_address, before every start and every pass), and the unit's
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
# address they give is blocked, at least one must answer), JOB_PATHS (the system paths every job sees:
# fork-fleet-job-paths.txt beside this launcher), ALLOW_PATHS (more paths jobs see read-only, comma-separated, say a
# tool cache: empty by default), IP_FORWARD, SYSTEMD_RUN, SYSTEMCTL, RUNNER_RELEASE (refresh's release record, an https
# URL), RUNNER_DOWNLOAD (the https base of its tarballs) and RUNNER_MIN (the oldest runner version refresh may install,
# empty for none) have defaults below.
# HOOK (and the two files beside it), WORK_ROOT and RESOLV_CONF are bound into every job too: never under a folder that
# holds data (/home, /root, /mnt, /media, /srv), also through a symlink (no_data). A bind mount of a data folder at
# one of them is not seen (it has no link to resolve): that is the operator's to avoid (.github/fork-fleet.md, step 4).
# FORK_FLEET_WALLS_ROOT: the walls' test knob (fork-fleet-walls.py), `paths` only and never as root: a fake machine
# whose launch.conf and paths it reads.
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

# The fake machine of the walls' tests, or '' (this one): refused for every verb but `paths`, and when root runs this.
ROOT=${FORK_FLEET_WALLS_ROOT-}
if [[ -n $ROOT ]]; then
  [[ ${1-} == paths && $EUID != 0 ]] || die "FORK_FLEET_WALLS_ROOT is set: it is for the walls' tests only (paths, not as root)"
  ROOT=$(realpath -e -- "$ROOT") && [[ $ROOT =~ ^/[A-Za-z0-9._/-]+$ ]] || die "FORK_FLEET_WALLS_ROOT is no plain folder"
fi
conf=${FORK_FLEET_LAUNCH_CONF:-/etc/fork-fleet/launch.conf}
# paths on a host not set up yet (no conf at all): what jobs would see with the defaults below.
conf_file=/dev/null
[[ ${1-} == paths && ! -e $ROOT$conf && ! -L $ROOT$conf ]] || { owned_safe "$ROOT$conf" && conf_file=$ROOT$conf; }
RUNNER_USERS='' TEMPLATE=/opt/fork-fleet/template/runner-template WORK_ROOT=/var/lib/fork-fleet/runners RUN_DIR=/run/fork-fleet HOOK=/opt/fork-fleet/hook.sh
LINGER_DIR=/var/lib/systemd/linger RESERVE_MB=2048 MAX_SECONDS=3600 MEMINFO=/proc/meminfo NET_BASE=10.204
RESOLV_CONF=/etc/fork-fleet/resolv.conf IP_FORWARD=/proc/sys/net/ipv4/ip_forward SYSTEMD_RUN=systemd-run
SYSTEMCTL=systemctl OWN_ADDRESS_URLS=https://api.ipify.org,https://checkip.amazonaws.com ALLOW_PATHS=''
JOB_PATHS='' RUNNER_RELEASE=https://api.github.com/repos/actions/runner/releases/latest
RUNNER_DOWNLOAD=https://github.com/actions/runner/releases/download RUNNER_MIN=''
setting='^(RUNNER_USERS|TEMPLATE|WORK_ROOT|RUN_DIR|HOOK|LINGER_DIR|RESERVE_MB|MAX_SECONDS|MEMINFO|NET_BASE|RESOLV_CONF|IP_FORWARD|SYSTEMD_RUN|SYSTEMCTL|OWN_ADDRESS_URLS|JOB_PATHS|ALLOW_PATHS|RUNNER_RELEASE|RUNNER_DOWNLOAD|RUNNER_MIN)=([^[:space:]]*)$'
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
  [[ $line =~ $setting ]] || die "$conf: not a setting: ${line%%=*}"
  printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
done <"$conf_file"
[[ -n $RUNNER_USERS && -n $TEMPLATE || ${1-} == paths ]] || die "$conf: RUNNER_USERS and TEMPLATE are required"
[[ $RESERVE_MB =~ ^[0-9]{1,7}$ && $MAX_SECONDS =~ ^[1-9][0-9]{0,6}$ ]] || die "$conf: RESERVE_MB and MAX_SECONDS are numbers"
{ [[ $NET_BASE =~ ^10\.(0|[1-9][0-9]{0,2})$ ]] && ((BASH_REMATCH[1] <= 255)); } || die "$conf: NET_BASE is 10.N (N up to 255)"
own_url='https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?'
[[ $OWN_ADDRESS_URLS =~ ^$own_url(,$own_url)*$ ]] || die "$conf: OWN_ADDRESS_URLS is a comma-separated list of https URLs"
PLAIN_PATH='(/[A-Za-z0-9._@+-]+)+'
[[ $ALLOW_PATHS =~ ^($PLAIN_PATH(,$PLAIN_PATH)*)?$ ]] || die "$conf: ALLOW_PATHS is a comma-separated list of plain paths"
# An absolute path (a relative one would be read from wherever this runs); on the fake machine, the fake one's.
[[ $JOB_PATHS =~ ^($PLAIN_PATH)?$ ]] || die "$conf: JOB_PATHS is a plain absolute path"
if [[ -n $JOB_PATHS ]]; then JOB_PATHS=$ROOT$JOB_PATHS; else
  JOB_PATHS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/fork-fleet-job-paths.txt
fi

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
# name under a symlink or a file, a symlink or hard link that leads outside the folder, a device, FIFO, sparse or other
# special file, a .fork-fleet-* name of its own, more than MAX_ENTRIES entries or MAX_BYTES of files, a long-name or pax
# header over MAX_HEADER bytes (refused before it is read into memory), a name or link target over MAX_NAME, symlinks
# that take more than MAX_STEPS steps to follow (fanned-out links would take hours). Stripped (as tar's
# --no-same-owner --no-same-permissions would): owner (all root's), setuid, setgid, sticky and group/other write, and
# every directory is 0755. Files are created with O_EXCL|O_NOFOLLOW and symlinks last, so nothing is ever written
# through a link; the version and floor files too.
read -r -d '' UNPACK_PY <<'EOF' || true
# fork-fleet-unpack
import os, posixpath, sys, tarfile, zlib
src, dest, version, floor = sys.argv[1:5]
MAX_ENTRIES, MAX_BYTES = 100000, 4 << 30
MAX_HEADER, MAX_NAME, MAX_STEPS = 65536, 4096, 1000000
NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
os.umask(0o022)


def no(why):
    sys.exit(f'refused: {why}')


def parts(name):
    if name.startswith('/'):
        no(f'{name!r} is an absolute path')
    p = [c for c in name.split('/') if c not in ('', '.')]
    if '..' in p:
        no(f'{name!r} has a ".."')
    return p


class Capped(tarfile.TarInfo):
    """Refuses a GNU long name or pax header over MAX_HEADER bytes before tarfile reads it whole into memory, and a
    GNU sparse file (its chained headers are read into memory too; a runner release has none)."""

    def _proc_member(self, tar):
        if self.type == tarfile.GNUTYPE_SPARSE:
            no(f'{self.name!r} is a sparse file')
        if self.type in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE,
                         tarfile.SOLARIS_XHDTYPE) and self.size > MAX_HEADER:
            no(f'a header of {self.size} bytes (more than {MAX_HEADER})')
        return super()._proc_member(tar)


try:
    tar = tarfile.open(src, 'r:gz', tarinfo=Capped)
    members = []
    for m in tar:
        # Each name as it is read: many long names would fill memory before a check after the loop.
        if len(m.name) > MAX_NAME or len(m.linkname) > MAX_NAME:
            no(f'a name or link target longer than {MAX_NAME}: {m.name[:100]!r}')
        members.append(m)
        if len(members) > MAX_ENTRIES:
            no(f'more than {MAX_ENTRIES} entries')
except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
    sys.exit(f'not a gzip tarball: {e}')

kinds, links, order, plan, total = {}, {}, {}, [], 0
for i, m in enumerate(members):
    p = parts(m.name)
    if m.isdir():
        kind = 'dir'
    elif m.issym():
        kind = 'symlink'
    elif m.islnk():
        kind = 'hardlink'
    elif m.isreg():
        kind = 'file'
    else:
        what = 'device' if m.ischr() or m.isblk() else 'FIFO' if m.isfifo() else 'special'
        no(f'{m.name!r} is a {what} file')
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
    if kind == 'file':
        total += m.size
        if total > MAX_BYTES:
            no(f'more than {MAX_BYTES} bytes of files')
    elif kind == 'symlink':
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
  local -A seen=() nets=()
  [[ $RUNNER_USERS =~ ^[a-z_][a-z0-9_-]{0,31}(,[a-z_][a-z0-9_-]{0,31})*$ ]] || die "$conf: RUNNER_USERS is not a list of user names"
  for user in ${RUNNER_USERS//,/ }; do
    uid=$(id -u -- "$user" 2>/dev/null) || die "no user $user"
    gid=$(id -g -- "$user")
    groups=$(id -G -- "$user")
    [[ $uid != 0 && $uid != "$EUID" ]] || die "each runner user must be a user of its own, not $user"
    [[ -z ${seen[$uid]+set} ]] || die "$conf: $user and ${seen[$uid]} are one user: each slot needs a user of its own"
    seen[$uid]=$user
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
  ((kb / 1024 - RESERVE_MB >= $1))
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
refresh_net() {
  local own name blocked old='' retry=$RUN_DIR/deny-retry
  own=$(own_address)
  setup
  [[ ! -f $RUN_DIR/own-address ]] || old=$(<"$RUN_DIR/own-address")
  merge_own "$own"
  if [[ $(addr_set "$blocked") == "$(addr_set "$old")" && ! -e $retry ]] &&
    nft list table inet fork_fleet >/dev/null 2>&1; then return 0; fi
  local failed=''
  : >"$retry"
  table_up
  for name in $(active); do
    "$SYSTEMCTL" set-property --runtime -- "fork-fleet-$name.service" "IPAddressDeny=$blocked" || failed+=" $name"
  done
  if [[ -n $failed ]]; then
    warn "refresh-net: IPAddressDeny not changed for$failed (the table blocks $blocked); tried again next pass"
  else rm -f -- "$retry"; fi
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

# A path's real path: every symlink in every part of it resolved (on the fake machine: in it), as systemd follows
# them in a bind's source. What job_view checks and binds, so a link in a folder above a path (/opt/x -> /c, /home,
# a share) never leads a job past the checks. Dies when it is no plain path, or when a folder above it is one a user
# but root could swap for a link (others may write it, and it is not sticky): then the bind could come from another
# place than the one checked. A folder only its owner or its group may write passes: the runner users own nothing
# outside their copies and are in no group but their own (check_users), so they cannot swap a link into it.
real_path() {
  local real d
  real=$(realpath -m -- "$ROOT$1") || die "$1: cannot resolve it"
  if [[ -n $ROOT ]]; then
    [[ $real == "$ROOT"/* ]] || die "$1 leads out of FORK_FLEET_WALLS_ROOT"
    real=${real#"$ROOT"}
  fi
  [[ $real =~ ^$PLAIN_PATH$ ]] || die "$1 leads to ${real:-/}: not a plain path"
  d=$real
  while d=${d%/*} && [[ -n $d ]]; do
    [[ ! -e $ROOT$d ]] || (($(stat -c '0%a' -- "$ROOT$d") & 01002 ^ 02)) ||
      die "$1 leads to $real, under $d, which others may write: anyone could swap a link into it"
  done
  printf '%s\n' "$real"
}

# A path no job ever sees of this host's: its own of these or none (/proc, /sys, /dev, /run, /tmp, /var/tmp), or one
# that holds data (no_data). The rule of the list's and ALLOW_PATHS' lines.
never() {
  case $1/ in /proc/* | /sys/* | /dev/* | /run/* | /tmp/* | /var/tmp/*)
    die "$2: a job never sees it from this host (its own, none, or data): not a path to allow" ;;
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
  local line p real hook_dir=${HOOK%/*}
  local -a paths=() plain=()
  [[ $HOOK =~ ^$PLAIN_PATH$ && $WORK_ROOT =~ ^$PLAIN_PATH$ ]] || die "HOOK ($HOOK) and WORK_ROOT must be plain paths"
  owned_safe "$JOB_PATHS"
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    paths+=("$line")
  done <"$JOB_PATHS"
  # shellcheck disable=SC2206 # plain paths only, checked below
  paths+=(${ALLOW_PATHS//,/ })
  view_ro=() view_links=()
  for p in "${paths[@]}"; do
    [[ $p =~ ^$PLAIN_PATH$ && /$p/ != */./* && /$p/ != */../* ]] || die "$JOB_PATHS or ALLOW_PATHS: not a plain path: $p"
    never "$p" "$p"
    if [[ -L $ROOT$p ]]; then
      view_links+=("$p" "$(readlink -- "$ROOT$p")")
    elif [[ -e $ROOT$p ]]; then
      real=$(real_path "$p")
      never "$real" "$p (that is $real)"
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

# job_view for a unit: its root ($1, root's), its copy ($2, read-write; already a real path, in WORK_ROOT's), and
# the jobs' resolv.conf at $3. Sets props_fs and resolv_bind; seen, the mount points check-net's probe allows (its
# own /proc, /sys, /dev and memory file systems are allowed there); and from, each of them with the device and inode
# its source has here, which the probe compares with what the unit has there (a link swapped in after job_view shows).
job_fs() {
  local root=$1 rw=$2 resolv=$3 i b real
  local -a ro=() view_ro view_links
  job_view
  rm -rf --one-file-system -- "$root"
  mkdir -m 0755 -- "$root"
  for ((i = 0; i < ${#view_links[@]}; i += 2)); do
    mkdir -p -m 0755 -- "$root${view_links[i]%/*}"
    ln -s -- "${view_links[i + 1]}" "$root${view_links[i]}"
  done
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
  job_fs "$4" "$5" "$resolv"
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

start() {
  local name=$1 dir env_file line key value user resolv bad own blocked seen from f real
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
  "$SYSTEMD_RUN" --quiet --unit "fork-fleet-$name" --collect --uid "$user" --working-directory "$dir" \
    -p Type=exec -p KillMode=control-group -p "EnvironmentFile=$env_file" "${props[@]}" -p "ReadWritePaths=$dir" \
    -p "RuntimeMaxSec=$MAX_SECONDS" -- "$dir/run.sh"
}

# Runs in the sandbox as the runner user: $1 the URL that must answer, $2 this host's own public addresses, which the
# walls must refuse (a refusal, not a timeout: a router that drops the packet would prove nothing), then HOST PORT
# pairs that must be closed.
read -r -d '' PROBE <<'EOF' || true
url=$1 bad=0
for own in $2; do
  if err=$(LC_ALL=C timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/443"' "$own" 2>&1); then
    echo "FAIL  this host's own public address $own is reachable from inside"
    bad=1
  elif [[ $err =~ (Operation\ not\ permitted|No\ route\ to\ host|Permission\ denied|Network\ is\ unreachable) ]]; then
    echo "ok    this host's own public address $own is refused (${BASH_REMATCH[1]})"
  else
    echo "FAIL  this host's own public address $own is not refused by the walls (${err:-no answer in 5 s})"
    bad=1
  fi
done
shift 2
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
# POINT=DEVICE:INODE: its source as job_fs checked it, seen from outside). job: in the sandbox,
# as the runner user; a line per wall, exit 1 when one is open or could not be tried. host: as root, outside, the
# control: whether this host has what the walls close (where it has not, the refusal inside shows nothing; the walls
# are still set). Each 32-bit call is made in a child that may not dump core: the filter ends it with SIGSYS; another
# signal means this kernel has no such calls. i386: getpid (20) through int 0x80; x32: getpid (39 | 0x40000000).
# Then what the files show: device nodes besides the pseudo ones (a GPU, /dev/dxg); every mount point the unit has
# (the allowed ones, its own /proc, /sys and /dev, and nothing else); whether any of its mounts receives the host's
# mounts (mountinfo's master: or shared: tag: a disk mounted after the job started would show up in it); whether each
# bound point is the very file or folder job_fs checked (a link swapped in before systemd bound it would show);
# entries in /mnt or /media; and sockets in the places a desktop keeps them (WSLg's X11 and PulseAudio). Other mount
# points are read from mountinfo, never looked at: a share that hangs cannot stop the probe.
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
line(whole and not socks, 'sockets: ' + (f'{few(socks)} inside' if socks else 'none in /tmp, /var/tmp, /run, /dev/shm'
                                         if whole else 'too many files to look through'))
sys.exit(bad)
EOF

check_net() {
  local url=$1 target host port user resolv own blocked seen from rc=0
  local -a pairs=() props
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
  # Every probe's words (the names of files any local user may make among them) reach the terminal through shown().
  /usr/bin/python3 -I -c "$WALLS" host "$seen" "$from" 2>&1 | shown_lines
  echo "check-net: as $user, in its namespace fork-fleet-$user"
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-net --uid "$user" "${props[@]}" \
    -p RuntimeMaxSec=120 -- /bin/bash --norc --noprofile -c "$PROBE" check-net "$url" "$blocked" "${pairs[@]}" </dev/null 2>&1 |
    shown_lines || rc=$?
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-walls --uid "$user" "${props[@]}" \
    -p RuntimeMaxSec=60 -- /usr/bin/python3 -I -c "$WALLS" job "$seen" "$from" </dev/null 2>&1 |
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
  check-net:*)
    (($# >= 3)) || die "usage: check-net URL HOST:PORT..."
    shift
    check_net "$@"
    ;;
  *) die "usage: start NAME | ready NEWEST | active | fits MB | stop NAME | prune | refresh | refresh-net | paths | check-net URL HOST:PORT..." ;;
esac
