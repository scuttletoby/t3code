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

Debian or Ubuntu with systemd (`/run/reboot-required`, `apt-config`). Plain Python 3 standard library (3.9 or
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

1. Outside the quiet window (`quietHour` for `windowMinutes`, in `timeZone`), or with no `/run/reboot-required`:
   nothing.
2. Role `live`: one alert `live-restart-pending` per window, with the packages from
   `/run/reboot-required.pkgs`. Never a restart.
3. Roles `dev` and `personal`: once every `retryMinutes`, it looks at the idle facts (below).
   - **Idle:** it writes why to the journal and to the state file (`restart pending since ... for linux-image-...;
     idle in the quiet window 05:00-07:00 Europe/London (role dev)`), then `systemctl reboot`.
   - **Busy:** the reasons go to the journal (`busy, so no restart now (next look in 15 min ...): user 1000: 2 T3
     agent scope(s) running ...`).
4. The first tick after a window that ended with the restart still pending after a busy look sends one alert
   `restart-pending` (with the last busy reasons and the next window). A window with no look (the machine was off)
   sends none.
5. At most one restart per window: none when the machine booted inside the window already, or a restart was asked
   for in it. A restart that fails is logged and alerted (`restart-failed`), never tried again in a loop.

After a boot, `update-policy booted` writes `back up: booted ..., N min after the restart asked at ...` to the
journal when the last restart was the kit's, and alerts `back-up` when `alertOnBoot` is on.

## Idle

The machine is idle when none of these is true. Anything it cannot read counts as busy ("cannot tell: ...").

- **T3 agents:** a scope with a process in it below any user's systemd manager that T3 made for an agent: a scope
  right below an `app-t3code*` slice, or a `t3code-*.scope` anywhere below the manager (the rule of
  [heavy facts](../heavy/README.md)).
- **heavy jobs:** a scope with a process in it in a user's heavy slice (`heavySlice`), or a live ticket in that
  user's heavy queue (`/run/user/<uid>/heavy-queue/<slice>/`, held by a running or waiting `heavy --kind` job).
- **Containers:** a running container of the system Docker (`/run/docker.sock`, unless `systemDocker` is false),
  or of the rootless Docker of a user in `containerUsers` (`/run/user/<uid>/docker.sock`, asked as that user,
  never as root), less the names `ignoreContainers` matches (`*` and `?`: a database that always runs, say).
- **Inhibitor locks:** a `block` lock on `shutdown` (logind). This is the one standard way for any job to say "do
  not restart now":

  ```
  systemd-inhibit --what=shutdown --mode=block --who=mutation-tests --why="hours left" ./run-mutation-tests
  ```

  `delay` locks (`unattended-upgrade-shutdown` holds one all day) do not count.

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

**The alert command** gets the text on stdin and `UPDATE_POLICY_EVENT` (`restart-pending`,
`live-restart-pending`, `restart-failed`, `back-up`, `drift`) and `UPDATE_POLICY_HOST` in a plain environment; it
runs as root from `/`, at most 60 seconds. Exit 0 is sent. A failure goes to the journal, fails the unit, and is
drift in the next baseline check until an alert goes out again.

**Ports** are `PROTO/PORT`, `PROTO/LOW-HIGH`, either with `@ADDRESS` or `@NETWORK` (`tcp/22`, `udp/32768-60999`,
`tcp/3773@100.64.0.0/10`, `tcp/8080@127.0.0.1`). Without an address an entry allows the port on every address. A
listener on every address (`0.0.0.0`, `::`) needs an entry without one (or `@0.0.0.0/0`, `@::/0`). TCP counts when
it listens, UDP when it is bound and unconnected.

## The daily baseline check

`update-policy baseline` (`update-policy-baseline.timer`, daily around 08:00) prints one line per drift and
nothing else; exit 1 when there is any, and the alert command gets the lines (`drift`). It checks:

- `unattended-upgrades` is installed; `APT::Periodic::Update-Package-Lists` and `Unattended-Upgrade` are on;
  `apt-daily.timer` and `apt-daily-upgrade.timer` are enabled; an `Allowed-Origins` or `Origins-Pattern` entry
  names a security origin;
- the restart policy is the kit's: apt's effective `Unattended-Upgrade::Automatic-Reboot` is false,
  `update-policy.timer` and `update-policy-boot.service` are enabled, and the last alert went out;
- no restart has been pending for more than `pendingDays` days;
- no listening port outside `baseline.ports` (with the program that holds it);
- ssh allows no password login: `sshd -T` says `passwordauthentication no`, and not keyboard-interactive with PAM
  (no sshd: nothing checked);
- no disk of `diskPaths` fuller than `diskPercent`.

A fact it cannot read is drift ("cannot read ..."), never a pass.

## Install

As root, from a commit on the fork's protected `ci-release` branch (the shared
[bootstrap](../common/bootstrap.sh) refuses anything else). Placeholders in CAPITALS. Write the machine file's
`update-policy` section first.

