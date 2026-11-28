#!/usr/bin/env python3
"""claims: who holds a test server now, shared or alone. One folder of claim files and one lock; general (no
project names: a server is any name the caller gives).

  - A claim is `shared` (runs beside other shared claims: test runs that do not interfere) or `alone` (nothing else
    on that server: a restart, a reset of its data). Conflict table: shared + shared is fine; alone conflicts with
    everything.
  - A waiting restart goes first: an `alone` claim made with restart=True that cannot be granted leaves a wait entry,
    and while one waits no new claim on that server is granted (shared or alone) except to the oldest waiter, so a
    stream of shared runs cannot starve it.
  - A holder is a process (pid and its start time, so a reused pid is not the same holder; and its process group,
    so a holder killed while its command runs on stays alive until the group's last process ends) or a service
    unit; a claim or wait whose holder is gone is swept (removed) at every call. A holder it cannot check counts as
    alive.
  - Every call reads and writes under one lock (flock on <folder>/.lock); claim files are written whole (a temporary
    file renamed into place), so a reader never sees half a claim; a temporary file a crashed writer left is removed.
  - A claim file this version does not write (junk, a wrong shape, a holder with no start time) is swept: it would
    otherwise hold the server for ever. Fields it does not know are kept and ignored (judged by the ones it knows), so
    a release that changes what a claim means must raise VERSION. Fails closed where it cannot judge: a folder it
    cannot read refuses every claim; a claim a newer version wrote (`v` above VERSION) is never swept and refuses
    every claim on the server it names (every server when it names none), naming why: a caller waits, and only that
    release (or a person) removes it once its holder is gone. An empty or missing folder holds nothing.
"""
import fcntl, json, os, subprocess, time

MODES = ('shared', 'alone')
SUFFIX = '.claim.json'
TMP = '.tmp-'
VERSION = 1   # the claim files' version: a file with a higher one is a newer release's (never swept on a guess)


class ClaimsError(Exception):
    """The folder cannot be read or holds a claim it cannot parse: nothing is granted."""


def conflicts(held, wanted):
    """The conflict table: only shared beside shared."""
    return not (held == 'shared' and wanted == 'shared')


