# cleaner: free disk space by rules, and never delete what could be needed

`fork-cleaner` removes what a host no longer needs: finished git worktrees, unused anonymous Docker volumes, old
temp folders, and the scratch folders of agent sessions that are over (after it has copied out the small reports
in them worth keeping). Which folders, which repositories and how many days come from the machine file. What must never go
is in the code and cannot be switched off.

It lives in `fork-kit` because it runs on a machine (not in CI and not inside T3), reads its facts from the
machine file like every kit, and ships with the kits in `ci-release`. `fork-ci` holds only what CI itself runs.

The core, four rules, an [installer](#install) and one [daily timer](#the-daily-timer). While the tools it is
to replace still delete on a host, the timer runs in compare mode: it plans, reports and compares what it would
delete with what they would, and deletes nothing.

| Part         | File                                                    | What it does                                                  |
| ------------ | ------------------------------------------------------- | ------------------------------------------------------------- |
| config       | `lib/cleaner/config.py`                                 | reads the `cleaner` section of the machine file               |
| facts        | `lib/cleaner/facts.py`                                  | reads the process list, systemd units, git, GitHub and Docker |
| guard        | `lib/cleaner/guard.py`                                  | the hard refusals: asked about every path, twice              |
| rules        | `lib/cleaner/rules.py`                                  | `worktrees`, `docker`, `tmp`, `agent-scratch`: what and why   |
| compress     | `lib/cleaner/compress.py`                               | `compress-cold`: cold files replaced by a checked zstd copy   |
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
and clones of one repository makes N + 1 reads. With [`mergedBranches`](#merged-branches) on, each clone's branches
take one read for the plan and one before each branch delete. Each item's last check also runs `git ls-remote`
against GitHub (git, not the API).

**How a delete is done.** A temp entry or a clone is removed through open folder descriptors, one name at a
time, never through a link: the entry must still be the folder (same device and inode) the last check judged,
and every file and folder removed under it must be one the last check walked. A link or another folder swapped
in after the last check is refused with `FAILED ... is not what the last check saw ... nothing removed` (exit
1); something moved into it since stops the delete there (`not there at the last check`, half deleted). An
agent session's folder is also stopped by anything whose inode time is after the check right before its delete
(`changed after the last check`, half deleted): ext4 hands a freed inode number to the next new file at once, so
the inode number alone does not show a file made during the delete. A file with several hard links is let
through (deleting one name changes the others' inode time). A worktree is checked the same way just before
`git worktree remove` runs.

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
usable machine file (nothing was done). 3 (`compare` and `daily` only): every step worked, and the compare found
paths only fork-cleaner would delete (see [The daily timer](#the-daily-timer)). There is no `--force` and no
option that names another config file.

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

One exception: an [`agent-scratch`](#agent-scratch) session folder is judged again by the next run, as #466
asked. When every check still passes for what is left (the session is still gone, no lock, nothing in use, no
git work only there) and no file in it changed after the delete began (its journal time), the run finishes the
delete (`finishing the delete an earlier run began at ...`), keeping its journal line until it is done. Otherwise
it stays `half deleted`, with the reason in front: `kept <path> (agent-scratch: half deleted: since ...: session
alive: ...)`.

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
  entries a pattern takes), `agent scratch` (every session folder of the agent-scratch root, idle or not; `off`
  while the rule is off) and `all docker volumes` (every volume, named ones too: Docker measures them all, and
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
  then does the mark move (a new FILE's folder is synced too, so its name survives a power cut). When the line
  cannot be written, it is still printed, the mark stays, the command
  says so and exits 1, and the next report counts those deletes again. Without `--out` the mark moves once the
  line is printed. `--no-mark` never moves it (a caller that delivers the line its own way moves it by running
  `report` again with `--out`). The daily timer uses `--out` with `report.log` in its folder. Delivery is at least
  once: a report killed after the line is written and before the mark moves writes those deletes again in the
  next line, so a sum over FILE can count them twice (the mark itself never does).

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
  read`. One `.git` is passed over, by the [`tmp`](#tmp) rule only: a `gitdir:` file whose git folder is
  provably gone (an orphaned worktree, see there) names no commit that could still be lost here; every other git
  folder in the entry is judged as above.
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

## The four rules

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
| `orphaned worktree`            | its `.git` file names a git folder that is gone (main repository removed): kept, but no error (exit 0)                                      |
| `unknown parent`               | tmp: its git folder is not there but may be elsewhere (see [`tmp`](#tmp)): kept, but no error (exit 0)                                      |

A worktree is removed with `git worktree remove`, never with `--force`: git itself refuses when a file changed
after the last check. The local branch and its reflog stay in the clone, so a commit that was amended or reset
away on the branch is still there afterwards. A full clone goes only in a later run than its worktrees, when the
same checks pass for the clone itself; its working files go first and `.git` last.

#### Merged branches

`git worktree remove` leaves the worktree's branch in its clone. With `"mergedBranches": true` on a repository
(it needs `branches`: only a branch a pattern names is ever looked at), the rule also deletes such a local branch of
a full clone directly in `root` (a clone elsewhere is not looked at, nor the branches of a clone that goes itself).
A branch some worktree has checked out (also one whose folder is gone), rebases or bisects is that worktree's: it
is not listed, and goes in a later run than its worktree. The others go only when all of this holds:

| Kept as                        | When                                                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| `open PR`                      | an open pull request has the branch                                                                                   |
| `base branch`                  | it is GitHub's default branch, or a pull request merges into it                                                       |
| `no merged PR`                 | no merged pull request **from this repository** has the branch                                                        |
| `commits not in the merged PR` | its tip is neither in a merged pull request's head (that this clone has) nor in GitHub's default branch right now     |
| `merged under 24 h ago`        | as for a worktree (`mergedHours`)                                                                                     |
| `commits GitHub does not have` | a commit its tip or its reflog names is on no branch or tag of GitHub (`git ls-remote`) and in no listed pull request |
| `used under 24 h ago`          | the newest time its reflog names, or its ref file, is within `usedHours`                                              |
| `KEEP file`, `must keep`       | the clone has a `KEEP` file at its top or is a must-keep path; or it is reached through a link (`symbolic link`)      |
| `origin on this machine`       | as for a clone                                                                                                        |
| `could not read`               | git or GitHub gave no full answer (named `branches in <clone>` when it is about the whole clone)                      |

A branch that a symbolic ref of the clone points at (`git symbolic-ref refs/heads/alias refs/heads/x`) is kept
as `symbolic ref names it`: deleting it would leave the alias dangling.

The plan names each one `branch <name> in <clone>` (size `0 B`). The delete is `git update-ref -d` with the commit
the last check judged: a branch that moved since is left as it is (`FAILED ...: nothing removed`). The log line
names that commit (`... (was <commit>)`), so one line undoes a delete: `git fetch origin <commit> && git branch
<name> <commit>` (the commit is on GitHub; a clone that still has it needs only the second step). Then its config
section (`branch.<name>.*`, its upstream) goes, as with `git branch -d`, but only while it is still the deleted
branch's: it is read before the delete, and left alone when the branch is there again after the delete (made anew
in between) or the section changed meanwhile. A section left behind this way stays for a later branch of that
name, as git itself would leave it. Deleting a branch deletes its reflog, so a
commit amended or reset away that only the reflog holds keeps the branch, until git's own reflog expiry drops it
(`gc.reflogExpireUnreachable`, 30 days by default). A branch delete is one git step on a ref while its clone stays,
so it gets no journal line: killed after git removed the branch and before its reflog or config section went, the
next run finds no branch there and judges nothing (the reflog left behind still counts for the clone, `git log
--reflog`). A worktree checked out on the branch between the last check and the delete finds it gone; every commit
of it is on GitHub, so nothing is lost. Branches of a clone that is `half deleted` are not judged.

Two limits. A clone whose refs are in the `reftable` format (`git init --ref-format=reftable`) has no ref or
reflog files, and the time of last use is read only from those: `used under 24 h ago` cannot protect its branches,
only `mergedHours` and the other checks do (the commits its reflogs name are still asked of git and checked). Two repositories of the machine file that share one `root` are each judged against their own GitHub
repository (a clone counts only for the repository its `origin` names); the `worktrees` rule's last check still
takes the first repository whose root holds the folder.

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

An orphaned worktree (its `.git` file names a git folder that is gone, as when another tool removed its main
repository) is taken like a plain folder (#519): its commits, index and HEAD were in that git folder, so nothing in
it is a commit that could still be lost. That holds only when the git folder is really gone, not just out of
sight, so it counts as orphaned only when all of this is proven:

- the `gitdir:` line names an absolute path;
- the deepest part of that path that still exists is a real folder (no link on the way, no broken link), on the
  same filesystem as the worktree, and not a mount point;
- no drive could be missing there: the path is under no `/etc/fstab` mount point that is not mounted now (an
  `/etc/fstab` that cannot be read counts as one), and not in `/mnt`, `/media` or `/run/media` unless a mounted
  drive below them holds it;
- no repository still names this worktree: the worktree lists (`.git/worktrees/*/gitdir` and, for a bare
  repository, `worktrees/*/gitdir`) of that deepest folder, of every folder in it and of every folder in a
  configured repository root are read, and none names this folder. A repository that was moved or renamed next
  to its old place is found this way, and so its worktree's HEAD and commits.

Anything else keeps the entry as `unknown parent` (no error, exit 0): the plan says why, for example `.../.git
still names it: the repository was moved or renamed (git worktree repair there)`. After `git worktree repair` in
the moved repository the worktree is judged as any worktree, with its commits. A folder that cannot be searched
for those lists means `could not read`. A repository moved anywhere else (another filesystem, a folder deeper
down or far away) cannot be seen: its worktree then counts as orphaned, and the files git never committed go
like those of any folder the pattern takes; its commits stay in the moved repository. Every other git folder in the entry (a clone inside it, say) is judged as
always, and every other refusal holds (`KEEP`, credential names, in use). What git cannot tell any more is which of
its files were never committed: like any folder the pattern takes, it goes once nothing in it changed for the
pattern's days. The plan says so: `...; orphaned worktree: its git folder <path> is gone, so it holds no commit`.
The `worktrees` and `agent-scratch` rules still keep an orphaned worktree.

Never taken, whatever the patterns say: hidden entries, `claude-*`, `tmux-*`, `ssh-*`, `systemd-private-*`,
`snap-private-tmp`, `snap.*` and `lost+found`. Agent scratch folders have a rule of their own:
[`agent-scratch`](#agent-scratch).

### `agent-scratch`

Off unless the machine file says `"enabled": true`. Agent sessions leave a scratch folder per session in the temp
folder (Claude Code: `/tmp/claude-<uid>/<project, as a name>/<session id>/` with `scratchpad/`, `tasks/` and
`images/`), and nothing removes them. The rule looks at the folders two levels below `root` (default
`/tmp/claude-{uid}`, `{uid}` being the user's id) whose name is a session id (a uuid); the project folders
themselves never go, and a project folder that is a link is kept and named. The root must be one user's own
folder: never a shared temp folder itself (`/tmp`, `/var/tmp`, `/dev/shm`, `/run/user`, the user's own runtime
folder `/run/user/<uid>` or `$XDG_RUNTIME_DIR`, `$TMPDIR`), never `/`, home or above it, never a folder that is,
holds or is inside a `transcripts` folder (by its path or its real path: agent logs are never deleted), and when
it is there, a real folder (not a link) of the cleaner's user; anything else stops the run as a machine file
error. A session folder goes only when all of this holds, the hard refusals of
[What it never deletes](#what-it-never-deletes) included:

| Kept as                               | When                                                                                                                                                                                     |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| (not listed)                          | anything in it changed less than `days` ago (3 or more): the newest content or inode time of all of it                                                                                   |
| `lock file`                           | a lock-named file is directly in it (`*.lock`, `lock`, `*.lck`); deeper, a clone's `yarn.lock` or `Cargo.lock` is ordinary content (a lock a process _holds_ anywhere in it is `in use`) |
| `transcript changed under 3 days ago` | the session's transcript (`<id>.jsonl` or its `<id>/` folder in a folder `transcripts` names) changed within `days`                                                                      |
| `session alive`, `in use`             | the session may still be alive: see [What counts as alive](#what-counts-as-alive)                                                                                                        |
| `could not read`                      | the process list, a process of the user (see below), a transcript folder or a folder in it cannot be read in full, or a `transcripts` pattern matches no folder                          |
| `git: unpushed commits`, ...          | a clone in it holds uncommitted, untracked, stashed or unpushed work: kept, and the clone is named                                                                                       |
| `report too large to keep first`      | a file a `keepFirst.patterns` pattern takes is over `maxFileBytes`: it stays until a person keeps it or it is gone (nothing is deleted before it is saved)                               |
| `too much to keep first`              | the files to keep first add up to more than `maxSessionBytes`                                                                                                                            |
| `credential name`, ...                | every refusal of the list above                                                                                                                                                          |

Transcripts are read by their times only, never their content, and are never changed (they are must-keep). Where
they are comes from the machine file (`transcripts`, default `~/.claude*/projects/*`: one folder per project);
each match is resolved to its real path, so a config folder that is a link (or `CLAUDE_CONFIG_DIR` named there)
works. A pattern that matches no folder at all is `could not read` for every session, never "no transcript": a
transcript folder that moved must not make every session look gone.

##### What counts as alive

Asked last in the plan, again by the last check before each delete, and once more after keep first, right before
the first unlink (the copy can take a while). A session folder is kept when any of these holds:

1. `in use` (the plan and the last check) or `session alive` (after keep first): a process of any user works in
   the folder or below it (its working folder or root), has a file or folder in it open (`/proc/<pid>/fd`, a
   deleted file included; a running session holds its `tasks/` folder open), maps a file in it, listens on a
   socket in it, holds a lock in it, or names its path on its command line.
2. `session alive`: a process names the id as a whole word, on its command line (`--session-id`, `--resume <id>`)
   or in its environment (`CLAUDE_CODE_SESSION_ID`, which every tool and MCP server of a session inherits). Of other
   users' processes only the command line is visible.
3. `could not read`: the process list cannot be read, or a process of the cleaner's own user hides its open files,
   working folder or environment (a process that is not dumpable, such as the Codex CLI) and has no readable
   ancestor to be judged by. A hidden process is judged by its nearest ancestor (`/proc/<pid>/stat` shows the
   parent even of a hidden process) that is the user's own and can be read in full: what that ancestor works in,
   holds open and names decides, under 1, 2 and 4 like any process (a Codex started by an agent's tool is that
   agent's session's; one started by the T3 server is no session's). Hidden ancestors are walked past; the walk
   stops, with no ancestor, at pid 1, the user's service manager (`systemd --user`), a process of another user or
   one that has ended. The login session's helpers that hide on purpose and hold no session (`systemd`,
   `(sd-pam)`, `gpg-agent`, `ssh-agent`, `scdaemon`, `keyboxd`, `gnome-keyring-d`, by `/proc/<pid>/comm`) need no
   ancestor. Their name excuses only what cannot be read: what can be read of such a process (its command line,
   such as a `--homedir` in a session; a socket in `/proc/net/unix`) still counts under 1 and 2.
4. `session alive`: after `/clear` (or a resume under a new id) the same process works on under another id. So a
   process of the user that works in or holds open another session folder of the _same project_ without naming
   that session's id, and that started before this session last changed (its folder or its transcript; a start
   time that cannot be read counts as before), keeps this session.
5. `transcript changed under ... ago`: as above.

Not covered: an interactive `claude` (no id on its command line) after `/clear`, when it holds nothing in the old
folder and its tools carry only the new id. (A T3-started session is covered: its process names its first id on
its command line for as long as it lives, so 2 keeps the old folder.) Also not covered: a hidden process whose
readable ancestor works elsewhere, but which itself works in a session folder (it cannot be seen). Its old folder goes once it and its transcript are `days` old; what is worth
keeping in it is kept first.

#### Keep first

Before a session folder is deleted, the files in it worth keeping for analysis are copied out (#466: "before raw
data is deleted, check whether anything in it is worth keeping; save that first; only then delete"). What is
worth keeping is set in the machine file (`keepFirst.patterns`, relative to the session folder; `*` within one
part). The default takes the small text reports an agent writes in its scratchpad, and not the clones,
downloads, media and build output deeper in it:

```
scratchpad/*.md  scratchpad/*.json  scratchpad/*.txt  scratchpad/*.tsv  scratchpad/*.csv  scratchpad/*/*.md
```

The list cannot be empty (keep first cannot be turned off; `[]` is a machine file error). A report over
`maxFileBytes` (256 KB) is not copied and keeps the whole session (`report too large to keep first`). Links are
never followed, and nothing inside a git folder or a clone is taken. Nothing else is read or analysed.

The full output of the session's background commands is kept too (`keepFirst.outputs`, default
`tasks/*.output`; a transcript holds only what the agent read of it), up to the same `maxFileBytes` each. An output
that is a link is followed only to a regular file inside the session folder; a link out (a sub-agent's output
links to its transcript, which stays anyway) is never followed and is listed in the manifest's `linksOut`. A
larger output is not copied and does not hold the session: the manifest's `notCopied` names its path, size and
sha256, so the record of what was there stays.

Every file is read by descriptor, one folder at a time from the session folder and never through a link, and only
when it is the file the last check walked; keep first refuses (and the folder stays) when what it finds is not what
the last check saw. The copy goes to `<archive>/<project>/<session id>/` (default archive
`~/.local/state/fork-cleaner/kept`), with a `manifest.json`: the source folder, the project, the session, the time,
and each file's path, size and sha256. New archive folders are made with mode 0700, each synced into its parent,
and every folder from the archive down must be a real folder of the cleaner's user (a link there fails keep
first). The copy is written as `.<session id>.partial`, every file and folder synced, read back and checked
against the sha256 of what was read from the session, renamed into place (the parent synced), and read back once
more. A `.<session id>.partial` already there is the cleaner's own half-written copy and is removed without
following a link: a link or a file at that name is unlinked as itself (what it leads to stays), and a folder is
removed by descriptors. Only then is the session folder deleted; a
copy that cannot be made or does not read back the same fails that delete (`FAILED ...: keep first: ...: nothing
deleted`, exit 1) and the folder stays whole. The run logs `kept first <path>: <n> files (<size>) in <copy>
(verified)` before its `deleting` line. The cleaner never deletes from the archive.

`plan` shows both: each session it would delete, the files it would keep first under it (`  would keep first
<path> (<size>)`), and a total (`agent-scratch: would keep first 2432 files (27.8 MB) in <archive>`).

## `compress-cold`: smaller, never gone

A fourth rule that deletes no content (#487, owner yes in #468). A file in a folder of `compressCold.folders` that
nobody has changed or read for that folder's `days` (its content, inode **and access** times) is replaced by a zstd
copy, `<name>.zst`, with the file's mode, times, group, ACL and `user.*` extended attributes. Without folders the rule does nothing. It may name folders inside
must-keep paths, such as old agent sessions or database copies: they stay whole, only smaller (`zstd -d` gives them
back).

```jsonc
// in the "cleaner" section
"compressCold": {
  "keepFreeGB": 5,
  "folders": [
    { "path": "~/.codex/sessions", "days": 30 },
    { "path": "~/.t3/repair-backups", "days": 14 }
  ]
}
```

| Key                           | Default  | Meaning                                                                                      |
| ----------------------------- | -------- | -------------------------------------------------------------------------------------------- |
| `compressCold.folders[].path` | required | a folder; files at any depth in it are looked at                                             |
| `compressCold.folders[].days` | required | days without a change or a read (7 or more)                                                  |
| `compressCold.keepFreeGB`     | 5        | GB the disk keeps free with a whole copy written (1 or more): a file that does not fit stays |

**The order, for each file.** The copy is written under a hidden part name in the same folder
(`.<name>.zst.<8 hex>.cleaner-part`), synced, tested with `zstd -t`, and decompressed again: the sha256 and length
of its content must equal those of what was read from the file. That checks every byte, so for a tar archive it
covers every file in it, not only their number. Only then does the copy get its name, by a hard link that never
replaces a file, and only then is the original removed, after one more look at the process list, the locks and the
file's own identity, size and times. Files are read with `O_NOATIME`, so a plan or a check never makes a cold file
look read. Before the read-back the copy's pages are dropped from the page cache (`posix_fadvise` DONTNEED), so
the check reads what is on the disk, not what was just written to memory. Each zstd step has a deadline of ten
minutes plus one second per MiB of the file; a step that runs out is stopped, its copy removed, and the original
stays. Just before the original is removed, both names are read once more: the original must still be the file
this run checked and `<name>.zst` the copy it linked (an editor's atomic save onto either name keeps both). A read
lease (`F_SETLEASE`) would close the last microseconds too, but only while the cleaner holds the file open, and an
open descriptor is exactly what the process-list check refuses, so the cleaner closes the file before that check
and takes no lease.

**Reads must be seen.** "Not read for N days" comes from the access time. On a filesystem mounted `noatime` reads
never change it, so every folder there is kept whole with `noatime` in the plan (mount it `relatime` to use the
rule). `relatime`, the Linux default, is fine: it updates the access time on the first read after a change and on
any read when the stored time is more than a day old. `days` is 7 or more, so a read within those days is always
seen.

**Never taken**, on top of what the config refuses: links (never followed, folders neither), folders with a `KEEP`
file, git folders, other filesystems and mount points, a folder shown somewhere else by a bind mount, other owners'
files, credential and lock names, a database with a `-wal`, `-shm` or `-journal` beside it (and those files),
hard-linked files, files already compressed (`.zst`, `.gz`, `.xz`, `.zip`, ...), files under 16 KB, a file whose
`.zst` name is taken by something else, and a folder named like `*log-archive*` (#468 keeps log archives as they
are). Skipped with a line in the plan instead of failing every run: a file its owner may not read, an immutable or
append-only file or folder (`chattr`), a file whose group is not one of the user's (the copy could not keep it), and
a file with extended attributes other than `user.*` and the ACL (`security.*`, `trusted.*`: the copy would lose
them). On a machine with SELinux every file has `security.selinux`, so every file is skipped this way and the rule
frees nothing there; that is safe (nothing changes, the run exits 0). The copy gets nothing the original lacks: an
ACL it would get from its folder is removed. The config refuses a folder that is `/`, the home folder or above it, a
must-keep path itself or a folder above one (`~/.codex` holds logins: name `~/.codex/sessions`), anything in or
above `~/.claude*` or the folder a `~/.claude*` link leads to (Claude Code reads its transcripts as plain text), a
folder named like `*log-archive*` or inside one, the cleaner's own log or daily folder (and, while `agent-scratch`
is on, its keep-first archive), a folder another rule deletes from (the `agent-scratch` root among them while that
rule is on), or a folder in or above another of its folders.

**The plan** says `would compress <file> (<size>, compress-cold: not changed or read for N days ...)` for each file,
and per folder group how much it would free: one zstd run over the start of every file (64 KB each, 16 MB in all)
gives the estimate. A run says `compressed <file> (<freed>, ...; now <file>.zst, <size>, read back and checked)`.
The report has a class `cold folders` (the configured folders, whole), and its freed figure counts compressed files
too.

**What happens when ...**

- **it runs again:** a `.zst` file is already compressed and is skipped; nothing new is done.
- **it stops half-way:** the original or a checked copy is there, never neither. Killed while writing: the part
  file stays beside the original; the next run removes it and compresses the file again. Killed after the link:
  both are there; the next run checks the copy against the original again and only then removes the original
  (a copy with other bytes keeps both, and the run fails until a person looks). A part file without its original
  beside it is left alone and named. A `<name>.zst` that is still the same file as its part file (killed between
  the link and the part's removal) is kept with `name taken: ... the copy of a run that stopped`; that run removes
  the part, and the next one checks the copy. A part file of another owner is never removed. No entry goes into the journal of half deletes: there is nothing half gone.
- **the file is in use:** it is kept and named (a process has it open or names it, it has a lock, a unit names it
  or a folder above it). A file opened or changed while it was compressed keeps its original; the new copy goes.
- **the disk is nearly full:** a file whose whole copy and `keepFreeGB` do not fit is kept, in the plan and again
  just before it is written. A disk that fills while zstd writes fails that file: its part file is removed and
  the original stays.
- **zstd is missing:** every folder is kept with "could not read" (exit 1); nothing is touched.
- **the machine file is older and has no `compressCold`:** the rule does nothing.
- **the cleaner is older than the machine file:** an older cleaner refuses the unknown `compressCold` key and every
  daily run exits 2. Install this cleaner on a machine first, then add the key.

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
          "branches": ["work/*"],
          "mergedBranches": true
        }
      ]
    },
    "docker": { "enabled": true, "days": 7 },
    "tmp": {
      "root": "/tmp",
      "patterns": { "example-build-*": 7, "example-crash-*": 1 }
    },
    "agentScratch": {
      "enabled": true,
      "days": 3,
      "keepFirst": { "maxSessionBytes": 8388608 }
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

| Key                                      | Default                                   | Meaning                                                                                              |
| ---------------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `logFile`                                | `~/.local/state/fork-cleaner/cleaner.log` | the log; its locks, journal and report mark are beside it (new, or the cleaner's own)                |
| `logLines`                               | 5000                                      | lines the log keeps (10 or more)                                                                     |
| `mustKeep`                               | none                                      | more paths that are never deleted (`*` within one part)                                              |
| `worktrees.repos[].root`                 | required                                  | the folder the clones and worktrees are directly in                                                  |
| `worktrees.repos[].github`               | required                                  | `owner/name` on GitHub                                                                               |
| `worktrees.repos[].regenerable`          | none                                      | plain names of ignored build folders, on top of `node_modules`, `__pycache__` and `.pytest_cache`    |
| `worktrees.repos[].branches`             | all                                       | branch patterns (shell style, case as written; `*` crosses `/`): only these branches are taken       |
| `worktrees.repos[].mergedBranches`       | false                                     | also deletes merged local branches the patterns name (see Merged branches); needs `branches`         |
| `worktrees.mergedHours`                  | 24                                        | hours after the merge (24 or more)                                                                   |
| `worktrees.usedHours`                    | 24                                        | hours since the worktree or clone was last used (24 or more)                                         |
| `docker.enabled`                         | false                                     | turns the rule on                                                                                    |
| `docker.days`                            | 7                                         | age of an unused anonymous volume (7 or more)                                                        |
| `tmp.root`                               | `/tmp`                                    | the temp folder                                                                                      |
| `tmp.patterns`                           | none                                      | name pattern to days without a change (1 or more)                                                    |
| `agentScratch.enabled`                   | false                                     | turns the rule on                                                                                    |
| `agentScratch.root`                      | `/tmp/claude-{uid}`                       | the folder of the project folders; `{uid}` is the user's id; one user's own folder (see above)       |
| `agentScratch.transcripts`               | `["~/.claude*/projects/*"]`               | the folders of the sessions' transcripts (patterns; at least one; one matching nothing keeps all)    |
| `agentScratch.days`                      | 3                                         | days without a change, of the folder and of its transcript (3 or more)                               |
| `agentScratch.keepFirst.archive`         | `~/.local/state/fork-cleaner/kept`        | where the files kept first go (never in a folder the cleaner deletes from)                           |
| `agentScratch.keepFirst.patterns`        | the list above                            | the reports kept first, relative to the session folder; at least one; a larger one keeps the session |
| `agentScratch.keepFirst.outputs`         | `["tasks/*.output"]`                      | the command outputs kept first; at least one; a larger one is named in the manifest, not copied      |
| `agentScratch.keepFirst.maxFileBytes`    | 262144                                    | the largest file kept first (65536 or more)                                                          |
| `agentScratch.keepFirst.maxSessionBytes` | 8388608                                   | more than this to keep first: the session stays whole                                                |
| `daily.mode`                             | `compare`                                 | what the timer does: `compare` (deletes nothing) or `run`                                            |
| `daily.oldTimers`                        | none                                      | timers (user or system) of the tools this one replaces: `run` refuses while one is on or unknown     |
| `daily.outDir`                           | `~/.local/state/fork-cleaner/daily`       | where `daily` writes its files                                                                       |
| `daily.keepDays`                         | 30                                        | days `daily` keeps its dated files (1 or more)                                                       |
| `daily.compare[]`                        | none                                      | the old tools' dry runs: `name`, `command` (absolute program, no shell), `pattern` (one group)       |

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
- **`run`**: `run` (deletes), then the report. It refuses (exit 2, nothing deleted, nothing written) unless every
  timer in `oldTimers` is provably off: two cleaners must not both delete. Each timer is asked in the user
  (`systemctl --user`) and the system scope (`systemctl --system`). It counts as off only when at least one scope
  knows it, and in each scope that does it is `disabled` or `masked` **and** `inactive` or `failed`, **and** the
  service it starts is `inactive` or `failed` (a run still deleting, `active` or `activating`, counts as on). A
  timer no scope knows refuses too: a misspelled name, or another user's timer, cannot be proven off; remove it
  from `oldTimers` once the old tool is gone. systemd that cannot answer refuses.

Everything goes to `outDir`: `plan-<day>.txt` (or `run-<day>.txt`) and `compare-<day>.txt` (each written whole,
kept `keepDays` days; a second run on the same day writes `plan-<day>.2.txt`, `.3.txt` and so on, never over the
first), and `report.log`, which gets each report's line before the mark moves. Only files of those names are ever
removed there. `outDir` may not be in a must-keep path, in or above a folder a rule deletes from, or be or hold
the home folder. The command prints one summary line (the journal keeps it), which ends with its exit code in
words:

| exit | meaning                                                                                                                                      |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | done                                                                                                                                         |
| 1    | something failed: a step could not read something, a file could not be written or trimmed, a source of the compare failed, the report failed |
| 2    | refused: run mode with an old timer on or unknown, or the plan refused                                                                       |
| 3    | compare mode: every step worked, and the compare found "new only" paths (each a bug until explained)                                         |

A 1 always wins over a 3: exit 3 never hides a real failure. An orphaned worktree in a repository's folder
(`orphaned worktree` above) is kept and is no failure; in the temp folder the `tmp` rule takes it by age.

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
the exit code is 3. **Old only** is where it is stricter, with its reason. A cleaner path is **both** only when an
old tool names that path or a folder it is in (the old tool deletes all of it too). A cleaner folder that holds
what an old tool deletes (the old tool deletes `/x/cache`, fork-cleaner all of `/x`) is new only, with
`deletes more: the old tools only /x/cache in it`; an old folder that holds a cleaner path is old only, with
`deletes more: fork-cleaner only ... in it`. A dry run that cannot start or ends with an exit code other than 0
makes the compare `incomplete` (exit 1, which wins over 3); what it printed still counts. Nothing is deleted.

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
again or to stop at any step; one installer at a time (`uninstall` too).

The installer reads the source without following a link anywhere below `--source` (a file or a folder on the way
that is a link is refused, never copied), so a link to a file only root may read cannot end up in the
world-readable copy. A copy of the same release that changed is made again beside it, self-tested, and only then
swapped in, in one step (`renameat2` exchange; two renames where the filesystem has none): a copy that stops half
way or fails its self-test leaves the copy in use as it was. The bootstrap passes only `--timer` or `--no-timer`
on to the installer; any other option is refused.

The installer writes no machine file: the `cleaner` section is the owner's, written by hand.

## What happens when ...

- **it runs again:** what is gone is no longer a candidate; a second run deletes nothing new and logs only its
  total line.
- **it stops half-way:** the entry it was deleting (temp folder, clone or worktree) is named `half deleted` by
  every later plan and run, never judged or deleted again, and the run exits 1 until a person acts; see
  [Half-deleted entries](#half-deleted-entries) for what to do. An agent session's folder is judged again and
  finished when every check passes and nothing in it changed since the delete began.
- **merged branches, the run stops half-way:** a branch is gone or still there (one git step); a reflog or config
  section left behind is no candidate. The log line naming the commit is written before the delete, so it is there
  even when the run stops right after git removed the branch. A config section left behind is inherited by a new
  branch of that name that has none of its own (as with git). Run again: nothing is left to judge. With `mergedBranches` off or missing (an
  older machine file), no branch is looked at. An older cleaner refuses the unknown key (exit 2): install this one
  first, then add it.
- **tmp, an orphaned worktree stopped half-way:** as any temp entry: `half deleted`, judged no more.
- **tmp, the drive or repository of a worktree comes back:** a worktree whose git folder may be elsewhere is
  `unknown parent` and kept; once the drive is mounted again or `git worktree repair` ran, the next run judges it
  as any worktree.
- **agent-scratch, keep first stopped half-way:** a half-written copy (`.<session>.partial`) is thrown away and
  made again by the next run; a copy that was made but whose delete never ran is found, read back and used again
  (no second copy); an older copy that differs stays, and the new one is `<session>.2`, and so on.
- **agent-scratch, the session comes back between plan and delete:** the last check reads the process list,
  the transcripts and the folder anew; it is `REFUSED` and nothing is copied or deleted. If it comes back while
  keep first copies, the check after the copy refuses it: the copy stays in the archive (the next run uses it
  again) and nothing is deleted.
- **agent-scratch, the session comes back during the delete:** a file it makes after the last check (its inode
  time is later) stops the delete there, even when it got the inode number of a file just deleted; the rest
  stays, `half deleted`, exit 1, and the next run sees "changed since the delete began".
- **agent-scratch, a half delete hit a hard-linked file:** unlinking one name of a file with another name changes
  its inode time, which the next run reads as "changed since the delete began": the session stays `half deleted`
  and the run exits 1 until a person removes it. This fails safe and is left so.
- **agent-scratch, the root is empty or missing:** nothing to do, exit 0. A machine file without
  `agentScratch` leaves the rule off and the root is not even read.
- **something is in use:** it is kept and named. A process, unit or pull request that arrives between plan and
  delete is seen by the last check before the delete.
- **the config is old, empty or wrong:** an empty section does nothing; anything it does not understand stops the
  command before it reads a single folder.
- **GitHub, git or Docker is down:** everything that needed the answer is kept, and the exit code is 1.
- **two runs start at once:** the second one stops with "another run" and deletes nothing.
- **the tool it replaces is still installed:** compare mode runs beside it and deletes nothing; run mode refuses
  while a timer in `oldTimers` is on, still running, or unknown.
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
- Drives are known from `/proc/self/mountinfo` and `/etc/fstab` only: a drive that a systemd `.mount` or
  automount unit, `autofs` or a desktop mounts elsewhere than `/mnt`, `/media` or `/run/media`, when it is not
  mounted now, cannot be seen, and a worktree whose repository was on it can count as orphaned in the temp folder.
- Linux only so far (`/proc`, systemd unit folders).

## Tests

```
python3 -I -B fork-kit/cleaner/tests/guard.test.py
python3 -I -B fork-kit/cleaner/tests/worktrees.test.py
python3 -I -B fork-kit/cleaner/tests/engine.test.py
python3 -I -B fork-kit/cleaner/tests/report.test.py
python3 -I -B fork-kit/cleaner/tests/compress.test.py
python3 -I -B fork-kit/cleaner/tests/scratch.test.py
python3 -I -B fork-kit/cleaner/tests/daily.test.py
python3 -I -B fork-kit/cleaner/tests/install.test.py
```

Everything lives in one temporary folder per test: real git repositories whose "GitHub" is a bare repository, stub
`gh` and `docker` commands, and a fake `/proc`. No real worktree, Docker volume or temp entry is touched. Each
file ends with a red check: it breaks one refusal at a time in a copy of the code and shows that the test for
that refusal then fails.
