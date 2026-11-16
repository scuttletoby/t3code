# desk: a desktop's side of a fork fleet desk pool

Turns a Linux machine (or WSL2 on Windows) into one pool of the fork fleet: the desk agent dials out to the
always-on host over ssh, and the dispatcher there starts one-job runners on this machine through it. Everything
the fleet runbook ([.github/fork-fleet.md](../../.github/fork-fleet.md), "The desktop's side") lists by hand,
done by one installer, with a PASS/FAIL check and an uninstall.

## The machine file

The facts of this machine go in the `desk` section of `/etc/fork-kit/machine.json` (root-owned; see
[common](../common/README.md)). Made-up example:

```json
{
  "version": 1,
  "desk": {
    "pool": "pc",
    "host": "fleet-host.example",
    "hostKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...",
    "slots": 2,
    "reserveMb": 4096
  }
}
```

`pool` must be a pool in the fleet config that is installed (the release's own has `pc`; a project's
`--fleet-config` has its own pools); `host` and `hostKey` are the always-on host and its ssh host key
(on that host: `cat /etc/ssh/ssh_host_ed25519_key.pub`, without the comment). Optional: `port` (22), `user` (`fleet-desk`), `slots` (1, at most 8; one
runner user each), `reserveMb` (memory the launcher keeps free for the owner), `netBase` (`10.N`, the runners' network,
default `10.204`: pick another when this machine's network already uses it), `freeCommand` (a free/v1 script,
[FREE.md](../common/FREE.md): `fits` then also needs it to answer free; the kit installs an example as
`/opt/fork-fleet/free-memory`; default none, the memory rule alone).

## Install, check, uninstall

As root, from the fork's protected release branch:

```sh
sh bootstrap.sh --repo OWNER/REPO --sha RELEASE_SHA [--fleet-config /path/to/project/fork-fleet.json]
sh bootstrap.sh --repo OWNER/REPO --sha RELEASE_SHA --release-fleet-config   # back to the release's own
/opt/fork-fleet/desk-install check        # every line PASS, then ALL PASS
/opt/fork-fleet/desk-install uninstall
```

`--fleet-config` is the project's fleet config (its pools); without it the release's own is used. It must be in a
folder only root (or you) can write; a link is followed, and the folders of both the link and the file it names are
checked. The project's is kept in `/etc/fork-fleet/fleet-config.json` and the choice is
recorded before any file is swapped, so a later install of a new release keeps it, even after a stopped run;
`--release-fleet-config` goes back to the release's own. Install prints the desk key's public half: add it on
the always-on host with `fork-fleet-desk-key.py add POOL`. The first runner template is made in the background by
the refresh timer; `check` says FAIL for `template` until it is there.

`check` only reads: the installed files against their manifest (text, root owner and mode), the folders' and the
desk key's modes and root owner (not a link), the project's fleet config against the installed one, each unit against
the release's (header plus `/opt/fork-fleet/units/UNIT`, mode and root owner), a restart a stopped install still owes, systemd drop-ins that change the kit's units,
every wall of this OS (`fork-fleet-walls.py describe`), the agent's settings, the units, the template, the settings
files against the machine file (text, mode and root owner), the runner users (each must exist, have no login shell,
a locked password, a uid other than 0, its own group only, no home that is there (`/nonexistent`) and no line in
`cron.allow`/`at.allow`; and it names a user the kit made that a smaller `slots` no longer uses) and IPv4
forwarding.

A `fork-runner-N` user made by hand before the install is used, but only when it passes the same checks (no login
shell, locked password, uid not 0, its own group only, no home that is there, not in an allow file; the launcher
refuses the same uid and group cases at each job start): otherwise install refuses before changing anything and
names the fix (`usermod -s /usr/sbin/nologin`, `passwd -l`, `gpasswd -d`, `usermod -d /nonexistent`). A
`/opt/fork-fleet`, `/opt/fork-fleet/template`, `/etc/fork-fleet` or desk key that is a link or not owned by root (a
hand setup) is refused the same way, not followed or fixed. One install or uninstall runs at a time (a lock on
`/etc/fork-fleet`, which only root can open); a second one is refused.

## Windows (WSL2)

[windows/start-desk.ps1](windows/start-desk.ps1) does the Windows side and then this kit inside one WSL2 distro.
Save the machine file on Windows, then in a normal (not administrator) Windows PowerShell window:

```powershell
powershell -ExecutionPolicy Bypass -File start-desk.ps1 -Repo OWNER/REPO -Sha RELEASE_SHA -MachineFile machine.json
```

