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
  "inputs": [{ "test": "tests/test_backup.py", "reads": [], "why": "lists only its temp folders" }],
  "follow_calls": [
    { "glob": "tools/build.py", "why": "most tool tests import it, each for a few functions" }
  ],
  "import_roots": ["lib"]
}
```

- `tests`: globs from the repository root (`**/` any folders, `*` and `?` inside one name) or literal paths, in the
  order the list keeps. A literal path that is not in the repository, or a glob that matches no file of it, fails the
  step (a renamed or moved test must be renamed here too; it would otherwise drop out of every run, green).
- `always_all`: a changed path that matches one runs every test, with its `why` in the log. List what tests read in
  ways the action cannot see: a runner, helpers every test loads, fixture folders, data files whose names come from
  data.
- `always_run`: test files (globs over `tests`' files) that run on every pull request, with their `why` in the log:
  a test the [trace check](#trace-check) shows reading files the action cannot place. A glob that matches no test
  file fails the step (a renamed test must be renamed here too).
- `inputs`: what a test reads that the action cannot see, declared: `test` (a glob over `tests`' files), `reads`
  (globs; may be empty) and `why`. The test's own "always runs" reasons from its own file then give way to `reads`:
  a changed path that matches one picks it, and the log says `(declared in inputs)`. A reason from any other file it
  reaches still runs it always. Use it for a test that lists folders the action cannot work out but that are not the
  repository (its temporary folders, passed to helpers as parameters). A wrong declaration is a miss the
  [trace check](#trace-check) reports. A `test` glob that matches no test file fails the step.
- `follow_calls`: large shared Python modules (globs over the repository's `.py` files) whose top-level functions
  count only for the tests that can reach them. Without it, a test that imports the module takes in every path the
  module names; with it, a path named inside a top-level function counts only when the function is reached:
  - module-level code, classes (their bodies run on import), decorated functions (a decorator may register them) and
    dunder functions (`__getattr__`) always count, and so does every import the module makes;
  - a function is reached when that code, a reached function, or any other file among the test's inputs says its
    name: in code, an import, an attribute (`build.make_x`), a keyword, or a word in a string or a script
    (`mock.patch('tools.build.make_x')`, `python3 -c '...'` in a shell file). Names, not a call graph: a name in
    common use picks more tests, never fewer;
  - a name it cannot work out counts the whole module: `getattr`/`setattr`/`hasattr`/`delattr` with a name that is
    not a written string, `globals()`, `locals()`, `vars()`, `dir()`, `eval`, `exec`, `inspect.getmembers`,
    `.__dict__` or `sys.modules`, anywhere in the module or in the test's other inputs;
  - a change to the module itself still picks every test that imports it.

  The [trace check](#trace-check) measures it as it measures the rest. A glob that matches no tracked `.py` file
  fails the step.

- `import_roots`: folders searched for imports after the importing file's folder and the `sys.path` entries it can
  work out (code that loads from an install path that mirrors a repository folder).

A change to the config itself, or to a workflow file (`.github/workflows/`), always runs every test.

### Checking the config against the full run

A project's own test that the config names exactly the files its full run runs can match more widely than the
selector does (Python's `fnmatch` lets `*` cross a `/`), and `git ls-files` lists an unmerged file once per stage
during a conflict. Use the selector's check instead, with its own matching and each tracked file once:

```sh
python3 -I fork-ci/actions/test-select/test_select.py --check-config --runs 'tests/test_*.py' --runs tools/check/test_install.py
```

`--runs` (repeat it) is what the full run runs: globs as above or paths. Exit 0 when the config's tests are exactly
those files; 1, naming each file, when the full run runs one the config misses (no pull request would pick it) or
the config names one the full run does not run. It also checks the rest of the config as a run would (an
`always_run`, `inputs` or `follow_calls` glob that names nothing).

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
  - a known folder with an unknown rest (`ROOT / "plugins" / name`, also `os.path.join(TOOLS, f"{name}.py")`) takes
    in that folder; for a folder the file is inside (its own folder or the root), see the next section;
  - an unknown start with a known rest (a parameter, `Path.cwd()`, `os.getcwd()`, an environment variable, `x +=
    "/name"`, a path built part by part in a loop, joined onto `"/data/b.json"`) takes in every repository path that
    ends in that rest (`**/data/b.json`; a listed one, everything under such folders); a rest without a `/` before
    it is not placed: see the next section;
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
  folder or the root that it cannot work out (`HERE / name`, `f"{ROOT}/{x}"`), lists a folder with an unknown start
  and a relative rest, or (shell) uses a path from a variable not set from its own folder;
- its own file starts a listing process (`find`, `ls`, `tree`, `du`, `grep -r`, `rg`, `fd`, in an argument list or a
  shell command string) on a folder it cannot work out or on the working directory (a temporary folder and a named
  repository folder are placed as above);
- it reaches, in any file, a module run by a name it cannot work out (`python -m`, `runpy`), a listing of the
  repository root, a value with more than 64 forms that names a repository path, or a value deeper than 40 steps;
- `always_run` names it.

`inputs` turns the first two kinds off for a test whose own file it declares (see Config).

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
  timeout-minutes: 120
  steps:
    - uses: actions/checkout@<sha>
    # Optional: skip when the last finished trace ran on this same commit. A read that fails runs the check and
    # says why (the exit status and the start of gh's message, on one line: never a workflow command).
    - id: last
      env:
        GH_TOKEN: ${{ github.token }}
      run: |
        if got=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/trace.yml/runs?status=success&per_page=1" \
                   --jq '.workflow_runs[0].head_sha // ""' 2>&1); then
          if [ "$got" = "$GITHUB_SHA" ]; then echo skip=true >> "$GITHUB_OUTPUT"; fi
        else
          rc=$?
          echo "trace skip: cannot read the last run (exit $rc: $(printf '%s' "$got" | tr -s '\r\n' '  ' | cut -c1-200)); running the check"
        fi
    - id: trace
      if: steps.last.outputs.skip != 'true'
      uses: scuttletoby/t3code/fork-ci/actions/test-select/trace@<release sha> # ci/vN
      with:
        run: python3 -I -B {test} # how one test runs; {test} is its path, shell-quoted
        jobs: "2"
    # Keep what finished when the run fails, times out or is cancelled.
    - if: always() && steps.trace.outputs.report != ''
      uses: actions/upload-artifact@<sha>
      with:
        name: trace-check
        path: ${{ steps.trace.outputs.report }}
        if-no-files-found: ignore
```

