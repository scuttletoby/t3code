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
    "pool": "desktop",
    "host": "fleet-host.example",
    "hostKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...",
    "slots": 2,
    "reserveMb": 4096
  }
}
```

`pool` must be a pool in the fleet config; `host` and `hostKey` are the always-on host and its ssh host key
(on that host: `cat /etc/ssh/ssh_host_ed25519_key.pub`, without the comment). Optional: `port` (22), `user` (`fleet-desk`), `slots` (1, at most 8; one
runner user each), `reserveMb` (memory the launcher keeps free for the owner), `netBase` (`10.N`, the runners' network,
default `10.204`: pick another when this machine's network already uses it).

## Install, check, uninstall

As root, from the fork's protected release branch:

```sh
sh bootstrap.sh --repo OWNER/REPO --sha RELEASE_SHA [--fleet-config /path/to/project/fork-fleet.json]
/opt/fork-fleet/desk-install check        # every line PASS, then ALL PASS
/opt/fork-fleet/desk-install uninstall
```

`--fleet-config` is the project's fleet config (its pools); without it the release's own is used. The project's
is remembered, so a later install of a new release keeps it. Install prints the desk key's public half: add it on
the always-on host with `fork-fleet-desk-key.py add POOL`. The first runner template is made in the background by
the refresh timer; `check` says FAIL for `template` until it is there.

`check` only reads: the installed files against their manifest, every wall of this OS (`fork-fleet-walls.py
describe`), the agent's settings, the units, the template, the runner users and IPv4 forwarding.

## Runs again, stops half-way, old or empty state

- **Again:** files, settings and units that did not change are not rewritten, and the agent is not stopped.
- **Stopped half-way:** the next install ends it. A new release is staged in `/opt/fork-fleet/.kit-stage-*` and
  checked first (gate loads with the fleet config, the pool is in it, `bash -n`, the walls know this OS); a release
  that fails is never installed. Files are renamed in one by one while the agent is stopped; `SOURCE` is written
  last, so `check` reports a half-done swap. Users are recorded in `/etc/fork-fleet/desk-kit.json` before they are
  made, so uninstall removes them even after a stop.
- **Old install:** a launcher, `launch.conf`, `desk.conf`, desk key or unit that this kit did not write is refused
  before anything changes. Remove the older (by hand) install first, the runbook's steps in reverse.
- **Empty:** `check` before an install says FAIL and writes nothing; `uninstall` with no record does nothing.

Uninstall stops every runner, removes the namespaces and the `fork_fleet` nft table, the kit's units, files and
settings, and only the users it made itself.

## Why one flat folder

The other kits keep one folder per version behind one link. The desk agent refuses a launcher or walls file whose
folder path has a link in it (so nobody can point it elsewhere), and every fleet default names `/opt/fork-fleet`, so
this kit installs flat there: changed files are renamed in, a manifest records each hash.
