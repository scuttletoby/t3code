"""agent_guards.facts: state tables, one per place (a machine, a project, the fork), looked up instead of re-derived;
and a search of the user's private record repo, after the tables.

A state table is a Markdown table whose header's first cell is "Thing", one row per thing:

  | Thing | What we made / how to use it | State now | Why | Updated |
  |---|---|---|---|---|
  | Example GPU | `gpu on\\|off\\|status` helper; turned off at boot | OFF on purpose; the vendor tool fails while off | quiet fans | 11-23 |

Only the "Thing" column (first) and an "Updated" column (by name, any place) mean something to the code; the rest is
free text. A file may hold more than one such table (a second header row starts a new one); other text and tables
in it are skipped. Current state only: history stays in git and the issues. A configured entry is a table file, or a
folder whose STATE.md is the table. Only regular files are read (a FIFO or a device is a note, never opened to wait).

Where the tables are comes from the machine file's "agent-guards" section, part "facts" (config.py): "dirs" for
every session on the machine, and "projects" ([{"match", "dirs", "text", "name"}]) for the sessions working in one
project, matched by the checkout's origin remote (host/owner/repo) or by a folder (that folder or below it), never by
a glob. FACTS_DIRS (paths joined by ":") replaces both lists, for a test or a one-off look. "records"
([{"path", "projectsDir", "shared"}]) names record repo checkouts: `facts WORDS` searches their text files after the
tables (this project's projects/<name>/ and the shared folders). Read only: nothing here writes.
Plain Python 3 standard library.
"""
import datetime, hashlib, os, re, stat, subprocess, time

from .config import _NAME_RE as NAME_RE   # one rule for a project's folder name

