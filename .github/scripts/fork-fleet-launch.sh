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
#   ready NEWEST exit 0 when the template's runner version (TEMPLATE/.fork-fleet-version, written by the runbook) is
#                not older than NEWEST, the newest actions/runner release. An older runner would update itself in
#                its copy before the job, which this fleet refuses: the owner refreshes the template instead.
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
#                job_fs allows, and none of them receives a mount the host makes later.
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
# writable by it alone. RUNNER_USERS (user names, comma-separated: at least the pool's slots) and TEMPLATE (an
# unconfigured actions/runner, owned by this user, on WORK_ROOT's filesystem) are required; WORK_ROOT, RUN_DIR, HOOK,
# LINGER_DIR, RESERVE_MB, MAX_SECONDS, MEMINFO, NET_BASE (10.N: the runners' 10.N.0.0/16, one /30 per user from its
# uid, unused by anything else on the host), RESOLV_CONF (the jobs' resolv.conf: public nameservers, as this host's
# own resolver is on its loopback), OWN_ADDRESS_URLS (https URLs, comma-separated, that answer with the address
# they see: every public address they give is blocked, at least one must answer), JOB_PATHS (the system paths every
# job sees: fork-fleet-job-paths.txt beside this launcher), ALLOW_PATHS (more paths jobs see read-only,
# comma-separated, say a tool cache: empty by default), IP_FORWARD, SYSTEMD_RUN and SYSTEMCTL have defaults below.
set -euo pipefail
umask 077

warn() { printf 'fork-fleet-launch: %s\n' "$*" >&2; }
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

conf=${FORK_FLEET_LAUNCH_CONF:-/etc/fork-fleet/launch.conf}
owned_safe "$conf"
RUNNER_USERS='' TEMPLATE='' WORK_ROOT=/var/lib/fork-fleet/runners RUN_DIR=/run/fork-fleet HOOK=/opt/fork-fleet/hook.sh
LINGER_DIR=/var/lib/systemd/linger RESERVE_MB=2048 MAX_SECONDS=3600 MEMINFO=/proc/meminfo NET_BASE=10.204
RESOLV_CONF=/etc/fork-fleet/resolv.conf IP_FORWARD=/proc/sys/net/ipv4/ip_forward SYSTEMD_RUN=systemd-run
SYSTEMCTL=systemctl OWN_ADDRESS_URLS=https://api.ipify.org,https://checkip.amazonaws.com ALLOW_PATHS=''
JOB_PATHS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/fork-fleet-job-paths.txt
setting='^(RUNNER_USERS|TEMPLATE|WORK_ROOT|RUN_DIR|HOOK|LINGER_DIR|RESERVE_MB|MAX_SECONDS|MEMINFO|NET_BASE|RESOLV_CONF|IP_FORWARD|SYSTEMD_RUN|SYSTEMCTL|OWN_ADDRESS_URLS|JOB_PATHS|ALLOW_PATHS)=([^[:space:]]*)$'
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
  [[ $line =~ $setting ]] || die "$conf: not a setting: ${line%%=*}"
  printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
done <"$conf"
[[ -n $RUNNER_USERS && -n $TEMPLATE ]] || die "$conf: RUNNER_USERS and TEMPLATE are required"
[[ $RESERVE_MB =~ ^[0-9]{1,7}$ && $MAX_SECONDS =~ ^[1-9][0-9]{0,6}$ ]] || die "$conf: RESERVE_MB and MAX_SECONDS are numbers"
{ [[ $NET_BASE =~ ^10\.(0|[1-9][0-9]{0,2})$ ]] && ((BASH_REMATCH[1] <= 255)); } || die "$conf: NET_BASE is 10.N (N up to 255)"
own_url='https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?'
[[ $OWN_ADDRESS_URLS =~ ^$own_url(,$own_url)*$ ]] || die "$conf: OWN_ADDRESS_URLS is a comma-separated list of https URLs"
PLAIN_PATH='(/[A-Za-z0-9._@+-]+)+'
[[ $ALLOW_PATHS =~ ^($PLAIN_PATH(,$PLAIN_PATH)*)?$ ]] || die "$conf: ALLOW_PATHS is a comma-separated list of plain paths"

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
  (umask 022 && mkdir -p -- "$WORK_ROOT")
  mkdir -p -- "$RUN_DIR"
  owned_safe "$WORK_ROOT"
  owned_safe "$RUN_DIR"
  chmod 0711 -- "$WORK_ROOT"
  chmod 0700 -- "$RUN_DIR"
  exec {lock}>"$RUN_DIR/launch.lock"
  flock -w 100 "$lock" || die "another launcher holds $RUN_DIR/launch.lock"
}

VERSION='^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$'

template_version() {
  local v
  [[ -f $TEMPLATE/.fork-fleet-version && ! -L $TEMPLATE/.fork-fleet-version ]] ||
    die "$TEMPLATE has no .fork-fleet-version: refresh the template (.github/fork-fleet.md, host step 2)"
  v=$(<"$TEMPLATE/.fork-fleet-version")
  [[ $v =~ $VERSION ]] || die "$TEMPLATE/.fork-fleet-version is not a version: refresh the template (host step 2)"
  printf '%s\n' "$v"
}

