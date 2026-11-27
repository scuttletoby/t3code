#!/usr/bin/env python3
"""trace-check: does test-select pick every test whose files change? Runs each test under strace and fails when a
test reads or runs a tracked file whose change would NOT pick that test on a pull request.

test-select works out a test's inputs by reading its code. This check measures them: every test runs once under
`strace -f` (open, exec and stat calls of the test and every process it starts), and every tracked file it touched is
put to test-select as if a pull request changed only that file. A file whose change would leave the test out is a
miss: fix test-select (a general rule) or name the test in the config's `always_run`. Run it where a miss is cheap
to see and fix: on a schedule or after each push to the default branch, never as the gate of a pull request.

  python3 -I trace_check.py --config .github/test-select.json [--run 'python3 -I -B {test}'] [--jobs 2]
      [--only GLOB]... [--root .] [--report FILE]
Needs strace on PATH and ptrace of its own children (kernel.yama.ptrace_scope 0 or 1; no root). Without strace, or
when a test's trace does not show its own file read, the check fails (it never passes unchecked). A test that fails,
errs or skips tests under the trace is "not checked" (what it did not run, it did not touch) and fails the check too:
run it as the test job does (same runner, env). Not seen: a folder it lists (the files it then reads are), files read
through a process that is not its child (a server already running), and files read through git (git reads .git).
Each test's lines print (and go to --report) as its trace ends. SIGTERM or SIGINT (a cancelled or timed-out job)
kills every traced test's process group at once and ends with a "cut off" line: what finished stays reported.
Exit 0: no miss; 1: a miss, a test not traced or not checked, no test to trace, or a run cut off.
"""
import argparse, codecs, os, posixpath, re, shlex, shutil, signal, subprocess, sys, tempfile, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_select as ts   # noqa: E402  (the same release: the rules this check measures)
docs_scope = ts.docs_scope

CALLS = ('open,openat,openat2,creat,execve,execveat,stat,lstat,newfstatat,statx,access,faccessat,faccessat2,'
         'readlink,readlinkat,chdir,fchdir,clone,clone3,fork,vfork')
LINE = re.compile(r'(\d+)\s+(.*)$')
CALL = re.compile(r'(\w+)\((.*)\)\s+=\s+(-?\d+|\?)(.*)$')
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
FD = re.compile(r'^(\d+|AT_FDCWD)(?:<([^>]*)>)?')
USES = {'open': 'reads', 'openat': 'reads', 'openat2': 'reads', 'creat': 'writes', 'execve': 'runs',
        'execveat': 'runs', 'readlink': 'reads', 'readlinkat': 'reads'}   # the rest: 'checks' (stat, access)


def text(s):
    return codecs.escape_decode(s.encode('latin-1', 'backslashreplace'))[0].decode('utf-8', 'surrogateescape')


def touched(log, root):
    """{absolute path: what (reads/runs/checks)} from one strace -f log; only calls that succeeded."""
    out, cwd, pending, first = {}, {}, {}, None
    for raw in log:
        m = LINE.match(raw.rstrip('\n'))
        if not m:
            continue
        pid, rest = m.groups()
        if first is None:
            first = pid
            cwd[pid] = root
        if rest.endswith('<unfinished ...>'):
            pending[pid] = rest[:-len('<unfinished ...>')]
            continue
        r = re.match(r'<\.\.\. \w+ resumed>(.*)$', rest)
        if r:
            rest = pending.pop(pid, '') + r.group(1)
        c = CALL.match(rest)
        if not c:
            continue
        name, args, ret, tail = c.groups()
        here = cwd.get(pid, root)
        if name in ('clone', 'clone3', 'fork', 'vfork'):
            if ret.isdigit():
                cwd[ret] = here
            continue
        if ret == '?' or int(ret) < 0:
            continue
        if name == 'fchdir':
            fd = FD.match(args)
            if fd and fd.group(2):
                cwd[pid] = fd.group(2)
            continue
        s = STRING.search(args)
        if not s:
            continue
        path, base = text(s.group(1)), here
        if name.endswith('at') or name in ('openat2', 'statx', 'faccessat2'):
            fd = FD.match(args)
            if fd and fd.group(1) != 'AT_FDCWD' and fd.group(2):
                base = fd.group(2)
        path = posixpath.normpath(posixpath.join(base, path))
        if name == 'chdir':
            cwd[pid] = path
            continue
        what = USES.get(name, 'checks')
        if out.get(path) not in ('reads', 'runs'):
            out[path] = what
        fdpath = re.match(r'\s*<([^>]*)>', tail)   # -y: the opened file as the kernel resolved it (links followed)
        if fdpath and name in ('open', 'openat', 'openat2'):
            out.setdefault(fdpath.group(1), what)
    return out


