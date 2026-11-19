"""The gap: while the kit has the boot service stopped, no other T3 server may take the home.

A T3 desktop app connected over SSH runs upstream's launch script (packages/ssh/src/tunnel.ts) on this machine as
soon as its server goes away. That script starts its own `t3 serve` on the home unless one of two things holds:

1. the update marker <home>/runtime/desktop-update-in-progress is valid: it then waits (at most 60 s per launch),
   adopting the server that comes up. Upstream's own self-update writes it (apps/server/src/cloud/selfUpdate.ts with
   desktopUpdate/DesktopUpdateMarker.ts); the launch reads it with the SSH client's own runtime
   (`__ssh-helper update-remaining`, clients from t3 0.1.10 on);
2. the home is owned (`__ssh-helper home-owned`): a boot service launcher holds runtime/service-serving.lock, or a
   server (or the kit) holds userdata/server-owner.sqlite. A launch whose marker wait ran out waits in this loop
   while the home stays owned, and starts its own server the moment it sees the home free.

So the kit keeps both true for the whole stop, like upstream's update does (root cause of #513 and the 11-19
switch: the kit wrote no marker, and let go of the owner lock BEFORE starting the service, so a launch that had
waited through the copy found the home free for the second before the new launcher took it):

- Gap.open(): before the kit stops T3, it writes upstream's marker exactly as DesktopUpdateMarker.publish does (same
  path, JSON {"startedAt", "expiresAt"} in ms with the same 60 s lifetime, written to <marker>.tmp and renamed), and
  a thread writes it again every RENEW seconds. served() (once the new server is verified) stops that and removes the
  marker only if it still holds what the kit wrote last (a newer upstream update's marker is left alone). A run that
  dies (kill, crash, power) stops renewing: its marker runs out by itself within 60 s, so no desktop waits forever.
- Gap.adopt(lock) / Gap.starting(fn): the owner lock the kit took after the stop is not let go before the start.
  starting() starts T3 (fn) and lets go of the lock only once the new launcher holds its serving lock (launchers
  from SERVING_LOCK_SINCE on take it before they start their server), so the home is owned the whole time; after
  HANDOVER_WAIT it lets go anyway (a launcher that never comes up). A build older than SERVING_LOCK_SINCE takes no
  serving lock, and its server would meet the kit's lock, idle and leave the home free for a minute: for it the kit
  lets go just before the start, as before (the marker still holds new launches; the backstop names the rest).

foreign() is only a backstop, for what the two cannot cover: an SSH client older than 0.1.10 (it reads no marker and
probes no lock), or a server started by hand. It names a server that serves the home but was not started by the boot
service, so a verify that waits for T3 stops at once with what to do instead of waiting minutes. The kit never stops
a server it did not start.
"""
import contextlib, json, os, sqlite3, threading, time

from golive import live

MARKER = 'desktop-update-in-progress'  # contracts DESKTOP_UPDATE_IN_PROGRESS_MARKER_FILE (tests/contract.test.py)
TTL_MS = 60_000                        # contracts DESKTOP_UPDATE_IN_PROGRESS_TTL_MS
SERVING_LOCK = 'service-serving.lock'  # serviceProtocol.ts SERVICE_SERVING_LOCK_FILE, under runtime/
# The first upstream build whose launcher takes the serving lock before it starts its server (serviceLauncher.ts
# #claimServing; the nightlies before it, and 0.1.9, take none).
SERVING_LOCK_SINCE = '0.1.10-nightly.20261114.4695'
RENEW = 15.0          # seconds between two writes of the marker: four tries inside its 60 s
HANDOVER_WAIT = 20.0  # seconds the kit keeps the owner lock after a start while no launcher holds the serving lock
HANDOVER_POLL = 0.02  # a new launcher's server needs far longer than this to start and take the owner lock
FOREIGN_GRACE = 10.0  # seconds a server not started by the boot service must stay before verify stops on it
_now_ms = lambda: int(time.time() * 1000)  # noqa: E731  (Date.now(), as upstream's reader compares)


def marker_path(home):
    return os.path.join(home, 'runtime', MARKER)


def publish(home):
    """Write the marker as DesktopUpdateMarker.publish does; return the bytes written."""
    path = marker_path(home)
    started = _now_ms()
    data = json.dumps({'startedAt': started, 'expiresAt': started + TTL_MS}, separators=(',', ':')).encode()
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.rename(tmp, path)  # a launch never reads a half-written marker
    return data


