# heavy: run builds and tests in a capped slice, and stop what they leave behind

`heavy <command>` runs a command inside a systemd user slice with a memory limit (a "heavy" slice, so a build or a
test server cannot starve the rest of the machine), in a scope of its own. When the command ends, every process it
left running in that scope is stopped, and heavy says so on stderr:

```
heavy: stopped 3 leftover processes (sleep, java)
```

Nothing is printed when nothing was left. The line is there so a leak is seen and fixed where it starts (a test
that forgets its background job, a server a script never stops), instead of piling up for hours.

Linux with systemd (user manager) and cgroup v2 only. Python 3 standard library only.

## Use

| Command                      | What happens                                                                                      |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| `heavy <command> [args...]`  | runs the command in `heavy-<name>-<random>.scope` in the slice; stops leftovers when it ends      |
| `HEAVY_KEEP=1 heavy <cmd>`   | the same in `heavy-keep-<name>-<random>.scope`, but leftovers keep running (a daemon)             |
| `heavy --self-check`         | checks the config, that the slice has a unit with a memory limit, and the installed files         |
| `systemctl --user stop UNIT` | stops what `HEAVY_KEEP=1` kept (the name is in heavy's message)                                   |

The command gets the same folder, environment, stdin, stdout, stderr and open files. heavy exits with the command's
exit code, or dies of the same signal (a shell sees `128+N`), so scripts and `make` see what they saw before. The
environment is exactly the caller's (Python's own locale change is not passed on), plus `HEAVY_SCOPE`, the name of
the scope (a test can label what it starts with it). Signal settings are the caller's too: SIGPIPE and SIGXFSZ are
back to the default, so a writer into a closed pipe ends as before. Only `HEAVY_KEEP=1` makes a `heavy-keep-*`
scope (a command named `keep` gets `heavy-keep_-<random>.scope`), so the leftover sweep can tell kept ones apart.

