#!/usr/bin/env python3
"""A nightly lock (fork-ci nightly-lock; usage and config: README.md next to this file): long and short heavy steps
on one shared CI machine (a weekly mutation run, a nightly soak) never run at the same time, and the short one goes
first.

    python3 -I nightly_lock.py [--name NAME] [--dir DIR] [--first] (--wait S | --until EPOCH) \
        [--gave-up-note FILE] [--github-output] -- <command...>

Runs the command while it holds an exclusive flock on DIR/NAME.lock, and exits with the command's exit code.

Who waits for whom:
- `--first` (the short step) waits in the kernel and is let in before every caller without it: while it waits it holds
  DIR/NAME-first.lock shared, and a caller without --first never takes the lock while that is held. So a --first caller
  waits at most for the one step that holds the lock now, and is never overtaken.
- A caller without --first takes the lock only when it is free and no --first caller waits; it looks again every POLL_S
  seconds, and says at its first look (and every SAY_EVERY_S) why it waits.
- --wait S or --until EPOCH bounds the wait. When it runs out: exit 75, the command does not run, and the line names
  who held the lock. A shared `concurrency` group would instead cancel a waiting run silently.

State: the locks are flocks, so the kernel frees them when the holder ends: a cancelled job, a killed runner, a reboot.
No stale lock, nothing to clean up, and a missing file (or folder) is made again; a lock file replaced between open and
flock is noticed. The lock fd is O_CLOEXEC: the command and its children never hold it. DIR/NAME.holder only says in
the log who took the lock last; it is never trusted for anything (a killed job leaves its line). Every take touches
the lock file, so a /tmp age limit never removes one in use.

The lock is held until the command AND every process it started have ended, never only until the command's first
process ends: a workflow runs `bash -ec '<several commands>'`, and a signal that ends bash leaves its child running. So
the command runs in its own process group with this process as its child subreaper (Linux: orphans of the command come
back to this process, whatever session they made). SIGTERM, SIGINT and SIGHUP go to the command's process group; once
the command has ended, whatever it left running gets SIGTERM, GRACE_S later SIGKILL (at once on a second signal), and
only then is the lock let go. Exit code: the command's, 128+N when a signal N ended it. Only a SIGKILL of this process
itself frees the lock early (the kernel does it).
`--until` that has already passed runs nothing (exit 75). A lock that cannot be used (the folder is a file, a lock file
cannot be opened) runs nothing (exit 71). `--gave-up-note FILE`: on exit 75 one line says why; the file
is removed at the start, so an old note never stays. `--github-output`: on exit 75 that line is also the step output
`gave_up` ($GITHUB_OUTPUT).
"""
import argparse, contextlib, ctypes, errno, fcntl, os, re, signal, subprocess, sys, threading, time

LOCK_DIR = '/tmp'   # --dir
NAME = 'nightly'   # --name: NAME.lock, NAME-first.lock (held shared by every --first caller while it waits), NAME.holder
POLL_S = float(os.environ.get('NIGHTLY_LOCK_POLL_S', '') or 30)   # env: tests only
SAY_EVERY_S = 600
GRACE_S = float(os.environ.get('NIGHTLY_LOCK_GRACE_S', '') or 30)   # leftovers: SIGTERM, then SIGKILL; env: tests only
PR_SET_CHILD_SUBREAPER = 36
EXIT_WAIT = 75
EXIT_LOCK = 71   # the lock itself cannot be used (EX_OSERR): never "ran", never "gave up waiting"
NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


def lock_names(name):
    return f'{name}.lock', f'{name}-first.lock', f'{name}.holder'


LOCK, WANT, HOLDER = lock_names(NAME)


def path(name):
    return os.path.join(LOCK_DIR, name)


def open_lock(p):
    """A read-only fd on p, made if missing. Never O_CREAT on a file that is there: in sticky /tmp the kernel refuses
    that for a file another user owns (fs.protected_regular), and a lock needs no write."""
    try:
        return os.open(p, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        pass
    fd = os.open(p, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o666)
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o666)   # other users (another runner's user, root) can open it too
    return fd


