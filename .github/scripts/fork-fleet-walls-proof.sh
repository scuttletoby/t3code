#!/usr/bin/env bash
# Fork only: proves the launcher's walls (fork-fleet-launch.sh check-net) on a real host, as root, with throwaway
# users, before that host joins a pool: a Linux machine, or a Windows PC's WSL2 distribution (systemd on). It changes
# nothing it does not undo: its own runner users, temp dir and listener; the nft table inet fork_fleet, the users'
# namespaces and links (it refuses a host that has them already: a pool runs there); Docker's two DOCKER-USER rules
# when it added them; ip_forward, set back. Runbook: .github/fork-fleet.md, "Walls (every pool)".
#
#   sudo bash .github/scripts/fork-fleet-walls-proof.sh [HOST:PORT...]
#
# HOST:PORT: more services this host reaches that a job must not (on WSL2: one of Windows's, if any answers). It
# adds this host's loopback and its own addresses on a port it listens on, and the default gateway's first answering
# port of 445, 135 and 53 (on WSL2's NAT network that gateway is Windows). Exit 0 only when check-net passes.
set -euo pipefail
die() {
  printf 'fork-fleet-walls-proof: %s\n' "$*" >&2
  exit 1
}
((EUID == 0)) || die "run it as root (sudo): it makes throwaway users, a network namespace and the nft table"
[[ -d /run/systemd/system ]] || die "systemd is not running here (WSL2: [boot] systemd=true in /etc/wsl.conf, then wsl --shutdown)"
for c in nft ip curl python3 systemd-run useradd userdel; do command -v "$c" >/dev/null || die "$c is missing"; done
launch=$(dirname -- "$(readlink -f -- "$0")")/fork-fleet-launch.sh
[[ -f $launch ]] || die "no $launch"
users=(fork-fleet-p1 fork-fleet-p2)
! nft list table inet fork_fleet >/dev/null 2>&1 || die "this host has the table inet fork_fleet: a pool runs here (or a run of this proof was killed: then nft delete table inet fork_fleet)"
[[ -z $(systemctl list-units --all --plain --no-legend 'fork-fleet-*') ]] || die "fork-fleet-* units are loaded here"
for u in "${users[@]}"; do
  ! id -u -- "$u" >/dev/null 2>&1 || die "user $u exists: a run of this proof stopped half-way? userdel -r $u"
  [[ ! -e /run/netns/$u ]] || die "namespace $u exists: ip netns del $u"
done
docker=() made=() tmp='' listener='' forward=$(</proc/sys/net/ipv4/ip_forward)
if iptables -w 60 -S DOCKER-USER >/dev/null 2>&1; then
  for r in "-o ffh-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" "-i ffh-+ -j ACCEPT"; do
    # shellcheck disable=SC2086 # $r is split into iptables' arguments on purpose
    iptables -w 60 -C DOCKER-USER $r 2>/dev/null || docker+=("$r")
  done
fi

cleanup() {
  local u uid r
  [[ -z $listener ]] || kill "$listener" 2>/dev/null || true
  systemctl stop fork-fleet-check-net.service fork-fleet-check-walls.service 2>/dev/null || true
  for u in "${made[@]}"; do
    uid=$(id -u -- "$u")
    ip netns del "fork-fleet-$u" 2>/dev/null || true
    ip link del "ffh-$uid" 2>/dev/null || true
    pkill -KILL -u "$uid" 2>/dev/null || true
    userdel -- "$u" || echo "fork-fleet-walls-proof: userdel $u failed: remove it by hand" >&2
  done
  nft delete table inet fork_fleet 2>/dev/null || true
  # shellcheck disable=SC2086
  for r in "${docker[@]}"; do iptables -w 60 -D DOCKER-USER $r 2>/dev/null || true; done
  [[ -z $tmp ]] || rm -rf --one-file-system -- "${tmp:?}"
  echo "$forward" >/proc/sys/net/ipv4/ip_forward
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "kernel $(uname -r) $(uname -m); $(systemctl --version | head -n1); $(nft --version)"
if command -v wslinfo >/dev/null; then echo "WSL networking: $(wslinfo --networking-mode 2>&1)"; fi
for u in "${users[@]}"; do
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin -- "$u"
  made+=("$u")
done
tmp=$(mktemp -d /var/tmp/fork-fleet-walls-proof.XXXXXX)
chmod 0711 "$tmp"
mkdir "$tmp/template"
(umask 022 && printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >"$tmp/resolv.conf")
printf '%s\n' "RUNNER_USERS=fork-fleet-p1,fork-fleet-p2" "TEMPLATE=$tmp/template" "WORK_ROOT=$tmp/runners" \
  "RUN_DIR=$tmp/run" "RESOLV_CONF=$tmp/resolv.conf" >"$tmp/launch.conf"
echo 1 >/proc/sys/net/ipv4/ip_forward

python3 -I -c 'import socket, sys
s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(64)
print(s.getsockname()[1], flush=True); sys.stdout.close()
while True: s.accept()[0].close()' >"$tmp/port" &
listener=$!
for _ in {1..50}; do [[ -s $tmp/port ]] && break; sleep 0.1; done
port=$(<"$tmp/port")
[[ $port =~ ^[0-9]+$ ]] || die "the listener did not start"
targets=("127.0.0.1:$port")
for a in $(ip -4 -o addr show scope global | awk '{ sub("/.*", "", $4); print $4 }'); do targets+=("$a:$port"); done
gw=$(ip -4 route show default | awk '{ print $3; exit }')
for p in 445 135 53; do
  [[ -n $gw ]] || break
  # shellcheck disable=SC2016
  if timeout 2 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$gw" "$p" 2>/dev/null; then targets+=("$gw:$p") && break; fi
done
targets+=("$@")
echo "must be closed inside: ${targets[*]}"
rc=0
FORK_FLEET_LAUNCH_CONF=$tmp/launch.conf bash "$launch" check-net https://github.com "${targets[@]}" || rc=$?
((rc == 0)) && echo "fork-fleet-walls-proof: PASSED" || echo "fork-fleet-walls-proof: FAILED (exit $rc)"
exit "$rc"
