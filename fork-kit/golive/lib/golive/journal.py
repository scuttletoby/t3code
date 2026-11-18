"""One run at a time (a lock), and a journal that says where a run got to.

The lock is a flock on <stateDir>/lock, so it dies with its holder: no stale lock, no reused pid. A changing run
holds it exclusively and writes who it is into the file; `check` holds it shared, so checks can run together but
never beside a changing run.

The journal is <stateDir>/journal.jsonl, append-only, one JSON object per line, each written and fsynced before
the step it names starts. Each line carries the sha256 of the line before it, so an edited or lost line is found.
A run is: begin, then per step "started" and "done" (with its facts, such as hashes), then end.

A run needs settling when it stopped (killed, or the machine went down) or ended with an error after its first
step started: either can leave a half-applied state. Every later run reports it, and a changing run refuses to
start until the owner puts the home right and runs `t3-golive settle` (settle()). A run that failed or was
killed before its first step changed nothing (every change is made inside a step) and needs no settling.

The rule every changing command keeps (fork #186): every change is made inside `r.step()`, and pre-checks run
before the first step. Kit code that changes something calls `r.require_step()` first, so a change outside a step
is an error instead of a run that later reads as "changed nothing". A command that stops and has undone every step
it started (each one journalled "done") raises Refused: the run ends "refused" and needs no settling.
A torn last line (killed while writing) is reported and ignored. Damage anywhere else refuses until settle moves
the journal aside; a journal written by a newer kit refuses with "update the kit".
"""
import contextlib, datetime, errno, fcntl, hashlib, json, os

JOURNAL_VERSION = 1
SETTLE_HINT = ('look at the T3 home and put it right, then run `t3-golive settle --reason "<what you did>"`')


class Busy(Exception):
    """Another golive command holds the lock."""


class Unfinished(Exception):
    """The last run needs settling; .run holds what the journal says about it."""

    def __init__(self, run):
        self.run = run
        super().__init__(f'{describe(run)}; {SETTLE_HINT}')


class Refused(Exception):
    """A changing run stopped on purpose and undid every step it started; nothing is half-applied."""


class Damaged(Exception):
    """The journal cannot be trusted (a line other than the last is broken, or the hash chain is cut)."""


