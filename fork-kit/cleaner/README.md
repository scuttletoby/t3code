# cleaner: free disk space by rules, and never delete what could be needed

`fork-cleaner` removes what a host no longer needs: finished git worktrees, unused anonymous Docker volumes and old
temp folders. Which folders, which repositories and how many days come from the machine file. What must never go
is in the code and cannot be switched off.

It lives in `fork-kit` because it runs on a machine (not in CI and not inside T3), reads its facts from the
machine file like every kit, and ships with the kits in `ci-release`. `fork-ci` holds only what CI itself runs.

This is the first part: the core and three rules. No installer and no timer yet: nothing runs it by itself.

| Part         | File                    | What it does                                                  |
| ------------ | ----------------------- | ------------------------------------------------------------- |
| config       | `lib/cleaner/config.py` | reads the `cleaner` section of the machine file               |
| facts        | `lib/cleaner/facts.py`  | reads the process list, systemd units, git, GitHub and Docker |
| guard        | `lib/cleaner/guard.py`  | the hard refusals: asked about every path, twice              |
| rules        | `lib/cleaner/rules.py`  | `worktrees`, `docker`, `tmp`: what each wants gone, and why   |
| `plan`,`run` | `lib/cleaner/engine.py` | the two commands, the lock and the log                        |

## Commands

```
fork-cleaner plan    # prints what a run would delete and what it keeps; changes nothing
fork-cleaner run     # deletes it; one log line per delete
```

`plan` writes no file, takes no lock and makes no log. `run` takes a lock (one run at a time), plans, then reads
everything about each item **again** just before its delete: git, the process list, Docker and the files
themselves. The pull request lists are the exception: they are read once per run.

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

The log (`logFile`, private to the user) gets one line per delete, per delete refused at the last check, per
failed delete, and the total of each run. It keeps its newest `logLines` lines. If the log cannot be opened
nothing is deleted; if it cannot be written the run stops.

## What it never deletes

These hold for every rule. A rule can only pick candidates; the guard decides, and no config key takes a line
off this list.

- **Must-keep paths:** agent transcripts (`~/.claude*/projects`), agent sessions (`~/.codex*/sessions`), the T3
  data folder with its databases and logs (`~/.t3*/userdata`), anything inside one, and any folder that holds one.
  `mustKeep` in the machine file adds paths; nothing removes one.
- **Must-keep names:** a file or folder called `repair-backups` or with `log-archive` in its name, wherever it is,
  and anything that holds one.
- **Anything in use:** a process has its working folder, its root, its program, an open file, a mapped file or a
  listening socket in it, or names it on its command line; a systemd unit file names it; a process holds a file
  lock on something in it; or it is itself a lock file (`*.lock`, `lock`, `*.lck`).
- **A `KEEP` file** directly in a folder keeps the folder. Its first line is the reason shown.
- **Git work that exists only there** (for a temp folder that is or holds a git folder): uncommitted or untracked
  changes, stash entries, commits no remote-tracking branch has (the reflog counts), worktrees hanging off it.
- **Unsafe paths:** a symbolic link (never followed, never removed), a root folder reached through a link,
  anything on another filesystem than its root folder (a mount), anything owned by another user, a socket, pipe
  or device, and anything not directly in a configured root folder.
- **Credential-looking names:** `.env`, `*.pem`, `*.key`, `id_rsa*`, names with `token`, `secret`, `password` or
  `credential` and the like, as the entry itself or anywhere inside it.
- **No answer means keep.** If the process list, a systemd unit, git, GitHub or Docker cannot be read in full,
  what was asked about stays, the line says `could not read`, and the command ends with exit code 1.

## The three rules

### `worktrees`

