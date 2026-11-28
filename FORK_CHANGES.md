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

Merge `origin/main`; `FORK_CHANGES.md` conflicts. Then, in the pull request's checkout:

```sh
node .github/scripts/fork-changes-list.cjs adopt
git checkout origin/main -- FORK_CHANGES.md
git add FORK_CHANGES.md docs/fork/changes
git commit --no-edit
```

`adopt` writes each row the pull request added or edited (against its merge base) as a change file,
and removes the file of a row it removed. A row whose file changed on `main` since is left alone and
named, to edit by hand. It can run again: a row a file already holds is skipped.
