# pc-agents: run T3's agents on a Windows PC, walled in a WSL2 distro of their own

A PC can take agent work from another T3 (a linked environment, with **Full access**). Full access runs as the PC
T3's user, so the link is not the wall: the distro is. This kit makes a fresh WSL2 distro into that wall and runs
T3 in it as `agent`, a user with no sudo, under a root-owned system unit. Everything that holds the wall is root's,
so the agent cannot undo it:

| Wall                    | What the kit does                                                                                                                                                                                                                                                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No Windows files        | `/etc/wsl.conf`: `[automount] enabled=false`, `mountFsTab=false`; the unit also hides `/mnt`, `/run/WSL`, `/tmp/.X11-unix`, `/usr/lib/wsl` and `/run/tailscale` (the Tailscale daemon's socket)                                                                                                                                                    |
| No Windows programs     | `[interop] enabled=false`, `appendWindowsPath=false`; WSL's binfmt handler masked                                                                                                                                                                                                                                                                  |
| No way out of the user  | one login user (`agent`), in no group but its own, no sudo rule, login shell `/usr/sbin/nologin`; the distro's default user is `root`, not `agent`; cron, at, ssh and the agent's systemd user manager (`user@<uid>.service`) masked, so no login, linger or user unit of the agent runs anything outside T3's unit                                |
| A sandboxed unit        | `t3code-agents.service` (system unit, `User=agent`): `ProtectSystem=strict`, only `/home/agent` writable, no `AF_VSOCK` (Hyper-V sockets to Windows), io_uring off, no namespaces, `PrivateDevices`, `MemoryMax`, `CPUQuota`                                                                                                                       |
| No LAN, no Windows host | twice, nftables for the agent's uid and the unit's `IPAddressDeny=`: private, link-local, CGNAT and multicast ranges blocked but for `allow` and the linking `peers`, so the private addresses of the Windows host, the home network and the tailnet are out of reach; public addresses go through (the home's own public address too: see Limits) |
| Localhost only for T3   | the unit's `SocketBindDeny=any` with `SocketBindAllow=` T3's port and `devPorts`, and nftables dropping the agent's other localhost traffic: the agent listens on, and reaches on `localhost`, only T3's port and `devPorts`, so servers of other distros on `localhost` are out of reach                                                          |
| Locked T3 settings      | `/etc/t3code/policy.json` (managed policy): linked environments on, automatic updates off, Connect, webhook tasks and browser cookie import off, `maxRuntimeMode` and your `lockedSettings` locked; T3 itself is root's (under `/opt/pc-agents/t3`)                                                                                                |
| Checked, not trusted    | `pc-agents check` runs before every start of T3 (the unit refuses to start on a failed wall) and every 2 minutes (a failed wall stops T3; a check that cannot run or crashes stops it too)                                                                                                                                                         |

`pc-agents check` is the fleet's WSL2 walls check (`.github/scripts/fork-fleet-walls.py` with
`fork-fleet-walls/wsl2`, from the same release, not a copy) plus this kit's own walls:

- every file above is root's, mode 644 and exactly what the kit writes (and recorded in the root-owned
  `/etc/pc-agents/installed.tsv`);
