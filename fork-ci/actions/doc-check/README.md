# doc-check

Rules for the Markdown files coding agents read. Instruction files (`AGENTS.md`, `CLAUDE.md`, `SKILL.md`, and any a
project adds) are loaded on every call, so they cost tokens all the time and grow without anyone noticing. This
action fails a pull request that:

- puts an instruction file over its **size budget** (lines or bytes; above `warn_at` of a budget it only warns, and
  says when this change moved the file past it);
- writes **history** into an instruction file (a changelog block, an "is live on" claim, a dated "Updated" banner);
- has a **broken relative link** in a Markdown file it adds or changes (code blocks, fenced or indented, read as
  CommonMark does, and inline code skipped; `/x` is from the repository root, as GitHub reads it; a link that leaves
  the repository is broken), or deletes or renames a file, or removes or retargets a symlink, so that a link in
  another Markdown file no longer resolves (through folder symlinks too);
- has a checked file that is **not UTF-8** or cannot be read, or is a **symlink** out of the repository, into
  `.git` or to no file (a symlink is checked as the file it points to and reported once when its target is checked
  too; sizes are the bytes on disk, CRLF included), or deletes, renames or moves away the file a symlink points to
  (or a folder or symlink on its way). An untouched symlink is checked again, under its own budget, when the change
  touches the file it reads, so `--base` and `--all` agree on it;
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
it through the config: a change to the config applies from the next pull request. `mode: all` and `--files` read it
from the checkout. The workflow that calls this action is not covered by that: GitHub runs a pull request's own
workflow file, so a change that edits the step or removes it is stopped only by review of `.github/workflows/`.

## Not covered

- Anchors (`#section`) and queries (`?plain=1`) are not checked, and neither are backticked paths in the text.
- A broken link or a symlink to no file (a loop too) that was already on the base in a file the change does not
  touch is not this change's: `mode: all` finds those.