def remaining_ms(home):
    """What upstream's reader (DesktopUpdateMarker.remainingWait, and the launch script's node fallback) says:
    the ms an SSH launch still waits, or None when the marker is missing, unreadable, expired or from the future."""
    try:
        with open(marker_path(home), encoding='utf-8') as f:
            m = json.load(f)
        started, expires = m['startedAt'], m['expiresAt']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    now = _now_ms()
    if not all(type(v) in (int, float) and v == v and abs(v) != float('inf') for v in (started, expires)):
        return None
    if started <= now and expires > now and expires > started:
        return min(expires - now, TTL_MS)
    return None


def _lock_held(path):
    """True when another process holds this SQLite lock (upstream's serviceLauncherIsRunning: a missing file is
    not held, and the probe never creates one)."""
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(f'file:{path}?mode=rw', uri=True, timeout=0, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        conn.execute('BEGIN EXCLUSIVE')
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()


def launcher_serving(home):
    return _lock_held(os.path.join(home, 'runtime', SERVING_LOCK))


def takes_serving_lock(version):
    """The launcher of this build takes the serving lock before its server (a version that cannot be compared: yes,
    the kit then waits HANDOVER_WAIT at most)."""
    try:
        return live.compare_versions(str(version), SERVING_LOCK_SINCE) >= 0
    except ValueError:
        return True


class Gap:
    """One stop of T3 by this process, from before the stop until the new server is verified (see the module)."""

    def __init__(self, home):
        self.home = home
        self.written = None  # the marker's bytes as the kit wrote them last
        self.error = None    # the last write that failed (the next one may work)
        self.lock = None     # the owner lock, kept from the stop to the start
        self.handed_to_launcher = False  # the last starting() let go once a launcher held its serving lock
        self._stop = threading.Event()
        self._thread = None
        self._mutex = threading.Lock()

    def open(self):
        """Write the marker (raises OSError when it cannot: T3 must not be stopped then) and keep it fresh."""
        with self._mutex:
            self.written = publish(self.home)
        if self._thread is None:
            self._thread = threading.Thread(target=self._renew, name='golive-marker', daemon=True)
            self._thread.start()
        return marker_path(self.home)

    def _renew(self):
        while not self._stop.wait(RENEW):
            with self._mutex:
                if self._stop.is_set():
                    return
                try:
                    self.written, self.error = publish(self.home), None
                except OSError as e:
                    self.error = str(e)

    def adopt(self, lock):
        """Keep the owner lock until starting() hands the home over (one lock at a time)."""
        if self.lock is not None and self.lock is not lock:
            self.lock.close()
        self.lock = lock

    def let_go(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def starting(self, fn, version):
        """Run fn (it starts T3 `version`: a service start, or `t3 update`), and let go of the owner lock only once
        the new launcher holds its serving lock (or HANDOVER_WAIT after fn returned, or fn failed). Returns fn's
        result. Afterwards the kit holds no lock on the home."""
        if self.lock is not None and not takes_serving_lock(version):
            self.let_go()
        if self.lock is None:
            return fn()
        done, outcome, returned = threading.Event(), {}, []

        def watch():  # HANDOVER_WAIT counts from fn's return (`t3 update` installs first, then restarts T3)
            while not done.is_set() and not launcher_serving(self.home):
                if returned and time.monotonic() >= returned[0] + HANDOVER_WAIT:
                    break
                done.wait(HANDOVER_POLL)
            outcome['serving'] = launcher_serving(self.home)
            self.let_go()
        watcher = threading.Thread(target=watch, name='golive-handover', daemon=True)
        watcher.start()
        try:
            result = fn()
            returned.append(time.monotonic())
            return result
        except BaseException:
            done.set()  # the start failed: nothing to hand over to
            raise
        finally:
            watcher.join()
            self.handed_to_launcher = outcome.get('serving', False)

    def close(self):
        """Stop renewing, let go of any lock. The marker stays to run out by itself (within 60 s)."""
        self._stop.set()
        with self._mutex:
            pass  # a write in flight has finished
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.let_go()

    def served(self):
        """The new server is verified: stop renewing and remove the marker if it is still the kit's. Returns what
        was done, for the journal."""
        self.close()
        path = marker_path(self.home)
        try:
            with open(path, 'rb') as f:
                now = f.read()
        except FileNotFoundError:
            return 'gone'
        except OSError as e:
            return f'left: {e}'
        if self.written is None or now != self.written:
            return 'left: not the kit\'s (a newer update wrote it)'
        try:
            os.unlink(path)
        except FileNotFoundError:
            return 'gone'
        except OSError as e:
            return f'left: {e}'
        return 'removed'


# One gap per home in this process: the stop, the start and the verify are far apart in the code (copy, switch, the
# ways back and their guards), and a run is one process holding the kit's lock.
_gaps = {}


def _key(home):
    return os.path.realpath(home)


def open_gap(home):
    """The gap for this home, its marker written (and kept fresh) from now on. Raises OSError."""
    g = _gaps.get(_key(home))
    if g is None:
        g = _gaps[_key(home)] = Gap(home)
    try:
        g.open()
    except BaseException:
        close(home)
        raise
    return g


def adopt(home, lock):
    """Hand the owner lock to an open gap (True), or False when none is open (the caller closes it)."""
    g = _gaps.get(_key(home))
    if g is None:
        return False
    g.adopt(lock)
    return True


def held(home):
    """The owner lock an open gap of this home holds, or None."""
    g = _gaps.get(_key(home))
    return None if g is None else g.lock


def starting(home, fn, version):
    """fn starts T3 `version`: see Gap.starting."""
    g = _gaps.get(_key(home))
    return fn() if g is None else g.starting(fn, version)


def served(home):
    """The new server is verified: end the gap; returns what happened to the marker (None: no gap was open)."""
    g = _gaps.pop(_key(home), None)
    return None if g is None else g.served()


def close(home):
    """End the gap without removing the marker (it runs out within 60 s); safe when none is open."""
    g = _gaps.pop(_key(home), None)
    if g is not None:
        g.close()


@contextlib.contextmanager
def run_scope(home):
    """Around one run (copy, switch, back): a gap it leaves open (it did not verify T3) is closed at its end, the
    marker left to run out. A run inside another in the same process (a guard tried while a run holds the kit's
    lock) leaves the outer run's gap alone."""
    mine = _key(home) not in _gaps
    try:
        yield
    finally:
        if mine:
            close(home)


# --- the backstop ---

def _cmdline(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return ' '.join(f.read().replace(b'\0', b' ').decode('utf-8', 'replace').split())[:160]
    except OSError:
        return ''


def foreign(home, svc, old_pid=None):
    """A live server the home's runtime record names that the boot service did not start (not a child of its main
    process), as a sentence, or None. old_pid: the server the kit itself stopped (it may still be exiting)."""
    try:
        with open(os.path.join(home, 'userdata', 'server-runtime.json'), encoding='utf-8') as f:
            pid = json.load(f).get('pid')
    except (OSError, ValueError, AttributeError):
        return None
    if type(pid) is not int or pid <= 0 or pid == old_pid:
        return None
    proc = live._proc(pid)
    if proc is None:
        return None
    exe, ppid = proc
    if svc.get('active') and ppid == svc.get('mainPid'):
        return None
    cmd = _cmdline(pid)
    return (f'a T3 server the boot service did not start serves the home: pid {pid} ({cmd or exe}), parent {ppid}; '
            f'most likely a T3 desktop app connected over SSH started it while T3 was stopped (an SSH client older '
            f'than 0.1.10 honours neither the update marker nor the home\'s locks), and the service then waits for '
            f'the home. The kit stops no server it did not start: stop that one (kill {pid}), then at once '
            f'`systemctl --user restart t3code.service`, then `t3-golive status`')


class ForeignWatch:
    """foreign() that has held for FOREIGN_GRACE seconds on the same pid (a server of the boot service's last run,
    still exiting, is not one)."""

    def __init__(self, home, old_pid=None):
        self.home, self.old_pid, self.seen = home, old_pid, None

    def check(self, svc):
        why = foreign(self.home, svc, self.old_pid)
        if why is None:
            self.seen = None
            return None
        if self.seen is None or self.seen[0] != why:
            self.seen = (why, time.monotonic())
        return why if time.monotonic() - self.seen[1] >= FOREIGN_GRACE else None
