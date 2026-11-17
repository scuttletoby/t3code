# test-select

A composite action that answers one question about a pull request: **which test files can its change reach?** A
test file runs when a changed path is one of its inputs; the rest need not run on that pull request. Anything it
cannot work out runs every test, and every run that is not a pull request (the default branch, a merge queue, a
schedule, a manual run) runs every test, so a missed input costs at most one pull request run: the default branch
still tests it.

It is the general half. The project half is a JSON config in the calling repository: the test files, the paths that
always run every test (the runner, shared helpers and fixtures), and extra folders imports load from.

## Usage

Pin a release commit by its full SHA, never a commit of main ([why](../../README.md#pinning)):

```yaml
jobs:
  tooling:
    steps:
      - uses: actions/checkout@<sha> # the default ref: on pull_request GitHub's merge commit ($GITHUB_SHA)
        with: { fetch-depth: 0 } # the merge base must be there
      - id: select
        uses: scuttletoby/t3code/fork-ci/actions/test-select@<release sha> # ci/vN
        with:
          config: .github/test-select.json # default
      - env:
          ALL: ${{ steps.select.outputs.all }}
          LIST: ${{ steps.select.outputs.list }}
        run: |
          if [ "$ALL" != false ]; then python3 run_tests.py tests/test_*.py   # every test (also: output missing)
          elif [ -s "$LIST" ]; then mapfile -t files < "$LIST"; python3 run_tests.py "${files[@]}"
          else echo "no tool test touched"; fi
```

Always test `all != 'false'`, never `all == 'true'`: a missing or empty output then means "every test". The list file
holds every test when `all` is not `false`, so a caller may also just run the list.

### Outputs

| Output   | Value                                                                                      |
| -------- | ------------------------------------------------------------------------------------------ |
| `all`    | `false` only when a pull request's tests were picked; `true` for every test                |
| `count`  | how many test files the list holds                                                         |
| `total`  | how many test files the config names                                                       |
| `list`   | a file with the test files to run, one per line, in config order                           |
| `reason` | one line: `selected N of M test files`, `no tool test touched: ...` or `all M test files:` |

The step log then prints one line per chosen test with the changed path it takes in. Control characters and bytes
that are not UTF-8 in a file name are printed escaped, so a file name can neither start a `::workflow-command::` line
nor add an output.

### Config

```json
{
  "tests": ["tests/test_*.py", "tools/check/test_install.py"],
  "always_all": [
    { "glob": "tests/run_tests.py", "why": "the test runner" },
    { "glob": "tests/_*.py", "why": "a shared test helper" },
    { "glob": "tests/fixtures/**", "why": "shared fixtures" }
  ],
  "import_roots": ["lib"]
}
```

- `tests`: globs from the repository root (`**/` any folders, `*` and `?` inside one name) or literal paths, in the
  order the list keeps. A literal path that is not in the repository fails the step (a renamed test must be renamed
  here too).
- `always_all`: a changed path that matches one runs every test, with its `why` in the log. List what tests read in
  ways the action cannot see: a runner, helpers every test loads, fixture folders, data files whose names come from
  data.
- `import_roots`: folders searched for imports after the importing file's folder and the `sys.path` entries it can
  work out (code that loads from an install path that mirrors a repository folder).

A change to the config itself, or to a workflow file (`.github/workflows/`), always runs every test.

## What counts as an input

Worked out from the checkout on every run (never cached, so it cannot go stale), starting at each test file:

- the test file itself;
- **Python**: every import that is a file of the repository, searched in the importing file's folder, the `sys.path`
  entries it can work out (`sys.path.insert`/`append`, under any alias; one list for the whole process), relative
  imports and packages (`__init__.py`), `import_roots`, and `importlib.import_module` with a literal name, followed to
  their own inputs;
- every path a Python file names that the action can work out: string literals, `__file__`, `os.path.join`,
  `dirname`, `abspath`, `pathlib` `/`, `.parent`, `.parents[n]`, `.joinpath`, f-strings and `+`, through names by
  Python's scope rule (every assignment of the name counts) and `self.x`. Then:
  - a path that names a file is an input (a script it runs is followed the same way);
  - a folder counts with everything under it only when the file lists it (`os.walk`, `os.listdir`, `glob`, `.glob`,
    `.rglob`, `.iterdir`, `shutil.copytree`); a folder only named is a base for other paths;
  - a glob with a known folder (`docs/*.md`) takes in the changed paths it matches; one that starts with a wildcard
    names no place;
  - a known folder with an unknown rest (`ROOT / "plugins" / name`) takes in that folder, unless the file is inside
    it (its own folder or the root: the base of nearly every path);
  - text with a space, quote, `(` or `$` is a message or a shell line, not a path;
- **shell and JavaScript files** it reaches (`.sh`, `.bash`, `.mjs`, `.cjs`, `.js`, or such a `#!` line): every word
  that names a file of the repository, relative to the file or the root, followed the same way; a folder word takes
  in the folder unless the file is inside it.

A deleted path counts as present, so a test that still names a deleted or renamed file runs (and fails there).

## Every test runs when

- the event is not `pull_request`;
- the change is unknown or empty (HEAD must be GitHub's merge commit: [docs-scope's rule](../docs-scope/README.md));
- the config, a workflow file or an `always_all` path changed;
- a Python file among a test's inputs does not parse;
- an `importlib.import_module` name cannot be worked out;
- an import names a module that is in the repository but not where the import looks (a `sys.path` entry it could
  not work out); an import of a name no repository file has is taken as the standard library or third-party;
- anything else goes wrong in the action.

## What it cannot see

A path built from data at run time: a name read from a JSON file, a loop over a list, a file in the test's own folder
or the root picked by a variable. Such a test is missed only on pull requests; the default branch runs every test
and catches it on the next run. Put such data files in `always_all`.

## Tests

`python3 fork-ci/actions/test-select/test-select.test.py` (real git repositories with GitHub's merge commit).
