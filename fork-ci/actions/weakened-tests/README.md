# weakened-tests

Which **existing** tests does this pull request delete, skip or weaken? The action lists them in the job summary and
in one pull request comment, and checks the pull request body for a "why" line for each one. It only reports: the
step never fails, whatever it finds or whatever goes wrong. Making it a gate later is the owner's decision.

Agents write most of the code here, and they can change existing tests in the same pull request as the code, so a
green run proves less than it seems. This action makes those changes visible to the review (the tests lens) without a
person having to spot them.

## Use

Run it on `pull_request`, after a checkout with `fetch-depth: 0` (it needs the base, the head and their merge base).
[`weakened-tests-caller.example.yml`](weakened-tests-caller.example.yml) is a whole caller workflow:

```yaml
permissions:
  contents: read
  pull-requests: write # read the body, post and edit the comment
steps:
  - uses: actions/checkout@<sha>
    with: { fetch-depth: 0, persist-credentials: false }
  - uses: scuttletoby/t3code/fork-ci/actions/weakened-tests@<release sha> # ci/vN
```

Listen to `edited` too (the example does), so a body edit that adds a "why" updates the comment. Give the workflow a
`concurrency` group per pull request without `cancel-in-progress`, so two runs never both post a first comment.

| Input            | Default                                       | What it is                                                         |
| ---------------- | --------------------------------------------- | ------------------------------------------------------------------ |
| `config`         | `.github/weakened-tests.json`                 | the project's config, read at the base commit; optional file       |
| `base`, `head`   | the event's `pull_request.base/head.sha`      | the change is `merge-base(base, head)..head`                       |
| `path`           | `.`                                           | where the repository is checked out                                |
| `pr`, `repo`     | the event's pull request, `github.repository` | where the comment goes                                             |
| `token`          | `github.token`                                | reads the pull request, posts and edits its comments               |
| `comment`        | `true`                                        | `false`: only the job summary and the outputs                      |
| `comment-author` | `github-actions[bot]`                         | the login the token comments as; only its marked comment is edited |

| Output        | Value                                                                                       |
| ------------- | ------------------------------------------------------------------------------------------- |
| `status`      | `ok` or `could-not-read`                                                                    |
| `count`       | items listed; empty when the change could not be read                                       |
| `missing_why` | listed items the body gives no why for; empty when the change or the body could not be read |

A pull request from a fork gets a read-only `github.token`: the comment then fails with a warning and the job summary
still has the report.

## What it lists

For each test that exists on the base side of a test file the pull request changes (a new test is never listed):

- **deleted**: gone from its file, and no test of the same name was added in another file the change touches (a
  moved test is compared where it went);
- **skipped or disabled**: a `skip` pattern matches more often in the test than before (`@Disabled`, `assumeTrue(`,
  `@pytest.mark.skip`, `xfail`, `it.skip`, `xit(`);
- **focused**: a `focus` pattern matches more often (`it.only`): the other tests in the file stop running;
- **assertion removed or changed**: a line that matches an `assert` pattern is gone or different (whitespace aside;
  the old line is shown);
- for the file itself (test name `(file)`): a skip or focus added outside any test (`pytestmark`, a class-level
  `@Disabled`, `describe.skip`), or a side that is not UTF-8 (**could not read this file**).

A test's lines run from its first line (with the decorator or annotation lines just above it) to the next test's
first line, so a decorator is counted for the test below it. Renamed files are paired by git's rename detection and
compared with their old selves: a rename is never a deletion.

A body line that names a listed test (the name as a whole word, or for `(file)` the file's path or name) counts as its
"why"; the comment marks each row `yes` or `no` and says how many have none.

## Config

Optional, read from the base commit. Each language has `files` (globs from the repository root, as in docs-scope:
`**/` any folders, `*` within a name) and regular expressions (Python syntax, multi-line mode): `test` (each needs a
`(?P<name>...)` group naming the test), `before` (lines above a test that belong to it), `skip`, `focus` and `assert`.
A file belongs to the first language whose `files` match, the defaults first.

```json
{
  "languages": {
    "java": { "skip": ["@Flaky\\b"] },
    "kotlin": {
      "files": ["**/src/test/**/*.kt"],
      "test": ["@Test\\s+fun\\s+`?(?P<name>[^`(]+)`?\\s*\\("],
      "before": ["^\\s*@"],
      "skip": ["@Disabled\\b"],
      "assert": ["\\bassert\\w*\\s*\\("]
    }
  },
  "max_bytes": 10000000
}
```

- An entry for a default language (`java`, `python`, `node`) **adds** its patterns to the defaults; a new name is a
  new language and needs `files` and `test`.
- `max_bytes` (default 10 000 000): the most bytes of test files (both sides) the action reads; more is "could not
  read".
- Unknown keys, wrong types, a pattern that does not compile and a `test` pattern without `name` make the report
  "could not read" (a typo must not drop a rule quietly).

The defaults are in `DEFAULT_LANGUAGES` in [`weakened_tests.py`](weakened_tests.py): Java (`**/src/test/**/*.java`,
`*Test.java`, `*Tests.java`, `*IT.java`; `@Test`, `@ParameterizedTest`, `@RepeatedTest`), Python (`test_*.py`,
`*_test.py`, `*.test.py`, files under `tests/` and `test/`; `def test*`), Node (`*.test.*`, `*.spec.*`, `__tests__/`
for js, jsx, ts, tsx, mjs, cjs, mts, cts; `it(` and `test(` with a string name).

## Runs again, stops half-way, old state

- The comment starts with `<!-- fork-ci weakened-tests -->`; the action edits the oldest such comment by
  `comment-author` and never another login's. A re-run with the same result edits nothing; a new push or a body edit
  that changes the result edits the same comment.
- The change is computed afresh on every run from the base and the head, so a force push is compared with the base
  again; a test that a later push restores drops off the list and the comment says none are left.
- Nothing to list and no comment yet: no comment (the job summary still says none). A run that stops before it
  posts leaves the comment as it was; the next run writes it.
- **Could not read** (a commit missing from the checkout, no merge base, a git error, a bad config, test files over
  `max_bytes`, an unexpected error) is said in the comment and the summary, never shown as an empty list. A failure to
  read the body, list the comments or post is a warning; the step still passes.

## Why a pull request cannot loosen it

The script runs from the action's own folder at the SHA the caller pins, and the config is read from the base commit,
never the checkout: a pull request that edits the config is judged by the base's patterns. As with every fork-ci
action, a `pull_request` run uses the workflow file from the pull request, so pin only release commits
([`fork-ci/README.md`](../../README.md#pinning)).

## Limits

Line-based, not a parser: a test is found by its first line, an assertion by the line it starts on (a change on a
continuation line of a multi-line assertion is not seen), a decorator between two tests that belongs to a class is
counted for the test above it, and a test named in a way the patterns do not match is not seen on either side. Add
patterns in the config for a project's own spellings.

## Tests

`weakened_tests.test.py` builds real git repositories and runs against a fake `gh`. It runs in the fork's CI
(`.github/fork-ci-tests.txt`): `python3 fork-ci/actions/weakened-tests/weakened_tests.test.py`.
