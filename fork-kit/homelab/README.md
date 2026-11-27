# homelab: the machines of a home lab, and test-machine leases

`homelab check` says whether this machine's private machine file describes the lab's machines correctly and
whether each one answers. `homelab lease` lets any project's agents share a test machine: one holder at a time,
and a lease that ends by itself. The kit names no machine: names, hosts and addresses live only in the private
machine file of the machine the agents run on (fork-kit/common/README.md, The machine file).

Part of #857 (owner decision in #1073): only what every host layout needs. Virtual-machine verbs (status, power,
guest commands, file push), timers and backups wait for the server design (#900).

Used from a checkout of the fork for now (`python3 -I -B fork-kit/homelab/bin/homelab ...`, or a link to it): the
installer and the bootstrap entry come in their own pull request.

## The machine file

The `homelab` section of `/etc/fork-kit/machine.json` (macOS: `/Library/Application Support/fork-kit/machine.json`),
written by the owner as root. Made-up values; [machine.example.json](../common/machine.example.json) has them too:

```json
{
  "version": 1,
  "homelab": {
    "leaseDir": "/var/lib/fork-homelab/leases",
    "machines": [
      {
        "name": "test-box",
        "host": "test-box.example",
        "port": 22,
        "roles": ["test"],
        "note": "shared test machine",
        "lease": { "defaultMinutes": 60, "maxMinutes": 480 }
      },
      { "name": "build-box", "host": "192.0.2.20", "roles": ["build"] }
    ]
  }
}
```

| Key                | What                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------ |
| `leaseDir`         | absolute folder of the leases; required when a machine has `lease`                         |
| `machines[].name`  | lower-case letters, digits and `-`, unique; what agents type                               |
| `machines[].host`  | host name or IP address                                                                    |
| `machines[].port`  | the port `homelab check` asks for an answer (default 22)                                   |
| `machines[].roles` | short words a project may filter on (default none)                                         |
| `machines[].note`  | free text for people                                                                       |
| `machines[].lease` | present: the machine can be leased. `defaultMinutes` (60) and `maxMinutes` (480, ≤ 7 days) |

Every rule of the machine file holds (root owner, no link, no unknown key, no secrets); a mistake is refused with
the key named, never ignored.

## homelab check

```
homelab check [--no-reach] [--timeout SECONDS] [--json]
```

One line per finding, `ok` or `FAIL`; exit 1 when any line fails. It changes nothing. `--json` prints the same
findings for scripts: `{"ok": true|false, "checks": [{"ok", "what", "machine", "text"}]}`, where `what` is
`machineFile`, `leaseDir`, `lease` or `reach` and `machine` is the machine's name or `null`.

- the machine file and its `homelab` section (a missing file says so, and where to read how to write it);
- the lease folder: there, a folder and not a link, not writable by everyone (o+w), and writable by the user who
  runs the check;
- each lease in it: free, or who holds it until when; a file the kit did not write fails;
- each machine: a TCP connect to `host:port` within the timeout (`--timeout`, 1 to 60 s, default 3), nothing
  sent. `--no-reach` skips this.

## Leases

```
homelab lease take MACHINE --holder HOLDER [--minutes N] [--note TEXT]
homelab lease release MACHINE --holder HOLDER
homelab lease list [--json]
```

- **take** gives the lease to `HOLDER` (for example `project/agent-name`) for `N` minutes (the machine's
  `defaultMinutes` when left out, at most `maxMinutes`). Taking a lease you hold renews it from now. A lease another
  holder holds is refused with exit code **3**, naming the holder and when it ends, so a script can wait and try
  again. `--note` says what it is for: one printable line of at most 200 characters (others see it in
  `list`).
- A lease **ends by itself** at its time: then anyone may take it, and `take` says whose lease ended.
- **release** gives your lease back; another holder's live lease is refused (exit 3). Releasing a free or expired
  lease is fine (exit 0).
- **list** shows each machine that can be leased: free, or holder, end time and note. `--json` for scripts.

Exit codes: 0 done, 1 the machine file or a lease cannot be used, 2 a usage error, 3 held by another holder.

How: one file per machine, `<leaseDir>/<name>.json`, and one lock file, `<name>.lock`. Every change takes the
lock (`flock`), so two takers at once never both win; the lease file is written beside it and renamed over it in
one step, so a reader never sees half a lease, and a take stopped half-way leaves the old lease or the new one. A
lease file the kit did not write (not JSON, another machine's, a link) is refused, never overwritten: look at it,
then remove it. A lock file that is not a plain file (a FIFO, a folder) is refused, not waited on. Every file is
reached through one open handle on the folder, so a folder swapped for a link while a command runs is not followed.
A temp file left by a take that stopped half-way is removed by the next take or release of that machine (under its
lock, so no take is still writing it).

The folder is shared by every agent user that may take leases. The owner makes it once, as root, with the agents'
group (files are written group-writable, mode 664):

```
install -d -m 2775 -g AGENTS_GROUP /var/lib/fork-homelab/leases
```

Leases are an agreement between agents, not a wall: a user who can write the folder can remove a lease. A folder
anyone can write (mode o+w, sticky or not) is refused by every command and fails `check`.

## Tests

```
python3 -I -B fork-kit/homelab/tests/lease.test.py
python3 -I -B fork-kit/homelab/tests/check.test.py
```

Temporary folders only, no root, no network beyond 127.0.0.1. Each test file's RedCheck breaks one guard at a time
in a copy of the kit and shows the test for it then fails.