It fails, never passes unchecked, when strace is missing, `--only` matches no test, a test's trace does not show its
own file read, or a test fails, errs or skips tests under the trace: what a test did not run, it did not touch, so it
is reported `not checked` (and still judged by what it touched). Run it as the test job runs the tests: the same
runner, environment variables and working directory, or tests that skip there skip here and fail the check. A test
whose inputs run every test is reported and checked on. The last line gives the number of tests, misses and tests
not checked. Each test's lines print, and go to the `report` file (the action's output of that name), as soon as its
trace ends. A cancelled or timed-out job (SIGTERM, SIGINT) kills every traced test's process group at once, so the
step stops within seconds, and the run ends with `trace-check: cut off by ...` and fails: what finished stays in the
report. Not seen: folder listings (the files read after them
are), files read by a process the test did not start, and files git reads.

## What it saves

It depends on how the tests are built. A test runs when its import closure reaches the change, so a helper module
most tests import passes its inputs to all of them. On its first caller (116 test files, most importing one large
tool module), 48 tests always ran on every pull request before `inputs`; see that caller's pull request for the
measured wall time per pull request.

## Tests

`python3 -B fork-ci/actions/test-select/test-select.test.py` (real git repositories with GitHub's merge commit) and
`python3 -B fork-ci/actions/test-select/trace-check.test.py` (its end-to-end tests skip without strace).
