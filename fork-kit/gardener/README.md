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

1. **Done today?** The project's state file holds a done marker (the day, in the project's time zone). If it is
   today, nothing happens: no check, no API call. `--force` runs it again.
2. **Window** (only with `--timer`, which the timer passes): the project runs once its `schedule` time has passed
   today and, with `quietHours`, only inside them.
3. **Checks**, one after another, each with its own timeout. A check that fails in any way becomes exactly one
   finding and the next check still runs (see [Checks](#checks)).
4. **Judgment** (optional): with a `model` configured, the findings go to a local model; see
   [Judgment](#judgment).
5. **Report**: the report issue is written (see [Report issue](#report-issue)) and a copy saved as `report.md` in
   the state folder. Then the done marker.

`gardener run --dry-run` runs the checks and prints the report instead. It writes nothing (no issue, no state, no
done marker), ignores the done marker and the window, and runs checks set to fix with fix off. `gardener check`
reads the config and lists each project and its checks; it runs nothing.

Exit codes: 0 fine (also: skipped, already done), 1 a project's run or report failed (it runs again on the next
tick), 2 the config is refused.

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

| Key                       | Default                    | Meaning                                                                           |
| ------------------------- | -------------------------- | --------------------------------------------------------------------------------- |
| `stateDir`                | `~/.local/state/gardener`  | state folder of the user that runs it; one subfolder per project                  |
| `gh`                      | `gh`                       | the `gh` command (a name on PATH or an absolute path), with that user's login     |
| `projects[].name`         | required                   | lower case letters, digits, `.`, `_`, `-`; names the state folder and the marker  |
| `repo`                    | none                       | `OWNER/REPO` the checks look at (workflows-off's default)                         |
| `reportRepo`              | none                       | `OWNER/REPO` of the report issue; none: `report.md` in the state folder only      |
| `root`                    | none                       | the project's folder: each check runs in it; relative check options are inside it |
| `schedule`                | `00:00`                    | the timer runs the project after this local time                                  |
| `quietHours`              | none                       | `HH:MM-HH:MM` (may wrap past midnight): the timer runs it only inside them        |
| `timeZone`                | the machine's              | for the day and the window                                                        |
| `model`                   | none                       | `{endpoint, model, timeoutSeconds 120, maxFiles 5, maxFileBytes 20000}`           |
| `checks[].name`           |                            | a general check of this kit (below)                                               |
| `checks[].path`           |                            | a project's own check: an absolute path or `~/...` (exactly one of the two)       |
| `checks[].id`             | the name, or the file name | names the check in the report; unique per project                                 |
| `checks[].options`        | `{}`                       | given to the check as it is                                                       |
| `checks[].fix`            | `false`                    | the check may make its documented fix                                             |
| `checks[].timeoutSeconds` | `300`                      | 1 to 3600; then the check and everything it started is stopped                    |

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
`OWNER/REPO: path`, ...) and `fix` (what a person does about it) are optional. Nothing else. Python checks can use
`gardener.contract.read_context()` and `emit()`.

A check that cannot start, exits non-zero, runs past its timeout, prints a line that is not a finding or more than
1 MiB becomes **one** finding `gardener.check-failed` (severity error) naming why; nothing else it printed is
used. More than 200 findings are cut to 200 and one `gardener.more-findings`.

### General checks

All report only, and none needs root. Options as in each script's header.

- **context-budget**: instruction files (`files`: `{path: bytes}`), memory indexes (`memoryIndexes`: size,
  `indexMaxChars` 6000, lines over `indexMaxLine` 160, entries whose file is missing, files the index does not
  list) and skills (`skillDirs`: each `*/SKILL.md` against `skillMaxBytes` 8000). Near a budget (`warnRatio` 0.85)
  is a note.
- **facts**: `facts --check --no-records` of the [agent-guards](../agent-guards/README.md) kit, in the project
  root: the machine's state tables and this project's, never `--all` (other projects' rows may be private). Not
  installed: one note.
- **worktrees**: git worktrees and local branches (`repos`, default the root) that are merged into the base
  (`base`, default `origin/HEAD`, else `origin/main`, else `main`) or whose upstream is gone, and unmerged worktrees
  whose last commit is older than `staleDays` (14). `ignore`: branch patterns. No fetch: "merged" is as of the
  last fetch. With `fix`, it runs `git worktree prune` (records of worktrees whose folder is gone) and nothing
  else; removing worktrees and branches is the [cleaner](../cleaner/README.md) kit's job, or a person's.
- **workflows-off**: GitHub Actions workflows of `repos` (default the project's `repo`) that are switched off, by
  hand or for inactivity; `ignore` lists workflow files meant to be off. One API call per repository. Never fixes:
  a workflow someone switched off is never switched on by a tool.
- **install-drift**: each fork kit's install record (`/etc/<kit>/installed.tsv`) against the files and links on
  the machine, as the user that runs it (paths it cannot read are one note, not drift). With `release` (a commit),
  kits on another release.

## Report issue

One issue per project in `reportRepo`, titled `Gardener report: NAME`. Its body holds a hidden marker line,
`<!-- gardener-report:NAME -->`; everything from the marker down is rewritten on each run (a table of findings by
severity, notes, the judgment), and text a person writes above the marker is kept.

- **Missing** (never made, deleted, moved): a search for the marker finds an open one, or a new one is opened.
- **Closed**: left closed; a new one is opened. Closing the issue is how a person says "start over".
- **Marker edited away by hand**: that issue is no longer the report and is left alone; a new one is opened.
- **Two open issues with the marker**: the oldest is used, always the same one.

Before it goes out, the report has the project root replaced by `.` and the home folder by `~`, so local paths do
not reach a public issue. A check must still not print secrets. The body is kept under GitHub's size limit; findings
that do not fit are counted, and `--dry-run` shows them all.

API calls per project per run: at most one issue read (the number the state remembers), one search (only when that
read does not give the open report issue) and one write; checks make their own (workflows-off: one per
repository). A GitHub error, a rate limit too, fails that project's run with exit 1 and no done marker, so the next
hourly tick tries again; there are no retries inside a run.

## Judgment

Optional, and phase 1 is the interface only. With `model` set, the findings and a few of the files they name
(`maxFiles`, each cut to `maxFileBytes`, only regular files inside the root) go to the endpoint's OpenAI-style
`/chat/completions` in one request. The answer goes into the report under "Judgment". An endpoint that is not
reachable, or answers with an error or nothing, is a note in the report and the run goes on. There is no fallback
to a paid model, and the model never sees the whole repository.

## State: runs again, stops half-way, old or empty state

`<stateDir>/<name>/`, mode 700: `state.json` (`doneDay`, the report `issue` number, `lastRun`, the finding count),
`report.md` (the last report) and `lock`. Both files are written to a temporary file and renamed into place.

- **Runs again** the same day: the done marker; nothing runs and no API call. `--force` runs it again and updates
  the same issue. Two runs at once: the second finds the lock held and skips the project.
- **Stopped half-way**: the done marker is written last, so a run stopped before it runs again on the next tick.
  The issue number is saved as soon as the issue is written, and the marker search finds it even if that save was
  lost, so a stop never makes a second issue.
- **Old, broken or missing state:** read as empty, with a note in the report; the issue is found again by its
  marker (one search).
- **Host off for days:** one run for today; days missed are not made up.
- **One project fails** (a broken state folder, a GitHub error): the others still run; the exit code is 1.

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
Upgrade: the bootstrap with the new sha. Remove: each user runs `systemctl --user disable --now gardener.timer`,
then `gardener-install uninstall` as root. State folders and report issues stay.

## Tests

`tests/*.test.py`, run with `python3 -I -B fork-kit/gardener/tests/NAME.test.py` from the repository root: a fake
GitHub (in the process, and a fake `gh` script), a local fake model over HTTP, a fixed clock, temporary git
repositories and a temporary folder. No root, no systemd, no network. `RedCheck` in each file breaks each rule in a
copy of the code and shows its test then fails.

## General vs project

General (this kit): the runner, the check contract, the report issue, the state, the general checks and the timer.
Project (the machine file only): which projects, their repositories and folders, their schedule, their budgets and
their own checks, given by path. The kit never names a project or a project's check.