def same_file(fd, p):
    try:
        a, b = os.fstat(fd), os.stat(p)
    except FileNotFoundError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def try_lock(p, mode):
    """An fd holding a `mode` flock on the file at p now, or None when someone else holds it."""
    while True:
        fd = open_lock(p)
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError as ex:
            os.close(fd)
            if ex.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            raise
        if same_file(fd, p):
            with contextlib.suppress(OSError):
                os.utime(p)   # in use: /tmp's age limit must never reach it (needs write access: 0666, or our own)
            return fd
        os.close(fd)   # replaced while we took it: the lock others see is on the file there now


class _Alarm(Exception):
    pass


def flock_wait(fd, mode, seconds):
    """Takes a `mode` flock on fd, waiting in the kernel for at most `seconds` (SIGALRM; outside the main thread, where
    Python has no signals, it only tries once). True when held. The kernel wakes a waiter the moment the lock is let
    go, so a holder that takes it again at once cannot keep this out, which polling could not promise."""
    if seconds > 0 and threading.current_thread() is threading.main_thread():
        def ring(signum, frame):   # noqa: ARG001 (signal handler signature)
            raise _Alarm()
        old = signal.signal(signal.SIGALRM, ring)
        try:
            try:
                try:
                    signal.setitimer(signal.ITIMER_REAL, seconds)
                    fcntl.flock(fd, mode)
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
            except _Alarm:
                pass   # the time ran out, or rang just after the flock: the try below says which
        finally:   # its own finally: an alarm that rings inside the one above can never skip it
            signal.signal(signal.SIGALRM, old)
    try:   # on our own open file this is a no-op when held, so it answers "held?" with no race against the alarm
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        return True
    except OSError as ex:
        if ex.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise


def wait_lock(p, mode, seconds):
    """Like try_lock, but waits in the kernel for at most `seconds` (flock_wait); None when it is still held then."""
    end = time.monotonic() + seconds
    while True:
        fd = open_lock(p)
        try:
            held = flock_wait(fd, mode, max(0.0, end - time.monotonic()))
        except BaseException:
            os.close(fd)
            raise
        if not held:
            os.close(fd)
            return None
        if same_file(fd, p):
            with contextlib.suppress(OSError):
                os.utime(p)
            return fd
        os.close(fd)   # replaced while we waited: wait on the file there now


def log(msg):
    print(f'nightly_lock: {msg}', file=sys.stderr, flush=True)


def me():
    e = os.environ
    if e.get('GITHUB_RUN_ID'):
        return f'{e.get("GITHUB_WORKFLOW", "?")} / {e.get("GITHUB_JOB", "?")} run {e["GITHUB_RUN_ID"]}'
    return f'pid {os.getpid()}'


def holder():
    try:
        with open(path(HOLDER)) as f:
            return f.read().strip()[:200] or 'unknown'
    except OSError:
        return 'unknown'


def note_holder():
    """Best effort: the line is for people reading the log, never for a decision."""
    tmp = path(f'.{HOLDER}.{os.getpid()}')
    try:
        with open(tmp, 'w') as f:
            f.write(f'{me()} since {time.strftime("%H:%M UTC", time.gmtime())}\n')
        os.replace(tmp, path(HOLDER))
    except OSError as e:
        log(f'WARN: could not write {HOLDER} ({e}); the lock itself is held')
        try:
            os.unlink(tmp)
        except OSError:
            pass


def take_first(deadline):
    """--first: WANT shared while waiting, then LOCK in the kernel. The fd, or None when the time ran out."""
    want = wait_lock(path(WANT), fcntl.LOCK_SH, max(0.0, deadline - time.time()))
    if want is None:   # only a caller without --first holds WANT, and only for a moment
        return None
    try:
        return wait_lock(path(LOCK), fcntl.LOCK_EX, max(0.0, deadline - time.time()))
    finally:
        os.close(want)


