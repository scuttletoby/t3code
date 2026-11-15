#!/usr/bin/env bash
# Fork only: proves the launcher's walls (fork-fleet-launch.sh check-net) on a real host, as root, with throwaway
# users, before that host joins a pool: a Linux machine, or a Windows PC's WSL2 distribution (systemd on). It changes
# nothing it does not undo: its own runner users, temp dir and listener; the nft table inet fork_fleet, the users'
# namespaces and links; Docker's two DOCKER-USER rules when it added them; ip_forward, set back. It refuses a host
# that has any part of a pool (its files, units, users, table, namespaces or links, running or not): that table and
# those links are the pool's too. Runbook: .github/fork-fleet.md, "Walls (every pool)".
#
#   sudo bash .github/scripts/fork-fleet-walls-proof.sh [HOST:PORT...]
#
# HOST:PORT: more services this host reaches that a job must not (on WSL2: one of Windows's, if any answers). It
# adds this host's loopback and its own addresses on a port it listens on, and the default gateway's first answering
# port of 445, 135 and 53 (on WSL2's NAT network that gateway is Windows). Exit 0 only when check-net passes, each of
# the two network blocks applies by itself, and every cleanup step worked.
set -euo pipefail
die() {
  printf 'fork-fleet-walls-proof: %s\n' "$*" >&2
  exit 1
}
((EUID == 0)) || die "run it as root (sudo): it makes throwaway users, a network namespace and the nft table"
[[ -d /run/systemd/system ]] || die "systemd is not running here (WSL2: [boot] systemd=true in /etc/wsl.conf, then wsl --shutdown)"
for c in nft ip curl python3 systemd-run useradd userdel getent; do command -v "$c" >/dev/null || die "$c is missing"; done
here=$(dirname -- "$(readlink -f -- "$0")")
launch=$here/fork-fleet-launch.sh
[[ -f $launch && -f $here/fork-fleet-job-paths.txt ]] || die "no $launch or its fork-fleet-job-paths.txt"
users=(fork-fleet-p1 fork-fleet-p2)

# A pool's parts, whether it runs now or not (after a reboot a pool has no table and no unit until its next job).
pool=()
for p in /etc/fork-fleet* /opt/fork-fleet* /var/lib/fork-fleet* /run/fork-fleet*; do [[ ! -e $p ]] || pool+=("$p"); done
while read -r unit _; do pool+=("unit $unit"); done < <(systemctl list-unit-files --no-legend --plain \
  'fork-fleet*' 't3code-fleet*' 2>/dev/null; systemctl list-units --all --no-legend --plain 'fork-fleet*' 't3code-fleet*')
