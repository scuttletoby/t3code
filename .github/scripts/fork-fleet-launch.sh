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
#   check-net URL HOST:PORT...
#                the proof of the network (.github/fork-fleet.md, host step 6): runs a probe in a runner's real
#                sandbox, as a free runner user. Exit 0 only when URL (public) answers and every HOST:PORT (services
#                this host reaches: its loopback, its LAN address, Tailscale) is closed from inside.
#
# The network: each runner user has a namespace of its own, fork-fleet-USER, so a job has its own loopback and reaches
# no service on this host's (nor an abstract socket). Its only link, ffh-UID (host side), goes to the public internet:
# the nft table inet fork_fleet refuses the host itself, RFC 1918, CGNAT (Tailscale), link-local and every IPv6 packet,
# and the unit's IPAddressDeny refuses the same ranges again, in the kernel, should the table ever go. Every start
# (re)makes the namespace and the table under the launch lock and fails closed: no runner starts when either fails.
#
# Settings: /etc/fork-fleet/launch.conf (or $FORK_FLEET_LAUNCH_CONF), KEY=value lines, owned by this user and
# writable by it alone. RUNNER_USERS (user names, comma-separated: at least the pool's slots) and TEMPLATE (an
# unconfigured actions/runner, owned by this user, on WORK_ROOT's filesystem) are required; WORK_ROOT, RUN_DIR, HOOK,
# LINGER_DIR, RESERVE_MB, MAX_SECONDS, MEMINFO, NET_BASE (10.N: the runners' 10.N.0.0/16, one /30 per user from its
# uid, unused by anything else on the host), RESOLV_CONF (the jobs' resolv.conf: public nameservers, as this host's
# own resolver is on its loopback), IP_FORWARD, SYSTEMD_RUN and SYSTEMCTL have defaults below.
set -euo pipefail
umask 077

die() {
  printf 'fork-fleet-launch: %s\n' "$*" >&2
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
SYSTEMCTL=systemctl
setting='^(RUNNER_USERS|TEMPLATE|WORK_ROOT|RUN_DIR|HOOK|LINGER_DIR|RESERVE_MB|MAX_SECONDS|MEMINFO|NET_BASE|RESOLV_CONF|IP_FORWARD|SYSTEMD_RUN|SYSTEMCTL)=([^[:space:]]*)$'
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
  [[ $line =~ $setting ]] || die "$conf: not a setting: ${line%%=*}"
  printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
done <"$conf"
[[ -n $RUNNER_USERS && -n $TEMPLATE ]] || die "$conf: RUNNER_USERS and TEMPLATE are required"
[[ $RESERVE_MB =~ ^[0-9]{1,7}$ && $MAX_SECONDS =~ ^[1-9][0-9]{0,6}$ ]] || die "$conf: RESERVE_MB and MAX_SECONDS are numbers"
{ [[ $NET_BASE =~ ^10\.(0|[1-9][0-9]{0,2})$ ]] && ((BASH_REMATCH[1] <= 255)); } || die "$conf: NET_BASE is 10.N (N up to 255)"

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

must() { "$@" || die "network: $* failed: no runner starts without its network (.github/fork-fleet.md, host step 1)"; }

# Makes what is missing of runner user $1's network and (re)applies the table; changes nothing that is right already
# and interrupts no connection (the table is replaced in one transaction; conntrack keeps the flows). Dies on any
# failure, so start never runs a unit without it. Under the launch lock: two launchers never apply it at once.
net_up() {
  local user=$1 uid ns host peer off net gw addr private
  uid=$(id -u -- "$user")
  ns=fork-fleet-$user host=ffh-$uid peer=ffp-$uid
  # Its /30 in NET_BASE.0.0/16: the host side .1, the namespace .2 (off is a multiple of 4, so both stay in it).
  off=$((uid % 16384 * 4))
  net=$NET_BASE.$((off / 256))
  gw=$net.$((off % 256 + 1)) addr=$net.$((off % 256 + 2))
  [[ $(<"$IP_FORWARD") == 1 ]] ||
    die "network: $IP_FORWARD is not 1: the runners would reach nothing (.github/fork-fleet.md, host step 1)"
  private=${PRIVATE4[*]}
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
    iifname "ffh-*" meta nfproto ipv6 counter drop
    oifname "ffh-*" ct state != { established, related } counter drop
  }
  chain postrouting {
    type nat hook postrouting priority 100; policy accept;
    ip saddr $NET_BASE.0.0/16 oifname != "ffh-*" masquerade
  }
}
EOF
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

