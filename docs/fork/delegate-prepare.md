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

| Key              | Default         | What                                                                                                                                               |
| ---------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base`           | `"origin/main"` | The ref merged into the checkout first. A base on a remote (`origin/...`) is fetched first.                                                        |
| `install`        | `[]`            | Shell commands run in order in the checkout's top folder. The first one that fails (or runs past the time limit) stops the prepare: it **failed**. |
| `baseline`       | none            | One shell command. `{files}` becomes the touched files, quoted for a POSIX shell. A failing run is reported as **red**, not as a failed prepare.   |
| `timeoutMinutes` | `30`            | Per command, at most 240. A command still running then is stopped with everything it started (its process group).                                  |

Any other key is refused, so a typo such as `instal` fails the prepare instead of quietly skipping
the install. The commands are the checkout's own, read from the checkout as it is when the prepare
starts: run it only on branches you would run the project's tests on anyway, as with upstream's
worktree setup script.

The touched files are the branch's own changes against the base (`git diff base...HEAD`, files that
still exist) plus any `--paths`. With `{files}` in the command and no touched files, the baseline is
skipped; a command without `{files}` always runs.

## What it does, in order

1. **Checkout.** `--clone <url>`: clones into `--dir` when it does not exist, or reuses it when it is
   a clone of the same URL (anything else there is refused and left alone). Without `--clone`, `--dir`
   (default: the current folder) must be a git checkout.
2. **Branch.** `--branch <name>`: the local branch, else the remote's (tracked), else a new one from
   the base. It does not switch over local changes.
3. **Merge the base.** Skipped over local changes or when the base is not found. A conflict lists
   the conflicted files and **undoes the merge**, so the agent starts on a clean checkout and merges
   with thought. A merge or rebase someone left in progress fails the prepare and is never undone.
4. **Install**, then the **baseline** tests.

The result goes to `t3-prepare.json` and `t3-prepare.md` in the checkout's own git folder
(`git rev-parse --absolute-git-dir`: per worktree, never committed), and the summary to stdout.
Progress and command output go to stderr.

| Exit | Meaning                                                                                             |
| ---- | --------------------------------------------------------------------------------------------------- |
| 0    | Prepared (a red baseline or a merge conflict included), or no project config                        |
| 1    | The prepare failed: checkout, clone, branch, config, a merge that failed without conflicts, install |
| 2    | Bad arguments                                                                                       |

## Using it

**A clone per agent** (the orchestrator's current way): run it before `delegate_task` and put its
summary in the brief, or tell the agent where the result file is.

```sh
node <fork checkout>/apps/server/src/fork/delegatePrepare/prepareCli.ts \
  --clone https://github.com/<owner>/<repo> --dir <work folder> --branch <pr branch>
```

**`delegate_task` with `isolation: "worktree"`:** upstream runs the project's setup script (the
`runOnWorktreeCreate` script in `t3.json`) in the new worktree before the agent starts, and a setup
script that exits non-zero fails the launch before the agent spends anything. A project that makes
this script its setup script gets the prepare on every new worktree; the agent then reads
`"$(git rev-parse --absolute-git-dir)/t3-prepare.md"`. That runs on every new worktree thread, not
only delegated ones.

## When it runs again, stops half-way, or meets old or empty state

- **Again:** the same checkout gives the same result: a reused clone, the branch already checked out,
  the base already merged ("already up to date"), install and baseline run again.
- **Stopped half-way:** the result file says `running` before any command starts, so an older run's
  result never looks current. A merge it started is either finished or undone in the same step; a
  merge left by anything else stops the next run. Installs are the project's own commands, which
  should be safe to rerun (`pnpm install` is).
- **Old or empty state:** no config file means nothing but the asked-for clone and branch; a
  checkout from before the config existed is prepared by whatever config the checkout has. A base
  that is not found skips the merge with a note; a fetch that fails leaves a note that the base may
  be behind.

## Not yet

- `delegate_task` does not run the prepare itself or put its result into the child's prompt: no fork
  seat on that path sees the new worktree or the prompt, and adding one is new upstream lines
  (`.github/fork-seams.json`).
- Push and stop: writers already push and stop, and the pull request watch wakes them
  ([#991](https://github.com/scuttletoby/t3code/issues/991)).