def start_time(pid):
    """A process's start time (clock ticks since boot, /proc/<pid>/stat field 22), or None when it cannot be read."""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            stat = f.read().decode('ascii', 'replace')
        return stat[stat.rindex(')') + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def me():
    """This process as a holder (with its process group: the command it runs is in it)."""
    start = start_time(os.getpid())
    if start is None:
        raise ClaimsError('cannot read this process\'s start time from /proc')
    return {'pid': os.getpid(), 'start': start, 'pgid': os.getpgrp()}


def _whole(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def valid_holder(h):
    """A holder this version can judge: {"unit"[, "user"]} or {"pid", "start"[, "pgid"]}, and any fields it does not
    know (ignored)."""
    if not isinstance(h, dict):
        return False
    if 'unit' in h:
        return isinstance(h['unit'], str) and bool(h['unit']) and isinstance(h.get('user', True), bool)
    return _whole(h.get('pid')) and isinstance(h.get('start'), str) and bool(h['start']) and \
        ('pgid' not in h or _whole(h['pgid']))


def valid_entry(c):
    return (isinstance(c, dict) and c.get('kind') in ('claim', 'wait') and c.get('mode') in MODES and
            isinstance(c.get('server'), str) and bool(c['server']) and valid_holder(c.get('holder')) and
            isinstance(c.get('lane', ''), str) and isinstance(c.get('since'), (int, float)) and
            not isinstance(c['since'], bool) and isinstance(c.get('note', {}), dict))


def alive(holder):
    """Is the holder still there? pid + start time (a reused pid is another process), or a systemd unit. What it
    cannot check counts as alive (a claim is never swept on a guess)."""
    if 'unit' in holder:
        try:   # inactive or failed (also a unit that does not exist): gone; anything else, or no answer: alive
            r = subprocess.run(['systemctl', '--user' if holder.get('user', True) else '--system', 'show',
                                '-p', 'ActiveState', '--value', '--', str(holder['unit'])],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return True
        return not (r.returncode == 0 and r.stdout.strip() in ('inactive', 'failed'))
    pid = holder.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        there = True
    except ProcessLookupError:
        there = False
    except PermissionError:
        there = True   # another user's process: it is there
    if there:
        want = holder.get('start')
        if want is None or not os.path.isdir('/proc'):
            return True
        got = start_time(pid)
        if got is None or got == want:
            return True
        # Its pid is another process's now. The number is not reused while a process group of that id exists, so
        # the holder's own group (led by it) is gone too; a group it did not lead is checked below.
        if holder.get('pgid') == pid:
            return False
    return group_alive(holder.get('pgid'))


def group_alive(pgid):
    """Does a process of the group still run (the command of a holder that was killed)? No group: no."""
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Claims:
    def __init__(self, folder, alive=alive, clock=time.time):
        self.folder, self.alive, self.clock = folder, alive, clock

    # ---------------------------------------------------------------- the folder, under one lock

    def _locked(self):
        try:
            os.makedirs(self.folder, exist_ok=True)
            fd = os.open(os.path.join(self.folder, '.lock'), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise ClaimsError(f'cannot use the claims folder {self.folder} ({e.strerror or e})')
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _read(self):
        return self._scan()[0]

    def _scan(self):
        """(entries, newer) under the lock. newer: (server or None, version, path) of each claim file a newer
        release wrote (never swept: this version cannot judge it). Removes what a crash or a planter left:
        temporary files (every write is under this lock, so none is half-way now) and claim files this version
        does not write."""
        try:
            listed = os.listdir(self.folder)
        except OSError as e:
            raise ClaimsError(f'cannot read the claims folder {self.folder} ({e.strerror or e})')
        for n in listed:
            if n.startswith(TMP):
                self._unlink(n)
        out, newer = [], []
        for n in sorted(n for n in listed if n.endswith(SUFFIX) and not n.startswith('.')):
            path = os.path.join(self.folder, n)
            try:
                with open(path, encoding='utf-8') as f:
                    c = json.load(f)
            except FileNotFoundError:
                continue
            except OSError as e:
                raise ClaimsError(f'cannot read the claim {path} ({e.strerror or e})')
            except ValueError:
                c = None
            v = c.get('v', VERSION) if isinstance(c, dict) else None
            if isinstance(v, int) and not isinstance(v, bool) and v > VERSION:
                server = c.get('server')
                newer.append((server if isinstance(server, str) and server else None, v, path))
                continue
            if v != VERSION or not valid_entry(c):
                self._unlink(n)   # not a claim any release wrote: it would hold the server for ever
                continue
            c['id'] = n[:-len(SUFFIX)]
            out.append(c)
        return out, newer

    def _unlink(self, name):
        try:
            os.unlink(os.path.join(self.folder, name))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise ClaimsError(f'cannot remove {os.path.join(self.folder, name)} ({e.strerror or e})')

    def _write(self, entry):
        ident = f'{time.time_ns():020d}-{os.getpid()}-{os.urandom(3).hex()}'
        body = {'v': VERSION, **{k: v for k, v in entry.items() if k != 'id'}}
        tmp = os.path.join(self.folder, f'{TMP}{ident}')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(body, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(self.folder, ident + SUFFIX))
        return ident

    def _remove(self, ident):
        self._unlink(ident + SUFFIX)

    def _sweep(self, entries):
        live, gone = [], []
        for c in entries:
            (live if self.alive(c['holder']) else gone).append(c)
        for c in gone:
            self._remove(c['id'])
        return live, gone

    # ---------------------------------------------------------------- the calls

    def claim(self, server, mode, holder, lane='', restart=False, admit=None, keep_wait=False, since=None,
              note=None):
        """(token or None, why). token: the claim's id, for release(). restart: an alone claim that waits first in
        line when it cannot be granted (since: the time its lane began waiting, the same on every server it waits
        on, so one lane is first on all of them; default now). keep_wait: a grant keeps the wait (a lane that still
        needs another server; withdraw() ends it). admit(lanes) -> (ok, why): the pool's admission, checked last;
        lanes: one live claim per lane (a holder and its lane name), each with its note. note: a dict kept with the
        claim (the pool's cost of the lane)."""
        if mode not in MODES:
            raise ValueError(f'mode must be one of {MODES}')
        if restart and mode != 'alone':
            raise ValueError('a restart claims the server alone')
        if not valid_holder(holder):
            raise ValueError(f'not a holder: {holder!r}')
        fd = self._locked()
        try:
            entries, newer = self._scan()
            live, _ = self._sweep(entries)
            mine = lambda c: c['holder'] == holder and c.get('lane', '') == lane   # noqa: E731
            held = [c for c in live if c['kind'] == 'claim' and c['server'] == server]
            waits = waiting(live, server)
            why = None
            clash = [c for c in held if conflicts(c['mode'], mode)]
            newest = [n for n in newer if n[0] in (None, server)]
            if newest:
                why = (f'{server}: a claim from a newer release holds it ({newest[0][2]}, version {newest[0][1]}; '
                       f'this release reads {VERSION}): that release removes it once its holder is gone')
            elif clash:
                why = (f'{server} is held {clash[0]["mode"]} by lane {clash[0].get("lane") or "?"}'
                       f'{f" (+{len(clash) - 1} more)" if len(clash) > 1 else ""}')
            elif waits and not mine(waits[0]):
                why = f'{server}: a waiting restart goes first (lane {waits[0].get("lane") or "?"})'
            elif admit is not None:
                lanes = {}
                for c in live:
                    if c['kind'] == 'claim' and not mine(c):
                        lanes.setdefault(lane_key(c), c)
                ok, said = admit(list(lanes.values()))
                why = None if ok else said
            if why:
                if restart and not any(mine(w) for w in waits):
                    self._wait(server, holder, lane, since)
                return None, why
            if not keep_wait:
                for w in waits:
                    if mine(w):
                        self._remove(w['id'])
            entry = {'kind': 'claim', 'server': server, 'mode': mode, 'holder': holder, 'lane': lane,
                     'since': self.clock()}
            if note:
                entry['note'] = dict(note)
            return self._write(entry), f'{server} claimed {mode}'
        finally:
            os.close(fd)

    def _wait(self, server, holder, lane, since):
        self._write({'kind': 'wait', 'server': server, 'mode': 'alone', 'holder': holder, 'lane': lane,
                     'since': self.clock() if since is None else since})

    def queue(self, server, holder, lane='', since=None):
        """Wait first in line on a server without claiming it (a restart that needs several servers keeps its
        place on each); a wait it already has stays as it is."""
        if not valid_holder(holder):
            raise ValueError(f'not a holder: {holder!r}')
        fd = self._locked()
        try:
            live, _ = self._sweep(self._read())
            if not any(c['holder'] == holder and c.get('lane', '') == lane for c in waiting(live, server)):
                self._wait(server, holder, lane, since)
        finally:
            os.close(fd)

    def release(self, token):
        """Give a claim back (a token already gone is fine: release twice, or swept)."""
        fd = self._locked()
        try:
            self._remove(token)
        finally:
            os.close(fd)

    def withdraw(self, server, holder, lane=''):
        """Stop waiting (a restart that gives up)."""
        fd = self._locked()
        try:
            for c in self._read():
                if c['kind'] == 'wait' and c['server'] == server and c['holder'] == holder and c.get('lane', '') == lane:
                    self._remove(c['id'])
        finally:
            os.close(fd)

    def sweep(self):
        """Remove the claims and waits whose holder is gone; returns them."""
        fd = self._locked()
        try:
            return self._sweep(self._read())[1]
        finally:
            os.close(fd)

    def holders(self, server=None):
        """The live claims and waits (swept first), oldest first."""
        fd = self._locked()
        try:
            live, _ = self._sweep(self._read())
        finally:
            os.close(fd)
        return sorted((c for c in live if server is None or c['server'] == server),
                      key=lambda c: (c['since'], c['id']))


def lane_key(c):
    """One lane: its holder and its lane name (two runners that pass one name are two lanes)."""
    return json.dumps(c['holder'], sort_keys=True) + '\0' + c.get('lane', '')


def waiting(live, server):
    """The waits on a server, first in line first: by the time the lane began waiting, then by lane (so on every
    server two lanes share, the same one is first)."""
    return sorted((c for c in live if c['kind'] == 'wait' and c['server'] == server),
                  key=lambda c: (c['since'], lane_key(c), c['id']))