# The sandbox of every runner unit, and of check-net's probe (which proves it): the props array. Its own network
# namespace, the private ranges refused again in the kernel, its own /run, /dev/shm, /tmp and /var/tmp (memory, gone
# with the unit), no /home, no other user's processes, its own IPC, and the system read-only.
sandbox() {
  local user=$1 resolv=$2
  props=(
    -p "NetworkNamespacePath=/run/netns/fork-fleet-$user"
    -p "IPAddressDeny=${PRIVATE4[*]} ::/0" -p IPAddressAllow=localhost
    -p "TemporaryFileSystem=/run /dev/shm:size=1G,mode=1777 /tmp:size=1G,mode=1777 /var/tmp:size=1G,mode=1777"
    -p "BindReadOnlyPaths=$RESOLV_CONF:$resolv"
    -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p ProtectProc=invisible -p PrivateIPC=yes
    -p RemoveIPC=yes
  )
}

start() {
  local name=$1 dir env_file line key value user resolv
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

  setup
  # cp -al hard-links the template's files: it cannot cross filesystems (and a full copy would be writable).
  [[ $(stat -c %d -- "$TEMPLATE") == "$(stat -c %d -- "$WORK_ROOT")" ]] ||
    die "TEMPLATE ($TEMPLATE) and WORK_ROOT ($WORK_ROOT) must be on one filesystem: cp -al hard-links the template"
  dir=$WORK_ROOT/$name
  env_file=$RUN_DIR/$name.env
  [[ ! -e $dir ]] || die "$dir exists: a runner of that name ran already (prune removes it once its unit is gone)"
  user=$(free_user)
  reset_user "$user"
  net_up "$user"
  sandbox "$user" "$resolv"
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

# Runs in the sandbox as the runner user: $1 the URL that must answer, then HOST PORT pairs that must be closed.
read -r -d '' PROBE <<'EOF' || true
url=$1 bad=0
shift
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

check_net() {
  local url=$1 target host port user resolv rc=0
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
  check_users
  resolv=$(resolv_target)
  setup
  user=$(free_user)
  reset_user "$user"
  net_up "$user"
  sandbox "$user" "$resolv"
  echo "check-net: as $user, in its namespace fork-fleet-$user"
  "$SYSTEMD_RUN" --quiet --wait --pipe --collect --unit fork-fleet-check-net --uid "$user" "${props[@]}" \
    -p RuntimeMaxSec=120 -- /bin/bash --norc --noprofile -c "$PROBE" check-net "$url" "${pairs[@]}" </dev/null || rc=$?
  ((rc == 0)) || die "check-net: the runners' network is NOT as it must be (see above): do not use this pool"
  echo "check-net: the runners reach $url and none of the $((${#pairs[@]} / 2)) services named"
}

prune() {
  setup
  prune_now
}

# Root's rm -rf here cannot be led astray (no directory in it swapped for a symlink mid-walk, as in /dev/shm before):
# it removes a copy, or an env file in root's 0700 RUN_DIR, only once no unit of that name is loaded. The unit's
# processes went with its cgroup, a runner user has no other way to run anything (no login, lingering, cron or at),
# and every other unit, of that user too, sees the copy read-only (ProtectSystem=strict, ReadWritePaths=its own copy).
prune_now() {
  local live entry name
  live=$(active)
  for entry in "$WORK_ROOT"/* "$RUN_DIR"/*.env; do
    [[ -e $entry && ! -L $entry ]] || continue
    name=${entry##*/}
    name=${name%.env}
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
  check-net:*)
    (($# >= 3)) || die "usage: check-net URL HOST:PORT..."
    shift
    check_net "$@"
    ;;
  *) die "usage: start NAME | ready NEWEST | active | fits MB | stop NAME | prune | check-net URL HOST:PORT..." ;;
esac