**Signals.** When heavy gets INT, TERM, HUP or QUIT, the command gets it too: from the terminal (Ctrl-C reaches
both) or from heavy (when someone sent it to heavy alone; a signal sent to the whole process group, as `timeout`
or a shell's `kill %1` do, reaches the command once). heavy then waits up to the grace time (30 s by default, so a
command that handles Ctrl-C, a REPL or a server saving its data, has time) for the command, and stops the whole
scope (TERM, then KILL after the grace time again). A second signal stops it at once. A command that handles the
signal and exits keeps its own exit code. USR1 and USR2 are passed on. If heavy is killed with
`kill -9`, the command is killed with it (as before, when heavy and the command were one process); anything the
command had started keeps running in its scope (the leftover sweep of issue #159 is for that case).

**Never uncapped.** heavy refuses (exit 1, the reason on stderr, the command not run) when the systemd user
manager cannot be reached, when the slice has no unit file (systemd would make an empty one up on the fly, with no
limits), or when the slice has neither `MemoryMax` nor `MemoryHigh`.

**Nested.** `heavy` inside a `heavy` command runs the inner command in the outer scope; the outer heavy cleans it
up and prints the one line. With `HEAVY_KEEP=1`, the inner one gets a scope of its own (outside the outer one).
`HEAVY_KEEP` is in the command's environment, so commands it starts that also use heavy keep their leftovers too.

## Settings

`/etc/heavy/config.json` (written by the installer):

```json
{ "version": 1, "slice": "heavy.slice", "grace_seconds": 30 }
```

`HEAVY_CONFIG` names another file; `HEAVY_SLICE` and `HEAVY_GRACE` override one value (tests use them;
`HEAVY_SYSTEMCTL` names another systemctl). Without a config file the defaults above apply. `grace_seconds` is how
long a command gets after a signal, and leftovers after TERM, before KILL. If the last check of the scope fails (an
overloaded host: systemctl errors or times out), heavy says it could not check for leftovers, never nothing. The limits live in the slice unit, not here: either a unit you already have
(for example `~/.config/systemd/user/heavy.slice`), or one the installer writes to `/etc/systemd/user/` when given
limit options. A user's own unit in `~/.config/systemd/user/` wins over the one in `/etc/systemd/user/`.

## Files on the host

| Path                                       | What                                                                      |
| ------------------------------------------ | ------------------------------------------------------------------------- |
| `/usr/local/bin/heavy`                     | link to `current/bin/heavy`                                               |
| `/usr/local/sbin/heavy-install`            | link to `current/sbin/heavy-install`                                      |
| `/usr/local/libexec/heavy/versions/<sha>/` | the code of one release, with `MANIFEST.sha256` (root 755)                |
| `/usr/local/libexec/heavy/current`         | link to the release in use (switched in one step)                         |
| `/usr/local/libexec/heavy/replaced/`       | a plain `/usr/local/bin/heavy` from before the kit, put back on uninstall |
| `/etc/heavy/config.json`                   | slice name and grace time                                                 |
| `/etc/heavy/installed.tsv`                 | every installed path with its link target or hash (`--self-check`)        |
| `/etc/systemd/user/<slice>`                | only with limit options: the slice unit                                   |

## Install (owner, as root)

`RELEASE_SHA` is a commit on the fork's protected `ci-release` branch; the bootstrap refuses anything else. Every
merge to main that changes `fork-kit/` makes a new release commit, so the newest one is the head of `ci-release`.

```
sha=$(git ls-remote https://github.com/FORK_OWNER/FORK refs/heads/ci-release | cut -f1) && curl -fsSL "https://raw.githubusercontent.com/FORK_OWNER/FORK/$sha/fork-kit/heavy/bootstrap.sh" -o /root/heavy-bootstrap.sh && sh /root/heavy-bootstrap.sh --repo FORK_OWNER/FORK --sha "$sha"
```

It prints the sha, the hash of the download and every installed path. Add `--slice NAME`, `--grace SECONDS`, or
`--memory-high 5G --memory-max 6G --swap-max 1G --cpu-weight 40` to have it write the slice unit (then each user
runs `systemctl --user daemon-reload`). Upgrade: the same line again. Remove:
`/usr/local/sbin/heavy-install uninstall` (puts an old plain `/usr/local/bin/heavy` back). Check:
`heavy --self-check` (as the user: config, slice and files) or `heavy-install self-check` (files only).

## Runs again, stops half-way, old or empty state

- **Runs again:** every step checks what is there and only fixes what differs; a second run changes nothing.
- **Stops half-way:** the steps run in this order: check, folders, copy, self-test, config, slice unit, switch,
  links, record. A version folder is copied to a temporary name and renamed; a half-copied or changed one is made
  again. The old `/usr/local/bin/heavy` keeps working until the links step, which copies it to `replaced/` before
  the link replaces it. Running the same line again finishes the job.
- **Old state:** a plain-file `/usr/local/bin/heavy` (the one-line wrapper from before) is kept and put back by
  uninstall. A link there that points elsewhere, a folder where the kit expects its own, or a slice unit in
  `/etc/systemd/user/` the kit did not write is refused with a message, never overwritten. Config values stay
  unless an option names a new one.
- **Empty state:** a fresh machine installs with the defaults. heavy then refuses to run commands until the slice
  has a unit with a memory limit (install with limit options, or add the unit).

## Tests

`python3 -I -B fork-kit/heavy/tests/install.test.py` (the installer on a fake host in a temporary folder, no root)
and `python3 -I -B fork-kit/heavy/tests/heavy.test.py` (the wrapper with the real systemd user manager and a slice
with a memory limit, `HEAVY_TEST_SLICE`, default `heavy.slice`; it skips without one, and fails instead with
`HEAVY_REAL_TESTS=1`, as in CI). `HEAVY_TEST_BIN=fork-kit/heavy/tests/old-heavy.sh` runs the wrapper tests against
the one-line wrapper from before: every leftover test fails (the red check).