For each repository listed in the machine file: the folders directly in `root` that are a git worktree or a full
clone **of that GitHub repository** (by the `origin` URL in the folder's own git config). Other folders in the same
`root` are not looked at. One goes only when all of this holds:

| Kept as                        | When                                                                                                       |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `locked`                       | `git worktree lock` was used on it                                                                         |
| `no branch`                    | HEAD is detached                                                                                           |
| `open PR`                      | an open pull request has its branch                                                                        |
| `no merged PR`                 | no merged pull request has its branch                                                                      |
| `commits not in the merged PR` | HEAD is not the head of a merged pull request of that branch, nor inside one                               |
| `merged under 24 h ago`        | the pull request merged less than `mergedHours` ago (24 or more)                                           |
| `uncommitted or untracked`     | `git status` shows anything                                                                                |
| `ignored files`                | a file git ignores is in it that is not build output (see `regenerable`)                                   |
| `hidden from git status`       | a file is marked skip-worktree or assume-unchanged                                                         |
| `unfinished git operation`     | a merge, rebase, cherry-pick, revert or bisect is under way, or `index.lock`                               |
| `refs of its own`              | it has `refs/worktree/*`, `refs/bisect/*` or `refs/rewritten/*`                                            |
| `commits only in its reflog`   | worktree: a commit made with no branch checked out, that no branch ever held                               |
| `worktrees hang off it`        | clone: git still lists a worktree whose folder is there (anywhere), or locked                              |
| `stash entries`                | clone: `git stash list` is not empty                                                                       |
| `commits GitHub does not have` | clone: a commit on any branch, tag or reflog that no branch on GitHub now and no listed pull request holds |
| `another git folder inside`    | a nested clone or submodule                                                                                |
| `in use`, `KEEP file`, ...     | every refusal of the list above                                                                            |
| `could not read`               | git or GitHub gave no full answer, or a merge time is unreadable                                           |

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
          "regenerable": ["dist", "*.tsbuildinfo"]
        }
      ]
    },
    "docker": { "enabled": true, "days": 7 },
    "tmp": {
      "root": "/tmp",
      "patterns": { "example-build-*": 7, "example-crash-*": 1 }
    }
  }
}
```

| Key                             | Default                                   | Meaning                                                                                    |
| ------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------ |
| `logFile`                       | `~/.local/state/fork-cleaner/cleaner.log` | the log; the lock is beside it                                                             |
| `logLines`                      | 5000                                      | lines the log keeps (10 or more)                                                           |
| `mustKeep`                      | none                                      | more paths that are never deleted (`*` within one part)                                    |
| `worktrees.repos[].root`        | required                                  | the folder the clones and worktrees are directly in                                        |
| `worktrees.repos[].github`      | required                                  | `owner/name` on GitHub                                                                     |
| `worktrees.repos[].regenerable` | none                                      | names of ignored build output, on top of `node_modules`, `__pycache__` and `.pytest_cache` |
| `worktrees.mergedHours`         | 24                                        | hours after the merge (24 or more)                                                         |
| `docker.enabled`                | false                                     | turns the rule on                                                                          |
| `docker.days`                   | 7                                         | age of an unused anonymous volume (7 or more)                                              |
| `tmp.root`                      | `/tmp`                                    | the temp folder                                                                            |
| `tmp.patterns`                  | none                                      | name pattern to days without a change (1 or more)                                          |

An empty section (`"cleaner": {}`) is valid and does nothing. An unknown key, a wrong type, a number below its
floor, a root folder that is `/`, the home folder or a folder above it: the command names the file and the key,
exits 2 and does nothing. `regenerable` never makes a credential-looking name build output.

GitHub is read with the `gh` command of the user the cleaner runs as.

## What happens when ...

- **it runs again:** what is gone is no longer a candidate; a second run deletes nothing new and logs only its
  total line.
- **it stops half-way:** every delete is one step of its own. A temp folder or clone that is half deleted is
  still there: the next run takes the temp folder again by the same rule; a half-deleted clone is kept and named
  (git no longer answers, or shows changes) and needs a person. A worktree is removed by git in one command.
- **something is in use:** it is kept and named. A process that arrives between plan and delete is seen by the
  last check before the delete.
- **the config is old, empty or wrong:** an empty section does nothing; anything it does not understand stops the
  command before it reads a single folder.
- **GitHub, git or Docker is down:** everything that needed the answer is kept, and the exit code is 1.
- **two runs start at once:** the second one stops with "another run" and deletes nothing.

## Limits

- Processes of other users are seen only as far as the kernel shows them (usually the command line). Files of
  other users are never deleted anyway.
- The credential-name check is by name only. A temp folder holding a clone with a tracked file called
  `token.py` is kept; inside `.git`, `node_modules` and the like, names are not checked.
- A worktree with a submodule or any nested git folder is kept.
- A list of 1000 or more open pull requests counts as "could not read".
- Linux only so far (`/proc`, systemd unit folders).

## Tests

```
python3 -I -B fork-kit/cleaner/tests/guard.test.py
python3 -I -B fork-kit/cleaner/tests/worktrees.test.py
python3 -I -B fork-kit/cleaner/tests/engine.test.py
```

Everything lives in one temporary folder per test: real git repositories whose "GitHub" is a bare repository, stub
`gh` and `docker` commands, and a fake `/proc`. No real worktree, Docker volume or temp entry is touched. Each
file ends with a red check: it breaks one refusal at a time in a copy of the code and shows that the test for
that refusal then fails.
