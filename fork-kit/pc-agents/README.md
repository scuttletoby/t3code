# pc-agents: run T3's agents on a Windows PC, walled in a WSL2 distro of their own

A PC can take agent work from another T3 (a linked environment, with **Full access**). Full access runs as the PC
T3's user, so the link is not the wall: the distro is. This kit makes a fresh WSL2 distro into that wall and runs
T3 in it as `agent`, a user with no sudo, under a root-owned system unit. Everything that holds the wall is root's,
so the agent cannot undo it:

| Wall                    | What the kit does                                                                                                                                                                                                                                                                                |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| No Windows files        | `/etc/wsl.conf`: `[automount] enabled=false`, `mountFsTab=false`; the unit also hides `/mnt`, `/run/WSL`, `/tmp/.X11-unix`, `/usr/lib/wsl`                                                                                                                                                       |
| No Windows programs     | `[interop] enabled=false`, `appendWindowsPath=false`; WSL's binfmt handler masked                                                                                                                                                                                                                |
| No way out of the user  | one login user (`agent`), in no group but its own; cron, at and ssh masked                                                                                                                                                                                                                       |
| A sandboxed unit        | `t3code-agents.service` (system unit, `User=agent`): `ProtectSystem=strict`, only `/home/agent` writable, no `AF_VSOCK` (Hyper-V sockets to Windows), io_uring off, no namespaces, `PrivateDevices`, `MemoryMax`, `CPUQuota`                                                                     |
| No LAN, no Windows host | nftables, for the agent's uid only: private, link-local, CGNAT (Tailscale) and multicast ranges dropped, so the Windows host (the NAT gateway), the home network and the tailnet are out of reach. Public addresses (GitHub, Anthropic, package mirrors) and replies to the linked T3 go through |
| Locked T3 settings      | `/etc/t3code/policy.json` (managed policy): linked environments on, automatic updates off, Connect, webhook tasks and browser cookie import off, `maxRuntimeMode` and your `lockedSettings` locked                                                                                               |
| Checked, not trusted    | `pc-agents check` runs before every start of T3 (the unit refuses to start on a failed wall) and daily (a failed wall stops T3)                                                                                                                                                                  |

`pc-agents check` is the fleet's WSL2 walls check (`.github/scripts/fork-fleet-walls.py` with
`fork-fleet-walls/wsl2`, from the same release, not a copy) plus this kit's own walls: every file above is root's,
mode 644 and exactly what the kit writes, nothing overrides the unit (no drop-in), the masks are in place, `agent` is
the only login user, the kit's code is root's, no Windows drive is mounted, the firewall table is loaded, and the
default gateway (the Windows host) is in a blocked range. The fleet's `mounts`, `sockets` and `userns` walls are
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
    "tailscaleTag": "tag:pc-agents"
  }
}
```

| Key                     | What                                                                                                        |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| `organization`          | the name the managed policy shows in T3's settings                                                          |
| `t3Version`             | the exact T3 version to run (the same as the T3 that links to this PC); installed with `scripts/install.sh` |
| `t3ReleaseBaseUrl`      | where that version's release files are (`T3CODE_RELEASE_BASE_URL` of `scripts/install.sh`)                  |
| `port`                  | T3's port (`T3CODE_PORT`); the Tailscale ACL opens it to the linking T3 only                                |
| `agentUid`              | the `agent` user's uid (default 1100; not 1000, the first user of every other distro, see Limits)           |
| `maxRuntimeMode`        | the highest runtime mode T3 allows (`full-access` for a linked PC)                                          |
| `lockedSettings`        | more T3 settings to lock, `{path: value}`, one field per dotted path: the child ceiling (`agentSlots.…`)    |
| `memoryMax`, `cpuQuota` | the unit's `MemoryMax` and `CPUQuota`                                                                       |
| `nameservers`           | 1 to 3 public DNS servers for `/etc/resolv.conf` (WSL's own one is on the blocked Windows host)             |
| `allow`                 | optional: private networks the agent may still reach (CIDR list); empty by default                          |
| `tailscaleTag`          | the tag this PC's Tailscale node gets (`tag:name`)                                                          |

Every T3 setting can be locked through `lockedSettings`, the fork's own settings (`agentSlots`, `usagePacingGate`,
`usagePacing`, `waitFor`, ...) too: the managed policy locks any key of the server settings, upstream's or the fork's
(`apps/server/src/policy/managedSettings.ts`), and a test proves it for the fork's keys. Lock one field per dotted
path (`agentSlots.maxPerMachine`): T3 takes an object only as a setting's complete value and refuses to start on a
policy it cannot apply, so the kit refuses object values. A field inside a setting that can be empty must be locked
as the whole setting; T3 says so in its log (`journalctl -u t3code-agents`) and does not start.

## Limits

- **One VM for all distros.** WSL2 runs every distro in one VM: one kernel, one network namespace, one memory cap.
  The firewall table is therefore global and matches the agent's uid in every distro (keep `agentUid` apart from
  the other distros' users); `localhost` is shared, so a server another distro runs on localhost is reachable.
  Global kernel switches (user namespaces, sysctls) are left alone: the unit closes them for T3 instead.
- **Public internet is open** to the agent: the firewall blocks private ranges, not the public internet (GitHub's
  and Anthropic's addresses are many and change). Add an outbound proxy if that matters.
- **No namespaces in the unit** (`RestrictNamespaces=yes`): provider sandboxes that need them (bubblewrap) do not
  run; the distro is the sandbox, so use full access.
- **No code hash manifest yet:** the kit's code is checked for root ownership and its written files byte for byte,
  but the code files are not hashed.

## Tests

```
python3 -I -B fork-kit/pc-agents/tests/check.test.py
```

A fake distro in a temporary folder (`PC_AGENTS_ROOT`, refused for root; `tests/fake_distro.py`), with fake `nft` and
`systemctl`: every wall passes on a walled distro, and each one fails on its own when its part is taken away.
`apps/server/src/policy/managedSettings.forkKeys.test.ts` proves the managed policy locks the fork's settings.
