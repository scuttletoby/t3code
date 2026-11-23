# facts: one state table per place, looked up instead of re-derived

After a compaction, a handoff or a new session, an agent does not know what was already made and why things are the
way they are. It spends usage finding out again, and sometimes gets it wrong: it calls a part broken that was set up
and switched off on purpose the day before. The answer is short and usually known. A **state table** holds it, one row
per thing, and the agent reads it before it starts.

Part of the agent-guards kit: the `facts` command, the `facts-hook` hook, and the `"facts"` part of the machine file's
`"agent-guards"` section. Python 3 standard library only; read only, it never writes a table.

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
- **Updated**: `MM-DD` (this year, or last year when that would be in the future) or `YYYY-MM-DD`.

Current state only: no history (that is in git and the issues) and no long documents. Only the first column (`Thing`)
and the `Updated` column (found by its name) mean something to the code; the other columns are free text, and a table
may have more or fewer of them. A `|` inside a cell is written `\|`. Other text and other tables in the file are
skipped; a file may hold more than one state table.

**Upkeep rule:** a change that alters a thing's state edits its row in the same change (the same commit, the same
turn). A new thing gets a row when it is made; a thing that is gone loses its row.

## Where the tables are

The machine file's `"agent-guards"` section, part `"facts"` ([README.md](README.md), Settings). Every key is optional;
with none, the hook and the command do nothing.

| Key          | Default | What it is                                                                                       |
| ------------ | ------- | ------------------------------------------------------------------------------------------------ |
| `dirs`       | `[]`    | Tables for every session on the machine: a table file, or a folder whose `STATE.md` is the table |
| `projects`   | `[]`    | `{"match", "dirs", "text"}`: more tables, and a line of text, for the sessions of one project    |
| `staleDays`  | `30`    | The gardener's check reports a row whose `Updated` is older                                      |
| `maxRows`    | `60`    | The gardener's check reports a table with more rows                                              |
| `indexLines` | `40`    | Things the session-start hook shows at most                                                      |

A project's `match` is its remote as `host/owner/repo` (compared with the checkout's `origin` remote, any spelling:
https, ssh, `.git`), or a folder (the session's folder is that one or below it). Never a glob over a shared worktrees
folder: a linked worktree of the project matches by its remote, a worktree of another project does not. A folder that
is not a checkout, a git that does not answer within 2 s, or a worktree removed while the hook runs: no project
matches, the machine's tables are still shown.

```json
"agent-guards": {
  "facts": {
    "dirs": ["/etc/fork-kit/facts"],
    "projects": [
      { "match": "github.com/example/project", "dirs": ["/srv/example/project-facts"], "text": "" },
      { "match": "/srv/example/notes-project", "dirs": ["/srv/example/notes-project/facts"] }
    ]
  }
}
```

Facts live where the thing lives: a machine's table beside the machine file, a project's table in the project (a
private project keeps it private), the fork's feature table in the fork's own project. The kit ships only the tool,
the format and the hook.

`FACTS_DIRS` (paths joined by `:`) replaces both lists for one command or session: for a test, or a look at another
place's table.

## The command

```sh
facts gpu                 # the rows that hold every word (any case), with their table's header; Thing hits first
facts example job paused  # no row holds all of them: the rows with some, and it says so
facts --index             # every thing, one per line, under its table file
facts --dirs              # where it looked: each entry, where it was configured, what it found
facts --check             # the gardener's check (report only), exit 1 when it finds something
facts --all job           # also every other project's tables, not only the machine's and this folder's project's
```

A missing file or folder, a folder with no `STATE.md`, a file with no state table, or a row whose cell count differs
from the header's is a `note:` line: the rest is still shown, and a broken row is still listed and searched.

## The hook

`facts-hook`, wired like the kit's other hooks (`agent-guards hooks`, or the installer's managed settings drop-in):

- **SessionStart** (startup, resume, clear, and after compaction): adds context: the `Thing` column of every table
  configured for the session (at most `indexLines` things, then "... and N more") and "Check the state table before
  diagnosing or redoing something: `facts <words>` prints the matching rows". Then the project's `text`.
- **PreCompact**: adds to the compaction instructions: keep only the names of the things looked up or changed, not
  their rows (they are read again after compaction), and mark a thing whose row was not edited yet.

No table with a row for the session, or any other event: nothing. A broken `"facts"` part: the defaults (nothing
shown) and a note, as for every hook of the kit.

## The gardener's check

`facts --check` reads every configured table (every project's too) and reports, changing nothing:

- a row whose `Updated` is older than `staleDays`, or has no date it can read;
- a table with no `Updated` column, or with more than `maxRows` rows;
- a row whose cell count differs from the header's, or a header with no `|---|` line;
- an entry that is not there, a folder with no `STATE.md`, a file with no state table.

A daily gardener (a timer, or a project's own garden job) runs it and passes on what it prints.

## Tests

[tests/facts.test.py](tests/facts.test.py): the table reader (escaped pipes, a broken row, a header with no `|---|`
line, two tables in one file), search, the index, a missing and an empty folder, staleness by `Updated` (`MM-DD` across
the year's end), project matching by remote and folder, and the hook's answers.