RUNNING, STOP = set(), threading.Event()   # the traced tests' processes; set when the job is cancelled
LOCK = threading.Lock()


class Cut(Exception):
    """The run was stopped (SIGTERM, SIGINT: a cancelled or timed-out job)."""


def stop(signum, frame):
    """A cancelled or timed-out job: kill every traced test now (its whole process group: strace, the test and what it
    started), so the runner never waits on them; main() then reports what finished."""
    STOP.set()
    with LOCK:
        for p in list(RUNNING):
            with_group(p, signal.SIGKILL)
    raise Cut(signal.Signals(signum).name)


def with_group(p, sig):
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


SKIPPED = re.compile(rb'\bskipped=(\d+)|\b(\d+) skipped\b')   # unittest's "OK (skipped=2)", pytest's "2 skipped"


def trace(root, test, run, keep):
    """(touched paths, note or None). A note means the test was not checked whole: no trace, or it failed, erred or
    skipped tests under the trace (what it did not run, it did not touch)."""
    fd, log = tempfile.mkstemp(prefix='trace-', suffix='.log', dir=keep)
    os.close(fd)
    fd, out = tempfile.mkstemp(prefix='trace-', suffix='.out', dir=keep)
    try:
        cmd = ['strace', '-f', '-qq', '-z', '-y', '-s', '4096', '--seccomp-bpf', '-e', 'trace=' + CALLS, '-o', log, '--',
               'sh', '-c', run.replace('{test}', shlex.quote(test))]
        try:   # its own process group, so a cancel (stop) kills everything it started
            with LOCK:   # (a stop between the check and the start would miss it)
                if STOP.is_set():
                    return {}, 'no trace: the run was stopped'
                p = subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                                     start_new_session=True)
                RUNNING.add(p)
            try:
                p.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                with_group(p, signal.SIGKILL)
                p.wait()
                raise
            finally:
                with LOCK:
                    RUNNING.discard(p)
            r = p
        finally:
            os.close(fd)
        if STOP.is_set() and r.returncode == -signal.SIGKILL:   # stopped (a cancel): half a trace says nothing
            return {}, 'no trace: the run was stopped'
        with open(out, 'rb') as f:
            f.seek(max(0, os.path.getsize(out) - 65536))
            said = f.read()
        last = said.decode(errors='replace').strip().splitlines()[-3:]
        with open(log, encoding='latin-1') as f:
            got = touched(f, root)
        if not got:
            return got, f'no trace (exit {r.returncode}: {" | ".join(last)[-300:]})'
        if r.returncode:
            return got, f'not checked: the test failed under the trace (exit {r.returncode}: {" | ".join(last)[-300:]})'
        skipped = sum(int(a or b) for a, b in SKIPPED.findall(said))
        if skipped:
            return got, f'not checked: {skipped} skipped under the trace (run it as the test job does: same runner, env)'
        return got, None
    except subprocess.TimeoutExpired:
        return {}, 'no trace: the test ran past an hour'
    finally:
        os.unlink(log)
        os.unlink(out)


