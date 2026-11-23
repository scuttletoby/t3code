"""agent_guards.facts: state tables, one per place (a machine, a project, the fork), looked up instead of re-derived.

A state table is a Markdown table whose header's first cell is "Thing", one row per thing:

  | Thing | What we made / how to use it | State now | Why | Updated |
  |---|---|---|---|---|
  | Example GPU | `gpu on\\|off\\|status` helper; turned off at boot | OFF on purpose; the vendor tool fails while off | quiet fans | 11-23 |

Only the "Thing" column (first) and an "Updated" column (by name, any place) mean something to the code; the rest is
free text. A file may hold more than one such table; other text and tables in it are skipped. Current state only:
history stays in git and the issues. A configured entry is a table file, or a folder whose STATE.md is the table.

Where the tables are comes from the machine file's "agent-guards" section, part "facts" (config.py): "dirs" for
every session on the machine, and "projects" ([{"match", "dirs", "text"}]) for the sessions working in one project,
matched by the checkout's origin remote (host/owner/repo) or by a folder (that folder or below it), never by a glob.
FACTS_DIRS (paths joined by ":") replaces both lists, for a test or a one-off look. Read only: nothing here writes.
Plain Python 3 standard library.
"""
import datetime, os, re, subprocess

TABLE_FILE = 'STATE.md'
CALL_TO_LOOK = 'check the state table before diagnosing or redoing something: `facts <words>` prints the matching rows'
_SEP = re.compile(r'^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')


class Place:
    """One configured entry: `entry` (as written), `source` (where it was configured), `file` (the table file or
    None), `tables` [Table], `notes` [text]: a missing file or folder, an empty folder, a file with no state table."""

    def __init__(self, entry, source):
        self.entry, self.source, self.file, self.tables, self.notes = entry, source, None, [], []

    def rows(self):
        return [r for t in self.tables for r in t.rows]


class Table:
    def __init__(self, file, header, sep, columns):
        self.file, self.header, self.sep, self.columns, self.rows, self.warnings = file, header, sep, columns, [], []

    def column(self, name):
        names = [c.strip().lower() for c in self.columns]
        return names.index(name.lower()) if name.lower() in names else None


class Row:
    def __init__(self, table, lineno, line, cells):
        self.table, self.lineno, self.line, self.cells = table, lineno, line, cells

    @property
    def thing(self):
        return unescape(self.cells[0]) if self.cells else ''

    def cell(self, name):
        k = self.table.column(name)
        return unescape(self.cells[k]) if k is not None and k < len(self.cells) else None

    def text(self):
        return ' '.join(unescape(c) for c in self.cells)


def cells(line):
    """The cells of one table line: split on | that is not escaped (\\|), the outer pipes dropped, each stripped."""
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|') and not s.endswith('\\|'):
        s = s[:-1]
    out, cur, k = [], [], 0
    while k < len(s):
        if s[k] == '\\' and k + 1 < len(s):
            cur.append(s[k:k + 2])
            k += 2
            continue
        if s[k] == '|':
            out.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(s[k])
        k += 1
    out.append(''.join(cur).strip())
    return out


def unescape(text):
    return text.replace('\\|', '|')


def parse(text, file='<text>'):
    """[Table] of every state table in `text`. A row whose cell count differs from the header's is kept (listed
    and searched as it is) and named in its table's warnings."""
    lines = text.split('\n')
    tables, k = [], 0
    while k < len(lines):
        line = lines[k]
        if line.lstrip().startswith('|') and cells(line)[0].strip().lower() == 'thing':
            sep = lines[k + 1] if k + 1 < len(lines) and _SEP.match(lines[k + 1].strip()) else None
            t = Table(file, line.rstrip(), sep.rstrip() if sep else None, cells(line))
            if sep is None:
                t.warnings.append(f'{file}:{k + 1}: the header has no |---| line under it')
            k += 2 if sep is not None else 1
            while k < len(lines) and lines[k].lstrip().startswith('|'):
                c = cells(lines[k])
                if any(c):
                    t.rows.append(Row(t, k + 1, lines[k].rstrip(), c))
                    if len(c) != len(t.columns):
                        t.warnings.append(f'{file}:{k + 1}: row "{unescape(c[0])[:60]}" has {len(c)} cells, the header '
                                          f'has {len(t.columns)}')
                k += 1
            tables.append(t)
            continue
        k += 1
    return tables