It prints the PC's facts and asks before it changes anything; the question lists each `.wslconfig` key it will
change, old value -> new. Before the question it checks the machine file and
`-FleetConfig` exist and refuses a WSL 1 distro of that name (`wsl -l -v`; WSL 1 runs no systemd:
`wsl --set-version DISTRO 2` first). A distro of that name that the script did not make
is named in the question (its whole `/etc/wsl.conf` is replaced) and must be confirmed by typing its name. Then: the
distro (`-Distro`, default `fork-desk`, Ubuntu 24.04, installed with `--version 2`; a run stopped while installing it
leaves a mark in `%LOCALAPPDATA%\fork-desk`, so the next run asks no takeover question, and a broken one names
`wsl --unregister DISTRO`); `.wslconfig`'s `[wsl2]` memory cap (`-MemoryGB`, default
three quarters of the RAM, at most the RAM less 6 GB, never under 4 GB; one that leaves Windows under 2 GB is
refused), swap (`-SwapGB`, 4), NAT and DNS tunnelling, for every WSL distro (read and written as UTF-8 without a
BOM; a dated copy of the old file is kept, other keys and sections stay; an old `networkingMode` or `dnsTunneling`
line under `[experimental]`, the form before WSL 2.0 that can still win over `[wsl2]`, is removed, and the check
FAILs while one is there; the question lists each change, old -> new, and the file is written only when it lists one,
so "no change" means the file is left alone); the distro's own settings the WSL2 walls
ask for (`/etc/wsl.conf`: systemd on, no Windows drives, programs or PATH; its own `/etc/resolv.conf`; WSL's binfmt
handler masked); one `wsl --shutdown` when one of these changed or the running WSL does not have them yet (PID 1 not
systemd, interop live, Windows drives mounted, `wsl.conf` changed after PID 1 started as far as the VM's clock tells
(`/proc/1/stat` and `btime`; unreadable means restart; the clock skips the PC's sleep, so this alone can miss a change,
hence the mark below), `.wslconfig` newer than the running WSL, more memory than the cap, not NAT, or a `.wslconfig`
or `wsl.conf` change whose shutdown is still owed: one mark in `%LOCALAPPDATA%\fork-desk` for every distro, like
`.wslconfig` itself, written before either change and removed after the shutdown, because WSL's uptime skips sleep),
so a run stopped before its shutdown restarts next time (every WSL distro and Docker Desktop stop); the packages the fleet needs; the
machine file and `-FleetConfig`, if given; this kit's bootstrap; a logon task that keeps the distro running while
the owner is logged in. Last, `desk-install check` and the Windows side's own PASS/FAIL lines (the `[wsl2]` keys);
any FAIL or error exits 1. Safe to run again. `-Uninstall` (needs no `-Repo` or `-Sha`) runs `desk-install uninstall` in the distro, stops and removes the logon
task and names the first `.wslconfig.bak-*` (the file before the first run); the distro and `.wslconfig`
stay (`wsl --unregister DISTRO` removes the distro), and so does an owed shutdown's mark.

Known limits: the `DISTRO.made` mark is per distro name; a run stopped inside `wsl --install`, then that distro
removed by hand and a distro of the same name made by hand, would skip the takeover question once (the mark is gone
after the next run's settings step). The PowerShell parts are tested only where `pwsh` is installed; the in-distro
parts run in the tests on a fake root.

## Runs again, stops half-way, old or empty state

- **Again:** files, settings and units that did not change are not rewritten (a file in `/opt/fork-fleet`, a
  setting or a unit whose mode or owner was changed, or that is a link, is written again as root's; the folders'
  modes are set back; a linked or foreign-owned folder or key is refused, above), and the agent is not stopped or
  restarted (a restart would end a job it is starting). After a change it is restarted. The agent reads its settings
  only when it starts, so before the first change the record gets a restart-pending mark, cleared after the
  restart: a run stopped in between restarts the agent on the next run. The mark is also set before a new fleet
  config choice is saved, so a run stopped between saving the choice and the swap fails `check` until the next run.
- **Stopped half-way:** the next install ends it. A new release is staged in `/opt/fork-fleet/.kit-stage-*` and
  checked first (gate loads with the fleet config, the pool is in it, `bash -n`, the walls know this OS); a release
  that fails is never installed. Files are renamed in one by one while the agent and the refresh timer are stopped;
  `SOURCE` is written
  last, so `check` reports a half-done swap. Users are recorded in `/etc/fork-fleet/desk-kit.json` before they are
  made, so uninstall removes them even after a stop.
- **Old record:** a record that names a project's fleet config while neither `/etc/fork-fleet/fleet-config.json`
  nor the installed copy is there is refused with the fix (`--fleet-config FILE` or `--release-fleet-config`).
- **Old install:** a launcher, `launch.conf`, `desk.conf`, desk key, unit, or a systemd drop-in for one of the kit's
  units (`UNIT.d/*.conf`, also the dash-prefix forms) that this kit did not write is refused before anything changes.
  The type-wide `service.d/` and `timer.d/` folders are not checked: they hold the distribution's defaults for every
  unit (refusing them would refuse a stock system), and only root can write them. Remove the older (by hand) install first, the runbook's steps in reverse.
- **Empty:** `check` before an install says FAIL and writes nothing; `uninstall` with no record does nothing.

Uninstall stops every runner (through the launcher, then `systemctl stop` for any left). While one still runs it
stops there and changes nothing else: the `fork_fleet` nft table, users and record stay; run it again. Then it
removes every `fork-fleet-*` namespace and `ffh-*` link, the nft table, the kit's units, the files of its manifest,
the template and runner copies, its settings and key, the users it made, and `cron.deny`/`at.deny` when the kit
made them (otherwise the lines it added, for every runner user, also one that was there before). The record goes last. Anything else in its folders (a file the kit
did not make, a drop-in) is kept and named. IPv4 forwarding stays on until the next reboot.

## Why one flat folder

The other kits keep one folder per version behind one link. The desk agent refuses a launcher or walls file whose
folder path has a link in it (so nobody can point it elsewhere), and every fleet default names `/opt/fork-fleet`, so
this kit installs flat there: changed files are renamed in, a manifest records each hash.
