#!/usr/bin/env python3
"""claims: who holds a test server now, shared or alone. One folder of claim files and one lock; general (no
project names: a server is any name the caller gives).

  - A claim is `shared` (runs beside other shared claims: test runs that do not interfere) or `alone` (nothing else
    on that server: a restart, a reset of its data). Conflict table: shared + shared is fine; alone conflicts with
    everything.
  - A waiting restart goes first: an `alone` claim made with restart=True that cannot be granted leaves a wait entry,
    and while one waits no new claim on that server is granted (shared or alone) except to the oldest waiter, so a
    stream of shared runs cannot starve it.
  - A holder is a process (pid and its start time, so a reused pid is not the same holder) or a service unit; a
    claim or wait whose holder is gone is swept (removed) at every call. A holder it cannot check counts as alive.
  - Every call reads and writes under one lock (flock on <folder>/.lock); claim files are written whole (a temporary
    file renamed into place), so a reader never sees half a claim.
  - Fails closed: a folder it cannot read or a claim file it cannot parse refuses every claim (never a grant on a
    guess), naming why. An empty or missing folder holds nothing.
"""
import fcntl, json, os, subprocess, time

MODES = ('shared', 'alone')
SUFFIX = '.claim.json'


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
    """This process as a holder."""
    return {'pid': os.getpid(), 'start': start_time(os.getpid())}


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
    except ProcessLookupError:
        return False
    except PermissionError:
        pass   # another user's process: it is there
    want = holder.get('start')
    if want is None or not os.path.isdir('/proc'):
        return True
    got = start_time(pid)
    return got is None or got == want


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
        try:
            names = sorted(n for n in os.listdir(self.folder) if n.endswith(SUFFIX) and not n.startswith('.'))
        except OSError as e:
            raise ClaimsError(f'cannot read the claims folder {self.folder} ({e.strerror or e})')
        out = []
        for n in names:
            path = os.path.join(self.folder, n)
            try:
                with open(path, encoding='utf-8') as f:
                    c = json.load(f)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                raise ClaimsError(f'cannot read the claim {path} ({type(e).__name__}); remove it once no lane holds it')
            if not (isinstance(c, dict) and c.get('kind') in ('claim', 'wait') and c.get('mode') in MODES and
                    isinstance(c.get('server'), str) and isinstance(c.get('holder'), dict)):
                raise ClaimsError(f'the claim {path} is not one this version writes; remove it once no lane holds it')
            c['id'] = n[:-len(SUFFIX)]
            out.append(c)
        return out

    def _write(self, entry):
        ident = f'{time.time_ns():020d}-{os.getpid()}-{os.urandom(3).hex()}'
        body = {k: v for k, v in entry.items() if k != 'id'}
        tmp = os.path.join(self.folder, f'.tmp-{ident}')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(body, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(self.folder, ident + SUFFIX))
        return ident

    def _remove(self, ident):
        try:
            os.unlink(os.path.join(self.folder, ident + SUFFIX))
        except FileNotFoundError:
            pass

    def _sweep(self, entries):
        live, gone = [], []
        for c in entries:
            (live if self.alive(c['holder']) else gone).append(c)
        for c in gone:
            self._remove(c['id'])
        return live, gone

    # ---------------------------------------------------------------- the calls

    def claim(self, server, mode, holder, lane='', restart=False, admit=None):
        """(token or None, why). token: the claim's id, for release(). restart: an alone claim that waits first in
        line when it cannot be granted. admit(live lanes) -> (ok, why): the pool's admission, checked last."""
        if mode not in MODES:
            raise ValueError(f'mode must be one of {MODES}')
        if restart and mode != 'alone':
            raise ValueError('a restart claims the server alone')
        fd = self._locked()
        try:
            live, _ = self._sweep(self._read())
            mine = lambda c: c['holder'] == holder and c.get('lane', '') == lane   # noqa: E731
            held = [c for c in live if c['kind'] == 'claim' and c['server'] == server]
            waits = sorted((c for c in live if c['kind'] == 'wait' and c['server'] == server),
                           key=lambda c: (c['since'], c['id']))
            why = None
            clash = [c for c in held if conflicts(c['mode'], mode)]
            if clash:
                why = (f'{server} is held {clash[0]["mode"]} by lane {clash[0].get("lane") or "?"}'
                       f'{f" (+{len(clash) - 1} more)" if len(clash) > 1 else ""}')
            elif waits and not mine(waits[0]):
                why = f'{server}: a waiting restart goes first (lane {waits[0].get("lane") or "?"})'
            elif admit is not None:
                ok, said = admit(len({c.get('lane') or c['id'] for c in live if c['kind'] == 'claim'}))
                why = None if ok else said
            if why:
                if restart and not any(mine(w) for w in waits):
                    self._write({'kind': 'wait', 'server': server, 'mode': 'alone', 'holder': holder, 'lane': lane,
                                 'since': self.clock()})
                return None, why
            for w in waits:
                if mine(w):
                    self._remove(w['id'])
            token = self._write({'kind': 'claim', 'server': server, 'mode': mode, 'holder': holder, 'lane': lane,
                                 'since': self.clock()})
            return token, f'{server} claimed {mode}'
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
        return sorted((c for c in live if server is None or c['server'] == server), key=lambda c: (c['since'], c['id']))