def read_place(entry, source):
    """A Place for one configured entry (a table file, or a folder holding STATE.md). Never raises."""
    p = Place(entry, source)
    path = os.path.expanduser(entry) if entry.startswith('~/') else entry
    if not os.path.isabs(path):
        p.notes.append(f'{entry}: not an absolute path (or ~/...): skipped')
        return p
    if os.path.isdir(path):
        path = os.path.join(path, TABLE_FILE)
        if not os.path.exists(path):
            p.notes.append(f'{entry}: no {TABLE_FILE} in this folder yet')
            return p
    elif not os.path.exists(path):
        p.notes.append(f'{entry}: not there')
        return p
    p.file = path
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read(4 * 1024 * 1024)
    except OSError as e:
        p.notes.append(f'{path}: cannot be read ({e.strerror or e})')
        return p
    p.tables = parse(text, path)
    if not p.tables:
        p.notes.append(f'{path}: no state table in it (a header row starting "| Thing |")')
    return p


def remote_key(url):
    """'github.com/owner/repo' for any spelling of a remote URL (https, ssh, scp-like, .git, a trailing /)."""
    u = (url or '').strip().lower()
    u = re.sub(r'^[a-z+]+://', '', u)
    u = re.sub(r'^[^@/]+@', '', u)
    if ':' in u.split('/', 1)[0]:
        host, rest = u.split(':', 1)
        u = host + '/' + rest.lstrip('/') if not rest.split('/', 1)[0].isdigit() else host + '/' + rest.split('/', 1)[-1]
    u = u.rstrip('/')
    return u[:-4] if u.endswith('.git') else u


def origin(cwd, timeout=2):
    """The origin remote of the checkout at `cwd` (a linked worktree too), or ''. A folder that is gone, not a
    checkout, or a git that hangs: ''."""
    try:
        r = subprocess.run(['git', '-C', cwd, 'config', '--get', 'remote.origin.url'], capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError, ValueError):
        return ''


def _below(cwd, folder):
    try:
        c, f = os.path.realpath(cwd), os.path.realpath(os.path.expanduser(folder))
    except (OSError, ValueError):
        return False
    return c == f or c.startswith(f.rstrip('/') + '/')


def projects_for(cfg, cwd):
    """The configured projects that `cwd` belongs to: a folder match (that folder or below), or the checkout's
    origin remote. git is asked only when a project matches by remote."""
    out, remote = [], None
    for p in cfg.get('projects') or []:
        m = p['match']
        if m.startswith('/') or m.startswith('~/'):
            if cwd and _below(cwd, m):
                out.append(p)
            continue
        if remote is None:
            remote = remote_key(origin(cwd)) if cwd and os.path.isdir(cwd) else ''
        if remote and remote == remote_key(m):
            out.append(p)
    return out


def entries(cfg, cwd, env=None, every=False):
    """[(entry, source)] to read: FACTS_DIRS when set; else the machine's dirs and those of the projects `cwd`
    belongs to (every project's with every=True), each entry once."""
    env = os.environ if env is None else env
    if env.get('FACTS_DIRS'):
        return [(e, 'FACTS_DIRS') for e in dict.fromkeys(x for x in env['FACTS_DIRS'].split(':') if x)]
    out = [(e, 'machine') for e in cfg.get('dirs') or []]
    for p in (cfg.get('projects') or []) if every else projects_for(cfg, cwd):
        out += [(e, f'project {p["match"]}') for e in p.get('dirs') or []]
    seen, uniq = set(), []
    for e, s in out:
        if e not in seen:
            seen.add(e)
            uniq.append((e, s))
    return uniq


def places(cfg, cwd, env=None, every=False):
    return [read_place(e, s) for e, s in entries(cfg, cwd, env, every)]


def _words(words):
    return [w.lower() for w in words if w.strip()]