def take_after_first(deadline):
    """Without --first: LOCK only when it is free and no --first caller waits. (fd, None), or (None, why it was not
    taken at the last look) when the time ran out. It always looks once, and says at that first look why it waits."""
    said = 0.0
    while True:
        want = try_lock(path(WANT), fcntl.LOCK_EX)   # fails while any --first caller holds it shared
        if want is not None:
            try:
                fd = try_lock(path(LOCK), fcntl.LOCK_EX)
            finally:
                os.close(want)
            if fd is not None:
                return fd, None
            why = f'lock held by {holder()}'
        else:
            why = 'a --first caller is waiting for the lock; it goes first'
        if not said or time.time() - said >= SAY_EVERY_S:
            log(f'waiting for the nightly lock: {why}')
            said = time.time()
        left = deadline - time.time()
        if left <= 0:
            return None, why
        time.sleep(min(POLL_S, left))


def become_subreaper():
    """Orphans of the command (bash ended, a test runner's own sessions) are re-parented here, not to init. Linux only."""
    try:
        if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0:
            return True
    except (OSError, AttributeError):
        pass
    log('WARN: no child subreaper here; only the command\'s process group is waited for')
    return False


def descendants(root):
    """Live (not zombie) processes below root, from /proc; [] where there is no /proc."""
    kids = {}
    try:
        names = os.listdir('/proc')
    except OSError:
        return []
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(f'/proc/{n}/stat') as f:
                stat = f.read()
        except OSError:
            continue
        rest = stat[stat.rfind(')') + 2:].split()
        if len(rest) > 1 and rest[0] != 'Z':
            kids.setdefault(int(rest[1]), []).append(int(n))
    out, todo = [], [root]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def reap():
    """Collects every child of this process that has ended (orphans come here as the subreaper)."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def signal_all(pids, pgid, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def end_the_rest(pgid, hurry):
    """After the command ended: what it left running (its process group, orphans re-parented here) gets SIGTERM, then
    SIGKILL after GRACE_S (or at once when hurry() says a second signal came), and is waited for."""
    reap()
    left = descendants(os.getpid())
    if not left:
        return
    log(f'the command ended, {len(left)} process(es) it started still run (pids {" ".join(map(str, left[:10]))}): '
        'stopping them before the nightly lock is let go')
    signal_all(left, pgid, signal.SIGTERM)
    killed = False
    end = time.monotonic() + GRACE_S
    while True:
        reap()
        left = descendants(os.getpid())
        if not left:
            return
        if not killed and (time.monotonic() >= end or hurry()):
            log(f'still running after SIGTERM: SIGKILL to pids {" ".join(map(str, left[:10]))}')
            signal_all(left, pgid, signal.SIGKILL)
            killed, end = True, time.monotonic() + 60
        elif killed and time.monotonic() >= end:   # a SIGKILLed process runs no more code; it only has not gone yet
            log(f'WARN: pids {" ".join(map(str, left[:10]))} have not gone 60 s after SIGKILL; letting the lock go')
            return
        time.sleep(0.05)


def run(cmd):
    """The command's exit code (128+N when signal N ended it). It runs in its own process group; signals to this process
    go to that group, and the lock is held until everything the command started has ended (end_the_rest)."""
    got = []
    proc = []

    def forward(signum, frame):   # noqa: ARG001 (signal handler signature)
        got.append(signum)
        if proc:
            try:
                os.killpg(proc[0].pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):   # before the start: no window where a signal ends only us
        signal.signal(s, forward)
    become_subreaper()
    p = subprocess.Popen(cmd, process_group=0)   # close_fds and O_CLOEXEC: it never inherits the lock
    proc.append(p)
    if got:   # a signal came while it started: it goes to the command too
        try:
            os.killpg(p.pid, got[-1])
        except (ProcessLookupError, PermissionError):
            pass
    while True:   # waits for the command AND collects every orphan that ends meanwhile: as the subreaper we get the
        try:          # orphans of each test a long night runs; uncollected they pile up as zombies until the end
            pid, status = os.waitpid(-1, 0)
        except InterruptedError:
            continue
        if pid == p.pid:
            rc = p.returncode = os.waitstatus_to_exitcode(status)
            break
    seen = len(got)
    end_the_rest(p.pid, lambda: len(got) > seen)
    return 128 - rc if rc < 0 else rc


def gave_up(args, cmd, why):
    log(f'{why}; did not run: {" ".join(cmd)[:200]}')
    line = ' '.join(why.split())[:300]
    if args.gave_up_note:
        try:
            with open(args.gave_up_note, 'w') as f:
                f.write(line + '\n')
        except OSError as e:
            log(f'WARN: could not write {args.gave_up_note} ({e})')
    if args.github_output:
        out = os.environ.get('GITHUB_OUTPUT')
        try:
            if not out:
                raise OSError('GITHUB_OUTPUT is not set')
            with open(out, 'a') as f:
                f.write(f'gave_up={line}\n')
        except OSError as e:
            log(f'WARN: could not write the gave_up output ({e})')
    return EXIT_WAIT


def main(argv=None):
    global LOCK_DIR, LOCK, WANT, HOLDER
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--name', default=NAME, help=f'the lock\'s name: NAME.lock, NAME-first.lock, NAME.holder '
                                                 f'(default {NAME}); every caller of one lock passes the same')
    ap.add_argument('--dir', default=LOCK_DIR, help=f'the folder of the lock files (default {LOCK_DIR}); made if missing')
    ap.add_argument('--first', action='store_true', help='the short job: let in before every caller without --first')
    when = ap.add_mutually_exclusive_group(required=True)
    when.add_argument('--wait', type=float, help='wait at most this many seconds')
    when.add_argument('--until', type=float, help='wait at most until this epoch second')
    ap.add_argument('--gave-up-note', metavar='FILE', help='on exit 75, write one line why here (removed at the start)')
    ap.add_argument('--github-output', action='store_true', help='on exit 75, also write that line as the step output '
                                                                 'gave_up ($GITHUB_OUTPUT)')
    ap.add_argument('cmd', nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ['--'] else args.cmd
    if not cmd:
        ap.error('no command after --')
    if not NAME_RE.fullmatch(args.name):
        ap.error(f'--name {args.name!r}: letters, digits, . _ - (64 at most, not starting with . _ -)')
    if not args.dir:
        ap.error('--dir is empty')
    LOCK_DIR = args.dir
    LOCK, WANT, HOLDER = lock_names(args.name)
    if args.gave_up_note:
        try:
            os.unlink(args.gave_up_note)   # a note from an earlier run never stays
        except FileNotFoundError:
            pass
    start = time.time()
    deadline = start + args.wait if args.wait is not None else args.until
    if args.until is not None and args.until <= start:
        return gave_up(args, cmd, f'the --until time {time.strftime("%H:%M UTC", time.gmtime(args.until))} has passed')
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        fd, why = (take_first(deadline), None) if args.first else take_after_first(deadline)
    except OSError as e:   # a lock folder that is a file or a broken link, a lock file that cannot be opened
        log(f'cannot use the lock in {LOCK_DIR} ({e}); did not run: {" ".join(cmd)[:200]}')
        return EXIT_LOCK
    if fd is None:
        return gave_up(args, cmd, (why or f'lock held by {holder()}')
                       + f' (gave up after {(time.time() - start) / 60:.0f} min)')
    waited = (time.time() - start) / 60
    note_holder()
    log(f'took the nightly lock' + (f' after {waited:.0f} min' if waited >= 1 else '') + f' ({me()})')
    try:
        return run(cmd)
    finally:
        os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
