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

Linux (WSL2, systemd, Debian or Ubuntu) only. Python 3 standard library only.

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

## Owner steps (once per PC)

Commands are for Windows PowerShell unless a step says otherwise. `<distro>` is a name of your choice (for example
`t3-agents`); `<fork>` is the fork's `OWNER/NAME`, `<sha>` a commit of its `ci-release` branch.

1. **Make a fresh distro** (not the CI one, not your own): `wsl --install -d Ubuntu-24.04 --name <distro> --no-launch`.
2. **Write the machine file** inside it as root: `wsl -d <distro> -u root`, then
   `mkdir -p /etc/fork-kit && nano /etc/fork-kit/machine.json` (the example above, your values), and leave with `exit`.
3. **Run the setup** as root:
   `wsl -d <distro> -u root -- sh -c 'curl -fsSL https://raw.githubusercontent.com/<fork>/<sha>/fork-kit/pc-agents/bootstrap.sh -o /root/pc-agents-bootstrap.sh && sh /root/pc-agents-bootstrap.sh --repo <fork> --sha <sha>'`.
   The first run writes everything, then says a wall does not hold (Windows' drives are still mounted) and does
   not start T3.
4. **Restart the distro** so `wsl.conf` applies: `wsl --terminate <distro>`.
5. **Run the setup again** (the same command as step 3). Now every wall holds and T3 starts. It prints the next step.
6. **Join Tailscale** with the tag (as root in the distro): install it
   (`curl -fsSL https://tailscale.com/install.sh | sh`), then `tailscale up --advertise-tags=<tag>` and open the
   link it prints in a browser on any machine. The ACL is below.
7. **Log in to Claude as the agent:** install Claude Code for `agent` and log in
   (`wsl -d <distro> -u agent`, then the install command from Claude Code's docs, then `claude`); open the link it
   prints in your browser. Codex: the same with `codex login`.
8. **Log in to GitHub as the agent:** install `gh` (as root, from GitHub CLI's docs), then
   `wsl -d <distro> -u agent -- gh auth login --with-token` and paste a fine-grained token for the agents' repos into
   that window only.
9. **Link from the other T3:** on this PC's T3 (`http://<tailscale address>:<port>`), Settings → Sharing & access →
   make a pairing code. In the other T3: Settings → Environments → Linked environments → Link, the same address
   and the code, **Full access**.

Run steps 3 to 5 again for a new release or a changed machine file; T3 restarts only when something changed. To
remove everything: `wsl --unregister <distro>`.

## Tailscale tag and ACL

The PC's node is tagged, so it is not you: it gets no access of its own, only what the ACL grants. Grant one thing,
the linking T3 reaching T3's port here, and nothing from the PC:

```json
{
  "tagOwners": { "tag:pc-agents": ["autogroup:admin"], "tag:orchestrator": ["autogroup:admin"] },
  "grants": [{ "src": ["tag:orchestrator"], "dst": ["tag:pc-agents"], "ip": ["tcp:3773"] }]
}
```

With no grant whose `src` is `tag:pc-agents`, the PC starts no connection into the tailnet; and the agent's uid cannot
reach any tailnet address anyway (100.64.0.0/10 and `fc00::/7` are dropped for it). Replies to the linking T3 are
allowed (connection tracking).

## Runs again, stops half-way, meets old or empty state

- **Again:** every file is compared before it is written; an unchanged file is not touched, and T3 restarts only
  when a file, the release or the T3 version changed. The tests run the setup twice and find no change.
- **Stopped half-way:** each file is written whole (a new file, then one rename) and recorded at once in
  `/etc/pc-agents/installed.tsv`. T3 is enabled only at the end, after every wall holds; a run that stops anywhere
  (the tests stop it at every write) leaves T3 off, and the next run finishes the job.
- **A distro that is not fresh:** refused before anything is written, with the reasons: another login user, an
  `agent` with another uid or in another group, a file the kit would write that it did not write (or that was
  edited since, for example a hand-written `wsl.conf` or `policy.json`), a cron, at or ssh unit where the kit masks
  one. A fresh distro's own `wsl.conf` (systemd only) and WSL's generated `resolv.conf` are replaced.
- **Old releases:** each release has its own folder under `/opt/pc-agents/`, switched by the `current` link; old ones
  stay (small) until the distro is removed.
- **No or a broken machine file:** setup and check refuse, naming the key; the unit does not start.

## Limits

- **One VM for all distros.** WSL2 runs every distro in one VM: one kernel, one network namespace, one memory cap.
  The firewall table is therefore global and matches the agent's uid in every distro (keep `agentUid` apart from
  the other distros' users); `localhost` is shared, so a server another distro runs on localhost is reachable.
  Global kernel switches (user namespaces, sysctls) are left alone: the unit closes them for T3 instead.
- **Public internet is open** to the agent: the firewall blocks private ranges, not the public internet (GitHub's
  and Anthropic's addresses are many and change). Add an outbound proxy if that matters.
- **No namespaces in the unit** (`RestrictNamespaces=yes`): provider sandboxes that need them (bubblewrap) do not
  run; the distro is the sandbox, so use full access.
- **Tailscale, Claude Code and `gh` are yours to install** (steps 6 to 8): their installers and logins need you.
- **No uninstall** but removing the distro; the distro holds nothing else.
- **No code hash manifest yet:** the kit's code is checked for root ownership and its written files byte for byte,
  but the code files are not hashed. The bootstrap duplicates the heavy kit's (a shared bootstrap is a follow-up).

## Tests

```
python3 -I -B fork-kit/pc-agents/tests/setup.test.py
python3 -I -B fork-kit/pc-agents/tests/check.test.py
```

A fake distro in a temporary folder (`PC_AGENTS_ROOT`, refused for root), with fake `useradd`, `runuser`, `systemctl`
and `nft`: every file and mode, a second run changes nothing, a run stopped at every write finishes on the next,
each kind of foreign distro is refused, and each wall fails on its own. `apps/server/src/policy/managedSettings.forkKeys.test.ts`
proves the managed policy locks the fork's settings.