def picks(config, rules, always_run, graph, test, rel):
    """Why test-select would pick `test` when only `rel` changed, or None."""
    for g, why in rules:
        if docs_scope.glob_re(g).match(rel):
            return f'every test ({why})'
    if test in always_run:
        return 'always_run'
    try:
        files, dirs, globs, marks = graph.inputs(test)
    except ts.AllTests as e:   # test-select runs every test then: nothing to miss
        return f'every test ({e})'
    if marks:
        return 'always'
    return 'takes it in' if ts.reaches([rel], files, dirs, globs) else None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--config', default=ts.DEFAULT_CONFIG)
    p.add_argument('--root', default='.')
    p.add_argument('--run', default='python3 -I -B {test}', help='how one test runs; {test} is its path')
    p.add_argument('--jobs', type=int, default=2)
    p.add_argument('--only', action='append', default=[], help='trace only the tests matching this glob')
    p.add_argument('--report', default='',
                   help='also write each line here as it is known (kept when the run is cut off)')
    a = p.parse_args(argv)
    if not shutil.which('strace'):
        print('trace-check: strace is not on PATH; nothing was checked (install strace on the runner)')
        return 1
    root = os.path.realpath(a.root)
    tracked = ts.tracked_files(root)
    tests, always, import_roots, always_run, inputs, follow_calls = ts.load_config(root, a.config)
    every = ts.universe(tests, tracked)
    always_run = ts.always_run_of(always_run, every, a.config)
    declared = ts.declared_of(inputs, every, a.config)
    follow = ts.follow_of(follow_calls, tracked, a.config)
    if a.only:
        every = [t for t in every if any(docs_scope.glob_re(g).match(t) for g in a.only)]
    if not every:   # a check of nothing never passes
        print(f'trace-check: no test to trace (--only {a.only} matches none of the config\'s tests)')
        return 1
    rules = [(a.config, 'the test-select config')] + list(ts.BUILTIN_ALL) + always
    repo = ts.Repo(root, tracked)
    graph = ts.Graph(repo, import_roots, declared, follow)
    STOP.clear()
    report = open(a.report, 'w', encoding='utf-8') if a.report else None
    if report and os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(f'report={os.path.abspath(a.report)}\n')
    misses, broken, done = [], [], []

    def say(line):
        print(line, flush=True)
        if report:
            report.write(docs_scope.shown(line) + '\n')
            report.flush()

    def judge(t, got, note):
        """Print one test's result as soon as its trace ends (a cut-off run keeps what finished)."""
        done.append(t)
        if note:   # still judged by what it touched; but never clean
            say(f'{t}: {note}')
            broken.append(t)
            if not got:
                return
        try:
            graph.inputs(t)
        except ts.AllTests as e:
            say(f'{t}: every test runs when it is reached ({e})')
        seen = {}
        for path, what in got.items():
            rel = repo.rel(path) if path.startswith(root + '/') else None
            if rel in tracked and rel not in seen:
                seen[rel] = what
        if t not in seen:   # the run command never ran it: nothing was measured
            say(f'{t}: the trace never shows the test file read (does --run run it?)')
            if t not in broken:
                broken.append(t)
            return
        for rel, what in sorted(seen.items()):
            if not picks(a.config, rules, always_run, graph, t, rel):
                misses.append((t, rel, what))
                say(f'miss: {t} {what} {rel}; a pull request that changes only {rel} would not run it')
        say(f'{t}: {len(seen)} tracked files touched')

    say(f'trace-check: tracing {len(every)} tests, {max(1, a.jobs)} at once')
    keep = tempfile.mkdtemp(prefix='trace-check-')
    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    cut = None
    pool = ThreadPoolExecutor(max(1, a.jobs))
    try:
        futures = {pool.submit(trace, root, t, a.run, keep): t for t in every}
        for f in as_completed(futures):
            judge(futures[f], *f.result())
    except Cut as e:
        cut = str(e)
    finally:
        STOP.set()   # (also on an error: no test starts after it)
        pool.shutdown(wait=True, cancel_futures=True)
        for s, h in old.items():
            signal.signal(s, h)
        shutil.rmtree(keep, ignore_errors=True)
    if cut:
        say(f'trace-check: cut off by {cut} after {len(done)} of {len(every)} tests; {len(misses)} misses so far '
            f'(the rest not checked)')
    else:
        say(f'trace-check: {len(every)} tests, {len(misses)} misses, {len(broken)} not traced or not checked '
            f'(failed or skipped under the trace)')
    if report:
        report.close()
    return 1 if misses or broken or cut else 0


if __name__ == '__main__':
    sys.exit(main())
