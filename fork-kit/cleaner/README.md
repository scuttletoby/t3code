# cleaner: free disk space by rules, and never delete what could be needed

`fork-cleaner` removes what a host no longer needs: finished git worktrees, unused anonymous Docker volumes and old
temp folders. Which folders, which repositories and how many days come from the machine file. What must never go
is in the code and cannot be switched off.

It lives in `fork-kit` because it runs on a machine (not in CI and not inside T3), reads its facts from the
machine file like every kit, and ships with the kits in `ci-release`. `fork-ci` holds only what CI itself runs.

The core, three rules, an [installer](#install) and one [daily timer](#the-daily-timer). While the tools it is
to replace still delete on a host, the timer runs in compare mode: it plans, reports and compares what it would
delete with what they would, and deletes nothing.

| Part         | File                                                    | What it does                                                  |
| ------------ | ------------------------------------------------------- | ------------------------------------------------------------- |
| config       | `lib/cleaner/config.py`                                 | reads the `cleaner` section of the machine file               |
| facts        | `lib/cleaner/facts.py`                                  | reads the process list, systemd units, git, GitHub and Docker |
| guard        | `lib/cleaner/guard.py`                                  | the hard refusals: asked about every path, twice              |
| rules        | `lib/cleaner/rules.py`                                  | `worktrees`, `docker`, `tmp`: what each wants gone, and why   |
| `plan`,`run` | `lib/cleaner/engine.py`                                 | the two commands, the lock and the log                        |
| `report`     | `lib/cleaner/report.py`                                 | one line: GB per class, and what runs freed                   |
| `compare`    | `lib/cleaner/compare.py`                                | its plan next to the dry runs of the tools it replaces        |
| `daily`      | `lib/cleaner/daily.py`                                  | what the timer runs: compare mode or run mode                 |
| installer    | `sbin/fork-cleaner-install`, `bootstrap.sh`, `systemd/` | the version folder, the links, the timer                      |

## Commands

```
fork-cleaner plan    # prints what a run would delete and what it keeps; changes nothing
fork-cleaner run     # deletes it; one log line per delete
fork-cleaner report  # one line for the daily report: GB per class, and what runs freed; deletes nothing
fork-cleaner report --out FILE   # the same, appended to FILE before the mark moves
fork-cleaner report --no-mark    # the same, and the mark stays: the next report counts these deletes again
fork-cleaner compare # its plan next to the old tools' dry runs; deletes nothing
fork-cleaner daily   # what the timer runs (cleaner.daily.mode)
```

`plan` writes no file, takes no lock and makes no log. `run` takes a lock (one run at a time), plans, then reads
everything about each item **again** just before its delete, dropping every answer the plan read: the files
themselves, git, `git ls-remote`, the process list (with command lines and file locks), the mount list, the
systemd unit files, the pull request lists and Docker's list.

**When GitHub is asked.** The pull request lists of a repository are read once for the plan, and again before
every worktree or clone delete of that repository (never for a temp entry or a volume). Each read is two `gh pr
list` calls (merged and open, `--limit 1000`); gh fetches 100 rows per GraphQL page, so one read costs
ceil(merged/100) + ceil(open/100) GraphQL requests, at least 2 and at most 20. A run that deletes N worktrees
and clones of one repository makes N + 1 reads. Each item's last check also runs `git ls-remote` against GitHub
(git, not the API).

**How a delete is done.** A temp entry or a clone is removed through open folder descriptors, one name at a
time, never through a link: the entry must still be the folder (same device and inode) the last check judged,
and every file and folder removed under it must be one the last check walked. A link or another folder swapped
in after the last check is refused with `FAILED ... is not what the last check saw ... nothing removed` (exit
1); something moved into it since stops the delete there (`not there at the last check`, half deleted). A
worktree is checked the same way just before `git worktree remove` runs.

It refuses to run as root (exit 2): `~` would then be root's home, and the must-keep paths would miss the
user's own agent folders.

Each line names the path, the rule and the reason:

```
kept /srv/example/worktrees/fix-login (worktrees: open PR: #12)
would delete /srv/example/worktrees/fix-typo (1.2 GB, worktrees: #9 merged 3 days ago, nothing in it that is only here)
  worktrees: would delete 1 worktree (1.2 GB)
  worktrees: kept 1 (800.0 MB): open PR
total: would delete 1 (1.2 GB), kept 1
```

Exit code 0: done. 1: something could not be read or a delete failed (it was kept). 2: wrong command, or no
usable machine file (nothing was done). There is no `--force` and no option that names another config file.

The log (`logFile`, private to the user) gets a `deleting X` line before each delete and a `deleted X` (or
`FAILED X`) line after it, a line per delete refused at the last check, and the total of each run. It keeps its
newest `logLines` lines. If the log cannot be opened nothing is deleted; if a line cannot be written the run
stops before the next delete (exit 1). `logFile` must be new, empty or a log the cleaner wrote (a regular file
of the user's own whose first line starts with a UTC time); anything else, a symbolic link included, is never
opened for writing: `plan` and `run` both exit 2 and name it, and nothing (not even the lock) is made next to
it. Its folder is judged as its links resolve. Neither it, nor the lock and journal beside it, may lie in a
must-keep path, and it may not lie in a folder a rule deletes from.

### Half-deleted entries

Before each delete the run also writes the entry to the journal `<logFile>.deleting`, and takes it out again
when the delete ends. A delete that failed part-way, or a run that was killed (power cut, OOM kill) during a
delete, leaves its line there. Every later `plan` and `run` then keeps that path and names it:

```
kept /srv/example/worktrees/old-clone (worktrees: half deleted: since 2026-01-02T03:04:05Z: an earlier run
stopped while deleting it, so it may be partly gone; nothing judges it again until you remove it by hand, move
it away, or (to have it judged again) delete its line from ~/.local/state/fork-cleaner/cleaner.log.deleting)
```

and exits 1, every day, until a person does one of those three things. When the path is gone (removed by hand
or moved away) the next run drops the line by itself. A delete refused before anything was removed leaves no
line: `git worktree remove` found a changed file, git could not say where the worktree's clone is, or the entry
was not what the last check saw. A `git worktree remove` that was killed or failed while deleting ("failed to
delete") does leave its line. The journal line is written (and synced) before the delete starts, and a journal
that cannot be written stops the run before that delete. A journal that cannot be read, or is not a regular
file, stops `run` before any delete; `plan` says so, lists every planned path as `kept ... could not read`
instead of `would delete`, and exits 1.

## The report

`fork-cleaner report` prints one line for the daily report of this host, for example:

```
disk on host-1: worktrees 12.3 GB (could not read 1 worktree: /home/u/work/x (config: exit 128)), all docker
volumes off, temp 0.4 GB (on another disk), agent tool homes 20.1 GB, T3 data could not read
(/home/u/.t3/userdata/x: Permission denied), build caches no rule yet, everything else 139.3 GB (with what could
not be read) (180.2 GB of 250.0 GB used); freed since the last report at 2026-01-02T03:04:05Z 2.1 GB (4 deletes,
1 of unknown size); 2 log lines not understood, skipped
```

(one line; wrapped here). It deletes nothing.

- **The classes** are `CLASSES` in `config.py`, the one list, and each name says what it measures. A rule's class
  is what that rule looks at: `worktrees` (the worktrees and clones of the listed repositories), `temp` (the temp
  entries a pattern takes) and `all docker volumes` (every volume, named ones too: Docker measures them all, and
  the rule takes only unused anonymous ones). A must-keep class is its group of must-keep paths, whole:
  `agent tool homes` (`AGENT_HOMES`: the agent tools' home folders, with transcripts, sessions, settings and
  caches) and `T3 data` (`T3_DATA`). `build caches` has no rule yet. Every rule of `rules.RULES` (the list `plan`
  runs) needs a class; a test fails until it has one. `everything else` is the used space of the home folder's
  disk that no class holds; a class on another disk says `(on another disk)` and is not taken off it.
- **Sizes** are blocks on disk. A link is never followed (it counts as itself), another filesystem or a mount
  point inside is not entered (a bind mount of a folder of the same disk too: the mount list
  `/proc/self/mountinfo` shows it), and each folder and each hard-linked file counts once in the whole report, in
  the first class that reaches it. Docker's volumes are root's, so their size is Docker's own (`docker system df
  -v`); with the `docker` rule off, Docker is not asked (`all docker volumes off`).
- **Could not read:** a class that cannot be read in full (a folder without permission, Docker that gives no
  full answer) says `could not read` and why; the other classes still print, and the command exits 1. A
  worktree that cannot be read (its main repository is gone, say) is named, and the others still count:
  `worktrees 12.3 GB (could not read 1 worktree: <path> (<why>))`, exit 1. A mount list it cannot read is said
  at the end of the line (a bind mount may then be counted twice), exit 1.
- **Freed** adds up the `deleted` lines of the log after the last line the previous report counted. That line is
  kept in the mark `<logFile>.reported`. No log: `freed 0.0 GB`. No mark yet, or a mark it cannot use (it says so
  and exits 1): the whole log counts. When the log was trimmed past that line, every line left is newer and
  counts, but the deletes trimmed away with it were never counted, so the figure says `at least ... GB; log
  trimmed since the last report`. A line it does not understand (an older or newer version's, or a delete without
  a size and rule it knows) is skipped and counted. A line still being written (no line end yet) waits for the
  next report. A log that is not the cleaner's: `freed could not read`, exit 1, and the mark stays.
- **One at a time:** a report holds a lock (`<logFile>.reported.lock`) while it reads the log and moves the mark.
  A second report meanwhile prints `another report is running`, exits 1 and leaves the mark, so no delete is
  counted twice. The mark and its lock are the only files the report writes, and only when the log's folder is
  there.
- **The mark moves only once the line is kept.** With `--out FILE` (an absolute path) the line is appended to
  FILE and synced to disk first; FILE must be a regular file or new (a link, folder or pipe is refused). Only
  then does the mark move. When the line cannot be written, it is still printed, the mark stays, the command
  says so and exits 1, and the next report counts those deletes again. Without `--out` the mark moves once the
  line is printed. `--no-mark` never moves it (a caller that delivers the line its own way moves it by running
  `report` again with `--out`). The daily timer uses `--out` with `report.log` in its folder.

## What it never deletes

These hold for every rule. A rule can only pick candidates; the guard decides, and no config key takes a line
off this list.

- **Must-keep paths:** the agent tools' homes whole (`~/.claude*` and `~/.codex*`: transcripts, sessions,
  archived sessions, thread databases, logs, settings and logins), the T3 data folders with their databases and
  logs (`~/.t3*/userdata`, and `~/.t3*/dev` of a development build), where other agent tools keep sessions,
  history and logins (`~/.gemini`, `~/.qwen`, `~/.cursor`, `~/.config/Cursor`, `~/.aider*`, `~/.continue`,
  `~/.copilot`, `~/.config/github-copilot`, `~/.codeium`, `~/.kiro`, `~/.amazon-q` and its config and share
  folders, and the config, share (and state) folders of opencode, crush, goose and amp), anything inside one,
  and any folder that holds one. `mustKeep` in the machine file
  adds paths; nothing removes one. A path is compared both as written and as its symbolic links resolve, so a
  must-keep path written through a link still keeps the folder the link leads to.
- **Must-keep names:** a file or folder called `repair-backups` or with `log-archive` in its name, wherever it is,
  and anything that holds one.
- **Anything in use:** a process has its working folder, its root, its program, an open file (also one deleted
  since), a mapped file or a listening socket in it, or names it on its command line, also inside a shell text
  such as `sh -c 'cd "/tmp/my dir" && ./run'` (the path, quoted or with `\ ` before spaces, followed by its end
  or `/`); a systemd unit file names it; a process holds a file lock on it or on something in it; or it is itself
  a lock file (`*.lock`, `lock`, `*.lck`).
- **A `KEEP` file** anywhere in a folder keeps the whole folder. Its first line is the reason shown, with where
  the file is when it is not at the top.
- **Git work that exists only there** (for a temp folder that is or holds a git folder): uncommitted or untracked
  changes, stash entries, worktrees hanging off it, any commit (on a branch, a tag or in a reflog) that no remote
  on another machine has right now, and any tag such a remote does not have under the same name and object
  (annotated and lightweight). The same holds for every submodule repository in the git folder
  (`.git/modules/...`), also one `git submodule deinit` emptied. "Has right now" is asked live with `git
  ls-remote`; the folder's own remote-tracking refs (`origin/x`) go stale and do not count. A remote counts only
  when every URL git really uses for it (after `url.<base>.insteadOf` and `pushInsteadOf`, every URL and every
  push URL) is on another machine: a folder, a `file://` URL, `localhost`, `127.*`, `::1` or this machine's own
  name is not. A remote that cannot be asked means `could not read`. Git never fetches while it is asked: in a
  partial clone a missing object is a missing object, not a download. A `.git` that is a
  symbolic link or anything but a folder or a `gitdir:` file is never passed over: the entry is kept as `could not
  read`.
- **Unsafe paths:** a symbolic link (never followed, never removed), a root folder reached through a link,
  anything on another filesystem than its root folder or holding a mount point at any depth (bind mounts on the
  same filesystem too: the mount list `/proc/self/mountinfo` is read, and a list that cannot be read means
  `could not read`), anything that is or holds the source of a bind mount seen in any mount namespace whose
  `/proc/<pid>/mountinfo` the user can read (`in use`), anything owned by another user, a socket, pipe or device,
  and anything not directly in a configured root folder.
- **Credential-looking names:** `.env`, `*.pem`, `*.key`, `id_rsa*`, names with `token`, `secret`, `password` or
  `credential` and the like, as the entry itself or anywhere inside it.
- **No answer means keep.** If the process list, the mount list, a systemd unit, git, GitHub, Docker or the entry
  itself (a folder without read permission) cannot be read in full, what was asked about stays, the line says
  `could not read`, and the command ends with exit code 1.

## The three rules

### `worktrees`

For each repository listed in the machine file: the folders directly in `root` that are a git worktree or a full
clone **of that GitHub repository** (by the `origin` URL in the folder's own git config). Other folders in the same
`root` are not looked at. One goes only when all of this holds:

| Kept as                        | When                                                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `locked`                       | `git worktree lock` was used on it                                                                                                          |
| `no branch`                    | HEAD is detached                                                                                                                            |
| `no commit`                    | its branch has no commit: deleted under the worktree, or never had one                                                                      |
| `open PR`                      | an open pull request has its branch                                                                                                         |
| `base branch`                  | it is GitHub's default branch (`git ls-remote --symref`), or a pull request merges into its branch                                          |
| `no merged PR`                 | no merged pull request **from this repository** (not a fork) has its branch                                                                 |
| `commits not in the merged PR` | HEAD is not the head of a merged pull request of that branch, nor inside one                                                                |
| `merged under 24 h ago`        | the pull request merged less than `mergedHours` ago (24 or more)                                                                            |
| `branch not listed`            | `branches` is set and the branch matches none of its patterns                                                                               |
| `used under 24 h ago`          | in the last `usedHours` (24 or more): a file in it, its HEAD reflog, `HEAD`, `index`, `ORIG_HEAD`, `FETCH_HEAD` or `COMMIT_EDITMSG` changed |
| `uncommitted or untracked`     | `git status` shows anything                                                                                                                 |
| `ignored files`                | a file git ignores is in it that is not build output (see `regenerable`)                                                                    |
| `hidden from git status`       | a file is marked skip-worktree or assume-unchanged                                                                                          |
| `unfinished git operation`     | a merge, rebase, cherry-pick, revert or bisect is under way, or `index.lock`                                                                |
| `refs of its own`              | it has `refs/worktree/*`, `refs/bisect/*` or `refs/rewritten/*`                                                                             |
| `commits only in its reflog`   | worktree: a commit made with no branch checked out, that no branch ever held                                                                |
| `worktrees hang off it`        | clone: git still lists a worktree whose folder is there (anywhere), or locked                                                               |
| `stash entries`                | clone: `git stash list` is not empty                                                                                                        |
| `commits GitHub does not have` | clone: a commit on any branch, tag or reflog that no ref on GitHub (`git ls-remote`) or listed pull request holds                           |
| `tags GitHub does not have`    | clone: a tag GitHub does not have with the same name and object                                                                             |
| `origin on this machine`       | clone: a URL git really uses for `origin` (after `insteadOf`) is a folder or host on this machine                                           |
| `git: unpushed commits`, ...   | a submodule repository in its git folder (also after `git submodule deinit`) holds work that is only here                                   |
| `another git folder inside`    | a nested clone or submodule                                                                                                                 |
| `in use`, `KEEP file`, ...     | every refusal of the list above                                                                                                             |
| `could not read`               | git or GitHub gave no full answer; an unreadable merge time or folder; a moved worktree                                                     |

A worktree is removed with `git worktree remove`, never with `--force`: git itself refuses when a file changed
after the last check. The local branch and its reflog stay in the clone, so a commit that was amended or reset
away on the branch is still there afterwards. A full clone goes only in a later run than its worktrees, when the
same checks pass for the clone itself; its working files go first and `.git` last.

### `docker`

Off unless the machine file says `"enabled": true`; while off, Docker is not even asked. It takes volumes that

- have an anonymous name (exactly 64 hex characters): a named volume is never touched and never listed;
- no container uses, running or stopped (Docker's own `dangling=true` list, read again before each delete);
- were made `days` or more ago (7 or more);
- have no process in their folder.

`docker volume rm` is used, never with `--force`. Docker does not tell the size of a volume, so sizes say
`size not known`.

### `tmp`

The entries directly in the temp folder (`root`, default `/tmp`) whose name matches a pattern of the machine
file, when nothing in them changed for that pattern's number of days. The first pattern that matches counts. The
default list is empty: with no pattern the rule does nothing. "Changed" is the newest content or inode time of the
entry and of everything in it, so a folder copied today with old dates is new.

Never taken, whatever the patterns say: hidden entries, `claude-*`, `tmux-*`, `ssh-*`, `systemd-private-*`,
`snap-private-tmp`, `snap.*` and `lost+found`. Agent scratch folders get a rule of their own later.

## The machine file

The `cleaner` section of the machine file ([common](../common/README.md)). A whole file, with made-up values:

```json
{
  "version": 1,
  "cleaner": {
    "logFile": "~/.local/state/fork-cleaner/cleaner.log",
    "logLines": 5000,
    "mustKeep": ["/srv/example/ledger", "~/notes"],
    "worktrees": {
      "mergedHours": 24,
      "repos": [
        {
          "root": "~/worktrees",
          "github": "example-owner/example-repo",
          "regenerable": ["dist", "coverage"],
          "branches": ["work/*"]
        }
      ]
    },
    "docker": { "enabled": true, "days": 7 },
    "tmp": {
      "root": "/tmp",
      "patterns": { "example-build-*": 7, "example-crash-*": 1 }
    },
    "daily": {
      "mode": "compare",
      "oldTimers": ["example-retention.timer"],
      "compare": [
        {
          "name": "example-retention",
          "command": ["/usr/local/bin/example-retention", "--dry-run"],
          "pattern": "^would remove (/.+?) \\("
        }
      ]
    }
  }
}
```

| Key                             | Default                                   | Meaning                                                                                           |
| ------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `logFile`                       | `~/.local/state/fork-cleaner/cleaner.log` | the log; its locks, journal and report mark are beside it (new, or the cleaner's own)             |
| `logLines`                      | 5000                                      | lines the log keeps (10 or more)                                                                  |
| `mustKeep`                      | none                                      | more paths that are never deleted (`*` within one part)                                           |
| `worktrees.repos[].root`        | required                                  | the folder the clones and worktrees are directly in                                               |
| `worktrees.repos[].github`      | required                                  | `owner/name` on GitHub                                                                            |
| `worktrees.repos[].regenerable` | none                                      | plain names of ignored build folders, on top of `node_modules`, `__pycache__` and `.pytest_cache` |
| `worktrees.repos[].branches`    | all                                       | branch patterns (shell style, case as written; `*` crosses `/`): only these branches are taken    |
| `worktrees.mergedHours`         | 24                                        | hours after the merge (24 or more)                                                                |
| `worktrees.usedHours`           | 24                                        | hours since the worktree or clone was last used (24 or more)                                      |
| `docker.enabled`                | false                                     | turns the rule on                                                                                 |
| `docker.days`                   | 7                                         | age of an unused anonymous volume (7 or more)                                                     |
| `tmp.root`                      | `/tmp`                                    | the temp folder                                                                                   |
| `tmp.patterns`                  | none                                      | name pattern to days without a change (1 or more)                                                 |
| `daily.mode`                    | `compare`                                 | what the timer does: `compare` (deletes nothing) or `run`                                         |
| `daily.oldTimers`               | none                                      | user timers of the tools this one replaces: `run` refuses while one is on                         |
| `daily.outDir`                  | `~/.local/state/fork-cleaner/daily`       | where `daily` writes its files                                                                    |
| `daily.keepDays`                | 30                                        | days `daily` keeps its dated files (1 or more)                                                    |
| `daily.compare[]`               | none                                      | the old tools' dry runs: `name`, `command` (absolute program, no shell), `pattern` (one group)    |

An empty section (`"cleaner": {}`) is valid and does nothing. An unknown key, a wrong type, a number below its
floor, a root folder that is `/`, the home folder or a folder above it: the command names the file and the key,
exits 2 and does nothing.

Every key can only pick what a rule looks at, raise a floor, or keep more (`KEY_KINDS` in `config.py`; a test
fails for a key that is not placed there). The one key that frees more, `regenerable`, takes plain folder names
only: no `/` and no wildcard, so `["*"]` is refused and can never take the `ignored files` refusal away. A name
frees an ignored folder of that name (and what is in it), never a file, and never a credential-looking name.

GitHub is read with the `gh` command of the user the cleaner runs as.

## The daily timer

`fork-cleaner daily` is what the kit's one timer (`fork-cleaner.timer`, once a day) runs. `cleaner.daily.mode` in
the machine file says what it does:

- **`compare`** (the default): `plan`, then the report, then the [compare](#the-compare). Nothing is deleted,
  whatever else runs on the host.
- **`run`**: `run` (deletes), then the report. It refuses (exit 2, nothing deleted, nothing written) while a timer
  in `oldTimers` is enabled or active for this user, or when `systemctl --user` cannot say: two cleaners must not
  both delete. A timer counts as off only when it is `disabled`, `masked` or not found **and** `inactive` or
  `failed`.

Everything goes to `outDir`: `plan-<day>.txt` (or `run-<day>.txt`) and `compare-<day>.txt` (each written whole,
kept `keepDays` days), and `report.log`, which gets each report's line before the mark moves. Only files of those
names are ever removed there. `outDir` may not be in a must-keep path, in or above a folder a rule deletes from,
or hold the home folder. The command prints one summary line (the journal keeps it) and exits with the highest
code of its steps.

## The compare

`fork-cleaner compare` (and `daily` in compare mode) runs `plan` and the dry run of each tool listed in
`cleaner.daily.compare`: its `command` (an absolute program and its arguments, run without a shell and with no
input; it must be read-only by its own promise) and a `pattern` whose one group is a path that tool would delete.
Every path goes in one of three lists:

```
compare: fork-cleaner would delete 3, the old tools 4 (example-retention 4)
new only (a bug until explained): 1
  /tmp/example-build-9 (tmp: not changed for 9 days (example-build-*: 7 days))
old only (fork-cleaner keeps it): 2
  /home/u/worktrees/fix-1 [example-retention] (worktrees: open PR: #12)
  /tmp/x [example-retention] (no kept line: no rule of fork-cleaner takes it, or it is too new for one)
both: 2
```

**New only** is what this tool would delete and no old one would: each is a bug until somebody explains it, and
the exit code is 1. **Old only** is where it is stricter, with its reason. A path counts as the same when it is
the other one or inside it. A dry run that cannot start or ends with an exit code other than 0 makes the compare
`incomplete` (exit 1); what it printed still counts. Nothing is deleted.

## Install

From a commit of the fork's protected release branch, as root:

```
sh bootstrap.sh --repo FORK_OWNER/FORK --sha RELEASE_SHA --timer
```

The bootstrap checks the commit is on `ci-release`, downloads exactly that commit and runs
`sbin/fork-cleaner-install install`. That copies the kit into `/usr/local/libexec/fork-cleaner/versions/<sha>/`
(root-owned, with `MANIFEST.sha256`), runs its `--help` as a self-test, switches `current` to it and links
`/usr/local/bin/fork-cleaner`. `--timer` writes the user units `fork-cleaner.service` and `fork-cleaner.timer` in
`/etc/systemd/user/`; each user who wants it runs
`systemctl --user daemon-reload && systemctl --user enable --now fork-cleaner.timer`. `--no-timer` removes them;
without either option the last choice stays (`/etc/fork-cleaner/config.json`). A unit of the same name the kit did
not write is refused and left alone. `fork-cleaner-install self-check` reports drift from `installed.tsv`;
`uninstall` removes the kit, and leaves the machine file and what the cleaner wrote in each home. Safe to run
again or to stop at any step; one installer at a time.

The installer writes no machine file: the `cleaner` section is the owner's, written by hand.

## What happens when ...

- **it runs again:** what is gone is no longer a candidate; a second run deletes nothing new and logs only its
  total line.
- **it stops half-way:** the entry it was deleting (temp folder, clone or worktree) is named `half deleted` by
  every later plan and run, never judged or deleted again, and the run exits 1 until a person acts; see
  [Half-deleted entries](#half-deleted-entries) for what to do.
- **something is in use:** it is kept and named. A process, unit or pull request that arrives between plan and
  delete is seen by the last check before the delete.
- **the config is old, empty or wrong:** an empty section does nothing; anything it does not understand stops the
  command before it reads a single folder.
- **GitHub, git or Docker is down:** everything that needed the answer is kept, and the exit code is 1.
- **two runs start at once:** the second one stops with "another run" and deletes nothing.
- **the tool it replaces is still installed:** compare mode runs beside it and deletes nothing; run mode refuses
  while a timer in `oldTimers` is on.
- **the old tool's log and state are left behind:** never read, changed or counted. The report counts only this
  tool's own log, so the old tool's deletes are not in its freed figure.

## Limits

- Processes of other users are seen only as far as the kernel shows them (usually the command line). Files of
  other users are never deleted anyway.
- The credential-name check is by name only. A temp folder holding a clone with a tracked file called
  `token.py` is kept; inside `.git`, `node_modules` and the like, names are not checked.
- A worktree with a submodule or any nested git folder is kept.
- Bind mounts are seen only as far as this user can read them. Not seen: the mount namespaces of processes of
  other users and of root (a rootful Docker container's volumes, a service's `PrivateTmp`); a bind mount whose
  source is a folder **above** the entry (it shares the entry with the mount, but the entry is not its
  source); and filesystems whose device numbers differ per subvolume or layer (btrfs, overlay), where a source
  is matched by device and path only. Processes in a container also report paths as the container sees them,
  so the in-use check can miss them. #491 (the timer) must keep the roots away from container volumes for that
  reason.
- A list of 1000 or more open pull requests counts as "could not read".
- Linux only so far (`/proc`, systemd unit folders).

## Tests

```
python3 -I -B fork-kit/cleaner/tests/guard.test.py
python3 -I -B fork-kit/cleaner/tests/worktrees.test.py
python3 -I -B fork-kit/cleaner/tests/engine.test.py
python3 -I -B fork-kit/cleaner/tests/report.test.py
python3 -I -B fork-kit/cleaner/tests/daily.test.py
python3 -I -B fork-kit/cleaner/tests/install.test.py
```

Everything lives in one temporary folder per test: real git repositories whose "GitHub" is a bare repository, stub
`gh` and `docker` commands, and a fake `/proc`. No real worktree, Docker volume or temp entry is touched. Each
file ends with a red check: it breaks one refusal at a time in a copy of the code and shows that the test for
that refusal then fails.
