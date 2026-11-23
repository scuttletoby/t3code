# gardener: a daily report of what has drifted, one issue per project

Agent-run projects drift in small ways nobody is watching: instruction files grow past their budget, a state table
goes stale, merged worktrees and branches pile up, a workflow gets switched off, an installed kit no longer matches
its release. `gardener` runs a list of checks for each project once a day and writes what they find to one report
issue per project, updated in place. It reports; it changes nothing, unless a check's config says `"fix": true`
(and then only what that check documents as its fix).

Linux, plain Python 3 standard library. A systemd user timer runs it every hour; each project runs once a day, in
its own window.

## What one run does

For each project in the config (or the one `--project` names):

1. **Done today?** The project's state file holds a done marker (the project's day, in its time zone; see
   [Window](#window)). If it is today, nothing happens: no check, no API call, unless the day's report issue is
   still to be written (then that alone is tried again). `--force` runs it again.
2. **Window** (only with `--timer`, which the timer passes): see [Window](#window).
3. **Checks**, one after another, each with its own timeout. A check that fails in any way becomes exactly one
   finding and the next check still runs (see [Checks](#checks)).
4. **Judgment** (optional): with a `model` configured, the findings go to a local model; see
   [Judgment](#judgment).
5. **Report**: the full report is saved as `report.md` and the issue's part as `issue.md` in the state folder.
   Then the done marker. Then the report issue is written (see [Report issue](#report-issue)); if that fails, only
   the issue is tried again on later ticks, at most 3 times a day; the checks and the model do not run again.

`gardener run --dry-run` runs the checks and prints both reports (the issue's part and the local one) instead. It writes nothing (no issue, no state, no
done marker), ignores the done marker and the window, and runs checks set to fix with fix off. `gardener check`
reads the config and lists each project and its checks; it runs nothing.

Exit codes: 0 fine (also: skipped, already done), 1 a project's run or report issue failed, 2 the config is
refused.

### Window

Without `quietHours` the project's day is its local date, and the timer runs it once a day, after the `schedule`
time if one is set (else at the first tick of the day). With `quietHours` (which may wrap past midnight) the day
starts when they start: with `22:00-06:00`, 01:00 on the 24th still belongs to the 23rd's window, so one window is
one run, and a tick missed at 23:00 runs at 01:00. The timer runs it only inside them, and a `schedule` inside them
holds it until that time (one outside them is not used).

## Config

The machine file's `"gardener"` section (`/etc/fork-kit/machine.json`, root-owned; [common](../common/README.md)).
A key it does not know, or a value it cannot use, refuses the whole section: a typo is never ignored. Project facts
(repositories, folders, project checks) live only here, never in the kit. An example with placeholder names:

```json
"gardener": {
  "stateDir": "~/.local/state/gardener",
  "gh": "gh",
  "projects": [{
    "name": "example",
    "repo": "OWNER/REPO",
    "reportRepo": "OWNER/REPO",
    "root": "/srv/example/repo",
    "schedule": "03:00",
    "quietHours": "02:00-07:00",
    "timeZone": "Europe/London",
    "model": { "endpoint": "http://127.0.0.1:8080/v1", "model": "example-local-model" },
    "checks": [
      { "name": "context-budget", "options": { "files": { "AGENTS.md": 6000 } } },
      { "name": "worktrees", "fix": true },
      { "path": "/srv/example/bin/doc-check", "id": "docs", "timeoutSeconds": 120 }
    ]
  }]
}
```

| Key                       | Default                    | Meaning                                                                                             |
| ------------------------- | -------------------------- | --------------------------------------------------------------------------------------------------- |
| `stateDir`                | `~/.local/state/gardener`  | state folder of the user that runs it; one subfolder per project                                    |
| `gh`                      | `gh`                       | the `gh` command (a name on PATH or an absolute path), with that user's login                       |
| `projects[].name`         | required                   | lower case letters, digits, `.`, `_`, `-`; names the state folder and the marker                    |
| `repo`                    | none                       | `OWNER/REPO` the checks look at (workflows-off's default)                                           |
| `reportRepo`              | none                       | `OWNER/REPO` of the report issue; none: `report.md` in the state folder only                        |
| `root`                    | none                       | the project's folder: each check runs in it; relative check options are inside it                   |
| `schedule`                | none                       | `HH:MM`: the timer runs the project after this local time; none: any time                           |
| `quietHours`              | none                       | `HH:MM-HH:MM` (may wrap past midnight): the timer runs it only inside them                          |
| `timeZone`                | the machine's              | for the day and the window                                                                          |
| `model`                   | none                       | `{endpoint, model, timeoutSeconds 120, maxFiles 5, maxFileBytes 20000}`                             |
| `checks[].name`           |                            | a general check of this kit (below)                                                                 |
| `checks[].path`           |                            | a project's own check: an absolute path or `~/...` (exactly one of the two)                         |
| `checks[].id`             | the name, or the file name | names the check in the report; unique per project                                                   |
| `checks[].options`        | `{}`                       | given to the check as it is                                                                         |
| `checks[].fix`            | `false`                    | the check may make its documented fix                                                               |
| `checks[].publicText`     | `false`                    | a project's own check: its text and fix (masked) go to the issue; see [Report issue](#report-issue) |
| `checks[].timeoutSeconds` | `300`                      | 1 to 3600; then the check and everything it started is stopped                                      |

Paths starting with `~/` are in the home of the user that runs the gardener.

## Checks

A check is any executable. It gets one JSON object on stdin:

```json
{
  "version": 1,
  "project": { "name": "example", "repo": "OWNER/REPO", "root": "/srv/example/repo" },
  "check": "docs",
  "options": {},
  "fix": false,
  "today": "2026-11-23",
  "gh": "gh"
}
```

It runs in the project root, in a process group of its own. It prints one finding per line on stdout and nothing
else (notes go to stderr), and exits 0:

```json
{ "id": "docs.stale", "severity": "warn", "where": "docs/setup.md", "text": "...", "fix": "..." }
```

`id`, `severity` (`error`, `warn` or `info`) and `text` are required; `where` (a path relative to the root, or
`OWNER/REPO: path`, ...) and `fix` (what a person does about it) are optional. Nothing else. An `id` is a letter or
digit, then up to 99 letters, digits, `.`, `_` or `-`. Python checks can use `gardener.contract.read_context()`
and `emit()`.

A check that cannot start, exits non-zero, runs past its timeout, prints a line that is not a finding or more than
256 KiB becomes **one** finding `gardener.check-failed` (severity error) naming why; nothing else it printed is
used. Its output is read as it comes: past 256 KiB, or past its timeout, the check and everything it started is
stopped, so a check that prints without end costs no memory. Only the last 4 KiB of its stderr are kept, for the
local report. An error inside the gardener itself on one check is one such finding too, and the next check runs.
More than 200 findings are cut to 200 and one `gardener.more-findings`.

### General checks

All report only, and none needs root. Options as in each script's header.

- **context-budget**: instruction files (`files`: `{path: bytes}`), memory indexes (`memoryIndexes`: size,
  `indexMaxChars` 6000, lines over `indexMaxLine` 160, entries whose file is missing, files the index does not
  list) and skills (`skillDirs`: each `*/SKILL.md` against `skillMaxBytes` 8000; a missing or broken one is a
  warning). Near a budget (`warnRatio` 0.85)
  is a note.
- **facts**: `facts --check --no-records` of the [agent-guards](../agent-guards/README.md) kit, in the project
  root: the machine's state tables and this project's, never `--all` (other projects' rows may be private). The
  issue names rows only of a table inside the repository; the machine's tables are only counted. Not installed:
  one note.
- **worktrees**: git worktrees and local branches (`repos`, default the root) that are merged into the base
  (`base`, default `origin/HEAD`, else `origin/main`, else `main`) or whose upstream is gone, and unmerged worktrees
  whose last commit is older than `staleDays` (14). `ignore`: branch patterns. No fetch: "merged" is as of the
  last fetch. A branch with no work of its own yet (a worktree just started, also one whose upstream is the base
  itself) is not "merged". Fix hints quote every name and keep a leading `~/` expandable. With `fix`, it runs
  `git worktree prune` (records of worktrees whose folder is gone) and nothing
  else; removing worktrees and branches is the [cleaner](../cleaner/README.md) kit's job, or a person's.
- **workflows-off**: GitHub Actions workflows of `repos` (default the project's `repo`) that are switched off, by
  hand or for inactivity; `ignore` lists workflow files meant to be off. One API call per repository. Never fixes:
  a workflow someone switched off is never switched on by a tool.
- **install-drift**: each fork kit's install record (`/etc/<kit>/installed.tsv`) against the files and links on
  the machine, as the user that runs it (paths it cannot read are one note, not drift). With `release` (a commit),
  kits on another release.

## Report issue

One issue per project in `reportRepo`, titled `Gardener report: NAME`. The report repository may be public, so the
issue is built from an allow-list and the full report stays on the machine (`report.md` in the state folder; the
issue says so):

- Per finding: its severity, check, id, and its `where` only when that is a path inside the project's repository
  (relative) or `OWNER/REPO: path`; its text and fix with what still looks like a local name (a path, also a
  Windows drive or `\\server` path, URL, IPv4 or IPv6 address, e-mail or `user@host`, a home folder encoded in a
  folder name) masked. The project root and home folder are replaced only as whole folders (`/srv/app` is not
  part of `/srv/app2`).
- The text and fix of a project's own check (`path`) are shown only with `"publicText": true`; otherwise the
  issue shows its id, count and `where`, and the text stays in the local report. General checks are shown.
- A finding whose `where` is outside the repository, or that has none, is only counted per check
  (`<outside repo>`), without its id. The gardener's own findings are shown, with its fixed words.
- **Counts only**: when the report repository is another repository and not known to be private, and the project
  repository is private (or its visibility cannot be read, or the project has no `repo`), the issue shows only
  counts per severity and check, and no judgment.
- A failed check shows its id and why in fixed words (`exited with code 1`); what it printed on stderr goes to the
  local report only.
- Notes are the gardener's own words; the model's endpoint is never shown (`model reachable` or `unreachable`).
- The judgment is masked like the rest.

A check must still not print secrets. Check authors: with `publicText`, everything your check prints in `text`
and `fix` (host names, user names, paths not masked above) may be published in the issue.

The issue body holds a hidden marker line, `<!-- gardener-report:NAME -->`; everything from the marker down is
rewritten on each run (a table of findings by severity, notes, the judgment), and text a person writes above the
marker is kept.

- **Missing** (never made, deleted, moved): a search by the title, then by the marker, finds an open one, or a new
  one is opened.
- **Closed**: left closed; a new one is opened. Closing the issue is how a person says "start over".
- **Marker edited away by hand**: that issue is no longer the report and is left alone; a new one is opened.
- **Two open issues with the marker**: the oldest is used, always the same one.

The whole body (a person's text above the marker too) is kept under GitHub's size limit: findings that do not fit
are counted, a person's text is cut with a note when it is too long, and `report.md` has them all.

API calls per project per run: at most one issue read (the number the state remembers), up to two searches (by
title, then by marker; only when that read does not give the open report issue) and one write; checks make their
own (workflows-off: one per repository). When the report repository is not the project repository, up to two
reads of their visibility (`repos/OWNER/REPO`), also in a dry run; a failed read only means counts only. Any other
GitHub error, a rate limit too, fails that project's report with exit 1. The done marker is already written, so
the checks do not run again: the next ticks try the issue alone, at most 3 times a day; after that, the next day's
run. There are no retries inside a run.

## Judgment

Optional, and phase 1 is the interface only. With `model` set, the findings as the issue shows them (nothing from
outside the repository) and a few of the files they name (`maxFiles`, each cut to `maxFileBytes`, only files git
tracks in the root: never an ignored or untracked file such as a local `.env`) go to the endpoint's OpenAI-style
`/chat/completions` in one request. The answer goes into the report under "Judgment", masked like the rest of the
issue. An endpoint that is not reachable, or answers with an error, nonsense or not HTTP at all, is a note
(`model unreachable`) and the run goes on. The endpoint is never shown. There is no fallback to a paid model, and
the model never sees the whole repository.

## State: runs again, stops half-way, old or empty state

`<stateDir>/<name>/`, mode 700: `state.json` (`doneDay`, the report `issue` number, `lastRun`, the finding count,
`reportPending`, `reportTries` and the `reportRepo` it last reported to), `report.md` (the last full report),
`issue.md` (the last issue part) and
`lock`. Each file is written to a temporary file and renamed into place.

- **Runs again** the same day: the done marker; nothing runs and no API call. A changed `reportRepo` runs the
  day again and writes to the new repository (the old issue number is dropped). `--force` runs it again and updates
  the same issue. Two runs at once: the second finds the lock held and skips the project.
- **Stopped half-way**: stopped before the done marker, the run starts again on the next tick. Stopped after it,
  the next tick sends the saved `issue.md` alone (`reportPending`). The issue number is saved once the issue is
  written, and the title and marker searches find it even if that save was lost, so a stop never makes a second
  issue.
- **Old, broken or missing state:** read as empty, with a note in the report; the issue is found again by a
  search. A state file of a **newer** version (a downgraded kit) is refused and kept: that project fails with
  exit 1 until the kit is upgraded again (`--dry-run` still runs).
- **Host off for days:** one run for today; days missed are not made up.
- **One project fails** (a broken state folder, a GitHub error, any error inside the gardener): the others still
  run; the exit code is 1.

## Install

As root, from a commit on the fork's protected `ci-release` branch (the shared
[bootstrap](../common/bootstrap.sh) refuses anything else). Placeholders in CAPITALS.

```
curl -fsSL https://raw.githubusercontent.com/FORK_OWNER/FORK/RELEASE_SHA/fork-kit/common/bootstrap.sh -o /root/fork-kit-bootstrap.sh
sh /root/fork-kit-bootstrap.sh --kit gardener --repo FORK_OWNER/FORK --sha RELEASE_SHA --timer
```

Installing never enables the timer for anyone. Add the `"gardener"` section to the machine file, then as the user
that runs it (with a `gh` login that can write issues to each `reportRepo`):

```
gardener check
gardener run --dry-run
systemctl --user daemon-reload && systemctl --user enable --now gardener.timer
loginctl enable-linger "$USER"     # as root, if the user is not always logged in: user timers need it
```

Files: `/usr/local/libexec/gardener/versions/<sha>/` (code, checks, with `MANIFEST.sha256`), the link `current`,
`/usr/local/bin/gardener`, `/usr/local/sbin/gardener-install`, `/etc/gardener/config.json` (only whether the timer
units are installed) and `installed.tsv`, and with `--timer` the user units `gardener.service` and `gardener.timer`
in `/etc/systemd/user/` (hourly, `Nice=10`, idle I/O, `UMask=0077`). `gardener-install self-check` reports drift.
Upgrade: the bootstrap with the new sha. Remove: first each user runs `systemctl --user disable --now
gardener.timer` (the uninstall says so before it removes the units), then `gardener-install uninstall` as root.
State folders and report issues stay.

## Tests

`tests/*.test.py`, run with `python3 -I -B fork-kit/gardener/tests/NAME.test.py` from the repository root: a fake
GitHub (in the process, and a fake `gh` script), a local fake model over HTTP, a fixed clock, temporary git
repositories and a temporary folder. No root, no systemd, no network. `RedCheck` in each file breaks each rule in a
copy of the code and shows its test then fails.

## General vs project

General (this kit): the runner, the check contract, the report issue, the state, the general checks and the timer.
Project (the machine file only): which projects, their repositories and folders, their schedule, their budgets and
their own checks, given by path. The kit never names a project or a project's check.