while IFS=: read -r name _; do pool+=("user $name"); done < <(getent passwd | grep -E '^fork-(fleet|runner)-' || true)
! nft list table inet fork_fleet >/dev/null 2>&1 || pool+=("nft table inet fork_fleet")
while read -r ns _; do pool+=("namespace $ns"); done < <(ip netns list | grep -E '^fork-fleet-' || true)
while read -r link; do pool+=("link $link"); done < <(ip -o link show | awk -F': ' '$2 ~ /^ffh-/ { print $2 }')
while read -r pid cmd; do pool+=("process $pid $cmd"); done < <(pgrep -af 'fork-fleet(\.py|-launch\.sh)' || true)
((${#pool[@]} == 0)) || die "this host has a pool's parts (a pool runs here, or did, or a run of this proof stopped" \
  "half-way: undo them by hand first): ${pool[*]}"
for u in "${users[@]}"; do [[ ! -e /run/netns/fork-fleet-$u ]] || die "namespace fork-fleet-$u exists"; done

docker=() made=() failed=() tmp='' listener='' forward=$(</proc/sys/net/ipv4/ip_forward)
if iptables -w 60 -S DOCKER-USER >/dev/null 2>&1; then
  for r in "-o ffh-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" "-i ffh-+ -j ACCEPT"; do
    # shellcheck disable=SC2086 # $r is split into iptables' arguments on purpose
    iptables -w 60 -C DOCKER-USER $r 2>/dev/null || docker+=("$r")
  done
fi

# Every cleanup step runs, whichever failed before it; the failed ones are named at the end.
step() { "$@" || failed+=("$*"); }
kill_all() { pkill -KILL -u "$1" || (($? == 1)); }
cleanup() {
  local u uid r unit
  set +e
  [[ -z $listener ]] || step kill "$listener"
  while read -r unit _; do step systemctl stop -- "$unit"; done < <(systemctl list-units --no-legend --plain 'fork-fleet-*')
  for u in "${made[@]}"; do
    if ! uid=$(id -u -- "$u"); then failed+=("id $u"); continue; fi
    [[ ! -e /run/netns/fork-fleet-$u ]] || step ip netns del "fork-fleet-$u"
    ! ip link show "ffh-$uid" >/dev/null 2>&1 || step ip link del "ffh-$uid"
    step kill_all "$uid"
    step userdel -- "$u"
  done
  ! nft list table inet fork_fleet >/dev/null 2>&1 || step nft delete table inet fork_fleet
  # Only the rules the launcher added (a run that stopped before it did leaves none): no false failed step.
  # shellcheck disable=SC2086
  for r in "${docker[@]}"; do ! iptables -w 60 -C DOCKER-USER $r 2>/dev/null || step iptables -w 60 -D DOCKER-USER $r; done
  [[ -z $tmp ]] || step rm -rf --one-file-system -- "${tmp:?}"
  step sysctl -qw "net.ipv4.ip_forward=$forward"
  if ((${#failed[@]})); then
    printf 'fork-fleet-walls-proof: cleanup steps that failed (undo them by hand): %s\n' "${failed[@]}" >&2
    exit 1
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "kernel $(uname -r) $(uname -m); $(systemctl --version | head -n1); $(nft --version)"
cg=$(stat -fc %T /sys/fs/cgroup)
[[ $cg == cgroup2fs ]] && echo "cgroup v2 (unified): IPAddressDeny can apply" ||
  echo "cgroup $cg (v1 or hybrid): IPAddressDeny cannot apply here; only the nft table blocks"
if command -v wslinfo >/dev/null; then echo "WSL networking: $(wslinfo --networking-mode 2>&1)"; fi
for u in "${users[@]}"; do
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin -- "$u"
  made+=("$u")
done
tmp=$(mktemp -d /var/tmp/fork-fleet-walls-proof.XXXXXX)
chmod 0711 "$tmp"
mkdir "$tmp/template"
(
  umask 022
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >"$tmp/resolv.conf"
  cp -- "$here/fork-fleet-job-paths.txt" "$tmp/job-paths.txt"
  printf '#!/bin/sh\n' >"$tmp/hook.sh"
)
printf '%s\n' "RUNNER_USERS=fork-fleet-p1,fork-fleet-p2" "TEMPLATE=$tmp/template" "WORK_ROOT=$tmp/runners" \
  "RUN_DIR=$tmp/run" "RESOLV_CONF=$tmp/resolv.conf" "JOB_PATHS=$tmp/job-paths.txt" "HOOK=$tmp/hook.sh" \
  >"$tmp/launch.conf"
chmod 0600 "$tmp/launch.conf"
sysctl -qw net.ipv4.ip_forward=1

python3 -I -c 'import socket, sys
s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(64)
print(s.getsockname()[1], flush=True); sys.stdout.close()
while True: s.accept()[0].close()' >"$tmp/port" &
listener=$!
for _ in {1..50}; do [[ -s $tmp/port ]] && break; sleep 0.1; done
port=$(<"$tmp/port")
[[ $port =~ ^[0-9]+$ ]] || die "the listener did not start"
targets=("127.0.0.1:$port")
own=$(ip -4 -o addr show scope global | awk '{ sub("/.*", "", $4); print $4 }')
for a in $own; do targets+=("$a:$port"); done
gw=$(ip -4 route show default | awk '{ print $3; exit }')
for p in 445 135 53; do
  [[ -n $gw ]] || break
  # shellcheck disable=SC2016
  if timeout 2 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$gw" "$p" 2>/dev/null; then targets+=("$gw:$p") && break; fi
done
targets+=("$@")
echo "must be closed inside: ${targets[*]}"
rc=0
# check-net: the network, the walls and the files (only the allowed mounts, none of the host's later, the pseudo
# devices only, no GPU, no socket of the host's: WSLg's X11 and PulseAudio ones, WSL's interop ones in /run).
FORK_FLEET_LAUNCH_CONF=$tmp/launch.conf bash "$launch" check-net https://github.com "${targets[@]}" || rc=$?

# Each of the two network blocks by itself (check-net proves the two together): the nft table, from the runner's
# namespace without a unit; the unit's IPAddressDeny, in a unit outside the namespace.
connect='import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close(); print("open")
except OSError as e:
    print(type(e).__name__)'
first=${own%%$'\n'*}
if [[ -n $first && -e /run/netns/fork-fleet-${users[0]} ]]; then
  got=$(ip netns exec "fork-fleet-${users[0]}" python3 -I -c "$connect" "$first" "$port")
  [[ $got != open ]] && echo "ok    the nft table alone refuses this host's $first:$port from a runner's namespace ($got)" ||
    { echo "FAIL  the nft table alone lets a runner's namespace reach $first:$port"; rc=1; }
else
  echo "FAIL  the nft table alone: not tried (no address of this host's, or no namespace)"; rc=1
fi
got=$(systemd-run --quiet --wait --pipe --collect -p IPAddressDeny=any -- /usr/bin/python3 -I -c "$connect" \
  127.0.0.1 "$port")
[[ $got != open ]] && echo "ok    IPAddressDeny alone refuses 127.0.0.1:$port in a unit ($got)" ||
  { echo "FAIL  IPAddressDeny does not apply here (cgroup $cg): a unit reaches 127.0.0.1:$port"; rc=1; }
((rc == 0)) && echo "fork-fleet-walls-proof: PASSED" || echo "fork-fleet-walls-proof: FAILED (exit $rc)"
exit "$rc"
