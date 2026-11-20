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
  that come often enough (or a poll stretched by load) look like a lock held the whole time. The service is read
  (`systemctl show`) by a thread of its own, never by the look: about once a second while it does not run, at once
  when a look finds someone holding the serving lock, and not at all once it runs: its MainPID is kept while that
  process lives, so a look after the claim is a read of /proc/locks, no subprocess (#550, #580). Before the stop,
  and before the run touches anything (its pre hooks), the kit checks that it can see the launcher in /proc/locks
  (handover_blind): where the running build takes the serving lock, that its launcher holds it now, by the very
  look the handover makes; for an older running build, a lock of its own. Where it cannot, it could never tell the
  new launcher, would keep the owner lock HANDOVER_WAIT after the start, and the new server (it waits 250 ms)
  would give up: T3 down about a minute, a pending update trial's database backup lost. The run refuses then,
  T3 not stopped. A build older than
  SERVING_LOCK_SINCE takes no serving lock, and its server would meet the kit's lock, idle and leave the home free
  for a minute: for it the kit lets go just before the start, as before (the marker still holds new launches; the
  backstop names the rest).

The holder (#530): a run that stops T3 and may leave it stopped on purpose (a W1: the home may be half-restored; a
switch until its guard puts T3 back) keeps the owner lock in a process of its own, a transient user unit
(`t3-golive hold`, coldcopy.hold_home), not in the run's process. A run that fails, or is killed, ends; its marker
runs out within 60 s, but the holder keeps the home owned, so a waiting SSH launch stays in its `home-owned` loop (it
has no time limit there) instead of starting its own server on that home. Whoever starts T3 next takes it over
without a gap: the module's starting() hands the holder's lock to the new launcher exactly like the kit's own, and
the next W1 adopts it at its stop step (one holder at most: a second failure starts none). `status` and `check`
name it, `settle` stops it. A reboot drops it: a known gap, kept on purpose. W1 left the unit disabled, so nothing
starts T3 at boot, but an SSH launch of the desktop app after the reboot finds the home free; `status` says so then
(no holder, T3 stopped and disabled, a run unfinished) and names the remedy. A unit that outlives a reboot would be
state on disk that can outlive its reason (the bug #530 avoids by finding the holder by what it is). A holder is
found by what it is, never by a file: a process with the holder's command line for this home among the owner lock's
holders in /proc/locks (find_holder), so a stale pid can never be taken for one. With /proc/locks unreadable the kit
starts no holder (it could not find it again); a holder unit that is active then is named by its unit.

foreign() is only a backstop, for what the two cannot cover: an SSH client older than 0.1.10 (it reads no marker and
probes no lock), or a server started by hand. It names a server that holds the home's owner lock but was not started
by the boot service, so a verify that waits for T3 stops at once with what to do instead of waiting minutes, and the
ways back refuse before they change anything. The kit never stops a server it did not start.
"""
import contextlib, fcntl, hashlib, json, os, signal, sqlite3, sys, tempfile, threading, time

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
# Seconds between two reads of the boot service (systemctl) while it does not run yet, in a thread of its own; a
# look that finds someone holding the serving lock has it read at once; once it runs its MainPID is kept (#550, #580).
SERVICE_REREAD = 1.0
# A build the kit cannot name before the stop: an update caught at the stop starts whatever build the service
# state names then (coldcopy.UpdateAtStop). It counts as one that takes the serving lock (takes_serving_lock).
ANY_BUILD = None
SERVER_LOCK_WAIT_MS = 250  # serverOwnership.ts busy_timeout (contract.test.py): a later handover may leave it idle
HOLDER_TAKE_WAIT = 30.0  # seconds the holder waits for the owner lock (the stopped server may still be exiting)
HOLDER_LET_GO_WAIT = 5.0  # seconds a stopped holder has to let go of the owner lock before it is killed
HOLDER_POLL = 0.002     # its let-go is awaited this finely: the new launcher's server waits 250 ms for the lock
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


def launcher_serving(home, svc, holders=None):
    """The boot service's launcher holds the serving lock: its main process is among the lock's holders. Upstream's
    launcher is the service's main process and the only process that keeps this lock (serviceLauncher.ts
    #claimServing); everyone else only probes it, each from a process of its own (an SSH launch's `__ssh-helper
    home-owned` or node), however long a probe may hold it. svc: the service as adapter.service() reads it. When
    /proc/locks cannot be read this says no, and the kit lets go after HANDOVER_WAIT (a run that stops T3 refuses
    before the stop then: handover_blind). holders: the lock's holders as the caller just read them (lock_holders),
    so the answer is about the look that saw them; None: read them now."""
    pid = svc.get('mainPid') if svc.get('active') else None
    if holders is None:
        holders = lock_holders(os.path.join(home, 'runtime', SERVING_LOCK))
    return bool(pid) and holders is not None and pid in holders


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _runs(svc):
    """The service as read runs, and its main process is still there (a restart gives it a new MainPID)."""
    pid = svc.get('mainPid')
    return bool(svc.get('active') and pid and _alive(pid))


def _read(service):
    try:
        return service()  # the adapter's usual timeout: no look waits for it (#580)
    except Exception:  # noqa: BLE001  (systemctl failed for a moment: read again)
        return {}


def sees_own_lock(home):
    """None when this process sees a POSIX lock of its own (the kind SQLite takes) on a file beside the serving lock
    listed under its own pid in /proc/locks, as launcher_serving needs it to see the launcher's; else why not. The
    file has no name where the filesystem supports that (O_TMPFILE), so not even a kill leaves it behind; elsewhere
    tempfile names it and unlinks it at once, and only a kill in those microseconds leaves an empty file."""
    try:
        f = tempfile.TemporaryFile(dir=os.path.join(home, 'runtime'))
    except OSError:
        return None  # a runtime folder the kit cannot write: the marker cannot be written either (open_gap refuses)
    with f:
        try:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            return f'a lock on a file in {os.path.join(home, "runtime")} cannot be taken: {e}'
        holders = lock_holders(f'/proc/self/fd/{f.fileno()}')
    if holders is None:
        return '/proc/locks cannot be read'
    if os.getpid() not in holders:
        return (f'/proc/locks does not list this process (pid {os.getpid()}) as the holder of its own lock (a /proc '
                'of another pid namespace?)')
    return None


def launcher_unseen(home, service):
    """None when the boot service's launcher holds the serving lock now as the handover will look for it
    (launcher_serving); else why not. service: reads the boot service (adapter.service)."""
    try:
        svc = service()
    except Exception as e:  # noqa: BLE001  (the stop needs systemctl too: refuse with what it said)
        return f'the boot service cannot be read: {e}'
    holders = lock_holders(os.path.join(home, 'runtime', SERVING_LOCK))
    if launcher_serving(home, svc, holders):
        return None
    if holders is None:
        return '/proc/locks cannot be read'
    pid = svc.get('mainPid') if svc.get('active') else None
    if not pid:
        return 'the boot service has no running main process'
    seen = f'pid {", ".join(map(str, sorted(holders)))}' if holders else 'no process'
    return (f'/proc/locks lists {seen} as the holder of runtime/{SERVING_LOCK}, not the boot service\'s main process '
            f'(pid {pid}): a /proc of another pid namespace, a launcher that is not the service\'s main process or '
            'takes another kind of lock, or a launcher whose claim of that lock gave up (`systemctl --user restart '
            't3code.service` has it claim the lock again)')


def handover_blind(home, versions, running=None, service=None):
    """Before the kit stops T3: None, or why it must not (the caller adds '; T3 was not stopped' or '; nothing was
    changed'). versions: the builds this run may start after the stop (ANY_BUILD: one it cannot name); for builds
    that take no serving lock the kit lets go before the start, and it needs to see nobody's lock. running: the
    build T3 runs now, service: reads the boot service (adapter.service). When the running build takes the serving
    lock, its launcher must show as serving now (launcher_unseen, #580): the look the handover depends on, made on
    the real launcher, so a kit in another pid namespace, a launcher that is not the service's main process, or a
    lock of another kind refuse here. Otherwise (an older running build, or none named) the kit must see a lock of
    its own (sees_own_lock, #550)."""
    if not any(takes_serving_lock(v) for v in versions):
        return None
    if running is not None and service is not None and takes_serving_lock(running):
        why = launcher_unseen(home, service)
    else:
        why = sees_own_lock(home)
    if why is None:
        return None
    return (f'the kit cannot tell the boot service\'s launcher by its locks here ({why}): after the start it would '
            f'keep the home\'s owner lock {HANDOVER_WAIT:g} s, the new server gives up after {SERVER_LOCK_WAIT_MS} ms, '
            'and T3 would stay down for about a minute (a pending update trial can lose its database backup)')


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
        # the last 'launcher' handover: at most this many ms between the launcher's claim of the serving lock and the
        # kit letting go of the owner lock (from the start of the kit's last look that did not find it serving)
        self.handover_ms = None
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
        if self.lock is not None and self.lock is not lock and not _same_holder(self.lock, lock):
            self.lock.close()
        self.lock = lock

    def let_go(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def starting(self, fn, version, service):
        """Run fn (it starts T3 `version`: a service start, or `t3 update`), and let go of the owner lock only once
        the new launcher holds its serving lock (or HANDOVER_WAIT after fn returned, or fn failed). service: reads
        the boot service (adapter.service), in a thread of its own (reader). Returns fn's result.
        Afterwards the kit holds no lock on the home; a holder (#530) is left holding unless the launcher took the
        home or the service is active.
        A handover to the launcher measures its delay (handover_ms, an upper bound: the claim came after the start of
        the last look that did not find the launcher serving, and the lock was let go before the clock is read)."""
        self.handover, self.handover_ms = None, None
        if self.lock is not None and not takes_serving_lock(version):
            self.let_go()
            self.handover = 'before the start (a build that takes no serving lock)'
        if self.lock is None:
            return fn()
        done, outcome, returned = threading.Event(), {}, []

        serving = os.path.join(self.home, 'runtime', SERVING_LOCK)

        looked = [time.monotonic()]  # the start of the last look that did not find the launcher serving

        svc, kick, ended = [{}], threading.Event(), threading.Event()  # svc[0]: the service as last read

        def reader():  # the looks never wait for systemctl: a slow read delays only the answer it brings (#580)
            while not ended.is_set():
                kick.clear()
                if not _runs(svc[0]):  # kept while its main process runs (#550)
                    svc[0] = _read(service)
                kick.wait(SERVICE_REREAD)

        def watch():  # HANDOVER_WAIT counts from fn's return (`t3 update` installs first, then restarts T3)
            try:
                while not done.is_set():
                    began = time.monotonic()
                    holders = lock_holders(serving)  # someone holds it: the launcher, or an SSH launch's probe
                    if holders and launcher_serving(self.home, svc[0], holders):
                        outcome['how'] = 'launcher'
                        self.let_go()
                        outcome['ms'] = (time.monotonic() - looked[0]) * 1000
                        return
                    if holders and not _runs(svc[0]):
                        kick.set()  # the launcher may have just claimed it: read the service now
                    looked[0] = began
                    if returned and time.monotonic() >= returned[0] + HANDOVER_WAIT:
                        outcome['how'] = 'timeout'
                        return
                    done.wait(HANDOVER_POLL)
                outcome['how'] = 'start failed'
            finally:
                ended.set()
                kick.set()
                # T3 did not come up (fn raised, or returned without starting it: `t3 update` exited non-zero or timed
                # out, a target that never runs): the holder keeps the home owned past this run (#530). It goes only
                # to a launcher, or when the service runs (an old launcher that took no serving lock).
                if outcome.get('how') != 'launcher' and _stays(self.lock) and not _active(service):
                    self.lock = None
                else:
                    self.let_go()
        # not joined: a read in flight ends by itself (the adapter's timeout), and nothing reads its answer then
        threading.Thread(target=reader, name='golive-service-read', daemon=True).start()
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
            if 'ms' in outcome:
                self.handover_ms = round(outcome['ms'])

    def close(self):
        """Stop renewing, let go of any lock. The marker stays to run out by itself (within 60 s). A holder (#530) is
        left holding: this run ends without having started T3, so the home must stay owned."""
        self._stop.set()
        with self._mutex:
            pass  # a write in flight has finished
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if _stays(self.lock):
            self.lock = None
        self.let_go()

    def served(self):
        """The new server is verified: stop renewing and remove the marker if it is still the kit's. Returns the
        facts for the verify step: 'marker' (what was done to it), 'handover' (how the owner lock was let go),
        'handoverMs' (handover_ms; with 'handoverLate' when it reached the server's 250 ms wait), and 'renewFailed'
        when a renewal failed (the marker may have run out mid-stop; the owner lock still held).
        A newer marker renamed in between the read and the unlink would still be removed: a window of microseconds,
        the same as upstream's own DesktopBackendPool.releaseUpdateMarker."""
        self.close()
        facts = {'marker': self._remove()}
        if self.handover is not None:
            facts['handover'] = self.handover
        if self.handover_ms is not None:  # the journal keeps how close the handover came to the server's wait
            facts['handoverMs'] = self.handover_ms
            if self.handover_ms >= SERVER_LOCK_WAIT_MS:
                facts['handoverLate'] = (f'the kit let go up to {self.handover_ms} ms after the launcher took its '
                                         f'serving lock; its server waits {SERVER_LOCK_WAIT_MS} ms for the owner lock '
                                         'and may have idled (the home is then free until its launcher tries again)')
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
    """fn starts T3 `version`; service reads the boot service (adapter.service): see Gap.starting. A holder an
    earlier run left (#530) is taken over first when this process holds no lock on the home: it lets go to the new
    launcher like the kit's own lock (version None: unknown, waits for a launcher up to HANDOVER_WAIT)."""
    g = _gaps.get(_key(home))
    if g is None or g.lock is None:
        pid = find_holder(home)
        if pid is not None:
            g = g or Gap(home)  # no marker of its own: a guard's start, a W2 or W3 behind a stuck W1
            g.adopt(Holder(home, pid))
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


def holder_unit(home):
    """The holder's transient unit for this home: one name per home, so systemd refuses a second one."""
    return f't3-golive-hold-{hashlib.sha256(os.path.realpath(home).encode()).hexdigest()[:12]}'


def holder_args(home):
    """The kit's arguments the holder runs with (after the kit's own command, `t3-golive`)."""
    return ['hold', '--home', os.path.realpath(home)]


def _argv(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return [os.fsdecode(a) for a in f.read().split(b'\0')[:-1]]
    except OSError:
        return []


def _is_holder(pid, home):
    return _argv(pid)[-3:] == holder_args(home)


def find_holder(home):
    """The pid of the kit's holder that keeps this home owned (#530): a process with the holder's command line for
    this home among the owner lock's holders. None when there is none (a reboot dropped it) or /proc/locks cannot be
    read (the kit then never starts one: it could not tell it holds)."""
    pids = lock_holders(os.path.join(home, 'userdata', OWNER_LOCK))
    for pid in sorted(pids or ()):
        if pid != os.getpid() and _is_holder(pid, home):
            return pid
    return None


class Holder:
    """The owner lock as the holder keeps it, in place of the kit's own connection: close() stops the holder and
    returns once it no longer holds the lock. Gap.close() leaves it holding (stays)."""
    stays = True

    def __init__(self, home, pid):
        self.home, self.pid = home, pid

    def close(self):
        pid, self.pid = self.pid, None
        if pid is None or not _is_holder(pid, self.home):  # gone already (its pid may be another process's since)
            return
        owner = os.path.join(self.home, 'userdata', OWNER_LOCK)
        for sig, wait in ((signal.SIGTERM, HOLDER_LET_GO_WAIT), (signal.SIGKILL, HOLDER_LET_GO_WAIT)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            end = time.monotonic() + wait
            while time.monotonic() < end:
                holders = lock_holders(owner)
                if holders is None or pid not in holders:
                    return
                time.sleep(HOLDER_POLL)


def _stays(lock):
    return getattr(lock, 'stays', False)


def _same_holder(a, b):
    return _stays(a) and _stays(b) and a.pid is not None and a.pid == b.pid


def _active(service):
    """Read after a start that did not reach a launcher, with the adapter's usual timeout (a slow systemctl must
    not keep the home held behind a T3 that runs)."""
    try:
        return bool(service().get('active'))
    except Exception:  # noqa: BLE001  (cannot tell: the safe side keeps the home owned)
        return False


def stop_holder(home):
    """Stop the holder of this home, if any (settle: the owner says the home is right); returns its pid or None."""
    pid = find_holder(home)
    if pid is not None:
        Holder(home, pid).close()
    return pid


def holder_to_service(home, svc):
    """A T3 the boot service runs needs the home: a holder an earlier run left lets go of it (a W1 killed after its
    update restarted T3, verified again later). Returns its pid, or None (none, or the service is not active: the
    holder then keeps the home)."""
    return stop_holder(home) if svc.get('active') else None


def holder_said(home, pid):
    return (f'the kit\'s holder {holder_unit(home)} (pid {pid}) keeps the home owned, so no SSH launch starts a server '
            'on it: a W1 or switch that stopped T3 and did not end ok left it (the home may be half-restored); the '
            'same `t3-golive back` takes it over, or put the home right and `t3-golive settle` stops it; do not start '
            't3code.service by hand while it holds (its server would idle)')


def holder_seen(home, unit_active):
    """What `status` and `check` say about the holder, or None when there is none: found in /proc/locks, or, when
    that cannot be read, its unit is active (unit_active: adapter.unit_active; it may raise)."""
    pid = find_holder(home)
    if pid is not None:
        return holder_said(home, pid)
    if lock_holders(os.path.join(home, 'userdata', OWNER_LOCK)) is not None:
        return None
    unit = holder_unit(home)
    try:
        if not unit_active(unit):
            return None
    except Exception:  # noqa: BLE001  (cannot tell: say nothing more than the failed read)
        return None
    return (f'the kit\'s holder unit {unit} is active, and /proc/locks cannot be read, so the kit cannot tell what it '
            f'holds: it may keep the home owned; once the home is right, `systemctl --user stop {unit}`')


def serve_holder(home, wait=None):
    """What the holder unit runs (`t3-golive hold --home <home>`): take the home's owner lock (waiting up to
    HOLDER_TAKE_WAIT for a stopping server) and keep it until stopped (SIGTERM, SIGINT, SIGHUP). Returns the exit
    code: 0 once let go, 1 when the lock could not be taken."""
    stops = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    signal.pthread_sigmask(signal.SIG_BLOCK, stops)  # from the start: a stop never comes between a look and a wait
    from golive import coldcopy
    try:
        lock = coldcopy.hold_owner_lock(os.path.join(home, 'userdata'), HOLDER_TAKE_WAIT if wait is None else wait)
    except (TimeoutError, OSError, sqlite3.Error) as e:
        print(f't3-golive hold: {e}', file=sys.stderr, flush=True)
        return 1
    print(f'holding {os.path.join(home, "userdata", OWNER_LOCK)} until stopped', flush=True)
    signal.sigwait(stops)
    lock.close()
    return 0


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