- nothing overrides the kit's units: no drop-in or other copy in any folder systemd reads (`system.control`,
  `system.attached`, the generators, `/run`, `/usr/lib`, prefix drop-ins like `t3code-.service.d`), and
  `systemctl show` agrees (the kit's file, no drop-in, no pending reload, T3 and the timer enabled);
- the masks are in place, no user lingers, the agent has no systemd user units, the timer is running;
- `agent` is the only login user, its login shell is `/usr/sbin/nologin`, and no sudo rule (`/etc/sudoers`,
  `/etc/sudoers.d`) names it, its group or uid, or every user;
- the kit's code (`/opt/pc-agents`, the release folder it runs) and T3's runtime are root's, and T3's `t3` is the one
  setup installed (its hash is in the record);
- no Windows drive is mounted;
- the loaded firewall table is the one from the kit's `firewall.nft`: `pc-agents load-firewall` saves the file's hash
  and what the kernel then lists, and any later change, or an older file left loaded, differs;
- the default gateway (the Windows host) is in a blocked range, and no `allow` or `peers` entry lets it through.

The fleet's `mounts`, `sockets` and `userns` walls are
about the CI launcher's job units, so they are not reported here; the unit's own sandbox is checked instead (it must
be the kit's file, byte for byte, with no drop-in).

Linux (WSL2, systemd, Debian or Ubuntu) only. Python 3 standard library only. This folder has the walls and their
check so far; `pc-agents setup`, which writes them into a fresh distro, and its owner steps are still to come.

## Config

The kit reads the `pc-agents` section of the machine file `/etc/fork-kit/machine.json` (root-owned, see
[common](../common/README.md)). Example (made-up values):

```json
{
  "version": 1,
  "pc-agents": {
    "organization": "Example home lab",
    "t3Version": "0.0.30",
    "t3ReleaseBaseUrl": "https://github.com/example/t3code/releases/download",
    "port": 3773,
    "maxRuntimeMode": "full-access",
    "lockedSettings": { "agentSlots.enabled": true, "agentSlots.maxPerMachine": 2 },
    "memoryMax": "8G",
    "cpuQuota": "400%",
    "nameservers": ["1.1.1.1", "9.9.9.9"],
    "peers": ["100.101.102.103/32"],
    "tailscaleTag": "tag:pc-agents"
  }
}
```

| Key                     | What                                                                                                              |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `organization`          | the name the managed policy shows in T3's settings                                                                |
| `t3Version`             | the exact T3 version to run (the same as the T3 that links to this PC); installed with `scripts/install.sh`       |
| `t3ReleaseBaseUrl`      | where that version's release files are (`T3CODE_RELEASE_BASE_URL` of `scripts/install.sh`)                        |
| `port`                  | T3's port (`T3CODE_PORT`); the Tailscale ACL opens it to the linking T3 only                                      |
| `agentUid`              | the `agent` user's uid (default 1100; not 1000, the first user of every other distro, see Limits)                 |
| `maxRuntimeMode`        | the highest runtime mode T3 allows (`full-access` for a linked PC)                                                |
| `lockedSettings`        | more T3 settings to lock, `{path: value}`: the child ceiling (`agentSlots.…`), see below                          |
| `memoryMax`, `cpuQuota` | the unit's `MemoryMax` and `CPUQuota`                                                                             |
| `nameservers`           | 1 to 3 public DNS servers for `/etc/resolv.conf` (WSL's own one is on the blocked Windows host)                   |
| `allow`                 | optional: private networks the agent may still reach (CIDR list); empty by default; never the Windows host        |
| `devPorts`              | optional: the TCP ports the agent's dev servers may listen on and be reached on, `LOW-HIGH` (default `4100-4199`) |
| `peers`                 | the linking T3's Tailscale address(es) (CIDR list, 1 to 16): the only private addresses the unit talks to         |
| `tailscaleTag`          | the tag this PC's Tailscale node gets (`tag:name`)                                                                |

Every T3 setting can be locked through `lockedSettings`, the fork's own settings (`agentSlots`, `usagePacingGate`,
`usagePacing`, `waitFor`, ...) too: the managed policy locks any key of the server settings, upstream's or the fork's
(`apps/server/src/policy/managedSettings.ts`), and a test proves it for the fork's keys. Lock one field per dotted
path (`agentSlots.maxPerMachine`), or give a whole setting (`agentSlots`) or one record entry
(`providerInstances.codex-work`) as an object holding every field: T3 takes an object only as a complete value. The kit
refuses what is cheap to tell (a field of a provider instance, an object deeper than a record entry, the kit's own
two settings, a blank `organization`); T3 refuses the rest, like a field inside a setting that can be empty (lock the
whole setting), says why in its log (`journalctl -u t3code-agents`) and does not start. T3 exits 1 on a policy it
cannot apply, the same code as any other failure, so the unit cannot tell it apart (`RestartPreventExitStatus=`);
it gives up after 5 starts in 10 minutes instead (`StartLimitBurst=5`).

A wall that fails stops T3, and nothing starts it again by itself: fix what `pc-agents check` names (or run
`pc-agents setup` again), then start it as root: `systemctl start t3code-agents.service` (its own start runs the
check first and refuses on a failed wall).

## Limits

- **One VM for all distros.** WSL2 runs every distro in one VM: one kernel, one network namespace, one memory cap.
  The firewall table is therefore global and matches the agent's uid in every distro (keep `agentUid` apart from
  the other distros' users); `localhost` is shared, so a server another distro runs on localhost is reachable on
  T3's port or `devPorts` (the agent's other localhost traffic is dropped). **Run no localhost servers in other
  distros** of this PC on those ports. What the agent serves on those ports is forwarded to Windows' `localhost` by
  WSL: open it in a browser only as you would an unknown site (a page on `localhost` shares cookies with every other
  `localhost` page, whatever the port).
  Global kernel switches (user namespaces, sysctls) are left alone: the unit closes them for T3 instead. Another
  distro's root can drop or change the global nft table: the check sees it within 2 minutes and stops T3, and the
  unit's own `IPAddressDeny=` (per cgroup, not global) holds meanwhile. Abstract unix sockets are shared by every
  distro too (the unit cannot hide them; `RestrictAddressFamilies` keeps `AF_UNIX` for T3 itself), so a program in
  another distro listening on one is reachable.
- **The policy binds an honest T3:** it is T3 that applies the locks. The kit makes sure the T3 that runs is the one
  root installed (root-owned under `/opt/pc-agents/t3`, its hash recorded and checked), not one the agent could
  change. An agent can still start its own programs (even its own copy of T3) inside the unit: those ignore the
  policy, but stay inside every other wall.
- **Public internet is open** to the agent: the firewall blocks private ranges, not the public internet (GitHub's
  and Anthropic's addresses are many and change). Add an outbound proxy if that matters. That includes the home's
  own public address: a router that loops it back (hairpin NAT) lets the agent reach its admin page, if the router
  serves it there, and every port-forwarded device of the home. Forward no ports to devices the agent must not
  reach, and keep the router's admin page off the public side.
- **Servers in the agent's dev range only:** a test or dev server that listens on a port outside `devPorts` cannot
  be reached over `localhost` (port 0, any free port, may be bound, but the agent cannot connect to it there).
  Point them at `devPorts`, or widen it.
- **Shared with root's programs in this distro:** the system D-Bus (`/run/dbus`, its own access rules decide),
  `/dev/shm` (mode 1777; others' files keep their modes), and `NETLINK_SOCK_DIAG`, which lists every distro's
  sockets (addresses and ports, not their data).
- **DNS** goes to the public `nameservers` only. A `systemd-resolved` stub (`127.0.0.53`), which may resolve LAN
  names through Windows, is out of reach: the agent's localhost traffic outside T3's port and `devPorts` is dropped.
- **Terminals:** what the agent prints in a terminal can use escape codes, such as OSC 52, which some terminals turn
  into a write to the Windows clipboard. Do not paste from the clipboard blindly after watching agent output.
- **No namespaces in the unit** (`RestrictNamespaces=yes`): provider sandboxes that need them (bubblewrap) do not
  run; the distro is the sandbox, so use full access.
- **No code hash manifest yet:** the kit's code is checked for root ownership and its written files byte for byte,
  but the code files are not hashed (T3's `t3` is).

## Tests

```
python3 -I -B fork-kit/pc-agents/tests/check.test.py
```

A fake distro in a temporary folder (`PC_AGENTS_ROOT`, refused for root; `tests/fake_distro.py`), with fake `nft` and
`systemctl` (it keeps which units are enabled and running): every wall passes on a walled distro, and each one
fails on its own when its part is taken away.
`apps/server/src/policy/managedSettings.forkKeys.test.ts` proves the managed policy locks the fork's settings.
