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
4. **Judgment** (optional): with a `model` configured, the findings go to a model (a local one, by design) for a
   draft opinion; see [Judgment](#judgment).
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
  "gh": "gh",
  "cacheDir": "/srv/example/state/example/cache/docs",
  "dryRun": false
}
```

`cacheDir` is this check's own folder in the project's private state folder, kept between runs (the check makes it
when it needs it); in a dry run (`dryRun` true) a check writes nothing there. Both came later in version 1: read
them with `.get()`.

It runs in the project root, in a process group of its own. It prints one finding per line on stdout and nothing
else (notes go to stderr), and exits 0:

```json
{ "id": "docs.stale", "severity": "warn", "where": "docs/setup.md", "text": "...", "fix": "..." }
```

`id`, `severity` (`error`, `warn` or `info`) and `text` are required; `where` (a path relative to the root, or
`OWNER/REPO: path`, `#N` for a pull request or issue of the project's repository, ...) and `fix` (what a person does about it) are optional. Nothing else. An `id` is a letter or
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
- **machine-tools**: the tools the machine needs and the ones it runs without a source. Each of `tools`
  (`{path, source}`, a path absolute or `~/...`) that is not there is an error (`machine-tools.missing`) that names
  where it comes from. A machine that is set up again or moved is told what it still lacks. Each entry of the
  `scan` folders (default `~/.local/bin`, `~/.config/systemd/user`, `/usr/local/bin`, `/usr/local/sbin`,
  `/usr/local/libexec`, `/etc/systemd/system`; `[]` scans none) needs a source:
  - a kit's install record (the 3- and 6-column rows install-drift reads, and pc-agents' `sha256<TAB>path` rows);
  - a link to a file committed in a git checkout (one `git ls-files` per checkout);
  - a link into the distribution's own folders (`/usr`, `/lib`, but not `/usr/local`);
  - a `tools` entry.

  Anything else is one warning per entry:
  - `no-source`: put there by hand, with no review, tests or history;
  - `untracked`: in a checkout, but not committed;
  - `broken-link`;
  - `unknown-source`: in a checkout git cannot read (a worktree whose main checkout is gone, an empty `.git` folder,
    another user's checkout). It fails closed: such an entry is never taken as committed.

  `ignore`: entry names (globs) that need no source (default: snapd's generated `snap-*` and `snap.*` units, and
  `__pycache__`). Only the `.wants`, `.requires`, `.upholds` and `.d` folders of unit folders, a
  mask (a unit linked to `/dev/null`) and a unit linked to another unit in the same folder (not a subfolder) are not
  entries of their own. Every path option (`tools` paths, `scan`) is absolute or `~/...`, and `tools` must be a list:
  anything else exits 1. Each finding's `where` is a machine path, so the report issue only
  counts them; the local report names each one. The [machine move](../../docs/fork/machine-move.md) template starts
  from this list. Never fixes.

- **review-tiers**: merged pull requests (last `days`, 7) of the project's own repository (`repo`; there is no
  option for another one) that had less review than their tier asks for. Without options it judges by the merge
  gate's own `review` block in the queue config (`queueConfig`, default `.github/pr-queue.json`, read from git at
  `base`); with `tiers` by the project's tiers: `rules` (globs to a tier), a `registry` file in the repository (rows
  of paths with a tier, for example a list of modules), `unmatched`, and per tier `requires` {`minLenses`,
  `lenses`: [{`name`, `match` (a regex on lens names), `count`}]}.
  Lens comments are read by the gate's own code (`fork-ci/actions/pr-queue`, copied into the install as `gate/`),
  so a comment counts exactly when the gate would take it as a lens, and only those posted by the queue config's
  lens authors; a valid override comment before the merge settles a pull request, and so does a comment of a
  counted author whose first line starts with one of `waivers` (a person decided, at any time; anyone the queue
  counts for lenses can waive, the pull request's own author too). `exemptLabels`, `exemptAuthors`. Inline review
  comments are not read; a pull request with more than 100 files is judged at the top tier (with the gate's
  `review` block: as critical).
  Findings: `review-tiers.skipped` (warn) no lens at all before the merge; `review-tiers.short` lenses before the
  merge, but not the ones its tier asks for (warn at the top tier or critical, else info); `review-tiers.late`
  (info) the lenses before and after the merge together are enough: reviewing a pull request after its merge
  clears the warning and leaves this note.
  GitHub: `gh api rate_limit` (free) and one paged GraphQL search per run (`maxPages`, 8; give the check about 10
  seconds a page in its `timeoutSeconds`); below `minRemaining` (1000) points left this hour it does not search and
  judges from its cache. The cache (in `cacheDir`) keeps per pull request only what the verdict needs (paths, lens
  names, no comment text), so the next run searches only pull requests updated since the day before: a page or two.
  A search cut off half-way (`maxPages`, the budget) is a `review-tiers.partial` warning; the cache keeps where it
  stopped, and the next runs go on from there (older pull requests first) until every pull request in the window was
  read, then search from the new day. A GitHub error fails the check and writes nothing; a page GitHub cannot answer
  is asked again once with fewer pull requests. An old, broken or other-repository cache, or one written by other
  gate code (a kit upgrade), is read as empty, a broken resume point is dropped, and a newer cache is never
  overwritten. Its findings name the pull request (`#N`). Never fixes: a review is done by people.

## Report issue

One issue per project in `reportRepo`, titled `Gardener report: NAME`. The report repository may be public, so the
issue is built from an allow-list and the full report stays on the machine (`report.md` in the state folder; the
issue says so):

- Per finding: its severity, check, id, and its `where` only when that is a path inside the project's repository
  (relative), `OWNER/REPO: path`, or a pull request or issue of the project's repository (`#N`, written
  `OWNER/REPO#N` when the report goes to another repository); its text and fix with what still looks like a local
  name (a path, also a Windows drive or `\\server` path, URL, IPv4 or IPv6 address, e-mail or `user@host`, a home
  folder encoded in a folder name) or a well-known token shape (`ghp_`, `github_pat_`, `sk-`, `xoxb-`, `AKIA`, a
  private key block) masked. The project root and home folder are replaced only as whole folders (`/srv/app` is not
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
- The judgment (a model draft) is masked like the rest.

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

Optional. With `model` set, the findings as the issue shows them (nothing from
outside the repository) and a few of the files they name (`maxFiles`, each cut to `maxFileBytes`, only files git
tracks in the root: never an ignored or untracked file such as a local `.env`) go to the endpoint's OpenAI-style
`/chat/completions` in one request. The answer goes into the report under "Judgment (model draft)" with a line
that nobody checked it; it passes the same filter as check text (masked; left out when the issue shows counts
only). An endpoint that is not reachable, takes longer than `timeoutSeconds` in all (connecting and reading the
whole answer, so a server that trickles bytes is cut off too), or answers with an error, nonsense or not HTTP at
all, is a note (`model unreachable`) and the run goes on. The endpoint is never shown, and the model never sees
the whole repository.

There is **no fallback to a paid model**, on purpose: the projects this was made for allow only their chosen large
paid models for agent work, and a cheap paid model is the wrong trade, while a local model costs nothing. A project
that does want a paid model can point `endpoint` at an OpenAI-compatible gateway itself.

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

General (this kit): the runner, the check contract, the report issue, the state, the general checks (review-tiers
too: tiers and how a review is shown come from options, the lens rules from the merge gate) and the timer.
Project (the machine file only): which projects, their repositories and folders, their schedule, their budgets,
their review tiers and waiver words, and their own checks, given by path. The kit never names a project or a
project's check.