TABLE_FILE = 'STATE.md'
CALL_TO_LOOK = 'check the state table before diagnosing or redoing something: `facts <words>` prints the matching rows'
MAX_READ = 4 * 1024 * 1024          # a table file is read up to this, the rest is cut (and named)
RECORD_MAX = 1024 * 1024            # record repo files over this are skipped
RECORD_TYPES = ('.md', '.txt', '.json', '.yml', '.yaml', '.sh')
RECORD_SECONDS = 10                 # a record repo search stops after this (and says so)
LINE_CHARS = 160
MORE_INDEX = 'run facts --index'
_SEP = re.compile(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$')


class Place:
    """One configured entry: `entry` (as written), `source` (where it was configured), `file` (the table file or
    None), `tables` [Table], `notes` [text]: a missing file or folder, an empty folder, a file with no state table;
    `digest`: sha256 of the bytes read (a record repo copy of the same file is not shown again)."""

    def __init__(self, entry, source):
        self.entry, self.source, self.file, self.tables, self.notes, self.digest = entry, source, None, [], [], None

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


def _is_header(line):
    return line.lstrip().startswith('|') and cells(line)[0].strip().lower() == 'thing'


def _is_sep(line):
    s = line.strip()
    return '|' in s and '-' in s and bool(_SEP.match(s))


def parse(text, file='<text>'):
    """[Table] of every state table in `text`. A row whose cell count differs from the header's is kept (listed
    and searched as it is) and named in its table's warnings. A second header row ends a table and starts the next."""
    lines = text.split('\n')
    tables, k = [], 0
    while k < len(lines):
        line = lines[k]
        if _is_header(line):
            sep = lines[k + 1] if k + 1 < len(lines) and _is_sep(lines[k + 1]) else None
            t = Table(file, line.rstrip(), sep.rstrip() if sep else None, cells(line))
            if sep is None:
                t.warnings.append(f'{file}:{k + 1}: the header has no |---| line under it')
            k += 2 if sep is not None else 1
            while k < len(lines) and lines[k].lstrip().startswith('|') and not _is_header(lines[k]):
                c = cells(lines[k])
                if any(c) and not _is_sep(lines[k]):
                    t.rows.append(Row(t, k + 1, lines[k].rstrip(), c))
                    if len(c) != len(t.columns):
                        t.warnings.append(f'{file}:{k + 1}: row "{unescape(c[0])[:60]}" has {len(c)} cells, the header '
                                          f'has {len(t.columns)}')
                k += 1
            tables.append(t)
            continue
        k += 1
    return tables


def read_regular(path, limit):
    """(bytes, cut, problem): at most `limit` bytes of a regular file. A FIFO, a device or a folder is not opened
    for reading (O_NONBLOCK, then fstat), so nothing here waits on a writer that never comes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
    except OSError as e:
        return None, False, f'cannot be read ({e.strerror or e})'
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, False, 'not a regular file: skipped'
        chunks, size = [], 0
        while size <= limit:
            b = os.read(fd, min(1 << 20, limit + 1 - size))
            if not b:
                break
            chunks.append(b)
            size += len(b)
        data = b''.join(chunks)
        return data[:limit], len(data) > limit, None
    except OSError as e:
        return None, False, f'cannot be read ({e.strerror or e})'
    finally:
        os.close(fd)


def _size(n):
    return f'{n // (1024 * 1024)} MB' if n >= 1024 * 1024 else f'{n} bytes'


def _expand(entry):
    return os.path.expanduser(entry) if entry.startswith('~/') else entry


def read_place(entry, source):
    """A Place for one configured entry (a table file, or a folder holding STATE.md). Never raises."""
    p = Place(entry, source)
    path = _expand(entry)
    if not os.path.isabs(path):
        p.notes.append(f'{entry}: not an absolute path (or ~/...): skipped')
        return p
    if os.path.isdir(path):
        path = os.path.join(path, TABLE_FILE)
        if not os.path.lexists(path):
            p.notes.append(f'{entry}: no {TABLE_FILE} in this folder yet')
            return p
    elif not os.path.lexists(path):
        p.notes.append(f'{entry}: not there')
        return p
    data, cut, problem = read_regular(path, MAX_READ)
    if problem:
        p.notes.append(f'{path}: {problem}')
        return p
    p.file, p.digest = path, hashlib.sha256(data).hexdigest()
    if cut:
        p.notes.append(f'{path}: (cut at {_size(MAX_READ)}): rows after that are not read')
    p.tables = parse(data.decode('utf-8', errors='replace'), path)
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


GIT = 'git'   # tests point it at a stand-in that hangs or fails


def origin(cwd, timeout=2):
    """The origin remote of the checkout at `cwd` (a linked worktree too), or ''. A folder that is gone, not a
    checkout, or a git that hangs (killed after `timeout` s) or fails: ''."""
    try:
        r = subprocess.run([GIT, '-C', cwd, 'config', '--get', 'remote.origin.url'], capture_output=True,
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
    origin remote. git is asked only when a project matches by remote, and at most once."""
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


def project_name(p):
    """The project's folder under a record repo's projects folder: its "name", else the last part of its remote
    (without .git) or folder; None when that is not a plain folder name ("..", ".", empty)."""
    if p.get('name'):
        return p['name']
    name = os.path.basename(_expand(p['match']).rstrip('/'))
    if not p['match'].startswith(('/', '~/')) and name.lower().endswith('.git'):
        name = name[:-4]
    return name if NAME_RE.match(name) else None


def _same(entry):
    """What makes two entries the same place: the real path (a link, ~/ and a trailing / do not count)."""
    path = _expand(entry)
    try:
        return os.path.realpath(path) if os.path.isabs(path) else entry
    except (OSError, ValueError):
        return entry


def entries(cfg, cwd, env=None, every=False, projects=None):
    """[(entry, source)] to read: FACTS_DIRS when set; else the machine's dirs and those of `projects` (default:
    the projects `cwd` belongs to; every project's with every=True), each place once (by its real path)."""
    env = os.environ if env is None else env
    if env.get('FACTS_DIRS'):
        out = [(e, 'FACTS_DIRS') for e in env['FACTS_DIRS'].split(':') if e]
    else:
        out = [(e, 'machine') for e in cfg.get('dirs') or []]
        if every:
            projects = cfg.get('projects') or []
        elif projects is None:
            projects = projects_for(cfg, cwd)
        for p in projects:
            out += [(e, f'project {p["match"]}') for e in p.get('dirs') or []]
    seen, uniq = set(), []
    for e, s in out:
        k = _same(e)
        if k not in seen:
            seen.add(k)
            uniq.append((e, s))
    return uniq


def places(cfg, cwd, env=None, every=False, projects=None):
    return [read_place(e, s) for e, s in entries(cfg, cwd, env, every, projects)]


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
    """Output lines: each table file, then its things, one per line (at most `limit` things in all; a file none of
    whose things fit is left out, and the last line says how many more there are)."""
    lines, shown, total = [], 0, sum(len(p.rows()) for p in found)
    for p in found:
        rows = p.rows()
        if not rows or (limit is not None and shown >= limit):
            continue
        lines.append(f'{p.file} ({len(rows)} rows):')
        for r in rows:
            if limit is not None and shown >= limit:
                break
            lines.append(f'  - {r.thing[:100]}')
            shown += 1
    if total > shown:
        lines.append(f'  +{total - shown} more: {MORE_INDEX}')
    return lines


def notes(found):
    out = [n for p in found for n in p.notes]
    out += [w for p in found for t in p.tables for w in t.warnings]
    return out


def updated(text, today):
    """The date in an Updated cell, or None: YYYY-MM-DD, or MM-DD (the latest such date that is not more than a
    day ahead of `today`: this year, last year, or for 02-29 the last leap year), each with any text after it
    ("11-23 (checked)")."""
    text = text or ''
    m = re.match(r'^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?![\d])', text)
    try:
        if m:
            return datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    m = re.match(r'^\s*(\d{1,2})-(\d{1,2})(?![\d-])', text)
    if not m:
        return None
    month, day = int(m[1]), int(m[2])
    for year in range(today.year, today.year - 8, -1):
        try:
            d = datetime.date(year, month, day)
        except ValueError:
            continue
        if d <= today + datetime.timedelta(days=1):
            return d
    return None


def check(found, stale_days, max_rows, today):
    """The gardener's findings (report only; nothing is changed): rows whose Updated is older than stale_days,
    in the future or cannot be read, tables without an Updated column or over max_rows, broken rows, entries that
    are missing, empty or hold no table."""
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
                elif d > today + datetime.timedelta(days=1):
                    out.append(f'{t.file}:{r.lineno}: "{r.thing[:60]}": Updated {cell.strip()} is in the future')
                elif (today - d).days > stale_days:
                    out.append(f'{t.file}:{r.lineno}: "{r.thing[:60]}": Updated {cell.strip()} is '
                               f'{(today - d).days} days old (staleDays {stale_days}): check the state, then edit '
                               'the row')
    return out


# The record repo ------------------------------------------------------------------------------------------------

class Records:
    """One configured record repo: `root` (its checkout), `folders` [relative folder to search], `note` (why it
    is skipped, or None)."""

    def __init__(self, root, folders, note=None):
        self.root, self.folders, self.note = root, folders, note


def records(cfg, projects, every=False):
    """[Records] for the configured record repos: the shared folders and, for each of `projects`, its folder under
    the projects folder (every=True: the whole projects folder). A repo that is not there has a note."""
    out = []
    for r in cfg.get('records') or []:
        root = _expand(r['path'])
        pdir = r.get('projectsDir') or 'projects'
        folders = list(r.get('shared', ['machines', 'orch', 'shared']))
        if every:
            folders.append(pdir)
        else:
            folders += [f'{pdir}/{n}' for n in (project_name(p) for p in projects) if n]
        folders = list(dict.fromkeys(folders))
        note = None
        try:
            if not os.path.isdir(root):
                note = f'record repo {r["path"]}: not there (or not a folder): skipped'
            elif not os.access(root, os.R_OK | os.X_OK):
                note = f'record repo {r["path"]}: cannot be read: skipped'
        except (OSError, ValueError) as e:
            note = f'record repo {r["path"]}: {e}: skipped'
        out.append(Records(root, folders, note))
    return out


def _record_files(rec, deadline):
    """(path, relative path) of every text file to search in one record repo: its folders, .git and links left
    out, in a sorted order. A folder whose real path is outside the repo (a link on the way) is left out. Stops at
    `deadline` (time.monotonic()), checked for each folder and each file."""
    try:
        real_root = os.path.realpath(rec.root)
    except (OSError, ValueError):
        return
    for folder in rec.folders:
        top = os.path.join(rec.root, folder)
        if os.path.islink(top) or not os.path.isdir(top):
            continue
        if not os.path.realpath(top).startswith(real_root.rstrip('/') + '/'):
            continue
        for d, dirs, files in os.walk(top):
            if time.monotonic() > deadline:
                return
            dirs[:] = sorted(x for x in dirs if x != '.git' and not os.path.islink(os.path.join(d, x)))
            for name in sorted(files):
                if time.monotonic() > deadline:
                    return
                path = os.path.join(d, name)
                if name.lower().endswith(RECORD_TYPES) and not os.path.islink(path):
                    yield path, os.path.relpath(path, rec.root)


def search_records(recs, words, skip=None, limit=10, seconds=RECORD_SECONDS):
    """(lines, notes, copies): the lines of the record repos' text files that hold every word (any case), as
    "path:line: text" under a "Records in <repo>:" line, at most `limit` hits and then "+N more". A file over
    RECORD_MAX, not a regular file, holding a NUL byte (not text), or with the same bytes as a table already read
    (`skip`: {sha256: table file}) is skipped; copies: [(record file, table file)] for each such copy that holds the
    words."""
    ws, skip = _words(words), skip or {}
    lines, out_notes, copies, shown, total = [], [], [], 0, 0
    deadline = time.monotonic() + seconds
    for rec in recs:
        if rec.note:
            out_notes.append(rec.note)
            continue
        head = False
        for path, rel in _record_files(rec, deadline):
            data, cut, problem = read_regular(path, RECORD_MAX)
            if problem or cut or data is None or b'\0' in data:
                continue
            table = skip.get(hashlib.sha256(data).hexdigest())
            for n, line in enumerate(data.decode('utf-8', errors='replace').split('\n'), 1):
                low = line.lower()
                if ws and all(w in low for w in ws):
                    if table:
                        copies.append((os.path.join(rec.root, rel), table))
                        break
                    total += 1
                    if shown < limit:
                        if not head:
                            lines.append(f'Records in {rec.root}:')
                            head = True
                        lines.append(f'{rel}:{n}: {line.strip()}'[:LINE_CHARS])
                        shown += 1
        if time.monotonic() > deadline:
            out_notes.append(f'record repo {rec.root}: search stopped after {seconds} s: some files not searched')
            break
    if total > shown:
        lines.append(f'+{total - shown} more (facts --records {" ".join(words)})')
    return lines, out_notes, copies


def record_line(recs):
    """The session-start line naming each record repo that is there; '' when none."""
    roots = [r.root for r in recs if not r.note]
    return f'Records: {", ".join(roots)} (facts <words> searches it)' if roots else ''


# The hook's text ------------------------------------------------------------------------------------------------

def _fit(head, body, tail, chars):
    """head + as many body lines as fit in `chars` + tail; a body cut short ends with "+N more: run facts
    --index" (N: the things left out)."""
    tail = [t[:chars // 4] for t in tail]
    budget = chars - len(head) - sum(len(t) + 1 for t in tail) - 40
    kept, used = [], 0
    for k, line in enumerate(body):
        if used + len(line) + 1 > budget:
            rest = body[k:]
            left = sum(1 for x in rest if x.startswith('  - '))
            m = re.match(r'^  \+(\d+) more', rest[-1]) if rest else None
            left += int(m[1]) if m else 0
            while kept and not kept[-1].startswith('  - '):    # no file line with none of its things
                kept.pop()
            kept.append(f'  +{left} more: {MORE_INDEX}')
            break
        kept.append(line)
        used += len(line) + 1
    return '\n'.join([head] + kept + tail)


def session_text(found, projects, limit, chars=4000, recs=()):
    """The session-start context: the things of every table (at most `limit` things), the call to look first, the
    projects' text and one line per record repo, all in at most `chars` characters; '' when there is nothing."""
    rec = record_line(recs)
    extra = [p['text'] for p in projects if p.get('text')]
    if not any(p.rows() for p in found):
        return '\n'.join(([rec] if rec else []))[:chars]
    head = (f'State tables (one row per thing: what we made, how to use it, its state now and why). '
            f'{CALL_TO_LOOK[0].upper()}{CALL_TO_LOOK[1:]}. Things:')
    return _fit(head, index(found, limit), extra + ([rec] if rec else []), chars)[:chars]


def compact_text(found, projects, chars=4000):
    """The compaction instructions' part: keep the things' names, not their rows."""
    if not any(p.rows() for p in found):
        return ''
    extra = [p['text'] for p in projects if p.get('text')]
    return '\n'.join(['State tables: in the summary keep only the names of the things you looked up or changed in '
                      'them, not their rows; `facts <words>` reads the rows again after compaction. A thing whose '
                      'state changed in this conversation and whose row was not edited yet: say "row NOT YET '
                      'UPDATED" with the new state.'] + extra)[:chars]
