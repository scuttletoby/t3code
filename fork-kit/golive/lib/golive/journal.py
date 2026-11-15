"""One run at a time (a lock), and a journal that says where a run got to.

The lock is a flock on <stateDir>/lock, so it dies with its holder: no stale lock, no reused pid. A changing run
holds it exclusively and writes who it is into the file; `check` holds it shared, so checks can run together but
never beside a changing run.

The journal is <stateDir>/journal.jsonl, append-only, one JSON object per line, each written and fsynced before
the step it names starts. Each line carries the sha256 of the line before it, so an edited or lost line is found.
A run is: begin, then per step "started" and "done" (with its facts, such as hashes), then end. A run with no end
whose lock is free stopped half-way (killed, or the machine went down): every later run reads and reports it, and
a changing run refuses to start until the command that owns that state settles it (close_unfinished()).
A torn last line (killed while writing) is reported and ignored; damage anywhere else refuses.
"""
import contextlib, datetime, errno, fcntl, hashlib, json, os

JOURNAL_VERSION = 1


class Busy(Exception):
    """Another golive command holds the lock."""


class Unfinished(Exception):
    """The last run stopped half-way; .run holds what the journal says about it."""

    def __init__(self, run):
        self.run = run
        super().__init__(describe(run))


class Damaged(Exception):
    """The journal cannot be trusted (a line other than the last is broken, or the hash chain is cut)."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _paths(state_dir):
    return os.path.join(state_dir, 'lock'), os.path.join(state_dir, 'journal.jsonl')


def _ensure_dir(state_dir):
    os.makedirs(state_dir, mode=0o700, exist_ok=True)


@contextlib.contextmanager
def lock(state_dir, command, exclusive):
    """Hold the kit's lock for a command; Busy when a changing run (or, for one, any command) holds it."""
    _ensure_dir(state_dir)
    path, _ = _paths(state_dir)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            raise Busy(_holder_text(fd)) from None
        if exclusive:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps({'pid': os.getpid(), 'command': command, 'at': _now()}).encode(), 0)
        try:
            yield
        finally:
            if exclusive:
                os.ftruncate(fd, 0)
    finally:
        os.close(fd)  # releases the flock


def _holder_text(fd):
    try:
        holder = json.loads(os.pread(fd, 4096, 0) or b'null')
        return (f'another golive run is active: {holder["command"]} (pid {holder["pid"]}, since {holder["at"]})'
                if isinstance(holder, dict) else 'a golive check is running; try again when it ends')
    except (ValueError, KeyError, TypeError):
        return 'another golive command holds the lock'


def read(state_dir):
    """All runs in the journal, oldest first: [{id, command, pid, began, steps: [...], end, torn}], and a note
    when the last line is torn. Raises Damaged."""
    _, path = _paths(state_dir)
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return [], None
    lines = data.split(b'\n')
    tail = lines.pop()  # b'' when the file ends with a newline
    runs, by_id, prev, note = [], {}, '', None
    for n, raw in enumerate(lines, 1):
        try:
            rec = json.loads(raw)
            ok = isinstance(rec, dict) and rec.get('prev') == prev and rec.get('v') == JOURNAL_VERSION
        except ValueError:
            ok = False
        if not ok:
            raise Damaged(f'{path} line {n} is broken or out of order; the journal cannot be trusted')
        prev = hashlib.sha256(raw).hexdigest()
        event, rid = rec.get('event'), rec.get('run')
        if event == 'begin':
            run = {'id': rid, 'command': rec.get('command'), 'pid': rec.get('pid'), 'began': rec.get('at'),
                   'steps': [], 'end': None}
            runs.append(run)
            by_id[rid] = run
        elif rid in by_id:
            run = by_id[rid]
            if event == 'step':
                run['steps'].append({'step': rec.get('step'), 'phase': rec.get('phase'), 'at': rec.get('at'),
                                     'data': rec.get('data')})
            elif event == 'end':
                run['end'] = {'result': rec.get('result'), 'detail': rec.get('detail'), 'at': rec.get('at')}
        else:
            raise Damaged(f'{path} line {n} names a run that never began')
    if tail:
        note = f'{path} ends with a torn line (a run was killed while writing); it was ignored'
    return runs, note


def unfinished(state_dir):
    """The last run if it has no end, else None. Only meaningful while the caller holds the lock."""
    runs, _ = read(state_dir)
    return runs[-1] if runs and runs[-1]['end'] is None else None


def describe(run):
    last = run['steps'][-1] if run['steps'] else None
    where = f'step "{last["step"]}" {last["phase"]} at {last["at"]}' if last else 'before its first step'
    return f'the last run ({run["command"]}, pid {run["pid"]}, began {run["began"]}) stopped half-way: {where}'


class Run:
    """A changing command's run. Use journal.run(); it holds the lock and writes begin and end."""

    def __init__(self, state_dir, command):
        self.state_dir, self.command = state_dir, command
        self.id = f'{_now()}-{os.getpid()}'
        self._path = _paths(state_dir)[1]

    def _append(self, event, **fields):
        _, note = read(self.state_dir)  # Damaged stops the run before it writes
        prev = ''
        if os.path.exists(self._path):
            with open(self._path, 'rb') as f:
                body = f.read()
            if note:  # drop the torn tail so the chain goes on from the last whole line
                with open(self._path, 'r+b') as f:
                    f.truncate(body.rfind(b'\n') + 1)
                    body = body[:body.rfind(b'\n') + 1]
            whole = body.rstrip(b'\n').rsplit(b'\n', 1)[-1]
            prev = hashlib.sha256(whole).hexdigest() if whole else ''
        line = json.dumps({'v': JOURNAL_VERSION, 'prev': prev, 'run': self.id, 'at': _now(), 'event': event,
                           **fields}, sort_keys=True).encode() + b'\n'
        new = not os.path.exists(self._path)
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if new:
            dfd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    @contextlib.contextmanager
    def step(self, name, **facts):
        """Journal "started" before the body and "done" after it; the body may add facts to the dict it gets."""
        self._append('step', step=name, phase='started', data=facts or None)
        done = {}
        yield done
        self._append('step', step=name, phase='done', data=done or None)


@contextlib.contextmanager
def run(state_dir, command):
    """Begin a changing run: take the lock (Busy), refuse an unfinished one (Unfinished), journal begin and end.
    An exception ends the run "failed" and goes on; a kill leaves it unfinished for the next run to report."""
    with lock(state_dir, command, exclusive=True):
        last = unfinished(state_dir)
        if last is not None:
            raise Unfinished(last)
        r = Run(state_dir, command)
        r._append('begin', command=command, pid=os.getpid())
        try:
            yield r
        except BaseException as e:
            r._append('end', result='failed', detail=f'{type(e).__name__}: {e}'[:500])
            raise
        r._append('end', result='ok', detail=None)


def close_unfinished(state_dir, reason):
    """Mark the unfinished run as settled (by the command that put things right). Holds the lock."""
    with lock(state_dir, 'close-unfinished', exclusive=True):
        last = unfinished(state_dir)
        if last is None:
            return None
        r = Run(state_dir, last['command'])
        r.id = last['id']
        r._append('end', result='stopped-half-way', detail=reason[:500])
        return last
