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
  path, JSON {"startedAt", "expiresAt"} in ms with the same 60 s lifetime, written to <marker>.tmp and renamed;
  startedAt one second back, so a wall clock that steps back does not void it), and
  a thread writes it again every RENEW seconds. served() (once the new server is verified) stops that and removes the
  marker only if it still holds what the kit wrote last (a newer upstream update's marker is left alone). A run that
  dies (kill, crash, power) stops renewing: its marker runs out by itself within 60 s, so no desktop waits forever.
- Gap.adopt(lock) / Gap.starting(fn): the owner lock the kit took after the stop is not let go before the start.
  starting() starts T3 (fn) and lets go of the lock only once the new launcher holds its serving lock (launchers
  from SERVING_LOCK_SINCE on take it before they start their server), so the home is owned the whole time; after
  HANDOVER_WAIT it lets go anyway (a launcher that never comes up). "The launcher holds it" means the boot
  service's main process is among the lock's holders in /proc/locks: an SSH launch's own `home-owned` probe takes
  the same lock for a moment, in a process of its own, and that probe must not free the home in the middle of
  `t3 update`. Only the holder's identity tells the two apart: a poll sees the lock held at one instant, and probes
  that come often enough (or a poll stretched by load) look like a lock held the whole time. A build older than
  SERVING_LOCK_SINCE takes no serving lock, and its server would meet the kit's lock, idle and leave the home free
  for a minute: for it the kit lets go just before the start, as before (the marker still holds new launches; the
  backstop names the rest).

foreign() is only a backstop, for what the two cannot cover: an SSH client older than 0.1.10 (it reads no marker and
probes no lock), or a server started by hand. It names a server that holds the home's owner lock but was not started
by the boot service, so a verify that waits for T3 stops at once with what to do instead of waiting minutes, and the
ways back refuse before they change anything. The kit never stops a server it did not start.
"""
import contextlib, json, os, sqlite3, threading, time

from golive import live

OWNER_LOCK = 'server-owner.sqlite'    # under userdata/ (coldcopy.OWNER_LOCK)
MARKER = 'desktop-update-in-progress'  # contracts DESKTOP_UPDATE_IN_PROGRESS_MARKER_FILE (tests/contract.test.py)
TTL_MS = 60_000                        # contracts DESKTOP_UPDATE_IN_PROGRESS_TTL_MS
SERVING_LOCK = 'service-serving.lock'  # serviceProtocol.ts SERVICE_SERVING_LOCK_FILE, under runtime/
# The first upstream build whose launcher takes the serving lock before it starts its server (serviceLauncher.ts
# #claimServing; the nightlies before it, and 0.1.9, take none).
SERVING_LOCK_SINCE = '0.1.10-nightly.20261114.4695'
RENEW = 15.0          # seconds between two writes of the marker: four tries inside its 60 s
# The marker says it started this long ago. Upstream's reader drops a marker that started after its own now, and a
# wall clock can step back (a WSL2 guest's resync: 150 ms seen, #546): written at "now", the marker would be void
# for SSH launches right after the stop, until the clock caught up. Its lifetime from now is TTL_MS minus this.
CLOCK_SLACK_MS = 1000
HANDOVER_WAIT = 20.0  # seconds the kit keeps the owner lock after a start while no launcher holds the serving lock
# Seconds between two looks for the launcher. It holds the serving lock from #claimServing on, and its server then
# waits 250 ms for the owner lock (serverOwnership.ts busy_timeout): the kit lets go well inside that.
HANDOVER_POLL = 0.02
FOREIGN_GRACE = 10.0  # seconds a server not started by the boot service must stay before verify stops on it
_now_ms = lambda: int(time.time() * 1000)  # noqa: E731  (Date.now(), as upstream's reader compares)


def marker_path(home):
    return os.path.join(home, 'runtime', MARKER)


def publish(home):
    """Write the marker as DesktopUpdateMarker.publish does (started CLOCK_SLACK_MS ago); return the bytes written."""
    path = marker_path(home)
    started = _now_ms() - CLOCK_SLACK_MS
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


def launcher_serving(home, svc):
    """The boot service's launcher holds the serving lock: its main process is among the lock's holders. Upstream's
    launcher is the service's main process and the only process that keeps this lock (serviceLauncher.ts
    #claimServing); everyone else only probes it, each from a process of its own (an SSH launch's `__ssh-helper
    home-owned` or node), however long a probe may hold it. svc: the service as adapter.service() reads it. When
    /proc/locks cannot be read this says no, and the kit lets go after HANDOVER_WAIT."""
    pid = svc.get('mainPid') if svc.get('active') else None
    return bool(pid) and holds_lock(pid, os.path.join(home, 'runtime', SERVING_LOCK)) is True


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
        self.error = None    # the last renewal that failed (the next one may work)
        self.failed = 0      # renewals that failed in all
        self.lock = None     # the owner lock, kept from the stop to the start
        # how the last starting() let go of the owner lock: 'launcher' (it held its serving lock), 'timeout' (none did
        # within HANDOVER_WAIT), 'start failed', 'before the start' (a build that takes no serving lock); None: none
        self.handover = None
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
                    self.written = publish(self.home)
                except OSError as e:  # the owner lock still covers waiting launches; the journal says it (served)
                    self.error, self.failed = str(e), self.failed + 1

    def adopt(self, lock):
        """Keep the owner lock until starting() hands the home over (one lock at a time)."""
        if self.lock is not None and self.lock is not lock:
            self.lock.close()
        self.lock = lock

    def let_go(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def starting(self, fn, version, service):
        """Run fn (it starts T3 `version`: a service start, or `t3 update`), and let go of the owner lock only once
        the new launcher holds its serving lock (or HANDOVER_WAIT after fn returned, or fn failed). service: reads
        the boot service (adapter.service). Returns fn's result. Afterwards the kit holds no lock on the home."""
        self.handover = None
        if self.lock is not None and not takes_serving_lock(version):
            self.let_go()
            self.handover = 'before the start (a build that takes no serving lock)'
        if self.lock is None:
            return fn()
        done, outcome, returned = threading.Event(), {}, []

        serving = os.path.join(self.home, 'runtime', SERVING_LOCK)

        def watch():  # HANDOVER_WAIT counts from fn's return (`t3 update` installs first, then restarts T3)
            try:
                while not done.is_set():
                    if lock_holders(serving):  # someone holds it: the launcher, or an SSH launch's probe
                        try:
                            svc = service()
                        except Exception:  # noqa: BLE001  (systemctl failed for a moment: look again)
                            svc = {}
                        if launcher_serving(self.home, svc):
                            outcome['how'] = 'launcher'
                            return
                    if returned and time.monotonic() >= returned[0] + HANDOVER_WAIT:
                        outcome['how'] = 'timeout'
                        return
                    done.wait(HANDOVER_POLL)
                outcome['how'] = 'start failed'
            finally:
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
            self.handover = outcome.get('how', 'start failed')

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
        """The new server is verified: stop renewing and remove the marker if it is still the kit's. Returns the
        facts for the verify step: 'marker' (what was done to it), 'handover' (how the owner lock was let go), and
        'renewFailed' when a renewal failed (the marker may have run out mid-stop; the owner lock still held).
        A newer marker renamed in between the read and the unlink would still be removed: a window of microseconds,
        the same as upstream's own DesktopBackendPool.releaseUpdateMarker."""
        self.close()
        facts = {'marker': self._remove()}
        if self.handover is not None:
            facts['handover'] = self.handover
        if self.failed:
            facts['renewFailed'] = f'{self.failed} time(s), last: {self.error}'
        return facts

    def _remove(self):
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


def starting(home, fn, version, service):
    """fn starts T3 `version`; service reads the boot service (adapter.service): see Gap.starting."""
    g = _gaps.get(_key(home))
    return fn() if g is None else g.starting(fn, version, service)


def served(home):
    """The new server is verified: end the gap; returns the facts for the verify step (Gap.served; marker None: no
    gap was open)."""
    g = _gaps.pop(_key(home), None)
    return {'marker': None} if g is None else g.served()


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


def lock_holders(path):
    """The pids that hold a lock on the file `path` (SQLite's locks are POSIX locks, listed with their process in
    /proc/locks): an empty set when none does or the file is missing, None when /proc/locks cannot be read."""
    try:
        ino = os.stat(path).st_ino
    except OSError:
        return set()
    try:
        with open('/proc/locks', encoding='ascii', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    pids = set()
    for line in lines:  # "1: POSIX  ADVISORY  WRITE 4242 08:02:131 1073741824 1073741825"; waiters: "1: -> POSIX ..."
        parts = line.split()[1:]
        if len(parts) < 5 or parts[0] == '->':
            continue
        try:
            if int(parts[4].rsplit(':', 1)[-1]) == ino:
                pids.add(int(parts[3]))
        except ValueError:
            continue
    return pids


def holds_lock(pid, path):
    """True when process `pid` holds a lock on the file `path`, False when it does not or the file is missing, None
    when /proc/locks cannot be read."""
    holders = lock_holders(path)
    return None if holders is None else pid in holders


def foreign(home, svc, old_pid=None):
    """{'pid', 'ppid', 'what'} of a live server that holds the home's owner lock and that the boot service did not
    start (not a child of its main process), named by the home's runtime record; or None. old_pid: the server the
    kit itself stopped (it may still be exiting). A stale record (its server was killed, its pid reused since) holds
    no lock, so it is never named: the kit must not tell anyone to kill an unrelated process."""
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
    owner = os.path.join(home, 'userdata', OWNER_LOCK)
    holds = holds_lock(pid, owner)
    if holds is None:  # no /proc/locks: someone at least holds the home
        holds = _lock_held(owner)
    if not holds:
        return None
    return {'pid': pid, 'ppid': ppid, 'what': _cmdline(pid) or exe}


def described(found):
    return (f'a T3 server the boot service did not start holds the home: pid {found["pid"]} ({found["what"]}), parent '
            f'{found["ppid"]}; most likely a T3 desktop app connected over SSH started it while T3 was stopped (an SSH '
            f'client older than 0.1.10 honours neither the update marker nor the home\'s locks), or someone started '
            f'it by hand')


def remedy(found, then):
    """What the owner does, in order: the desktop app first, or an old client starts the next server within a
    second of the kill."""
    return (f'the kit stops no server it did not start: first quit or disconnect the T3 desktop app connected over '
            f'SSH (or update it to 0.1.10 or later), then `kill {found["pid"]}`, then {then}')


RESTART = '`systemctl --user restart t3code.service`'
KEEP_STOPPED = ('do not start t3code.service: T3 stays stopped and is not started at boot on purpose (the home may '
                'be half-restored)')


def said(found, svc, again=None, stopped=False):
    """The sentence for a foreign server: what it is, and the remedy for where T3 stands. stopped: a way back
    stopped T3 and has not ended ok (the journal says so); a disabled service says the same. again: the command to
    run once it is gone (default: `t3-golive status`)."""
    if stopped or not svc.get('enabled', True):
        then = f'run {again or "the same `t3-golive back`"} again; {KEEP_STOPPED}'
    else:
        then = f'{RESTART}, then {f"{again} again" if again else "`t3-golive status`"}'
    return f'{described(found)}; {remedy(found, then)}'


class ForeignWatch:
    """foreign() that has held for FOREIGN_GRACE seconds on the same pid (a server of the boot service's last run,
    still exiting, is not one). check() returns it then, else None."""

    def __init__(self, home, old_pid=None):
        self.home, self.old_pid, self.seen = home, old_pid, None

    def check(self, svc):
        found = foreign(self.home, svc, self.old_pid)
        if found is None:
            self.seen = None
            return None
        if self.seen is None or self.seen[0] != found['pid']:
            self.seen = (found['pid'], time.monotonic())
        return found if time.monotonic() - self.seen[1] >= FOREIGN_GRACE else None
