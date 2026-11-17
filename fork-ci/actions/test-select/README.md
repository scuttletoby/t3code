# test-select

A composite action that answers one question about a pull request: **which test files can its change reach?** A
test file runs when a changed path is one of its inputs; the rest need not run on that pull request. Anything it
cannot work out runs every test (or, when it is one test's own unknown, always runs that test), and every run that is
not a pull request (the default branch, a merge queue, a schedule, a manual run) runs every test.

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
  "always_run": [{ "glob": "tests/test_inventory.py", "why": "reads file names from a data file" }],
  "import_roots": ["lib"]
}
```

- `tests`: globs from the repository root (`**/` any folders, `*` and `?` inside one name) or literal paths, in the
  order the list keeps. A literal path that is not in the repository fails the step (a renamed test must be renamed
  here too).
- `always_all`: a changed path that matches one runs every test, with its `why` in the log. List what tests read in
  ways the action cannot see: a runner, helpers every test loads, fixture folders, data files whose names come from
  data.
- `always_run`: test files (globs over `tests`' files) that run on every pull request, with their `why` in the log:
  a test the [trace check](#trace-check) shows reading files the action cannot place. A glob that matches no test
  file fails the step (a renamed test must be renamed here too).
- `import_roots`: folders searched for imports after the importing file's folder and the `sys.path` entries it can
  work out (code that loads from an install path that mirrors a repository folder).

A change to the config itself, or to a workflow file (`.github/workflows/`), always runs every test.

## What counts as an input

Worked out from the checkout on every run (never cached, so it cannot go stale), starting at each test file:

- the test file itself;
- **Python**: every import that is a file of the repository, searched in the order Python searches: the importing
  file's folder, the `sys.path` entries it can work out (`sys.path.insert`/`append`, under any alias; one list for
  the whole process), `import_roots`; a module more than one of them holds counts in each. Relative imports and
  packages (`__init__.py`, also `from . import x`), `importlib.import_module`, `__import__`, `python -m`/
  `runpy.run_module` (in a list or a command string) with a name it can work out; each followed to its own inputs;
- every path a Python file names that the action can work out: string literals, `__file__`, `os.path.join`,
  `dirname`, `abspath`, `pathlib` `/`, `.parent`, `.parents[n]`, `.joinpath`, f-strings, `+`, `'/'.join`,
  `.format`, `%`, `a or b` and `a if c else b` (each side), a loop variable over a literal list (each element), a
  function's return values, through names by Python's scope rule (every assignment counts; `global`; `with ... as`;
  `self.x`), and a name imported from a repository module (`from tools import ROOT`, `tools.ROOT`): its value there.
  Then:
  - a path that names a file is an input (a script it runs is followed the same way);
  - a folder counts with everything under it only when the file lists it (`os.walk`, `os.listdir`, `glob`, `.glob`,
    `.rglob`, `.iterdir`, `shutil.copytree`); a folder only named is a base for other paths;
  - a glob with a known folder (`docs/*.md`) takes in the changed paths it matches; one that starts with a wildcard
    names no place;
  - a known folder with an unknown rest (`ROOT / "plugins" / name`) takes in that folder; for a folder the file is
    inside (its own folder or the root), see the next section;
  - text with a space, quote, `(` or `$` is a message or a shell line, not a path; a temporary folder the test makes
    (`tempfile`) and paths under it are not the repository;
- **shell and JavaScript files** it reaches (`.sh`, `.bash`, `.mjs`, `.cjs`, `.js`, or such a `#!` line): every word
  that names a file of the repository, relative to the file or the root, followed the same way; `$HERE/x`,
  `${DIR}/x` and `$(dirname "$0")/x` resolve when the variable is set from the script's own folder (`dirname "$0"`,
  `BASH_SOURCE`, `readlink -f`, `cd ... && pwd`); a folder word takes in the folder unless the file is inside it;
- **symlinks**: a tracked link (to a file or a folder) counts as its target too, both ways: a change to the target
  picks every test that reaches the link, and a change to the link picks the tests that reach the target.

A deleted path counts as present, so a test that still names a deleted or renamed file runs (and fails there).

## A test always runs on a pull request when

The step log names the reason (`run X: always (file:line: why)`):

- its own file lists the repository (`git ls-files`), lists a folder it cannot work out, builds a path under its own
  folder or the root that it cannot work out (`HERE / name`, `f"{ROOT}/{x}"`), or (shell) uses a path from a
  variable not set from its own folder;
- it reaches, in any file, a module run by a name it cannot work out (`python -m`, `runpy`), a listing of the
  repository root, a value with more than 64 forms that names a repository path, or a value deeper than 40 steps;
- `always_run` names it.

## Every test runs when

- the event is not `pull_request`;
- the change is unknown or empty (HEAD must be GitHub's merge commit: [docs-scope's rule](../docs-scope/README.md));
- the config, a workflow file or an `always_all` path changed;
- a Python file among a test's inputs does not parse;
- an `importlib.import_module` or `__import__` name cannot be worked out;
- an import names a module that is in the repository but not where the import looks (a `sys.path` entry it could
  not work out); an import of a name no repository file has is taken as the standard library or third-party;
- anything else goes wrong in the action.

## What it guarantees, and what a miss costs

Guaranteed: a test runs on a pull request when the change touches a file its code names in a way listed above, or
when the test itself does something listed under "always runs". Not guaranteed: a path that only exists at run time
in a module the test reaches (a tool called with a folder its caller passes, a name read from data, `open(arg)`),
and a file read by a process that is not started by the test. Those are what the [trace check](#trace-check)
measures.

What a miss costs depends on the caller. With a merge queue that runs `merge_group` (every test, the event is not
`pull_request`), a miss is caught before it merges. **Without one, a missed test does not run before the merge: the
break lands on the default branch, and the default branch's next full run shows it.** Run the trace check on that
branch so a miss is found and closed (a general rule here, or `always_run`) before it costs a merge.

## Trace check

`trace/action.yml` (and `trace_check.py`) runs every test once under `strace -f` and puts each tracked file it
opened, ran or checked to this action as a one-file pull request. A file whose change would not pick the test is a
miss, and the step fails, naming the test and the file. Run it on a schedule or after pushes to the default branch,
never as a pull request's gate:

```yaml
trace-check:
  runs-on: ubuntu-latest # needs strace (sudo apt-get install -y strace) and ptrace of its own children
  steps:
    - uses: actions/checkout@<sha>
    - uses: scuttletoby/t3code/fork-ci/actions/test-select/trace@<release sha> # ci/vN
      with:
        run: python3 -I -B {test} # how one test runs; {test} is its path, shell-quoted
        jobs: "2"
```

It fails, never passes unchecked, when strace is missing or a test's trace does not show its own file read. A test
that fails under the trace is still judged by what it touched. Not seen: folder listings (the files read after them
are), files read by a process the test did not start, and files git reads.

## Tests

`python3 -B fork-ci/actions/test-select/test-select.test.py` (real git repositories with GitHub's merge commit) and
`python3 -B fork-ci/actions/test-select/trace-check.test.py` (its end-to-end tests skip without strace).
