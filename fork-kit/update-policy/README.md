# update-policy: restart only when idle, at the quiet hour, by role; and a daily baseline check

Linux machines install security updates by themselves (`unattended-upgrades`). Some updates (a kernel, libc) need
a restart. `unattended-upgrades`' own `Automatic-Reboot` restarts at a fixed time whatever runs then: long agent jobs
and containers that do not come back by themselves are cut off. This kit takes restarts over:

- **dev** and **personal** machines restart at their quiet hour when an update needs it, **only when idle**; busy,
  they look again every few minutes until the quiet window ends, then report "restart pending" through the alert
  command and try again the next night;
- **live** machines never restart by themselves: a needed restart raises an alert (once per quiet window) and is
  done by hand in a planned window;
- a **daily baseline check** compares the machine with the policy and prints (and alerts) only what drifted.

Debian or Ubuntu with systemd and cgroup v2 (`/run/reboot-required`, `apt-config`, logind's inhibitor locks);
without cgroup v2 the machine always counts as busy. Plain Python 3 standard library (3.9 or
later, for `zoneinfo`); nothing to install but the kit.

## Commands

| Command                  | What                                                                                    |
| ------------------------ | --------------------------------------------------------------------------------------- |
| `update-policy tick`     | one tick of the restart policy; `update-policy.timer` runs it every 5 minutes, as root  |
| `update-policy idle`     | looks only: `idle`, or one `busy:` line per reason; exit 0 idle, 75 busy (`--json` too) |
| `update-policy baseline` | the daily check: prints only drift and alerts with it; exit 0 none, 1 drift             |
| `update-policy booted`   | after a boot (`update-policy-boot.service`): says once that the kit's restart came back |
| `update-policy status`   | the role, the window, a pending restart and what the last ticks saw                     |

Run them as root: they read every user's cgroups, containers and sockets. Everything they say goes to stdout,
which is the journal under the units (`journalctl -u update-policy`).

## What one tick does

1. Every tick: apt's effective `Unattended-Upgrade::Automatic-Reboot` (`apt-config dump`) must be false, since the
   kit owns restarts. Anything else (a later `apt.conf.d` file or `/etc/apt/apt.conf` that wins over the kit's, or
   apt's settings that cannot be read) is an alert `apt-restarts` at once, then once a day while it stays.
2. A pending restart (`/run/reboot-required`) is dated by when this boot first saw it, kept in the state: the
   file's own time is new each time a package that needs a restart rewrites it.
3. Outside the quiet window (`quietHour` for `windowMinutes`, in `timeZone`), or with no restart pending: nothing
   more.
4. Role `live`: one alert `live-restart-pending` per window, with the packages from
   `/run/reboot-required.pkgs`. Never a restart.
5. Roles `dev` and `personal`: once every `retryMinutes`, it looks at the idle facts (below).
   - **Idle:** it writes why to the journal and to the state file (`restart pending since ... for linux-image-...;
     idle in the quiet window 05:00-07:00 Europe/London (role dev)`). As the very last step it reads the block
     locks on shutdown or sleep again: one taken since the look (or locks it cannot read) stops the restart, with one
     `restart-failed` alert. Then `systemctl reboot`. What is left is the time between that read and systemd
     starting the shutdown, well under a second: a lock taken in it is too late. `--check-inhibitors=yes` is not
     used: it also refuses while another user is logged in, and logged-in sessions are no reason to wait (they do
     not count as busy).
   - **Busy:** the reasons go to the journal (`busy, so no restart now (next look in 15 min ...): user 1000: 2 T3
     agent scope(s) running ...`).
6. The first tick after a window that ended with the restart still pending after a busy look sends one alert
   `restart-pending` (with the last busy reasons and the next window). A window with no look (the machine was off)
   sends none.
7. At most one restart per window: none when the machine booted inside the window already, or a restart was asked
   for in it. A restart that is stopped (a new lock) or refused by systemctl is logged and alerted
   (`restart-failed`) once, never tried again in a loop. A restart asked for that did not come (the tick was killed
   after it wrote the state, or systemctl said yes and nothing happened) is seen 10 minutes later by the boot id,
   still the same: one `restart-failed` alert. One such alert per window, never two for the same restart.

After a boot, `update-policy booted` writes `back up: booted ..., N min after the restart asked at ...` to the
journal when the last restart was the kit's, and alerts `back-up` when `alertOnBoot` is on.

## Idle

The machine is idle when none of these is true. Anything it cannot read counts as busy ("cannot tell: ..."): a
folder or process it cannot read, a tool that fails, no `busctl`. Text that comes from users (inhibitor reasons,
container names, a script's answer) is escaped to one line, so it cannot forge a journal record or an alert line.

- **No cgroup v2** at `/sys/fs/cgroup` (cgroup v1 or hybrid): agents and heavy jobs cannot be counted, so always
  busy.
- **T3 agents:** a scope with a process in it below any user's systemd manager that T3 made for an agent: a scope
  right below an `app-t3code*` slice, or a `t3code-*.scope` anywhere below the manager (the rule of
  [heavy facts](../heavy/README.md)).
- **T3 without agent scopes:** T3 turns its agent scopes off when it has no systemd user manager (a system
  service, no `XDG_RUNTIME_DIR`, a failed `systemd-run` probe); its agents are then its plain children, and no
  slice shows them. Then any of these is busy, unless it runs under its user's manager with an `app-t3code*` slice
  there (scopes on: its agents are counted as scopes):
  - a T3 server, however it was started: the `t3` binary or npm's `t3` link (by name or by `/proc/<pid>/exe`);
    any argument that is T3's server entry (`apps/server/dist/bin.mjs`, `apps/server/src/bin.ts`,
    `apps/server/scripts/dev.ts`, relative ones resolved against the process's own folder; in a package named
    `t3` also `dist/bin.mjs`), which covers `node apps/server/dist/bin.mjs`, `npm start`, npm's global install,
    `bun run src/bin.ts` and the desktop app's server (Electron running as node); a package runner (`npm`,
    `pnpm`, `bun`, `yarn`) starting a package named `t3`;
  - a process with T3's environment markers (`T3CODE_HOME`, `T3CODE_PORT`, `T3CODE_THREAD_ID` in
    `/proc/<pid>/environ`: the server's, inherited by its agents);
  - an agent CLI (`claude`, `codex`, `opencode`, ...) below such a server.

  So a T3 server with scopes off makes the machine busy as long as it runs, idle or not: such a machine rarely
  restarts by itself and sends `restart-pending` instead. That is intended (a running turn is never cut off);
  run T3 with a user manager (scopes on) for nightly restarts. A shell that exports `T3CODE_HOME` counts too.

- **heavy jobs:** a scope with a process in it in a user's heavy slice (`heavySlice`), or a live ticket in that
  user's heavy queue (`/run/user/<uid>/heavy-queue/<slice>/`, held by a running or waiting `heavy --kind` job).
- **Containers and VMs** (less the names `ignoreContainers` matches: `*` and `?`, a database that always runs, say):
  - Docker: the system one (unless `systemDocker` is false) and the rootless one of each user in `containerUsers`
    (`/run/user/<uid>/docker.sock`, asked as that user, never as root); each only while its `dockerd` runs, so a
    socket-activated `docker.sock` is never woken (that would start containers with `restart: always`);
  - Podman (rootless and system) and CRI-O: each running container has a `conmon`; its name is read from it;
  - incus and LXD (`incus list` / `lxc list`, while `incusd` / `lxd` runs), classic LXC (`lxc-ls --running`);
  - libvirt (`virsh -c qemu:///system list`, while `libvirtd` or `virtqemud` runs).
- **Inhibitor locks:** a `block` lock on `shutdown` or `sleep` (logind). This is the one standard way for any job
  to say "do not restart now"; it is read again right before the restart:

  ```
  systemd-inhibit --what=shutdown --mode=block --who=mutation-tests --why="hours left" ./run-mutation-tests
  ```

  `delay` locks (`unattended-upgrade-shutdown` holds one all day) do not count, and neither do logged-in sessions
  (a desktop left logged in, an open ssh session).

- **Package manager at work:** `apt`, `apt-get`, `aptitude`, `dpkg` or `unattended-upgrade` running.
- **Busy scripts:** each of `busyScripts` is a [free/v1 script](../common/FREE.md) (absolute path, root's, no
  arguments, 3 seconds); any answer but "free" is busy. `fork-kit/common/free/busy-while-running` is one to copy.

`update-policy idle` shows what a tick would see now.

## Machine file

The `update-policy` section of `/etc/fork-kit/machine.json` ([common](../common/README.md)): the facts of one
machine. `role`, `quietHour` and `timeZone` have no default: without them (or with any key the kit does not know)
the file is refused, and a refused file never restarts anything (the tick logs it at most once an hour while a
restart is pending; `baseline` reports it).

```json
{
  "version": 1,
  "update-policy": {
    "role": "dev",
    "quietHour": "05:00",
    "timeZone": "Europe/London",
    "windowMinutes": 120,
    "retryMinutes": 15,
    "alertCommand": ["/usr/local/bin/notify-owner", "--topic", "updates"],
    "alertOnBoot": false,
    "containerUsers": ["agent"],
    "systemDocker": true,
    "ignoreContainers": ["dev-db"],
    "heavySlice": "heavy.slice",
    "busyScripts": ["/usr/local/lib/busy/game-running"],
    "baseline": {
      "ports": ["tcp/22@100.64.0.0/10", "udp/41641", "tcp/3773@100.64.0.0/10"],
      "ignoreLoopback": true,
      "diskPercent": 90,
      "diskPaths": ["/", "/srv"],
      "pendingDays": 3
    }
  }
}
```

| Key                       | Default         | What                                                                                  |
| ------------------------- | --------------- | ------------------------------------------------------------------------------------- |
| `role`                    | required        | `dev` or `personal`: restarts when idle in its window; `live`: never restarts, alerts |
| `quietHour`               | required        | `HH:MM`, local time in `timeZone`                                                     |
| `timeZone`                | required        | an IANA name (`Europe/London`, `UTC`); the window follows its daylight saving time    |
| `windowMinutes`           | 120             | how long the window lasts (5 to 720)                                                  |
| `retryMinutes`            | 15              | how often it looks again while busy (5 to 240; the timer ticks every 5 minutes)       |
| `alertCommand`            | `[]`            | a program (absolute path) and its arguments; none: alerts go only to the journal      |
| `alertOnBoot`             | `false`         | also alert `back-up` after the kit's restart                                          |
| `containerUsers`          | `[]`            | users whose rootless Docker counts                                                    |
| `systemDocker`            | `true`          | the system Docker's containers count                                                  |
| `ignoreContainers`        | `[]`            | container names (`*`, `?`) that never count                                           |
| `heavySlice`              | `"heavy.slice"` | the heavy kit's slice name                                                            |
| `busyScripts`             | `[]`            | free/v1 scripts (absolute paths); any answer but free is busy                         |
| `baseline.ports`          | `null`          | the listening ports allowed (below); `null`: no port check                            |
| `baseline.ignoreLoopback` | `true`          | listeners on 127.0.0.0/8 and ::1 are never drift                                      |
| `baseline.diskPercent`    | 90              | a disk of `diskPaths` fuller than this is drift                                       |
| `baseline.diskPaths`      | `["/"]`         | the disks it looks at                                                                 |
| `baseline.pendingDays`    | 3               | a restart pending longer than this is drift                                           |
| `baseline.sshUsers`       | `["root"]`      | users whose ssh login from outside is checked with `sshd -T -C` (Match blocks)        |

**The alert command** gets the text on stdin and `UPDATE_POLICY_EVENT` (`restart-pending`,
`live-restart-pending`, `restart-failed`, `apt-restarts`, `back-up`, `drift`) and `UPDATE_POLICY_HOST` in a plain
environment; it runs as root from `/`, at most 60 seconds. Exit 0 is sent. A failure goes to the journal, fails the
unit, and is drift in the next baseline check until an alert goes out again. **No secrets in its arguments:**
every user can read a running program's arguments in `/proc`. Put a token in a root-only file (mode 600) that the
command reads; an argument that looks like one (`ghp_...`, `github_pat_...`, `sk-...`, `xox...-`, a long hex or
base64 run) is refused. Without a machine file there is no alert command either: a broken file is only logged
(the tick, hourly while a restart is pending) and reported by `baseline` (exit 1).

**Ports** are `PROTO/PORT`, `PROTO/LOW-HIGH`, either with `@ADDRESS` or `@NETWORK` (`tcp/22`, `udp/32768-60999`,
`tcp/3773@100.64.0.0/10`, `tcp/8080@127.0.0.1`). Without an address an entry allows the port on every address. A
listener on every address (`0.0.0.0`, `::`) needs an entry without one (or `@0.0.0.0/0`, `@::/0`). TCP counts when
it listens, UDP when it is bound and unconnected. The check sees the host's own network namespace only: a port
published into a container through DNAT (Docker with `userland-proxy: false`) or a listener inside another
namespace is not seen.

## The daily baseline check

`update-policy baseline` (`update-policy-baseline.timer`, daily around 08:00) prints one line per drift and
nothing else; exit 1 when there is any, and the alert command gets the lines (`drift`). It checks:

- `unattended-upgrades` is installed; `APT::Periodic::Update-Package-Lists` and `Unattended-Upgrade` are on
  (apt's own booleans: `false`, `no`, `off`, `disable`, `without` and `0` are off); `apt-daily.timer` and
  `apt-daily-upgrade.timer` are enabled; an `Allowed-Origins` or `Origins-Pattern` entry takes the distribution's
  own security pocket (`<codename>-security`, or `label=Debian-Security`): Ubuntu's ESM `-apps-security` and
  `-infra-security` lines alone do not count;
- the restart policy is the kit's: apt's effective `Unattended-Upgrade::Automatic-Reboot` is false; the kit's
  units are installed and enabled (`installed but disabled, not enabled` otherwise); `update-policy.service` and
  `update-policy-boot.service` did not fail their last run; the last alert went out;
- no restart has been pending for more than `pendingDays` days (since this boot first saw it);
- no listening port outside `baseline.ports` (with the program that holds it);
- ssh allows no password login: `sshd -T` says `passwordauthentication no`, and not keyboard-interactive with PAM,
  for every connection and, with `sshd -T -C user=USER,host=outside.invalid,addr=203.0.113.1`, for each of
  `sshUsers` connecting from outside, so a `Match` block that turns passwords back on is seen (no sshd: nothing
  checked);
- no disk of `diskPaths` fuller than `diskPercent`.

A fact it cannot read is drift ("cannot read ..."), never a pass; a check that breaks is one such line, and the
others still run.

## Install

As root, from a commit on the fork's protected `ci-release` branch (the shared
[bootstrap](../common/bootstrap.sh) refuses anything else). Placeholders in CAPITALS. Write the machine file's
`update-policy` section first.

```
curl -fsSL https://raw.githubusercontent.com/FORK_OWNER/FORK/RELEASE_SHA/fork-kit/common/bootstrap.sh -o /root/fork-kit-bootstrap.sh
sh /root/fork-kit-bootstrap.sh --kit update-policy --repo FORK_OWNER/FORK --sha RELEASE_SHA --timer
update-policy status && update-policy idle; update-policy baseline
```

`--timer` takes restarts over in an order that never leaves the machine with neither policy:

1. it writes the system units `update-policy.service` and `.timer` (every 5 minutes),
   `update-policy-baseline.service` and `.timer` (daily) and `update-policy-boot.service` (once after each boot)
   to `/etc/systemd/system/`, runs `systemctl daemon-reload`, enables and starts both timers and enables the boot
   unit. If that fails, it stops there: unattended-upgrades' own restarts still apply;
2. only then it writes `/etc/apt/apt.conf.d/zz-update-policy`: `Unattended-Upgrade::Automatic-Reboot "false";`.
   Its name sorts after the numbered files, so it wins over an `Automatic-Reboot "true"` there
   (`50unattended-upgrades`, a hand-made `99-reboot`); not over a later file (`zzz-local`) or `/etc/apt/apt.conf`,
   which apt reads last. Updates still install as before;
3. it reads apt's effective value (`apt-config dump`) and **fails** while it is not false, naming the cause; remove
   that line and run again. `--force` installs anyway (the tick then alerts `apt-restarts` once a day).

A file at one of those paths that the kit did not write (its first line says so) is refused, never overwritten.
Files: `/usr/local/libexec/update-policy/versions/<sha>/` (code, with `MANIFEST.sha256`), the link `current`,
`/usr/local/bin/update-policy`, `/usr/local/sbin/update-policy-install`, `/etc/update-policy/config.json` (only
whether `--timer` is on) and `installed.tsv`; the tick's state in `/var/lib/update-policy/`.
`update-policy-install self-check` reports drift of the installed files. Upgrade: the bootstrap with the new sha.

## Turn it off

- **For a while** (no restarts, no alerts): `systemctl disable --now update-policy.timer`. Restarts then stop
  altogether (unattended-upgrades still does not restart by itself); the daily baseline check reports the timer
  `installed but disabled, not enabled`.
- **Back to unattended-upgrades' own restarts:** the bootstrap with `--no-timer` (or `update-policy-install install
  --source /usr/local/libexec/update-policy/current --sha CURRENT_SHA --no-timer`). It removes the apt file first,
  so unattended-upgrades' own `Automatic-Reboot` applies again, then stops, disables and removes the units.
- **Remove:** `update-policy-install uninstall`: the same, then the code and the state.

## Runs again, stops half-way, old or empty state

- **Runs again:** the installer changes nothing when nothing changed (it enables the units again, which systemd
  takes as a no-op). Two ticks at once: the second finds the lock (`/var/lib/update-policy/lock`) held and does
  nothing. In one window, looks wait `retryMinutes`, and each alert is sent once (`apt-restarts` once a day).
- **Stops half-way, installer:** the shared steps (any step, then run again). The units are written and enabled
  before the apt file, so a stop in between leaves unattended-upgrades' own restarts on; a failed `systemctl` or an
  apt value that is not false fails the run before it is recorded, and the next run finishes it.
- **Stops half-way, tick:** killed before it writes the state: nothing happened, the next tick looks again. Killed
  after it (or systemctl said yes and no restart came): no second restart in that window, and 10 minutes later the
  same boot id is one `restart-failed` alert; the next window tries again.
- **Old, broken or missing state:** read as empty and said in the journal; the worst it costs is one more look or
  one more alert, and a pending restart dated from the file's time once. A restart already made in this window is
  still known from the boot time.
- **Existing unattended-upgrades config:** kept as it is; only `Automatic-Reboot` is overridden while `--timer` is
  on, and checked on every tick. The kit never turns updates on or off; the baseline check reports when they are
  off.
- **No machine file, or no section:** the tick restarts nothing and logs the reason (at most once an hour while a
  restart is pending); `idle`, `status` and `baseline` say so and exit 1. With `--timer` on, unattended-upgrades
  does not restart either, and no alert can go out (the alert command is in that file): fix the file, or install
  with `--no-timer`.
- **Clock and time zone:** the window is computed in `timeZone` on every tick, so a daylight saving change or a
  machine clock in UTC moves nothing. A start time the clock skips moves later by the gap; one it passes twice is
  the first.

## Tests

`tests/*.test.py`, run with `python3 -I -B fork-kit/update-policy/tests/NAME.test.py` from the repository root:
fake cgroups, `/proc`, sockets and programs in a temporary folder, a fixed clock. No root, no systemd, no network.
Each file ends with a red check that breaks each rule in a copy of the code (or the installer) and shows its test
then fails. What every kit's installer does is tested in
[kit_install.test.py](../common/tests/kit_install.test.py); `install.test.py` covers this kit's own files.