def search(found, words):
    """Output lines: for each table with a match, its file, header, |---| line and the matching rows as written.
    A row matches when it holds every word (any case); rows with the words in the Thing column come first. When no
    row holds them all, rows holding any of them are shown, and the first line says so."""
    ws = _words(words)
    rows = [r for p in found for r in p.rows()]

    def hits(r, part):
        return sum(w in part for w in ws)
    every = [r for r in rows if hits(r, r.text().lower()) == len(ws)]
    lines = []
    picked = every
    if not every:
        picked = [r for r in rows if hits(r, r.text().lower())]
        if picked:
            lines.append(f'No row holds all of: {" ".join(words)}. Rows with some of them:')
    picked.sort(key=lambda r: (-hits(r, r.thing.lower()), -hits(r, r.text().lower())))
    for t in dict.fromkeys(r.table for r in picked):
        lines.append(f'{t.file}:')
        lines.append(t.header)
        if t.sep:
            lines.append(t.sep)
        lines += [r.line for r in picked if r.table is t]
    if not picked:
        lines.append(f'No row matches: {" ".join(words)} (in {sum(1 for p in found if p.file)} table file(s); '
                     '`facts --index` lists every thing, `facts --dirs` where it looked).')
    return lines


def index(found, limit=None):
    """Output lines: each table file, then its things, one per line (at most `limit` things in all)."""
    lines, shown, total = [], 0, sum(len(p.rows()) for p in found)
    for p in found:
        if not p.rows():
            continue
        lines.append(f'{p.file} ({len(p.rows())} rows):')
        for r in p.rows():
            if limit is not None and shown >= limit:
                break
            lines.append(f'  - {r.thing[:100]}')
            shown += 1
    if limit is not None and total > shown:
        lines.append(f'  ... and {total - shown} more (`facts --index`)')
    return lines


def notes(found):
    out = [n for p in found for n in p.notes]
    out += [w for p in found for t in p.tables for w in t.warnings]
    return out


def updated(text, today):
    """The date in an Updated cell (YYYY-MM-DD, or MM-DD: this year, or last year when that would be in the
    future), or None."""
    m = re.match(r'^\s*(\d{4})-(\d{1,2})-(\d{1,2})', text or '')
    try:
        if m:
            return datetime.date(int(m[1]), int(m[2]), int(m[3]))
        m = re.match(r'^\s*(\d{1,2})-(\d{1,2})\s*$', text or '')
        if m:
            d = datetime.date(today.year, int(m[1]), int(m[2]))
            return d if d <= today + datetime.timedelta(days=1) else datetime.date(today.year - 1, int(m[1]), int(m[2]))
    except ValueError:
        return None
    return None


def check(found, stale_days, max_rows, today):
    """The gardener's findings (report only; nothing is changed): rows whose Updated is older than stale_days or
    cannot be read, tables without an Updated column or over max_rows, broken rows, entries that are missing,
    empty or hold no table."""
    out = list(notes(found))
    for p in found:
        for t in p.tables:
            if len(t.rows) > max_rows:
                out.append(f'{t.file}: {len(t.rows)} rows, over the cap of {max_rows} (maxRows): fold or drop the '
                           'rows of things that are gone')
            if t.column('updated') is None:
                out.append(f'{t.file}: the table has no "Updated" column')
                continue
            for r in t.rows:
                cell = r.cell('updated')
                d = updated(cell, today)
                if d is None:
                    out.append(f'{t.file}:{r.lineno}: "{r.thing[:60]}": no date in Updated ({(cell or "")[:20]!r})')
                elif (today - d).days > stale_days:
                    out.append(f'{t.file}:{r.lineno}: "{r.thing[:60]}": Updated {cell.strip()} is '
                               f'{(today - d).days} days old (staleDays {stale_days}): check the state, then edit '
                               'the row')
    return out


def session_text(found, cfg, cwd, limit):
    """The session-start context: the things of every table (capped) and the call to look first; '' when no table
    with a row is configured for this session."""
    if not any(p.rows() for p in found):
        return ''
    head = (f'State tables (one row per thing: what we made, how to use it, its state now and why). '
            f'{CALL_TO_LOOK[0].upper()}{CALL_TO_LOOK[1:]}. Things:')
    extra = [p['text'] for p in projects_for(cfg, cwd) if p.get('text')]
    return '\n'.join([head] + index(found, limit) + extra)


def compact_text(found, cfg, cwd):
    """The compaction instructions' part: keep the things' names, not their rows."""
    if not any(p.rows() for p in found):
        return ''
    extra = [p['text'] for p in projects_for(cfg, cwd) if p.get('text')]
    return '\n'.join(['State tables: in the summary keep only the names of the things you looked up or changed in '
                      'them, not their rows; `facts <words>` reads the rows again after compaction. A thing whose '
                      'state changed in this conversation and whose row was not edited yet: say "row NOT YET '
                      'UPDATED" with the new state.'] + extra)
