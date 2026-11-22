# doc-check

Rules for the Markdown files coding agents read. Instruction files (`AGENTS.md`, `CLAUDE.md`, `SKILL.md`, and any a
project adds) are loaded on every call, so they cost tokens all the time and grow without anyone noticing. This
action fails a pull request that:

- puts an instruction file over its **size budget** (lines or bytes; above `warn_at` of a budget it only warns, and
  says when this change moved the file past it);
- writes **history** into an instruction file (a changelog block, an "is live on" claim, a dated "Updated" banner);
- has a **broken relative link** in a Markdown file it adds or changes (code fences skipped; `/x` is from the
  repository root, as GitHub reads it; a link that leaves the repository is broken);
- **adds** a file whose name looks like a dated report, handoff or review (those go in the pull request or an issue).

The general rules this enforces, and the rest of the agent rules, are in
[docs/fork/agent-rules.md](../../../docs/fork/agent-rules.md).

## Use

Run it on `pull_request`, after a checkout with `fetch-depth: 0` (it needs the base and the merge base):

```yaml
steps:
  - uses: actions/checkout@<sha>
    with: { fetch-depth: 0, persist-credentials: false }
  - uses: scuttletoby/t3code/fork-ci/actions/doc-check@<release sha> # ci/vN
```

A daily or manual run over every file: `with: { mode: all }` (no base needed). Locally, from the repository root:

```sh
python3 -I fork-ci/actions/doc-check/doc_check.py --base origin/main   # the files this branch changes
python3 -I fork-ci/actions/doc-check/doc_check.py --all                # every tracked file
python3 -I fork-ci/actions/doc-check/doc_check.py --files AGENTS.md    # these files
```

| Input    | Default                             | What it is                                                                       |
| -------- | ----------------------------------- | -------------------------------------------------------------------------------- |
| `config` | `.github/doc-check.json`            | the project's config, read at the base commit; optional file                     |
| `base`   | the event's `pull_request.base.sha` | the files changed since `merge-base(base, HEAD)` are checked                     |
| `mode`   | `changed`                           | `all`: every tracked file the config selects (the config read from the checkout) |
| `path`   | `.`                                 | where the repository is checked out                                              |

Exit 0: no problem (warnings allowed). Exit 1: a rule is broken; each problem is a `DOC` line and an error
annotation. Exit 2: the config, the base or git could not be read; that is never a pass. Read-only and stateless:
a re-run gives the same output.

## Config

Every key is optional. A missing file, an empty file and `{}` all mean the defaults below. A key that is present
replaces its default whole (a project that lists `budgets` lists every budget it wants). Keys starting with `_` are
comments. An unknown key, a value of the wrong type or a pattern that is not a regular expression is an error.

```json
{
  "files": ["**/*.md"],
  "exclude": [],
  "budgets": [
    { "glob": "AGENTS.md", "lines": 150, "bytes": 12000 },
    { "glob": "**/AGENTS.md", "lines": 100, "bytes": 8000 },
    { "glob": "**/CLAUDE.md", "lines": 40, "bytes": 3000 },
    { "glob": "**/SKILL.md", "lines": 250, "bytes": 8000 }
  ],
  "warn_at": 0.9,
  "history": [{ "pattern": "<regular expression>", "flags": "im", "why": "what it is" }],
  "report_name": "<regular expression on the path>",
  "report_name_flags": "i",
  "links": true
}
```

- `files`, `exclude`: globs from the repository root (`**/` any folders, `*` within one name) of the files checked.
- `budgets`: the first matching glob gives a file its budget; a file with a budget is an instruction file (the
  history rules apply to it). A project file such as `ENGINEERING.md` gets a line here.
- `history`: the default three patterns are in [doc_check.py](doc_check.py) (`DEFAULTS`); `[]` turns them off.
- `report_name`: `null` turns the dated-report rule off.
- `links`: `false` turns link checks off.

The config is read at the base commit, never from the checkout, so a pull request cannot loosen the rules that judge
it: a change to the config applies from the next pull request. `mode: all` and `--files` read it from the checkout.

## Not covered

- A deleted or renamed file breaks the links that point to it from files the change did not touch; `mode: all`
  finds those.
- Anchors (`#section`) are not checked, and neither are backticked paths in the text.
- A symlinked Markdown file is listed as a warning and not checked.
