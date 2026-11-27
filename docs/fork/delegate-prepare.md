# Delegate prepare: set up a checkout before an agent starts

A delegated writer or lens should start by thinking, not by cloning, merging, installing and running
tests. `prepareCli.ts` does those fixed steps by script, before the agent starts, and writes what it
found for the brief: where the checkout is, whether the base merged or conflicted, and which baseline
tests were already red. A red baseline found before the agent starts stops it from "fixing" a test
that was red before its change.

It is a plain node script with no T3 server behind it:

```sh
node apps/server/src/fork/delegatePrepare/prepareCli.ts \
  [--dir <checkout>] [--clone <url>] [--branch <name>] [--base <ref>] [--json] [--paths <file>...]
```

General vs project: the script is general; each project gives its own install and test commands in
`.t3code/prepare.json`. A project without that file gets nothing but the clone and branch it asked
for.

## The project's config: `.t3code/prepare.json`

```json
{
  "base": "origin/main",
  "install": ["pnpm install --frozen-lockfile"],
  "baseline": "pnpm exec vitest related --run {files}",
  "timeoutMinutes": 30
}
```

| Key              | Default         | What                                                                                                                                                                                                    |
| ---------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base`           | `"origin/main"` | The ref merged into the checkout first. A base on a remote (`origin/...`) is fetched first.                                                                                                             |
| `install`        | `[]`            | Shell commands run in order in the checkout's top folder. The first one that fails (or runs past the time limit) stops the prepare: it **failed**.                                                      |
| `baseline`       | none            | One shell command. `{files}` becomes the touched files, quoted for a POSIX shell. A failing run is **red**, not a failed prepare; one that cannot run (exit 126 or 127) or is killed by a signal fails. |
| `timeoutMinutes` | `30`            | Per command, at most 240. A command still running then is stopped with everything it started (its process group).                                                                                       |

Any other key is refused, so a typo such as `instal` fails the prepare instead of quietly skipping
the install. The config is the one on the branch being prepared, read after the switch to it: a
branch that adds or changes it gets its own, and a reused clone never runs the last branch's
commands. They are that branch's own commands: run the prepare only on branches you would run the
project's tests on anyway, as with upstream's worktree setup script.

The touched files are the branch's own changes against the base (`git diff base...HEAD`, files that
still exist, any name) plus any `--paths`. A name that starts with `-` is passed as `./-name`, so
the test command never reads it as an option. With `{files}` in the command and no touched files,
the baseline is skipped; a command without `{files}` always runs.

## What it does, in order

1. **Checkout.** `--clone <url>`: clones into a folder beside `--dir` and renames it into place, so
   a clone stopped half-way is never taken for a finished one. An existing `--dir` is reused when
   it is a clone of the same URL, spelled the same, with a commit checked out; anything else there
   is refused and left alone. Without `--clone`, `--dir` (default: the current folder) must be a
   git checkout. One prepare runs on a checkout at a time (`t3-prepare.lock` in its git folder); a
   lock whose process is gone is taken over, with a note.
2. **Branch.** `--branch <name>`: the local branch, else the remote's (tracked), else a new one from
   the base. It does not switch over local changes, nor away from a detached HEAD whose commits are
   on no branch. A branch behind the same name on its remote is fast-forwarded; one that has diverged
   from it, or is behind with local changes, fails the prepare and is left as it is, so stale code
   is never reported as prepared. A configured upstream is not used for this: for a branch made
   from the base it is the base, which step 4 merges in.
3. **Config**, read from that branch.
4. **Merge the base.** Skipped over local changes or when the base is not found. A conflict lists
   the conflicted files and **undoes the merge**, so the agent starts on a clean checkout and merges
   with thought. A merge or rebase someone left in progress fails the prepare and is never undone.
   A merge that succeeds is a local commit until the agent pushes.
5. **Install**, then the **baseline** tests.

The result goes to `t3-prepare.json` and `t3-prepare.md` in the checkout's own git folder
(`git rev-parse --absolute-git-dir`: per worktree, never committed), and the summary to stdout.
Progress and command output go to stderr.

| Exit  | Meaning                                                                                                                                               |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0     | Prepared (a red baseline or a merge conflict included), or no project config                                                                          |
| 1     | The prepare failed: checkout, clone, branch, config, a merge that failed without conflicts, install, a baseline that could not run or was killed      |
| 2     | Bad arguments                                                                                                                                         |
| 128+N | Stopped by a signal (SIGINT, SIGTERM, SIGHUP) during install or baseline: the running command's process group is stopped and the result says `failed` |

## Using it

**A clone per agent** (the orchestrator's current way): run it before `delegate_task` and put its
summary in the brief, or tell the agent where the result file is.

```sh
node <fork checkout>/apps/server/src/fork/delegatePrepare/prepareCli.ts \
  --clone https://github.com/<owner>/<repo> --dir <work folder> --branch <pr branch>
```

**`delegate_task` with `isolation: "worktree"`:** upstream runs the project's `runOnWorktreeCreate`
script (an entry of `scripts` in `t3.json`) in each new worktree. Upstream's `async` defaults to
`true`, which starts the agent while the script still runs and only records a failure. Set
`"async": false` so the agent waits for the prepare and a failed prepare fails the launch:

```json
{
  "name": "Prepare",
  "command": "node <fork checkout>/apps/server/src/fork/delegatePrepare/prepareCli.ts",
  "icon": "configure",
  "runOnWorktreeCreate": true,
  "async": false
}
```

The agent then reads `"$(git rev-parse --absolute-git-dir)/t3-prepare.md"`. That runs on every new
worktree thread, not only delegated ones.

## When it runs again, stops half-way, or meets old or empty state

- **Again:** the same checkout gives the same result: a reused clone, the branch already checked out
  and brought up to its remote branch, the base already merged ("already up to date"), install and
  baseline run again. A second prepare while one runs is refused and leaves the first one's result
  alone.
- **Stopped half-way:** the result file says `running`, with the prepare's `pid`, before any
  command starts, so an older run's result never looks current. A signal stops the running
  command's process group and writes `failed` before exiting; only SIGKILL leaves `running`, and
  then the next run takes over the lock and says an earlier prepare was stopped. A clone stopped
  half-way stays beside `--dir` (`<dir>.t3-prepare-clone-<pid>`), never at it. Two first clones
  into one folder: the one that finishes second reuses the first one's clone. A merge it started is
  either finished or undone in the same step; a merge left by anything else stops the next run.
  Installs are the project's own commands, which should be safe to rerun (`pnpm install` is).
- **Old or empty state:** no config file means nothing but the asked-for clone and branch; a
  checkout from before the config existed is prepared by whatever config its branch has. A clone
  with no commit checked out is refused. A base that is not found skips the merge with a note; a
  fetch that fails leaves a note that branches may be behind.

## Known limits

- A signal sent to the prepare alone during a git step (clone, fetch, switch, merge) is handled
  when that step ends; if no install or baseline follows, the prepare finishes normally. Ctrl-C and
  SIGHUP reach git too, through the shared process group.
- After a SIGKILL of the prepare, its running command's group lives on; the next run takes over
  the lock while it may still write into the checkout.
- Two runs that find the same stale lock at the same moment can both take it over.
- Clone folders left beside `--dir` by a killed clone are not cleaned up.

## Not yet

- `delegate_task` does not run the prepare itself or put its result into the child's prompt: no fork
  seat on that path sees the new worktree or the prompt, and adding one is new upstream lines
  (`.github/fork-seams.json`).
- Push and stop: writers already push and stop, and the pull request watch wakes them
  ([#991](https://github.com/scuttletoby/t3code/issues/991)).