# Exit 0 when the template's runner is not older than NEWEST. A JIT runner older than the newest release updates
# itself in its copy before the job; this fleet does not rely on that, so it starts no runner until the owner
# refreshes the template.
ready() {
  local have i
  local -a a b
  [[ $1 =~ $VERSION ]] || die "ready: not a version: $1"
  owned_safe "$TEMPLATE"
  have=$(template_version)
  IFS=. read -ra a <<<"$have"
  IFS=. read -ra b <<<"$1"
  for i in 0 1 2; do
    ((10#${a[i]} > 10#${b[i]})) && return 0
    ((10#${a[i]} < 10#${b[i]})) &&
      die "the runner template is actions/runner $have, older than the newest release $1: no runner starts until" \
        "the owner refreshes the template (.github/fork-fleet.md, host step 2)"
  done
  return 0
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
# before (RUN_DIR/own-address), at most 4, newest first: a lookup that is down this time (each WAN link seen by
# another one) or an address that just changed opens nothing that was blocked. Under the launch lock.
merge_own() {
  local a old=''
  local -a all
  blocked=$1
  [[ ! -f $RUN_DIR/own-address ]] || old=$(<"$RUN_DIR/own-address")
  for a in $old; do
    if [[ $a =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ && " $blocked " != *" $a "* ]]; then blocked+=" $a"; fi
  done
  read -ra all <<<"$blocked"
  blocked=${all[*]:0:4}
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
refresh_net() {
  local own name blocked old=''
  own=$(own_address)
  setup
  [[ ! -f $RUN_DIR/own-address ]] || old=$(<"$RUN_DIR/own-address")
  merge_own "$own"
  if [[ $blocked == "$old" ]] && nft list table inet fork_fleet >/dev/null 2>&1; then return 0; fi
  table_up
  for name in $(active); do
    "$SYSTEMCTL" set-property --runtime -- "fork-fleet-$name.service" "IPAddressDeny=$blocked" ||
      warn "refresh-net: IPAddressDeny of fork-fleet-$name not changed (the table blocks $blocked)"
  done
}

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

# THE rule of what a job sees of this host's files, in one place (the per-OS walls read the same JOB_PATHS file): an
# allow list, never a list of what to hide, as a disk or share may be mounted anywhere (/mnt, /media, /srv, /data,
# /net). The unit's root is an empty directory of its own ($1, root's) holding only: each path of JOB_PATHS and
# ALLOW_PATHS that exists here, read-only (a symlink here, like /bin -> usr/bin, is the same symlink there); the hook
# and the two files it runs, read-only; its own copy ($2), read-write; the jobs' resolv.conf; and systemd's own /proc,
# /sys, a /dev of pseudo devices and memory file systems for /run, /tmp, /var/tmp and /dev/shm. Each path is one mount,
# not its submounts (norbind: WSL2's driver share under /usr/lib/wsl, a disk under /etc stay out), and no mount the
# host makes later reaches the unit (MountFlags=private: no mount of the unit receives the host's). Sets props_fs, and
# seen: the mount points check-net's probe allows (its own /proc, /sys, /dev and memory file systems are allowed there).
job_fs() {
  local root=$1 rw=$2 line p hook_dir=${HOOK%/*}
  local -a paths=() ro=()
  [[ $HOOK =~ ^$PLAIN_PATH$ && $rw =~ ^($PLAIN_PATH)?$ ]] || die "HOOK ($HOOK) and WORK_ROOT must be plain paths"
  owned_safe "$JOB_PATHS"
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    paths+=("$line")
  done <"$JOB_PATHS"
  # shellcheck disable=SC2206 # plain paths only, checked below
  paths+=(${ALLOW_PATHS//,/ })
  for p in "${paths[@]}"; do
    [[ $p =~ ^$PLAIN_PATH$ && /$p/ != */./* && /$p/ != */../* ]] || die "$JOB_PATHS or ALLOW_PATHS: not a plain path: $p"
    case $p/ in /proc/* | /sys/* | /dev/* | /run/* | /tmp/* | /var/tmp/* | /home/* | /root/*)
      die "$p: a job never sees it from this host (its own or none): not a path to allow" ;;
    esac
  done
  rm -rf --one-file-system -- "$root"
  mkdir -m 0755 -- "$root"
  seen='/'
  for p in "${paths[@]}"; do
    if [[ -L $p ]]; then
      mkdir -p -m 0755 -- "$root${p%/*}"
      ln -s -- "$(readlink -- "$p")" "$root$p"
    elif [[ -e $p ]]; then
      ro+=("$p:$p:norbind") seen+=" $p"
    fi
  done
  for p in "$HOOK" "$hook_dir/fork-fleet.py" "$hook_dir/fork-fleet.json"; do
    [[ $p == "$HOOK" || -e $p ]] || continue
    ro+=("$p:$p:norbind") seen+=" $p"
  done
  props_fs=(-p "RootDirectory=$root" -p MountAPIVFS=yes -p MountFlags=private -p "BindReadOnlyPaths=${ro[*]}")
  if [[ -n $rw ]]; then
    props_fs+=(-p "BindPaths=$rw:$rw:norbind")
    seen+=" $rw"
  fi
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
  local user=$1 resolv=$2 own=$3
  local -a props_fs
  job_fs "$4" "$5"
  seen+=" $resolv"
  props=(
    -p "NetworkNamespacePath=/run/netns/fork-fleet-$user"
    -p "IPAddressDeny=${PRIVATE4[*]} $own ::/0" -p IPAddressAllow=localhost
    "${props_fs[@]}"
    -p "TemporaryFileSystem=/run /dev/shm:size=1G,mode=1777 /tmp:size=1G,mode=1777 /var/tmp:size=1G,mode=1777"
    -p "BindReadOnlyPaths=$RESOLV_CONF:$resolv"
    -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p ProtectProc=invisible -p PrivateIPC=yes
    -p RemoveIPC=yes -p PrivateDevices=yes
    -p RestrictAddressFamilies=~AF_VSOCK
    -p "SystemCallFilter=~io_uring_setup:ENOSYS io_uring_enter:ENOSYS io_uring_register:ENOSYS"
    -p SystemCallArchitectures=native
  )
}

start() {
  local name=$1 dir env_file line key value user resolv own blocked seen
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
    [[ ! -e $TEMPLATE/$f ]] || die "$TEMPLATE/$f exists: the template must be an unconfigured runner (.github/fork-fleet.md)"
  done
  [[ -z $(find "$TEMPLATE" \( ! -uid "$EUID" -o \( ! -type l -perm /022 \) \) -print -quit) ]] ||
    die "$TEMPLATE has a file that is not this user's or that others may write"
  owned_safe "$HOOK"
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
  {
    printf 'ACTIONS_RUNNER_INPUT_JITCONFIG=%s\n' "${got[ACTIONS_RUNNER_INPUT_JITCONFIG]}"
    printf 'FORK_FLEET_RUN=%s\nFORK_FLEET_ATTEMPT=%s\n' "${got[FORK_FLEET_RUN]}" "${got[FORK_FLEET_ATTEMPT]}"
    printf 'HOME=%s\nTMPDIR=%s\n' "$dir/home" "$dir/tmp"
    printf 'ACTIONS_RUNNER_HOOK_JOB_STARTED=%s\n' "$HOOK"
  } >"$env_file"
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

# The walls' probe (python3 -I -c, arguments job or host, then the mount points job_fs allows). job: in the sandbox,
# as the runner user; a line per wall, exit 1 when one is open or could not be tried. host: as root, outside, the
# control: whether this host has what the walls close (where it has not, the refusal inside shows nothing; the walls
# are still set). Each 32-bit call is made in a child that may not dump core: the filter ends it with SIGSYS; another
# signal means this kernel has no such calls. i386: getpid (20) through int 0x80; x32: getpid (39 | 0x40000000).
# Then what the files show: device nodes besides the pseudo ones (a GPU, /dev/dxg); every mount point the unit has
# (the allowed ones, its own /proc, /sys and /dev, and nothing else); whether any of its mounts receives the host's
# mounts (mountinfo's master: or shared: tag: a disk mounted after the job started would show up in it); entries in
# /mnt or /media; and sockets in the places a desktop keeps them (WSLg's X11 and PulseAudio). Mount points are read
# from mountinfo, never looked at: a share that hangs cannot stop the probe.
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
line(not receive, 'later mounts: ' + (f'the host\'s reach {few(receive)}' if receive else
                                      'no mount inside receives the host\'s'))
line(not mnt, '/mnt and /media: ' + (f'hold {few(mnt)} inside' if mnt else 'nothing inside'))
line(whole and not socks, 'sockets: ' + (f'{few(socks)} inside' if socks else 'none in /tmp, /var/tmp, /run, /dev/shm'
                                         if whole else 'too many files to look through'))
sys.exit(bad)
EOF

check_net() {
  local url=$1 target host port user resolv own blocked seen rc=0
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
  /usr/bin/python3 -I -c "$WALLS" host "$seen"
  echo "check-net: as $user, in its namespace fork-fleet-$user"
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-net --uid "$user" "${props[@]}" \
    -p RuntimeMaxSec=120 -- /bin/bash --norc --noprofile -c "$PROBE" check-net "$url" "$blocked" "${pairs[@]}" </dev/null ||
    rc=$?
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-walls --uid "$user" "${props[@]}" \
    -p RuntimeMaxSec=60 -- /usr/bin/python3 -I -c "$WALLS" job "$seen" </dev/null || rc=$((rc | 2))
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
  refresh-net:1) refresh_net ;;
  check-net:*)
    (($# >= 3)) || die "usage: check-net URL HOST:PORT..."
    shift
    check_net "$@"
    ;;
  *) die "usage: start NAME | ready NEWEST | active | fits MB | stop NAME | prune | refresh-net | check-net URL HOST:PORT..." ;;
esac