class Newer(Damaged):
    """A line of the journal was written by a newer kit; this kit must not read or move it."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _paths(state_dir):
    return os.path.join(state_dir, 'lock'), os.path.join(state_dir, 'journal.jsonl')


def _ensure_dir(state_dir):
    os.makedirs(state_dir, mode=0o700, exist_ok=True)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock(state_dir, command, exclusive, wait=False, create=True):
    """Hold the kit's lock for a command; Busy when a changing run (or, for one, any command) holds it.
    wait=True blocks until the lock is free instead (the guard waits for the run it watches to end).
    create=False (shared only; `status`, which writes nothing): no state folder or lock file is made; with no lock
    file no golive run has ever taken the lock here, so it goes on without one."""
    if exclusive and not create:
        raise ValueError('a changing run makes its lock file')
    path, _ = _paths(state_dir)
    if create:
        _ensure_dir(state_dir)
    try:
        fd = os.open(path, (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | os.O_CLOEXEC, 0o600)
    except FileNotFoundError:
        if create:
            raise
        fd = None
    if fd is None:
        yield
        return
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if wait else fcntl.LOCK_NB))
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            raise Busy(_holder_text(fd, exclusive)) from None
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


def _holder_text(fd, wanted_exclusive):
    try:
        holder = json.loads(os.pread(fd, 4096, 0) or b'null')
    except ValueError:
        holder = None
    if isinstance(holder, dict) and {'command', 'pid', 'at'} <= set(holder) and _alive(holder['pid']):
        return f'another golive run is active: {holder["command"]} (pid {holder["pid"]}, since {holder["at"]})'
    # An empty lock file, or the name of a run that was killed (its name stays; the holder now is a check): a shared
    # holder, or a changing run in the instant before it wrote its name.
    if wanted_exclusive:
        return 'a golive check is running (or a run is just starting); try again when it ends'
    return 'a golive run is just starting; try again in a moment'


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OverflowError):
        return True
    return True


def _damaged(path, what):
    return (f'{path} {what}; the journal cannot be trusted. Read it to see where the last run got to, put the T3 '
            'home right, then run `t3-golive settle --reason "<what you did>"`: it moves this journal aside '
            '(journal.jsonl.damaged-<time>) and the next run starts a new one')


def read(state_dir):
    """All runs in the journal, oldest first: [{id, command, pid, began, steps: [...], end, settled}], and a note
    when the last line is torn. Raises Newer (a newer kit wrote it) or Damaged."""
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
        except ValueError:
            rec = None
        v = rec.get('v') if isinstance(rec, dict) else None
        if type(v) is int and v > JOURNAL_VERSION:
            raise Newer(f'{path} line {n} was written by a newer go-live kit (journal version {v}, this kit reads '
                        f'{JOURNAL_VERSION}): update the kit')
        if not (isinstance(rec, dict) and rec.get('prev') == prev and v == JOURNAL_VERSION):
            raise Damaged(_damaged(path, f'line {n} is broken or out of order'))
        prev = hashlib.sha256(raw).hexdigest()
        event, rid = rec.get('event'), rec.get('run')
        if event == 'begin':
            run = {'id': rid, 'command': rec.get('command'), 'pid': rec.get('pid'), 'began': rec.get('at'),
                   'steps': [], 'end': None, 'settled': None, 'scratch': rec.get('scratch') is True}
            runs.append(run)
            by_id[rid] = run
        elif rid in by_id:
            run = by_id[rid]
            if event == 'step':
                run['steps'].append({'step': rec.get('step'), 'phase': rec.get('phase'), 'at': rec.get('at'),
                                     'data': rec.get('data')})
            elif event == 'end':
                run['end'] = {'result': rec.get('result'), 'detail': rec.get('detail'), 'at': rec.get('at')}
            elif event == 'settle':
                run['settled'] = {'detail': rec.get('detail'), 'at': rec.get('at')}
        else:
            raise Damaged(_damaged(path, f'line {n} names a run that never began'))
    if tail:
        note = f'{path} ends with a torn line (a run was killed while writing); it was ignored'
    return runs, note


def needs_settling(run):
    """True when the run stopped half-way, or ended with an error, after its first step started, and the owner
    has not settled it. Killed before its first step (a copy waiting for a quiet point), it changed nothing."""
    if run['settled'] is not None or not run['steps'] or run.get('scratch'):
        return False  # a scratch run (rehearse) changes only its own clone, which its next run throws away
    return run['end'] is None or run['end']['result'] not in ('ok', 'refused')


def unfinished(state_dir):
    """The last run if it needs settling, else None. Only meaningful while the caller holds the lock."""
    runs, _ = read(state_dir)
    return runs[-1] if runs and needs_settling(runs[-1]) else None


def describe(run):
    last = run['steps'][-1] if run['steps'] else None
    where = f'step "{last["step"]}" {last["phase"]} at {last["at"]}' if last else 'before its first step'
    head = f'the last run ({run["command"]}, pid {run["pid"]}, began {run["began"]})'
    if run['end'] is None:
        return f'{head} stopped half-way: {where}'
    return f'{head} ended {run["end"]["result"]} after {where}: {run["end"]["detail"]}'


class Run:
    """A changing command's run. Use journal.run(); it holds the lock and writes begin and end."""

    def __init__(self, state_dir, command):
        self.state_dir, self.command = state_dir, command
        self.id = f'{_now()}-{os.getpid()}-{os.urandom(4).hex()}'
        self._path = _paths(state_dir)[1]
        self._holds_lock = False
        self._step = None
        self._broken = None  # the step an exception left without "done"
        # True: an interrupt or kill that is not an Exception (KeyboardInterrupt, SystemExit) leaves no end line, as a
        # SIGKILL would, so a guard waiting on this run (the switch's) puts things back; see run().
        self.hold_end = False

    @property
    def in_step(self):
        """The name of the step running now, or None."""
        return self._step

    def require_step(self, what):
        """Kit code calls this before any change: a change outside a step is an error (see the module text)."""
        if self._step is None:
            raise RuntimeError(f'{what}: a change outside a journalled step; make it inside r.step()')

    @property
    def holds_lock(self):
        """True only inside journal.run(): this run holds the kit's lock exclusively (see check.run_check)."""
        return self._holds_lock

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
            _fsync_dir(self.state_dir)

    @contextlib.contextmanager
    def step(self, name, **facts):
        """Journal "started" before the body and "done" after it; the body may add facts to the dict it gets."""
        if self._step is not None:
            raise RuntimeError(f'step "{name}" inside step "{self._step}": steps do not nest')
        self._append('step', step=name, phase='started', data=facts or None)
        self._step, done = name, {}
        try:
            yield done
        except BaseException:
            self._broken = name
            raise
        finally:
            self._step = None
        self._append('step', step=name, phase='done', data=done or None)


