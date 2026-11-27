# facts: one state table per place, looked up instead of re-derived

After a compaction, a handoff or a new session, an agent does not know what was already made and why things are the
way they are. It spends usage finding out again, and sometimes gets it wrong: it calls a part broken that was set up
and switched off on purpose the day before. The answer is short and usually known. A **state table** holds it, one row
per thing, and the agent reads it before it starts.

Part of the agent-guards kit: the `facts` command, the `facts-hook` hook, and the `"facts"` part of the machine file's
`"agent-guards"` section. Python 3 standard library only; read only, it never writes a table. After the tables,
`facts <words>` also searches a record repo (a private checkout of notes), when one is configured.

## The table

One Markdown file per place (a machine, a project, the fork's own features), named `STATE.md` by default. Its table's
header starts with `Thing`; one row per thing:

```markdown
# What we have and how it is now (one row per thing; edit the row when it changes)

| Thing       | What we made / how to use it                     | State now                                  | Why        | Updated |
| ----------- | ------------------------------------------------ | ------------------------------------------ | ---------- | ------- |
| Example GPU | `gpu on\|off\|status` helper; turned off at boot | OFF on purpose; the vendor tool fails then | Quiet fans | 11-23   |
| Example job | `job-ctl start\|stop`; runs nightly at 02:00     | Paused until the disk is replaced          | Disk alarm | 11-20   |
```

- **Thing**: the name an agent would search for. Put the words it would use (the part, the service, the feature).
- **What we made / how to use it**: the helper, unit, script or setting, and how to run it.
- **State now**: the current state, and what is normal in it (the known wrong conclusion: "fails while off: not
  broken").
- **Why**: the reason, in a few words.
- **Updated**: `MM-DD` (the latest such date not more than a day ahead: this year, last year, or for `02-29` the last
  leap year) or `YYYY-MM-DD`, each with any text after it (`11-23 (checked)`).

Current state only: no history (that is in git and the issues) and no long documents. Only the first column (`Thing`)
and the `Updated` column (found by its name) mean something to the code; the other columns are free text, and a table
may have more or fewer of them. A `|` inside a cell is written `\|`. The line under the header may be short (`|-|-|`)
or aligned (`|:-:|`). Other text and other tables in the file are skipped; a file may hold more than one state table,
and a second header row ends one table and starts the next. Only a regular file is read (a FIFO or a device is a note,
never opened to wait), up to 4 MB (the note says where it was cut); bytes that are not UTF-8 are replaced.

**Upkeep rule:** a change that alters a thing's state edits its row in the same change (the same commit, the same
turn). A new thing gets a row when it is made; a thing that is gone loses its row.

## Where the tables are

The machine file's `"agent-guards"` section, part `"facts"` ([README.md](README.md), Settings). Every key is optional;
with none, the hook and the command show only the kit's own table (below).

| Key          | Default | What it is                                                                                       |
| ------------ | ------- | ------------------------------------------------------------------------------------------------ |
| `dirs`       | `[]`    | Tables for every session on the machine: a table file, or a folder whose `STATE.md` is the table |
| `projects`   | `[]`    | `{"match", "dirs", "text", "name"}`: more tables, and a line of text, for one project's sessions |
| `staleDays`  | `30`    | The gardener's check reports a row whose `Updated` is older                                      |
| `maxRows`    | `60`    | The gardener's check reports a table with more rows                                              |
| `indexLines` | `40`    | Things the session-start hook shows at most                                                      |
| `indexChars` | `4000`  | Characters of the whole session-start context at most (project text and record line included)    |
| `records`    | `[]`    | `{"path", "projectsDir", "shared"}`: record repos that `facts <words>` searches after the tables |

A project's `match` is its remote as `host/owner/repo` (compared with the checkout's `origin` remote, any spelling:
https, ssh, `.git`), or a folder (the session's folder is that one or below it). Never a glob over a shared worktrees
folder: a linked worktree of the project matches by its remote, a worktree of another project does not. A folder that
is not a checkout, a git that does not answer within 2 s, or a worktree removed while the hook runs: no project
matches, the machine's tables are still shown. The same place named twice (a link to it, a `~/` spelling, a trailing
`/`) is read once.

```json
"agent-guards": {
  "facts": {
    "dirs": ["/etc/fork-kit/facts"],
    "projects": [
      { "match": "github.com/example/project", "dirs": ["/srv/example/project-facts"], "text": "" },
      { "match": "/srv/example/notes-project", "dirs": ["/srv/example/notes-project/facts"], "name": "notes" }
    ],
    "records": [
      { "path": "/srv/example/records", "projectsDir": "projects", "shared": ["machines", "orch", "shared"] }
    ]
  }
}
```

Facts live where the thing lives: a machine's table beside the machine file, a project's table in the project (a
private project keeps it private), the fork's feature table in the fork's own project. The kit ships only the tool,
the format and the hook.

