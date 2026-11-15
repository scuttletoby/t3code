# Desk link key tool (`desk-key`)

Fork only. `.github/scripts/fork-fleet-desk-key.py` runs on the always-on host. It keeps the
`authorized_keys` lines of the desk link: one line per desk pool, which lets that pool's desktop
log in only to run the desk link's `serve` for its own pool (row D2, `fork-fleet-desk.py`). The
fleet runbook is `.github/fork-fleet.md`.

```
desk-key = sudo -u fleet-desk -H /usr/bin/python3 -I /opt/fork-fleet/fork-fleet-desk-key.py --config /opt/fork-fleet/fork-fleet.json [--user NAME]

desk-key add POOL < desktop-key.pub   # add POOL's line (one ssh-ed25519 public key on stdin)
desk-key status [POOL]                # POOL's line, or every desk pool's and every retired pool's
desk-key remove POOL                  # remove POOL's line, also after POOL left the fleet config
```

The line it writes, and the only kind it removes:

```
restrict,command="/usr/bin/python3 -I /opt/fork-fleet/fork-fleet-desk.py serve POOL" ssh-ed25519 AAAA... fork-fleet-desk:POOL
```

Every other byte of the file stays as it was. Lines are split at `\n` only, as sshd splits
them. A line that names `fork-fleet-desk:POOL` but that the tool cannot read as one of its own
(for example one with a lone `\r` or another control byte in it, or a login line whose comment
contains a desk line) is shown by `status` and `remove` and never changed: edit it by hand.

## Exit codes

| Code | Meaning                                                                                                                                                                                          |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0    | done; `status`: every line is the exact line this version writes                                                                                                                                 |
| 1    | `status` or `remove` found a problem: a wrong line (run `remove`, then `add`), a line of a retired pool (run `remove POOL`), or a line naming a pool that the tool cannot read (edit it by hand) |
| 2    | refused, nothing changed: bad input, unknown pool, wrong user, unsafe file, bad config, bad usage                                                                                                |
| 3    | `status`: a desk pool has no line (and nothing else is wrong)                                                                                                                                    |
| 4    | error: an unexpected failure (for example a full disk); the file is the old one or the new one, never half                                                                                       |

## The desk-link user

The key goes into the `authorized_keys` of one dedicated, unprivileged user (default
`fleet-desk`), never root's. The tool runs as that user and refuses:

- root, and any user other than `--user`;
- a user in an admin group: `root`, `sudo`, `wheel`, `admin`, `adm`, `docker`, `lxd`, `libvirt`,
  `systemd-journal` (primary or supplementary);
- a system account (uid 999 or below) unless its name is the default `fleet-desk`, so a daemon's
  account (for example a database's) never gets the link; a user with another name needs a uid of
  1000 or more;
- a `$HOME` that is not the user's home in the password database (sshd reads that one; use
  `sudo -H`);
- a `~/.ssh`, `authorized_keys` or home (when it makes `~/.ssh`) that the user does not own, that
  is a symlink, or that group or others can write (sshd's StrictModes would ignore the file).

## Retiring a desktop

Take its pool out of the fleet config, then run `desk-key remove POOL`. `status` without a pool
reports any line left for a pool the config no longer has: "key for retired pool POOL still
allows a login: run desk-key remove POOL" (exit 1).

## What the setup (D8) must guarantee

The tool cannot check these from the desk-link user's side. The setup row (D8) makes them true
and its PASS/FAIL check proves them:

1. **The user and its password field.** The desk-link user is a system user with only its own
   group: no `sudo`, `wheel`, `adm`, `docker` or `systemd-journal`, and no sudoers rule. Its shell
   is `/bin/sh` (sshd runs the forced command through it, so `nologin` would close the link). Its
   password field is `*`: not empty, and not `!` either, because with `UsePAM no` OpenSSH treats a
   `!` account as locked and refuses even the key. Its home and `~/.ssh` are owned by it and are
   not group- or world-writable.
2. **sshd settings for that user.** A `Match User <desk-link user>` block with
   `AuthenticationMethods publickey`, `DisableForwarding yes`, `PermitTTY no`, `PermitTunnel no`,
   `PermitUserRC no`, `AllowAgentForwarding no`, `X11Forwarding no`. `PermitUserEnvironment`
   stays `no`. No `PermitRootLogin` change. If `AllowUsers` or `AllowGroups` is set, the user is
   added to it. The check runs `sshd -T -C user=...,host=...,addr=...` and checks that
   `authorizedkeysfile` is `.ssh/authorized_keys` and that those values hold.
3. **The /run folder.** `/run/fork-fleet-desk` is made by tmpfiles, owned by the desk-link user,
   mode 0710 with the socket group, and the desk link's `launch` accepts that owner.
4. **Root-owned install files.** `/opt/fork-fleet`, `fork-fleet-desk.py` and this tool are owned
   by root and not writable by the desk-link user; `/usr/bin/python3` exists (the forced command
   names it by its full path).
5. **A key used for nothing else.** The desktop's link key is made only for the link and kept by
   root on the desktop. The check confirms, as root and read-only, that its fingerprint is in no
   other `authorized_keys` on the host (root's, an admin's).
