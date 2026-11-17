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
| Locked T3 settings      | `/etc/t3code/policy.json` (managed policy): linked environments on, updates off (`limits.updates`), Connect, webhook tasks and browser cookie import off, `maxRuntimeMode` and your `lockedSettings` locked; T3 itself is root's (under `/opt/pc-agents/t3`)                                                                                       |
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
  setup installed (its hash is in the record). Setup makes the runtime root's alone after `scripts/install.sh` (its
  `tar` keeps the archive's owners and modes), and installs again a runtime an older setup left otherwise;
- no Windows drive is mounted;
- the loaded firewall table is the one from the kit's `firewall.nft`: `pc-agents load-firewall` saves the file's hash
  and what the kernel then lists, and any later change, or an older file left loaded, differs;
- the default gateway (the Windows host) is in a blocked range, and no `allow` or `peers` entry lets it through;
- no T3 runs that started before setup's last change: while setup's marker `/etc/pc-agents/restart-pending` is there
  (see "Runs again" below), a T3 process started before the time in it fails this wall, and so does a marker or a
  start time it cannot read. So the 2-minute check stops a T3 that a stopped or failed setup left running with old
  files, and `pc-agents as-agent` refuses meanwhile. Setup's own first check skips it.

The fleet's `mounts`, `sockets` and `userns` walls are
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
    "t3Version": "0.1.9",
    "t3ReleaseBaseUrl": "https://github.com/example/t3code/releases/download",
    "port": 3773,
    "maxRuntimeMode": "full-access",
    "lockedSettings": {},
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
| `t3Version`             | the exact T3 version (the linking T3's), 0.1.7 or newer (see below); installed with `scripts/install.sh`          |
| `t3ReleaseBaseUrl`      | where that version's release files are (`T3CODE_RELEASE_BASE_URL` of `scripts/install.sh`)                        |
| `port`                  | T3's port (`T3CODE_PORT`); the Tailscale ACL opens it to the linking T3 only                                      |
| `agentUid`              | the `agent` user's uid (default 1100; not 1000, the first user of every other distro, see Limits)                 |
| `maxRuntimeMode`        | the highest runtime mode T3 allows (`full-access` for a linked PC)                                                |
| `lockedSettings`        | more T3 settings to lock, `{path: value}`, only ones `t3Version` knows (`agentSlots.…`: a fork build), see below  |
| `memoryMax`, `cpuQuota` | the unit's `MemoryMax` and `CPUQuota`                                                                             |
| `nameservers`           | 1 to 3 public DNS servers for `/etc/resolv.conf` (WSL's own one is on the blocked Windows host)                   |
| `allow`                 | optional: private networks the agent may still reach (CIDR list); empty by default; never the Windows host        |
| `devPorts`              | optional: the TCP ports the agent's dev servers may listen on and be reached on, `LOW-HIGH` (default `4100-4199`) |
| `peers`                 | the linking T3's Tailscale address(es) (CIDR list, 1 to 16): the only private addresses the unit talks to         |
| `tailscaleTag`          | the tag this PC's Tailscale node gets (`tag:name`)                                                                |

`t3Version` must be 0.1.7 or newer: older releases have no managed policy, so they would run with none of the kit's
locks and with updates on. A 0.1.7 pre-release counts as older (some 0.1.7 nightlies were built before the policy),
and a pre-release of a later version as that version. This fork's stable build of a release (`0.1.7-fork.1`) counts
as that release, its nightlies as pre-releases. Setup and `check` refuse the older ones, naming the key. (`t3 pair`,
step 10, came earlier.)

Every T3 setting can be locked through `lockedSettings`, the fork's own settings (`agentSlots`, `usagePacingGate`,
`usagePacing`, `waitFor`, ...) too: the managed policy locks any key of the server settings, upstream's or the fork's
(`apps/server/src/policy/managedSettings.ts`), and a test proves it for the fork's keys. Lock one field per dotted
path (`agentSlots.maxPerMachine`), or give a whole setting (`agentSlots`) or one record entry
(`providerInstances.codex-work`) as an object holding every field: T3 takes an object only as a complete value. The kit
refuses what is cheap to tell (a field of a provider instance, an object deeper than a record entry, the kit's own
setting, a blank `organization`); T3 refuses the rest, like a field inside a setting that can be empty (lock the
whole setting), says why in its log (`journalctl -u t3code-agents`) and does not start; setup shows that line and
T3's exit status (77: a policy it cannot apply). A
policy may name only settings the installed `t3Version` knows: T3 does not start on a setting it lacks, so the fork's own
settings (`agentSlots`, ...) need a fork build, and `automaticUpdates` a version that has it (the kit turns updates
off through `limits.updates` on every version). Releases that exit 77 on a policy they cannot apply are not restarted
(`RestartPreventExitStatus=77`); on any other exit the unit gives up after 5 starts in 10 minutes
(`StartLimitBurst=5`). Setup clears that count
(`systemctl reset-failed`) before it starts T3, so running it again within the 10 minutes still starts T3.

A wall that fails stops T3, and nothing starts it again by itself: fix what `pc-agents check` names (or run
`pc-agents setup` again), then start it as root: `systemctl start t3code-agents.service` (its own start runs the
check first and refuses on a failed wall). If it says `start request repeated too quickly`, run
`systemctl reset-failed t3code-agents.service` first.

## Owner steps (once per PC)

Commands are for Windows PowerShell unless a step says otherwise. `<distro>` is a name of your choice (for example
`t3-agents`); `<fork>` is the fork's `OWNER/NAME`, `<sha>` a commit of its `ci-release` branch.

1. **Make a fresh distro** (not the CI one, not your own): `wsl --install -d Ubuntu-24.04 --name <distro> --no-launch`.
2. **Write the machine file** inside it as root: `wsl -d <distro> -u root`, then
   `mkdir -p /etc/fork-kit && nano /etc/fork-kit/machine.json` (the example above, your values), and leave with `exit`.
   Always open this distro with `-u root`. If a window ever asks you for a new user name and password (Ubuntu's
   first-launch setup), that makes a login user, which setup refuses: remove it as root (`userdel -r <name>`) or
   start again from step 1. The kit makes `root` the default user, so this cannot happen after step 4.
3. **Run the setup** as root:
   `wsl -d <distro> -u root -- sh -c 'curl -fsSL https://raw.githubusercontent.com/<fork>/ci-release/fork-kit/pc-agents/bootstrap.sh -o /root/pc-agents-bootstrap.sh && sh /root/pc-agents-bootstrap.sh --repo <fork> --sha <sha>'`.
   The bootstrap comes from the protected `ci-release` branch itself, not from `<sha>`, so it is a reviewed one; it
   refuses a `<sha>` that is not on that branch before it downloads or runs anything of it.
   The first run writes everything, then says a wall does not hold (Windows' drives are still mounted) and does
   not start T3.
4. **Restart the distro** so `wsl.conf` applies: `wsl --terminate <distro>`.
5. **Run the setup again** (the same command as step 3). Now every wall holds and T3 starts. It prints the next step.
6. **Join Tailscale** with the tag (as root in the distro): install it
   (`curl -fsSL https://tailscale.com/install.sh | sh`), then `tailscale up --advertise-tags=<tag> --accept-dns=false`
   (keep the kit's public nameservers; MagicDNS is on the tailnet, which the agent cannot reach) and open the link it
   prints in a browser on any machine. The linking T3's Tailscale address is already in `peers` (step 2's file needs
   it; setup refuses a file without it); if that address changes, edit the file and run step 3 again. The ACL is
   below.
7. **Log in to Claude as the agent:** never open a shell as `agent` (`wsl -u agent` would start the agent's own
   session, outside T3's walls). Run each command through the kit instead, which runs it as `agent` under the same
   walls as T3 (a transient system unit, `systemd-run`):
   `wsl -d <distro> -u root -- /opt/pc-agents/current/sbin/pc-agents as-agent -- sh -c '<the install command from Claude Code's docs>'`,
   then the same with `-- claude`; open the link it prints in your browser. The kit runs the command through `env`
   with the agent's `PATH` (`/home/agent/.local/bin` first), so a bare `claude` is found there, and with only
   `HOME`, `T3CODE_HOME`, `PATH` and `SHELL` set. Codex: its browser login waits on `localhost` port 1455, outside
   `devPorts`, so the walls refuse it: use its device-code login instead, the same with `-- codex login --device-auth`
   (`codex login --help` names it).
   If Claude says it cannot read its managed settings on `/mnt/c` (`EACCES`), choose "Continue without these
   settings": that is Windows' folder, which the walls hide on purpose. Then restart T3, which looks for providers
   only when it starts: `wsl -d <distro> -u root -- systemctl restart t3code-agents` (its start runs the check first).
   Every agent of this T3 runs as `agent`, in the one home, so a tool installed there once is every agent's: install a
   project's toolchain (Node, its package manager) the same way, through `as-agent`, before the first agent needs it.
   Setup installs the system libraries they need (`libatomic1`, without which Node does not start, and `unzip`).
8. **Log in to GitHub as the agent:** install `gh` (as root, from GitHub CLI's docs), then
   `wsl -d <distro> -u root -- /opt/pc-agents/current/sbin/pc-agents as-agent -- gh auth login --with-token` and paste
   a fine-grained token for the agents' repos into that window only. It waits without a prompt: paste the token,
   press Enter, then Ctrl+D. Then give the agent a git identity (its commits fail without one):
   `wsl -d <distro> -u root -e /opt/pc-agents/current/sbin/pc-agents as-agent -- git config --global user.name '<name>'`
   and
   `wsl -d <distro> -u root -e /opt/pc-agents/current/sbin/pc-agents as-agent -- git config --global user.email '<email>'`,
   with a name of your choice and an address that is no one's private one, for example the token account's noreply
   address (`<id>+<login>@users.noreply.github.com`, shown in its GitHub email settings). `-e`, not `--`: wsl runs
   the line without root's shell, so the value reaches git as typed (a name with an apostrophe is `'O''Brien'` in
   PowerShell). Run again, they only set the same values again.
9. **Keep the distro running:** WSL stops a distro soon after its last window closes, and does not start one when
   Windows starts. A logon task of your own (no administrator needed) keeps this one running while you are logged
   in. In PowerShell, as yourself (the same task as the desk starter's, `fork-kit/desk/windows/start-desk.ps1`):
   ```powershell
   $a = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument '--headless wsl.exe -d <distro> -u root --exec sleep infinity'
   Register-ScheduledTask -TaskName 'pc-agents <distro>' -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)) -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force
   Start-ScheduledTask -TaskName 'pc-agents <distro>'
   ```
   `conhost.exe --headless` shows no window. Running it again replaces the task. A distro set up before this step
   only needs these lines; nothing in the distro changes. To remove it:
   `Unregister-ScheduledTask -TaskName 'pc-agents <distro>' -Confirm:$false`.
10. **Pair, then link from the other T3:** this PC's T3 is walled from the Windows host on purpose, so a browser on
    this PC cannot open it, nor make a pairing code there. Make the code from the command line (PowerShell):
    `wsl -d <distro> -u root -- /opt/pc-agents/current/sbin/pc-agents pair`. It runs T3's own `t3 pair` as the
    agent under the walls, the T3 of `t3Version` that the unit starts
    (`/opt/pc-agents/t3/runtime/versions/<t3Version>/t3`; when setup has not installed that one yet, it says so: run
    step 3 again), and prints a token that expires in 5 minutes; for longer:
    `wsl -d <distro> -u root -- /opt/pc-agents/current/sbin/pc-agents pair --ttl 15m`. The token stays in this
    PowerShell window's scrollback, usable by anyone who sees it until it is used or expires: give a long `--ttl` only
    knowingly, and close the window once linked. The address it prints is the distro's own, not one to link. A kit
    without `pair` (older than this step):
    `wsl -d <distro> -u root -- /opt/pc-agents/current/sbin/pc-agents as-agent -- /opt/pc-agents/t3/runtime/versions/<t3Version>/t3 pair`
    (an upgrade leaves the old version's folder there, so name yours). In the other T3: Settings → Environments →
    Linked environments → Link, with this PC's tailnet DNS name (MagicDNS: `http://<name>.<tailnet>.ts.net:<port>`;
    `tailscale status` there shows the name), the token, and **Full access**. Not its tailnet IP address: T3 refuses
    to link a plain-http IP address.

Run step 3 again for a new release or a changed machine file (and steps 4 and 5 if it asks); T3 restarts only when
something changed. To remove everything: `wsl --unregister <distro>`.

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
- **Stopped half-way:** each file is written whole (a new file, then one rename). Before and after every change
  (and after a T3 install, and once more right before its restart), setup writes the root-owned marker
  `/etc/pc-agents/restart-pending` with this boot's id and the time (`CLOCK_MONOTONIC`, the clock of systemd's
  `ExecMainStartTimestampMonotonic`, which no clock change moves). It is removed only after T3 restarted and stayed
  up for 15 seconds (the same process, active, no automatic restart). So a run stopped anywhere, or a failed T3
  install, leaves the marker, and the next good run restarts T3 even when it has nothing left to write. Meanwhile
  the `restart` wall fails while the T3 that runs started before the marker's time, so the 2-minute check stops it
  (an old T3 never keeps running under new files and a passing check). A run stopped during setup's own restart or
  its 15 seconds leaves a T3 started after the marker: the wall passes, and the next run restarts it once more. A
  marker of an earlier boot passes (every T3 now started after it); one an older kit wrote, with no time in it,
  fails until setup runs again. A T3 that starts and then exits within the 15 seconds is reported (`T3 did not
  start or stay up`), and the marker stays. The record
  (`/etc/pc-agents/installed.tsv`) holds a file's old and new hash while it is replaced, so a run stopped right after
  a write, followed by an edit of the machine file, is not mistaken for a hand edit. A new T3 version is installed by
  root after the new firewall is loaded, so a slow or failed download never leaves the old firewall in place.
- **Timeouts and signals:** every program setup runs has a time limit (30 minutes for T3's download, 15 for
  `apt-get`); past it the whole process group is stopped and setup says so. Run it again. Ctrl-C, `SIGTERM` or
  `SIGHUP` (a closed window) stop setup the same way, the running download's whole group too.
- **Start limit:** a rerun within 10 minutes of five failed starts still starts T3 (`systemctl reset-failed` first).
- **An older kit's distro:** an `agent` made with a login shell gets `/usr/sbin/nologin` (`usermod`), once.
- **Packages:** setup installs `nftables`, `libatomic1` and `unzip` when `dpkg` does not list one as installed (held
  counts as installed; a distro set up by an older kit gets the two new ones at its next run; no T3 restart for
  them). It installs them after it writes its files, so `apt-get` resolves through the machine file's `nameservers`
  as they are now, and before it loads the firewall. A package install stopped half-way (`dpkg --audit` names it) is
  finished first (`dpkg --configure -a`, run only then). Nothing asks a question: a changed config file is kept and
  debconf takes its defaults. A lock another `apt-get` or `dpkg` holds (apt-daily on a new distro) is waited for, up
  to 5 minutes a step: `apt-get` waits for dpkg's lock itself, and setup runs `dpkg`, `apt-get update` and
  `apt-get install` again while they fail on the locks they take without a wait. A failed step stops setup, naming
  the command to run (for a lock still held, to wait for the other one); the files it wrote stay, and when one of them
  changed what T3 runs with, the walls check's `restart` wall fails (and the 2-minute check stops T3) until setup runs
  again to the end, as after a failed T3 download. A distro restart in between starts T3 again with the new files and
  firewall, without the packages.
- **A distro that is not fresh:** refused before anything is written, with the reasons: another login user, an
  `agent` with another uid or in another group, a file the kit would write that it did not write (or that was
  edited since, for example a hand-written `wsl.conf` or `policy.json`), a cron, at or ssh unit where the kit masks
  one. A fresh distro's own `wsl.conf` (systemd only) and WSL's generated `resolv.conf` are replaced.
- **Old releases:** each release has its own folder under `/opt/pc-agents/`, switched by the `current` link; old ones
  stay (small) until the distro is removed. A T3 that an older kit installed in the agent's home is left there and
  no longer run (the unit runs root's copy under `/opt/pc-agents/t3`).
- **Unsafe folders:** setup refuses a distro whose `/`, `/opt`, `/opt/pc-agents`, `/etc`, `/etc/systemd`,
  `/etc/systemd/system`, `/etc/pc-agents` or `/etc/t3code` is not root's alone.
- **No or a broken machine file:** setup and check refuse, naming the key; the unit does not start.

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
  into a write to the Windows clipboard. `pc-agents as-agent` runs with `--pty`, so the owner steps' output reaches
  your terminal that way too. Do not paste from the clipboard blindly after watching agent output.
- **Runs only while you are logged in:** the logon task (step 9) keeps the distro running while you are logged in
  and the PC is awake. Logging out, sleep, a Windows restart or `wsl --shutdown` stop T3 (the linking T3 sees this
  PC offline); the task starts it again at your next logon. Without the task, T3 stops soon after the last WSL
  window closes.
- **No namespaces in the unit** (`RestrictNamespaces=yes`): provider sandboxes that need them (bubblewrap) do not
  run; the distro is the sandbox, so use full access.
- **Tailscale, Claude Code and `gh` are yours to install** (steps 6 to 8): their installers and logins need you.
  Claude Code and logins go through `pc-agents as-agent`, never a shell as `agent`.
- **No uninstall** but removing the distro; the distro holds nothing else.
- **No code hash manifest yet:** the kit's code is checked for root ownership and its written files byte for byte,
  but the code files are not hashed (T3's `t3` is). The bootstrap duplicates the heavy kit's (a shared bootstrap is
  a follow-up).

## Tests

```
python3 -I -B fork-kit/pc-agents/tests/setup.test.py
python3 -I -B fork-kit/pc-agents/tests/check.test.py
```

A fake distro in a temporary folder (`PC_AGENTS_ROOT`, refused for root; `tests/fake_distro.py`), with fake
`useradd`, `usermod`, T3's installer, `systemctl` (it keeps which units are enabled and running, their main process
and start time, a start limit, and a T3 that dies a moment after it starts), `systemd-run` (a bare command is looked
up in the caller's `PATH`, as the real one does) and `nft`: every file and mode, a second run changes nothing, a run
stopped at every write finishes on the next and restarts T3, an upgrade stopped at every write fails `check` until
setup runs again, `SIGTERM` and `SIGHUP` end the download too, each kind of foreign distro is refused, and each wall
fails on its own when its part is taken away.
`apps/server/src/policy/managedSettings.forkKeys.test.ts` proves the managed policy locks the fork's settings.