`FACTS_DIRS` (paths joined by `:`) replaces both lists for one command or session: for a test, or a look at another
place's table. It does not change the record repos.

## The kit's commands

The kit ships one table of its own, [COMMANDS.md](COMMANDS.md): its ready-made commands (`fork-ci-why`, `fork-clone`,
`fork-precheck`; [README.md](README.md), Ready-made commands). It is read on every machine with the kit, after the
configured tables (so their things come first in the session-start context), with the source `kit` in `facts --dirs`.
It is not read when `FACTS_DIRS` is set (that replaces every place) or when the file is not there (an older copy of
the kit), and `facts --check` skips it: it changes only with the kit, and its tests keep it.

## The record repo

A record repo is a checkout of notes laid out by place: `projects/<name>/` for each project, and shared folders (by
default `machines/`, `orch/` and `shared/`). `facts <words>` searches it after the tables: the lines of its text files
(`.md`, `.txt`, `.json`, `.yml`, `.yaml`, `.sh`) that hold every word, as `path:line: text` (at most 160 characters),
at most 10, then `+N more (facts --records <words>)`. It searches the shared folders and the folder of this session's
project only (`projectsDir/<name>`, where `name` is the project's `"name"`, else the last part of its remote or
folder); with `--all`, every project's folder.

Skipped: `.git`, links, files over 1 MiB, files that are not regular, and a file with the same bytes as a table already
read (a mirrored copy of a `STATE.md` is not shown twice). The search is a Python file walk that stops after 10 s and
says so. A record repo that is not there or cannot be read is one `note:` line in the command; the hook says nothing
of it.

## The command

```sh
facts gpu                 # the rows that hold every word (any case), with their table's header; Thing hits first
facts example job paused  # no row holds all of them: the rows with some, and it says so
facts --index             # every thing, one per line, under its table file
facts --dirs              # where it looked: each entry, where it was configured, what it found
facts --records fan curve # the record repo only, at most 50 lines
facts --no-records gpu    # the tables only
facts --check             # the gardener's check of this folder's tables (report only), exit 1 when it finds something
facts --all job           # also every other project's tables and record folders
facts --check --all       # the check of every project's tables
```

**Never post `--all` output (or `--check --all`) to a public place** (an issue, a PR, a chat): it names the things of
every configured project, private ones too. Without `--all` the command reads the machine's tables and this folder's
project's only.

A missing file or folder, a folder with no `STATE.md`, a file with no state table, or a row whose cell count differs
from the header's is a `note:` line: the rest is still shown, and a broken row is still listed and searched.

## The hook

`facts-hook`, wired like the kit's other hooks (`agent-guards hooks`, or the installer's managed settings drop-in):

- **SessionStart** (startup, resume, clear, and after compaction): adds context: the `Thing` column of every table
  configured for the session (at most `indexLines` things) and "Check the state table before diagnosing or redoing
  something: `facts <words>` prints the matching rows". Then the project's `text`, and one line naming each record
  repo that is there (`Records: <path> (facts <words> searches it)`; never its content). All of it in at most
  `indexChars` characters: when the things are cut, the last one is followed by `+N more: run facts --index`.
- **PreCompact**: adds to the compaction instructions: keep only the names of the things looked up or changed, not
  their rows (they are read again after compaction), and mark a thing whose row was not edited yet.

No table with a row for the session and no record repo, or any other event: nothing. A broken `"facts"` part: the
defaults (nothing shown) and a note, as for every hook of the kit. It never fails a session: every error is caught,
git is asked at most once with a 2 s timeout, and the record repo is only checked to be there.

## The gardener's check

`facts --check` reads the tables of this folder (the machine's and this folder's project's; every project's with
`--all`) and reports, changing nothing. It never reads the record repo.

- a row whose `Updated` is older than `staleDays`, is more than a day in the future, or has no date it can read;
- a table with no `Updated` column, or with more than `maxRows` rows;
- a row whose cell count differs from the header's, or a header with no `|---|` line;
- an entry that is not there, a folder with no `STATE.md`, a file with no state table.

A daily gardener (a timer, or a project's own garden job) runs it and passes on what it prints; that wiring is a
separate change.

## Tests

[tests/facts.test.py](tests/facts.test.py): the table reader (escaped pipes, a broken row, a header with no `|---|`
line, short separators, a second header, two tables in one file, a FIFO, bytes that are not UTF-8, a cut file),
search, the index and the character cap, a missing and an empty folder, `Updated` dates (across the year's end,
`02-29`, a suffix, the future), project matching by remote and folder (a git that hangs or fails, asked once), the
record repo search, `--check` with and without `--all`, and the hook's answers. Dates are fixed in the tests
(`FACTS_TODAY`), so they do not depend on the day they run.
