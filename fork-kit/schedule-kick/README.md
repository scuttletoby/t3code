# schedule-kick: start scheduled workflows on time, from a host

GitHub starts `schedule` workflows on a best-effort basis, and on busy days that means hours late: a daily cron
created 8 hours after its time, a 5-minute cron that starts only every few hours. `schedule-kick` is a small
command that a systemd timer runs every 5 minutes. For each workflow listed in a config file it sends one
`workflow_dispatch` once the workflow's cron time (plus a few minutes' grace) has passed and no run of it exists for
that time yet. The workflow keeps its `schedule` trigger as a fallback (for when the host is off). GitHub's own
`schedule` run for a time the tool already started still comes, often hours later: a slot runs once only when the
workflow's first job is the [late check](#late-schedule-runs) below. Without it, a kicked slot can run twice.

Linux with systemd (user units). Plain Python 3 standard library; nothing to install but the kit.

## What one tick does

For each entry of the config:

1. **Slot:** the latest time the entry's cron names (UTC, like GitHub) that is at least the grace ago
   (`graceMinutes`, 5 by default): GitHub's own run, when it comes on time, is then found and nothing is sent.
2. **Done already?** If the state file says this slot was handled, nothing happens (no API call).
3. **After an error** it waits 5 minutes before the next call for that entry, doubling up to an hour.
4. **One runs-list call:** a `schedule` or `workflow_dispatch` run of the workflow on the entry's `ref`, created at
   or after the slot (GitHub's, one by hand, an earlier dispatch), means the slot is done. A `pull_request` or
   `push` run, or one on another branch, does not count: anyone can make a pull request run, and it must not hold
   off the scheduled job. Otherwise **one** `workflow_dispatch` with the entry's ref and inputs.
5. **Record** the slot in the state file (written to a temporary file, then renamed into place).

The workflow needs a `workflow_dispatch:` trigger (with the inputs the entry sends). At most one runs-list call and
one dispatch per entry per tick: 50 entries at most, so even a full config stays far inside the API budget. With
`ghAs` count about twice that (`gh` first looks the workflow file up, and `gh-as` may fetch a token for each call),
out of the App installation's budget, which every user of that App shares.

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
  ],
  "heads": [{ "repo": "OWNER/REPO", "workflow": "tests.yml", "graceMinutes": 10, "maxPerPass": 3 }],
  "keepOn": [{ "repo": "OWNER/REPO", "workflows": ["tests.yml", "follow.yml"] }]
}
```

`entries`, `heads` and `keepOn` are each optional, but a config needs at least one entry in one of them. `heads`
and `keepOn` are described in [Heads](#heads-every-trusted-head-gets-a-run) and [Keep-on](#keep-on-turn-named-workflows-back-on).

- `cron` is a copy of the workflow's own `schedule` line. Each field takes a number, `*`, `A-B`, `*/N`, `A-B/N`
  or a comma list of those. Anything else (names such as `MON`, `@daily`, `?`, `L`, six fields, `5/15`) is
  refused, so the tool never reads a line differently from GitHub. When both day fields are set, a day matches
  either (as in GitHub's cron).
- A workflow with several `schedule` lines: only the entry's line is kicked. Its runs list cannot tell the lines
  apart, so an on-time run of another line created after this line's slot counts as this slot's run. The late
  check (below) never ends a run of another line.
- One entry per repository and workflow file: the run list cannot tell two entries for the same workflow apart.
- `inputs` are strings (`"true"`, not `true`) without control characters.
- `graceMinutes` (0 to 60, default 5), for all entries at the top level or for one entry: how long after its slot
  an entry waits for GitHub's own run before it dispatches. Keep it above any gap between the host's clock and
  GitHub's (see Clock jumps). A cron whose slots can come less than `graceMinutes` + 5 minutes (the timer's tick)
  apart is refused: its dispatch would land after the next slot and count for it, so only every other slot would
  run. `*/5` needs `graceMinutes: 0` (safe with the late check: GitHub's on-time run then finds the dispatch and
  ends); a line with slots under 5 minutes apart cannot be kicked.
- `ref` should be the default branch: GitHub's `schedule` runs are on it, and only runs on `ref` count.
- `auth` is exactly one of:
  - `{"ghAs": "/usr/local/bin/gh-as"}`: the [gh-apps kit](../gh-apps/README.md)'s App login (`gh run list` and
    `gh workflow run`, both on its list). The App needs Actions read and write on each listed repository. The path
    must end in `gh-as` (never `gh` itself), and its real file must be root's, in folders only root can change, as
    the gh-apps kit installs it: a user's own copy (`~/.local/bin/gh-as`) is refused.
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
  than an hour ahead are dropped. Forward: one run for the new slot, as for a host that was off. A host clock a
  little ahead of GitHub's, with the state file lost: the grace puts the dispatch after the slot in GitHub's time
  too, so the run list still finds it (with `graceMinutes: 0` it may not, and the slot can run twice).
- **GitHub errors:** one entry's error, or a fault in the tool, does not stop the others; it waits (5 minutes,
  doubling, at most an hour) before trying again. A tick that breaks outside one entry writes `ok: false` and the
  reason to the status file and exits 1. A rate limit (also GitHub's secondary limit: a 403 or 429 with
  `retry-after`, or its message), or fewer than 100 calls left on the token, pauses every entry until GitHub's
  reset or `retry-after` (at most an hour; 15 minutes if GitHub gives none, as `gh-as` never does). `gh-as` reports
  no count of calls left, so with `ghAs` only a rate-limit error pauses. No retries inside a tick.
- **Many other runs:** one runs-list call reads up to 100 runs of the ref since the slot. If more than 100 push runs
  come first, the slot's run is not seen and one dispatch is sent: the tool errs towards running, never towards
  being held off.

## Heads: every trusted head gets a run

GitHub drops the events of a workflow while it is switched off, so a pull request head or a push to the default
branch can end up with no run at all. Each tick, for each `heads` entry, makes **one** GraphQL call: the default
branch's tip and the 100 most recently updated open pull requests, each head commit with its GitHub Actions check
suites. Only trusted heads count: the default branch's tip and heads of pull requests from a branch of the same
repository; a pull request from another fork is never dispatched. A head with any run of the entry's workflow
(any event, any state) needs nothing. A head without one is recorded the first time it is seen, and once it has been
seen for `graceMinutes` (0 to 60, default 10) it gets **one** `workflow_dispatch` on its branch with the inputs
`ref` (the head's commit) and `head` (`true`); at most `maxPerPass` (1 to 20, default 3) a tick, the default branch
first, then the pull requests updated last.

The workflow needs a `workflow_dispatch` with a `ref` input and a boolean `head` input that makes the run test its
own commit (`github.sha`, where its checks land), not the commit `ref` names: if the branch moved between the
GraphQL call and the dispatch, the new head is the one that is tested and gets the result. A branch whose workflow
file is older than the `head` input refuses the dispatch (HTTP 422): that commit is recorded as refused and not
sent again (its next push brings the new file). With `ghAs` the App needs Actions write and read access to pull
requests and contents.

| Case                                                     | What happens                                                                                             |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Runs again                                               | a head is recorded as sent before the call and never sent twice, also when its run is slow to show       |
| Stops half-way                                           | sent, state not written: the run's check suite shows next tick, nothing is sent again                    |
| Sent, but no run shows within 30 minutes                 | one ALERT line and an error exit (`lost`), not sent again                                                |
| No runs at all, empty or broken state                    | first tick records the heads; the check suites are what stop a second dispatch, not the state file       |
| A head force-pushed mid-pass                             | a pull request whose head moved while GitHub answered waits a tick; old heads are dropped from the state |
| More check suites on a commit than read (50), none a run | not sent (cannot tell), with a note                                                                      |
| API error                                                | that entry waits (5 minutes, doubling, at most an hour); a rate limit or a low GraphQL budget pauses     |

Budget: one GraphQL call a tick per entry (a few points of the 5,000 an hour) plus at most `maxPerPass` dispatches.

## Keep-on: turn named workflows back on

`schedule-kick keep-on` (its own timer, every 15 seconds) makes one workflow-list call per `keepOn` entry and turns
back on (`PUT .../enable`) each workflow the entry names that is `disabled_manually` or `disabled_inactivity`. Each
re-enable is one `ALERT` line in the journal (`journalctl --user -u schedule-kick-keep-on`) and counts in
`keep-on.json`. A named workflow that is missing, in another state (`disabled_fork`), or that cannot be turned on
(an API refusal) is an error line, exit 1, and a message through the machine's [alert
channel](../common/README.md#the-alert-channel) (event `workflow-off`), at most once an hour for each workflow. It
never switches a workflow off (the GitHub client has no call that could), and it never touches a workflow the entry
does not name. The token needs Actions write. Budget: one call per entry every 15 seconds (240 an hour) plus one
per re-enable. `schedule-kick status --keep-on` prints the last pass. Runs again: an `active` workflow needs no call;
two passes at once: the second does nothing (its own lock). Stopped after a re-enable, before the state is written:
the next pass finds it on. A broken state file reads as empty. A rate limit pauses keep-on until GitHub's reset.

## Late schedule runs

GitHub's `schedule` run for a slot the tool already started still comes, often hours late, and nothing on the host
can stop it. A `concurrency` group does not help: it only queues or cancels runs that overlap. So a workflow kicked
by this tool runs twice per slot **unless** its first job is `schedule-kick late-check`:

- on the first attempt of a `schedule` run of the kicked line (`SCHEDULE_KICK_CRON`, the entry's `cron`; the
  run's line is `github.event.schedule`) it takes that line and the run's own created time, which gives the slot
  the run belongs to. If a `workflow_dispatch` run of the same workflow on the same branch was created at or after
  that slot, the slot already ran: it writes `skip=true` to the job's outputs;
- on any other event, a re-run, a run of another `schedule` line, or with no such dispatch, `skip=false`;
- when it cannot tell (no token, no `SCHEDULE_KICK_CRON`, an API error, a cron line it does not read),
  `skip=false` with a warning: the run goes ahead, as it would without the check.

Paste this into the listed workflow (placeholders in CAPITALS: the fork and a release commit, as for Install; pin
`actions/checkout` the way the repository pins its actions, `KICKED_CRON` the entry's `cron`). Give each job that
needs no other job `needs: late` and the `if:` of `work` below. Give a job that needs other jobs, like `more`,
`!cancelled()` and `needs.JOB.result == 'success'` for each job it needs: without `!cancelled()` GitHub skips it
whenever `late` was skipped (every event but `schedule`), and without the results it would run after a job it
needs failed. A job's own condition goes inside, `&& (OWN) }}`.

```yaml
jobs:
  late:
    # GitHub's late schedule run for a slot schedule-kick already started ends here (fork-kit/schedule-kick)
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      actions: read
    outputs:
      skip: ${{ steps.check.outputs.skip }}
    steps:
      - uses: actions/checkout@CHECKOUT_SHA
        with:
          repository: FORK_OWNER/FORK
          ref: RELEASE_SHA
          path: schedule-kick
          sparse-checkout: fork-kit/schedule-kick
          persist-credentials: false
      - id: check
        run: python3 -I -B schedule-kick/fork-kit/schedule-kick/bin/schedule-kick late-check
        env:
          SCHEDULE_KICK_TOKEN: ${{ github.token }}
          SCHEDULE_KICK_CRON: "KICKED_CRON"

  work:
    needs: late
    if: ${{ !cancelled() && needs.late.outputs.skip != 'true' }}
    runs-on: ubuntu-latest
    steps:
      - run: echo "the workflow's own steps"

  more:
    needs: work
    if: ${{ !cancelled() && needs.work.result == 'success' }}
    runs-on: ubuntu-latest
    steps:
      - run: echo "a job after work"
