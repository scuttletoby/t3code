# git-clean

A composite action for the **first step of a job, before `actions/checkout`**. A self-hosted runner reuses its
workspace, and an earlier job may have run pull request code there. That code can write `.git/config` keys that
`actions/checkout` does not reset: `core.fsmonitor` (a command git runs during the checkout's own git calls),
`core.hooksPath`, `attr.tree` with a merge driver, an include of another file. The next job would run them before any
of its own steps runs pull request code.

- **Self-hosted runner:** it empties the workspace (`GITHUB_WORKSPACE`), so the checkout starts from nothing, as on a
  GitHub-hosted runner. Symlinks are removed as links, never followed: nothing outside the workspace is touched.
- **GitHub-hosted runner:** each job starts on a fresh disk, so nothing is removed.
- **Both:** it then refuses a `.git` at the checkout path whose config `actions/checkout` does not write (below).

The fork-ci actions that read a checkout with git (docs-scope, job-changed, test-select, head-check, pr-queue's
tested-base) run the same check as their first step, and every git call of theirs runs with no global or system
config (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`) and with hooks, `core.fsmonitor`, the commit-graph and
replace refs off. tested-base writes no note instead of failing.

## Usage

Pin a release commit by its full SHA, never a commit of main ([why](../../README.md#pinning)):

```yaml
jobs:
  build:
    runs-on: [self-hosted, build]
    steps:
      - uses: scuttletoby/t3code/fork-ci/actions/git-clean@<release sha> # ci/vN
      - uses: actions/checkout@<sha>
        with: { fetch-depth: 0 }
```

Put it in every job that runs on a self-hosted runner and checks out code, before the first checkout and before any
other step that writes the workspace. With several checkouts, list their paths (`path: | ...`): all of them are
checked.

| Input  | Default | Meaning                                                              |
| ------ | ------- | -------------------------------------------------------------------- |
| `path` | `.`     | the checkout path or paths (actions/checkout's `path`), one per line |

No outputs. A refusal fails the step, so the checkout and every later step do not run.

## What is refused

- `GITHUB_WORKSPACE` that is not an absolute folder, is a symlink, is not inside `RUNNER_WORKSPACE`, or holds `HOME`,
  `RUNNER_TEMP`, `RUNNER_TOOL_CACHE` or the action itself: nothing is removed.
- A delete that fails (a file of another user, for example from a container): the step fails with the path. What is
  left is removed by the next run once the file can be deleted.
- A checkout path that is absolute or leads outside the workspace.
- `<path>/.git` that is a file or a symlink (a worktree or submodule: git would read another repository), or that holds
  `commondir`.
- A key in `.git/config` or `.git/config.worktree` that `actions/checkout` (v4 to v7) and `git init` do not write, or
  one of those keys with another kind of value. Allowed: the core settings of `git init`, sparse checkout, the object
  format, `gc.auto 0`, `remote.origin` url/fetch/partial clone, `branch.*.remote`/`merge`, the token header, the LFS
  filter exactly as `git lfs install` writes it, submodule url/active, `user.name`/`user.email`. The include
  checkout writes for its token (`includeIf.gitdir:...path`) only to a `git-credentials-<uuid>.config` in
  `RUNNER_TEMP` that holds nothing but the token header. Everything else is refused by name, among it
  `core.fsmonitor`, `core.hooksPath`, `core.sshCommand` (checkout's `ssh-key` with `persist-credentials`: use
  `persist-credentials: false`), `core.pager`, `attr.tree`, merge/diff/filter drivers, `include.path`, other
  includes, `url.*.insteadOf` and `alias.*`.

## Not covered

The runner user's own files outside the workspace (`~/.gitconfig`, the system git config) are not removed: the fork-ci
git calls ignore them, `actions/checkout` does not. A runner that takes pull requests from people who are not trusted
also needs a job home that does not outlive the job.