```
curl -fsSL https://raw.githubusercontent.com/FORK_OWNER/FORK/RELEASE_SHA/fork-kit/common/bootstrap.sh -o /root/fork-kit-bootstrap.sh
sh /root/fork-kit-bootstrap.sh --kit update-policy --repo FORK_OWNER/FORK --sha RELEASE_SHA --timer
update-policy status && update-policy idle; update-policy baseline
systemctl daemon-reload && systemctl enable --now update-policy.timer update-policy-baseline.timer && systemctl enable update-policy-boot.service
```

`--timer` writes:

- `/etc/apt/apt.conf.d/zz-update-policy`: `Unattended-Upgrade::Automatic-Reboot "false";`. Its name sorts after
  the numbered files, so it wins over an `Automatic-Reboot "true"` there (`50unattended-upgrades`, a hand-made
  `99-reboot`); not over `/etc/apt/apt.conf`, which apt reads last. The installer then reads apt's effective value
  and warns when it is still true; the baseline check reports it every day. Updates still install as before.
- the system units `update-policy.service` and `.timer` (every 5 minutes), `update-policy-baseline.service` and
  `.timer` (daily) and `update-policy-boot.service` (once after each boot), in `/etc/systemd/system/`.

A file at one of those paths that the kit did not write (its first line says so) is refused, never overwritten.
The installer never runs `systemctl`: enabling and disabling are the owner's (the command above). Files:
`/usr/local/libexec/update-policy/versions/<sha>/` (code, with `MANIFEST.sha256`), the link `current`,
`/usr/local/bin/update-policy`, `/usr/local/sbin/update-policy-install`, `/etc/update-policy/config.json` (only
whether `--timer` is on) and `installed.tsv`; the tick's state in `/var/lib/update-policy/`.
`update-policy-install self-check` reports drift of the installed files. Upgrade: the bootstrap with the new sha.

## Turn it off

- **For a while** (no restarts, no alerts): `systemctl disable --now update-policy.timer`. Restarts then stop
  altogether (unattended-upgrades still does not restart by itself); the baseline check reports the timer off.
- **Back to unattended-upgrades' own restarts:** `systemctl disable --now update-policy.timer
  update-policy-baseline.timer update-policy-boot.service && systemctl daemon-reload`, then
  `update-policy-install install --source /usr/local/libexec/update-policy/current --sha CURRENT_SHA --no-timer`
  (or the bootstrap with `--no-timer`): the apt file and the units go, and apt's own `Automatic-Reboot` applies
  again.
- **Remove:** disable the units as above, then `update-policy-install uninstall`. `--no-timer` and `uninstall`
  refuse while a unit is still enabled, so no enabled unit is left pointing at nothing.

## Runs again, stops half-way, old or empty state

- **Runs again:** the installer changes nothing when nothing changed. Two ticks at once: the second finds the lock
  (`/var/lib/update-policy/lock`) held and does nothing. A tick in the same window after a busy look waits for
  `retryMinutes`; after an alert, no second one for that window.
- **Stops half-way:** the installer's steps are the shared ones (any step, then run again); the units and the apt
  file are written only after the release they run is in use. A tick writes its state (whole, through a rename)
  before it restarts, so a restart is never asked for twice in a window; a tick killed before that only looks
  again.
- **Old, broken or missing state:** read as empty and said in the journal; the worst it costs is one more look or
  one more alert. A restart already made in this window is still known from the boot time.
- **Existing unattended-upgrades config:** kept as it is; only `Automatic-Reboot` is overridden while `--timer` is
  on (see Install). The kit never turns updates on or off; the baseline check reports when they are off.
- **No machine file, or no section:** the tick restarts nothing and logs the reason (at most once an hour while a
  restart is pending); `idle`, `status` and `baseline` say so and exit 1.
- **Clock and time zone:** the window is computed in `timeZone` on every tick, so a daylight saving change or a
  machine clock in UTC moves nothing. A start time the clock skips moves later by the gap; one it passes twice is
  the first.

## Tests

`tests/*.test.py`, run with `python3 -I -B fork-kit/update-policy/tests/NAME.test.py` from the repository root:
fake cgroups, `/proc`, sockets and programs in a temporary folder, a fixed clock. No root, no systemd, no network.
`tick.test.py` and `idle.test.py` end with a red check that breaks each rule in a copy of the code and shows its
test then fails. What every kit's installer does is tested in
[kit_install.test.py](../common/tests/kit_install.test.py); `install.test.py` covers this kit's own files.