```

`!cancelled()` lets `work` run when `late` is skipped (every event but `schedule`) or fails; `work` needs only
`late`, so no failure gate is lost. One window stays: a late run whose check runs while a tick is between its
runs-list call and its dispatch sees no dispatch yet, so that slot runs twice; nothing guards it (a `concurrency`
group only keeps the two runs from overlapping). If the workflow has a `concurrency` group, use
`cancel-in-progress: false`: with `true`, a late run that comes while the dispatched run is still going cancels
it, then finds that dispatch and skips itself, so the slot does no work. A re-run always runs.

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
whether the timer is on) and `installed.tsv`, and with `--timer` the user units `schedule-kick.service`,
`schedule-kick.timer`, `schedule-kick-keep-on.service` and `schedule-kick-keep-on.timer` in `/etc/systemd/user/`
(a user with `keepOn` also runs `systemctl --user enable --now schedule-kick-keep-on.timer`). `schedule-kick-install self-check` reports drift. Upgrade: the
bootstrap with the new sha. Remove: each user runs `systemctl --user disable --now schedule-kick.timer schedule-kick-keep-on.timer`, then
`schedule-kick-install uninstall` as root. Users' own config and state folders stay (delete them by hand).

The service runs with `UMask=0077` and no other sandboxing: `gh-as` gets the App token through `sudo`, which
`NoNewPrivileges=` blocks, and in a user unit `PrivateTmp=`, `ProtectSystem=` and the like turn on
`PrivateUsers=`, where `sudo` cannot become root either. A user with `tokenFile` auth (no `sudo`) can add them in
a drop-in: `systemctl --user edit schedule-kick.service`, then `NoNewPrivileges=yes`, `PrivateTmp=yes`,
`ProtectSystem=strict`, `StateDirectory=schedule-kick` and `StateDirectoryMode=0700` (systemd makes the state
folder if it is missing, so the drop-in works before the first tick). These need unprivileged user namespaces,
which some distributions restrict: check `systemctl --user status schedule-kick.service` after adding them.

## Tests

`tests/*.test.py` (`late.test.py` for the late check, `heads.test.py` and `keepon.test.py` for those parts), run with
`python3 -I -B fork-kit/schedule-kick/tests/NAME.test.py` from the repository root: a fake GitHub (in the process,
a local HTTP server and a fake `gh-as`), a fixed clock and a temporary folder. No root, no systemd, no network. `RedCheck` in each file breaks every rule in a copy of the code and shows its test
then fails.
