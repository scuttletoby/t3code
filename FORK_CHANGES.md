# Fork changes

This repository is a fork of [T3 Code](https://github.com/pingdotgg/t3code). Every change the fork
makes has its own file in [docs/fork/changes/](docs/fork/changes/): what it does, why, its setting
and default, and the pull requests. Changes to the app are in
[docs/fork/changes/app/](docs/fork/changes/app/), changes to how this repository is built, tested
and released in [docs/fork/changes/repo/](docs/fork/changes/repo/). The fork's settings are
explained in [docs/fork/README.md](docs/fork/README.md), which also says how to install the fork
today.

The whole list as one table: `node .github/scripts/fork-changes-list.cjs print`.

The rule for every change: a machine that never names a fork setting behaves as upstream's T3 Code.
A setting is written as `server.<key>` (the server's `settings.json`) or `projectOverride.<key>`
(one project's overrides). A test (`.github/scripts/fork-changes-list.test.cjs`) fails when a fork
setting is missing from the change files or a setting named there does not exist.

## Adding a change

A pull request that adds a fork feature adds its own file, `docs/fork/changes/app/<name>.md` or
`docs/fork/changes/repo/<name>.md` (lowercase letters, digits and `-`; by habit the first pull
request's or issue's number, then a few words), and one that changes a feature edits that feature's
file. A file is exactly five lines, in this order:

```markdown
- Change: what the fork does, in one line.
- Why: why, in one line.
- Setting: `server.<key>` or none.
- Default: off, always, ...
- PR: [#123](https://github.com/scuttletoby/t3code/pull/123)
```

One file per change is what lets two pull requests that each add or edit a change merge without a
conflict; a table here made every pair of them conflict, since the formatter re-pads it. This page
holds no table row (the test refuses one).

A new fork setting also needs its row in `packages/contracts/src/forkDefaults.ts` and its row and
section in [docs/fork/README.md](docs/fork/README.md) (steps in
[updates.md](docs/fork/updates.md#adding-a-fork-setting)).

## An open pull request that still edits the old table

Merge its base (`origin/main`, or the base branch of a stacked pull request); `FORK_CHANGES.md`
conflicts. Then, in the pull request's checkout:

```sh
node .github/scripts/fork-changes-list.cjs adopt
git add FORK_CHANGES.md docs/fork/changes
git commit --no-edit
```

During a merge `adopt` reads the merge's own two sides. Its argument (`adopt <base branch>`,
default `origin/main`) is used only when no merge, cherry-pick or rebase is in progress.

`adopt` writes each row the pull request added or edited (against its merge base) as a change file,
and removes the file of a row it removed. Then it puts the base's entry page in `FORK_CHANGES.md`.
When anything is left to do by hand, it exits 1 and changes nothing: a row whose file changed on
`main` since, rows it cannot pair, conflict markers in the entry page's own text, a base with no
table, or a side with the old table still. It can run again: a row a file already holds is skipped,
and a `.md.tmp` file left by a run that stopped is removed.

A replay works too: during a `git cherry-pick` or `git rebase` stopped on `FORK_CHANGES.md`, `adopt`
reads the commit being replayed against its parent, then `git cherry-pick --continue` (or
`git rebase --continue`). A branch whose merge base has no table (a branch from before the list's
history was rewritten) is refused: replay its commits instead. A merge already committed with the
pull request's own table is undone with `git reset --merge HEAD^`, then merged again.

An abort may keep `adopt`'s edits: `git merge --abort` refuses while they are in tracked files, and
`git cherry-pick --abort` keeps them. To undo, before the abort: `git checkout -- docs/fork/changes`,
then the abort, then `git clean -fd -- docs/fork/changes` (this also deletes any other untracked file
there).

`print` lists the changes by first pull request number, not in the old table's order.
