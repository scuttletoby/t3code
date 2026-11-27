# homelab: the machines of a home lab, and leases for shared test machines

`homelab` reads the home lab's machines from the private machine file (one per machine,
[fork-kit/common/README.md](../common/README.md)), checks them, and leases shared test machines, so any project's
agents can tell who uses a test machine now and until when. The kit itself names no machine, host or address:
those facts live only in the machine file.

Linux and macOS. Plain Python 3 standard library.

This is the small part of the homelab kit (#857, owner decision in #1073): the machine file, `homelab check` and
leases. Commands that talk to a virtual-machine host (status, power, guest commands, file push) wait for the new
server layout (#900).

## Use

| Command                                                                  | What happens                                                                                                |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `homelab check [--json]`                                                 | the machine file's `homelab` section, the lease folder, whether each machine answers on its port, its lease |
| `homelab lease take MACHINE --holder WHO [--minutes N] [--purpose TEXT]` | leases the machine to WHO for N minutes (default `defaultMinutes`); refused while it is leased              |
| `homelab lease renew MACHINE --holder WHO [--minutes N]`                 | WHO's live lease now ends N minutes from now                                                                |
| `homelab lease release MACHINE --holder WHO`                             | ends WHO's lease; a machine that is not leased is fine                                                      |
| `homelab lease list [--json]`                                            | each machine and its live lease                                                                             |

Exit codes: 0 done (or all fine), 1 refused (leased to someone else, not leased to renew) or a problem found by
`check`, 2 a bad request or a machine file or lease folder that cannot be used. Every message names the file or
key and the reason.

`WHO` is one word that says who holds the machine, for example `example-project/agent-3`; the lease also records
the user that took it. `check` connects to each machine's `host:port` (TCP, `checkTimeoutSeconds`) and reports one
that does not answer; it changes nothing.

## The machine file's section

```json
{
  "version": 1,
  "homelab": {
    "machines": [
      { "name": "test-a", "host": "test-a.lan.example", "port": 22, "note": "shared test machine" },
      { "name": "test-b", "host": "192.0.2.20" }
    ],
    "leases": {
      "dir": "/var/lib/fork-kit/homelab/leases",
      "defaultMinutes": 60,
      "maxMinutes": 480
    },
    "checkTimeoutSeconds": 3
  }
}
```

| Key                     | What                                                                              |
| ----------------------- | --------------------------------------------------------------------------------- |
| `machines[].name`       | lowercase letters, digits and inner dashes; each name once                        |
| `machines[].host`       | a host name or address (never starting with `-`)                                  |
| `machines[].port`       | the port `check` connects to (default 22)                                         |
| `machines[].note`       | free text, shown by `check --json`                                                |
| `leases.dir`            | the lease folder (absolute path; below)                                           |
| `leases.defaultMinutes` | a lease's length when `--minutes` is not given (default 60, at most `maxMinutes`) |
| `leases.maxMinutes`     | the longest lease (default 480)                                                   |
| `checkTimeoutSeconds`   | how long `check` waits for each machine (1 to 60, default 3)                      |

An unknown key, a wrong type or a bad value is refused, so a typo is never silently ignored. The example values
are made up (`.example` names, a documentation address); the real ones stay in the machine file.

## Leases

One file per leased machine, `<dir>/<machine>.json`: the holder, the user, the purpose, when it was taken and when
it ends. A lease ends at its `until` time, whether or not the holder released it: after that the machine is free
again, and the next `take` replaces the expired lease (and says whose it was). A second `take` of a leased machine
is refused, also to the same holder (`renew` extends a lease); only the holder renews or releases it.

Every change happens under one lock (`<dir>/.lock`, `flock`), and a lease file is written to a temporary file first
and renamed into place: two takers at once never both win, and a stop half-way leaves the old lease or the new one.
A lease file that is not a whole lease (damaged by hand, say) fails closed: nobody can tell who holds the machine,
so `take` refuses and names the file; check who uses the machine, then remove it. No link is followed.

The owner makes the lease folder once, writable by the group of every user whose agents take leases, and not by
everyone (a folder everyone can write is refused):

```
install -d -m 2770 -g <lease-group> /var/lib/fork-kit/homelab/leases
```

Leases are a convention between cooperating agents, not a security boundary: anyone who can write the folder can
remove a lease file by hand. They stop two projects from using one test machine by mistake.

## Tests

```
python3 -I -B fork-kit/homelab/tests/check.test.py
python3 -I -B fork-kit/homelab/tests/leases.test.py
```

Temporary files only, no root, no network beyond `127.0.0.1`. Each test file ends with a red check that breaks each
rule in a copy of `lib/homelabkit/` and shows its test then fails.