def _detail(error):
    """An error for the journal, with its notes (a later error while putting things right, for example)."""
    notes = ''.join(f'; {n}' for n in getattr(error, '__notes__', ()))
    return f'{type(error).__name__}: {error}{notes}'[:500]


@contextlib.contextmanager
def run(state_dir, command, may_follow=(), scratch=False):
    """Begin a changing run: take the lock (Busy), refuse a run that needs settling (Unfinished) unless its command
    is in may_follow (`back` finishes a back or switch stopped half-way), journal begin and end. An exception ends
    the run "failed" and goes on (after a started step it then needs settling); Refused after every started step is
    done ends it "refused" (no settling), except in a run that follows one: that one still needs settling until a
    run ends ok. A kill leaves it with no end for the next run to report; so does an interrupt that is not an
    Exception once the run set hold_end (its guard then acts). scratch=True: every step changes only the run's own
    throwaway folder (rehearse's clone), so a stop or an error needs no settling: the next run redoes it from the
    start."""
    with lock(state_dir, command, exclusive=True):
        last = unfinished(state_dir)
        if last is not None and last['command'] not in may_follow:
            raise Unfinished(last)
        r = Run(state_dir, command)
        r._append('begin', command=command, pid=os.getpid(), **({'scratch': True} if scratch else {}))
        r._holds_lock = True
        try:
            try:
                if last is not None:  # until this run ends ok, the home still needs settling (a refusal too)
                    with r.step('follows', run=last['id']):
                        pass
                yield r
            except Refused as e:
                # Refused inside a step, or after a step that never reached "done", is a failure like any other.
                result = 'failed' if r._broken or last is not None else 'refused'
                r._append('end', result=result, detail=_detail(e))
                raise
            except BaseException as e:
                if r.hold_end and not isinstance(e, Exception):
                    raise  # no end line: to the journal this is a kill, and the run's guard acts on it
                r._append('end', result='failed', detail=_detail(e))
                raise
            r._append('end', result='ok', detail=None)
        finally:
            r._holds_lock = False


def settle(state_dir, reason):
    """The owner says the home is right again (`t3-golive settle`). Holds the lock exclusively (Busy).
    Returns ('settled', run) after journalling the reason against the run that needed settling, ('moved', path)
    after moving a damaged journal aside, or (None, None) when nothing needed settling. A journal from a newer
    kit raises Newer and is left alone. Running it again changes nothing."""
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('settle needs a reason: what was done to put the T3 home right')
    with lock(state_dir, 'settle', exclusive=True):
        _, path = _paths(state_dir)
        try:
            last = unfinished(state_dir)
        except Newer:
            raise
        except Damaged:
            aside = f'{path}.damaged-{datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
            if os.path.lexists(aside):
                aside = f'{aside}-{os.getpid()}'
            os.rename(path, aside)
            _fsync_dir(state_dir)
            return 'moved', aside
        if last is None:
            return None, None
        r = Run(state_dir, last['command'])
        r.id = last['id']
        r._append('settle', detail=reason[:500])
        return 'settled', last
