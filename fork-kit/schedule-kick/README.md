# schedule-kick: start scheduled workflows on time, from a host

GitHub starts `schedule` workflows on a best-effort basis, and on busy days that means hours late: a daily cron
created 8 hours after its time, a 5-minute cron that starts only every few hours. `schedule-kick` is a small
command that a systemd timer runs every 5 minutes. For each workflow listed in a config file it sends one
`workflow_dispatch` once the workflow's cron time has passed and no run of it exists yet. The workflow keeps its
`schedule` trigger as a fallback (for when the host is off), and its own `concurrency` group stays the last guard.

Linux with systemd (user units). Plain Python 3 standard library; nothing to install but the kit.

## What one tick does

For each entry of the config:

1. **Slot:** the latest time at or before now that the entry's cron names (UTC, like GitHub).
2. **Done already?** If the state file says this slot was handled, nothing happens (no API call).
3. **After an error** it waits 5 minutes before the next call for that entry, doubling up to an hour.
4. **One runs-list call:** a run of the workflow created at or after the slot, by any event (`schedule`, by hand,
   an earlier dispatch), means the slot is done. Otherwise **one** `workflow_dispatch` with the entry's ref and
   inputs.
5. **Record** the slot in the state file (written to a temporary file, then renamed into place).

The workflow needs a `workflow_dispatch:` trigger (with the inputs the entry sends). At most one runs-list call and
one dispatch per entry per tick: 50 entries at most, so even a full config stays far inside the API budget.

## Config

`~/.config/schedule-kick/config.json` of the user whose timer runs it (`$XDG_CONFIG_HOME` if set; `--config` for
another file). Project facts (repositories, workflow files, crons) live only here, never in the kit. An example
with placeholder names:

```json
{
  "version": 1,
  "auth": { "ghAs": "/usr/local/bin/gh-as" },
  "entries": [
    { "repo": "OWNER/REPO", "workflow": "nightly.yml", "cron": "17 4 * * *", "ref": "main" },
    {
      "repo": "OWNER/OTHER",
      "workflow": "sweep.yml",
      "cron": "*/15 * * * *",
      "ref": "main",
      "inputs": { "mode": "quick" }
    }
  ]
}
```

- `cron` is a copy of the workflow's own `schedule` line. Each field takes a number, `*`, `A-B`, `*/N`, `A-B/N`
  or a comma list of those. Anything else (names such as `MON`, `@daily`, `?`, `L`, six fields, `5/15`) is
  refused, so the tool never reads a line differently from GitHub. When both day fields are set, a day matches
  either (as in GitHub's cron).
- One entry per repository and workflow file: the run list cannot tell two entries for the same workflow apart.
- `inputs` are strings (`"true"`, not `true`).
- `auth` is exactly one of:
  - `{"ghAs": "/usr/local/bin/gh-as"}`: the [gh-apps kit](../gh-apps/README.md)'s App login (`gh run list` and
    `gh workflow run`, both on its list). The App needs Actions read and write on each listed repository. The path
    must end in `gh-as`: never `gh` itself.
  - `{"tokenFile": "/path/to/file"}`: a fine-grained token with **Actions: read and write** on the listed
    repositories only, in a file of this user with mode 600 (anything else is refused).

  A person's own login is never used: `GH_TOKEN`, `GITHUB_TOKEN` and gh's stored login are not read.

A config the tool does not fully understand (bad JSON, an unknown key, a bad cron, a token file others can read,
two entries for one workflow) is refused as a whole: nothing is sent, the reason goes to the journal and to the
status file, and the exit code is 2. `schedule-kick check` reads the config and prints each entry's last slot,
without the network.

## State and status

`~/.local/state/schedule-kick/` (`$XDG_STATE_HOME` if set; `--state-dir` for another folder), mode 700:
`state.json` (the last slot handled per entry, errors and waits), `status.json` (the last tick: `ok` and one line
per entry) and `lock`. `schedule-kick status` prints the status and exits 1 unless the last tick was fine. Every
tick also logs one line per entry to the journal (`journalctl --user -u schedule-kick`); an error exits 1, so
`systemctl --user --failed` shows it.

## Runs again, stops half-way, old or empty state

- **Runs again** (same slot): the state file says the slot is handled; no API call, no second dispatch. Two ticks
  at once: the second finds the lock held and does nothing.
- **Stopped half-way** (dispatch sent, state not written): the next tick's runs-list call finds that run and
  records the slot; nothing is sent again. The state is written after each entry, so a stop keeps what is done.
- **Old, broken or missing state:** read as empty and reported; the run list still stops a second run, so the
  only cost is one runs-list call per entry. Entries no longer in the config are dropped from it.
- **Host off or asleep past slots:** only the latest slot counts, so a host back after a day sends at most one
  run per entry, never a burst.
- **Clock jumps:** back: the slot is an older one whose run exists, so nothing is sent; waits that now lie more
  than an hour ahead are dropped. Forward: one run for the new slot, as for a host that was off.
- **GitHub errors:** one entry's error does not stop the others; it waits (5 minutes, doubling, at most an hour)
  before trying again. A rate limit, or fewer than 100 calls left on the token, pauses every entry until GitHub's
  reset (at most an hour; 15 minutes if GitHub gives none). No retries inside a tick.
- **Late `schedule` runs** still come: the workflow's `concurrency` group (or a check of the event) decides what
  they do. A late `schedule` run created after a later slot counts as that slot's run.

## Install

As root, from a commit on the fork's protected `ci-release` branch (the shared
[bootstrap](../common/bootstrap.sh) refuses anything else). Placeholders in CAPITALS.

```
curl -fsSL https://raw.githubusercontent.com/FORK_OWNER/FORK/RELEASE_SHA/fork-kit/common/bootstrap.sh -o /root/fork-kit-bootstrap.sh
sh /root/fork-kit-bootstrap.sh --kit schedule-kick --repo FORK_OWNER/FORK --sha RELEASE_SHA --timer
```

Then, as the user that runs it (a user with no person's login is best; with `ghAs`, one of the gh-apps kit's
agent users):

```
mkdir -p ~/.config/schedule-kick && $EDITOR ~/.config/schedule-kick/config.json
schedule-kick check
systemctl --user daemon-reload && systemctl --user enable --now schedule-kick.timer
loginctl enable-linger "$USER"     # as root, if the user is not always logged in: user timers need it
```

Files: `/usr/local/libexec/schedule-kick/versions/<sha>/` (code, with `MANIFEST.sha256`), the link `current`,
`/usr/local/bin/schedule-kick`, `/usr/local/sbin/schedule-kick-install`, `/etc/schedule-kick/config.json` (only
whether the timer is on) and `installed.tsv`, and with `--timer` the user units `schedule-kick.service` and
`schedule-kick.timer` in `/etc/systemd/user/`. `schedule-kick-install self-check` reports drift. Upgrade: the
bootstrap with the new sha. Remove: each user runs `systemctl --user disable --now schedule-kick.timer`, then
`schedule-kick-install uninstall` as root. Users' own config and state folders stay (delete them by hand).

## Tests

`tests/*.test.py`, run with `python3 -I -B fork-kit/schedule-kick/tests/NAME.test.py` from the repository root: a
fake GitHub (in the process, a local HTTP server and a fake `gh-as`), a fixed clock and a temporary folder. No
root, no systemd, no network. `RedCheck` in each file breaks every rule in a copy of the code and shows its test
then fails.
